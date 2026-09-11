import json
import sys
import types
from datetime import UTC, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import feedparser

from src import databricks_digest


def load_smoke_test():
    spec = spec_from_file_location("smoke_test", "scripts/smoke_test.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_post(post_id: str = "abc", flair: str = "News") -> databricks_digest.RedditPost:
    return databricks_digest.RedditPost(
        post_id=post_id,
        title="Databricks announcement",
        url=f"https://www.reddit.com/r/databricks/comments/{post_id}/post/",
        author="author",
        flair=flair,
        created_utc=1_700_000_000,
        selftext="",
    )


def test_fetch_posts_filters_to_news_and_event_flairs():
    payload = {
        "data": {
            "children": [
                {
                    "data": {
                        "id": "news",
                        "title": "News",
                        "permalink": "/news",
                        "link_flair_text": "News",
                        "created_utc": 1,
                        "author": "a",
                    }
                },
                {
                    "data": {
                        "id": "event",
                        "title": "Event",
                        "permalink": "/event",
                        "link_flair_text": "Event",
                        "created_utc": 2,
                        "author": "b",
                    }
                },
                {
                    "data": {
                        "id": "other",
                        "title": "Other",
                        "permalink": "/other",
                        "link_flair_text": "Question",
                        "created_utc": 3,
                        "author": "c",
                    }
                },
            ]
        }
    }
    response = MagicMock()
    response.__enter__.return_value = BytesIO(json.dumps(payload).encode())

    with patch.object(databricks_digest, "urlopen", return_value=response):
        posts = databricks_digest.fetch_posts()

    assert [post.post_id for post in posts] == ["news", "event"]


def test_fetch_posts_uses_rss_after_json_is_blocked():
    with (
        patch.object(databricks_digest, "fetch_json_posts", side_effect=HTTPError("url", 403, "Blocked", {}, None)),
        patch.object(databricks_digest, "fetch_rss_posts", return_value=[make_post()]) as fetch_rss,
    ):
        posts = databricks_digest.fetch_posts(1)

    assert posts == [make_post()]
    fetch_rss.assert_called_once_with(1)


def test_fetch_posts_reraises_non_rate_limit_errors():
    error = HTTPError("url", 500, "Server error", {}, None)
    with patch.object(databricks_digest, "fetch_json_posts", side_effect=error):
        try:
            databricks_digest.fetch_posts()
        except HTTPError as actual:
            assert actual is error
        else:
            raise AssertionError("Expected HTTPError")


def test_get_secret_reads_environment(monkeypatch):
    monkeypatch.setenv("RECIPIENT_EMAIL", "user@example.com")
    assert databricks_digest.get_secret("scope", "recipient-email") == "user@example.com"


def test_get_secret_reads_dbutils(monkeypatch):
    monkeypatch.delenv("SOME_KEY", raising=False)
    dbutils = MagicMock()
    dbutils.secrets.get.return_value = "secret-value"
    monkeypatch.setattr(databricks_digest, "dbutils", dbutils, raising=False)

    assert databricks_digest.get_secret("scope", "some-key") == "secret-value"
    dbutils.secrets.get.assert_called_once_with(scope="scope", key="some-key")


def test_get_secret_requires_runtime_or_environment(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.delattr(databricks_digest, "dbutils", raising=False)
    with patch.dict(sys.modules, {"databricks": None, "databricks.sdk": None, "databricks.sdk.runtime": None}):
        try:
            databricks_digest.get_secret("scope", "missing-key")
        except RuntimeError as error:
            assert "MISSING_KEY" in str(error)
        else:
            raise AssertionError("Expected RuntimeError")


def test_load_new_posts_excludes_processed_ids():
    spark = MagicMock()
    spark.sql.return_value.collect.return_value = [MagicMock(post_id="known")]

    posts = databricks_digest.load_new_posts(
        spark, "catalog.schema.posts", [make_post("known"), make_post("new", "Event")]
    )

    assert [post.post_id for post in posts] == ["new"]
    spark.sql.assert_any_call("SELECT post_id FROM catalog.schema.posts")


def test_request_json_loads_response():
    response = MagicMock()
    response.__enter__.return_value = BytesIO(b'{"data": {"children": []}}')
    with patch.object(databricks_digest, "urlopen", return_value=response):
        assert databricks_digest.request_json("https://example.test")["data"]["children"] == []


def test_rss_posts_are_parsed_and_deduplicated():
    first = feedparser.FeedParserDict(id="t3_first", title="First", link="https://reddit.test/first", author="/u/a")
    first.published_parsed = datetime(2026, 1, 1, tzinfo=UTC).timetuple()
    duplicate = feedparser.FeedParserDict(
        id="t3_first", title="Updated", link="https://reddit.test/first", author="/u/a"
    )
    duplicate.published_parsed = datetime(2026, 1, 2, tzinfo=UTC).timetuple()
    second = feedparser.FeedParserDict(id="t3_second", title="Second", link="https://reddit.test/second", author="")
    second.published_parsed = datetime(2026, 1, 1, tzinfo=UTC).timetuple()
    feed = MagicMock(entries=[first, duplicate, second])
    with patch.object(databricks_digest, "request_xml", side_effect=[feed, MagicMock(entries=[])]):
        posts = databricks_digest.fetch_rss_posts(3)

    assert [post.post_id for post in posts] == ["first", "second"]
    assert posts[1].author == "[deleted]"


def test_rss_posts_report_http_errors():
    error = HTTPError("url", 429, "Too many requests", {}, None)
    with patch.object(databricks_digest, "request_xml", side_effect=error):
        try:
            databricks_digest.fetch_rss_posts(1)
        except RuntimeError as actual:
            assert "429" in str(actual)
        else:
            raise AssertionError("Expected RuntimeError")


def test_request_xml_rejects_non_rss_response():
    response = MagicMock()
    response.__enter__.return_value = BytesIO(b"<html>challenge</html>")
    with (
        patch.object(databricks_digest, "urlopen", return_value=response),
        patch.object(databricks_digest.feedparser, "parse", return_value=MagicMock(bozo=True, entries=[])),
    ):
        try:
            databricks_digest.request_xml("https://example.test")
        except RuntimeError as error:
            assert "non-RSS" in str(error)
        else:
            raise AssertionError("Expected RuntimeError")


def test_request_xml_returns_valid_feed():
    response = MagicMock()
    response.__enter__.return_value = BytesIO(b"<?xml version='1.0'?><feed xmlns='http://www.w3.org/2005/Atom'/>")
    feed = feedparser.FeedParserDict(bozo=False, entries=[])
    with (
        patch.object(databricks_digest, "urlopen", return_value=response),
        patch.object(databricks_digest.feedparser, "parse", return_value=feed),
    ):
        assert databricks_digest.request_xml("https://example.test") is feed


def test_send_digest_uses_gmail_secrets_and_sends_sorted_posts():
    first = make_post("first", "Event")
    second = make_post("second", "News")
    first = databricks_digest.RedditPost(**{**first.__dict__, "created_utc": 2})
    second = databricks_digest.RedditPost(**{**second.__dict__, "created_utc": 1})
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp

    with (
        patch.object(databricks_digest, "get_secret", side_effect=["to@gmail.com", "app-password"]),
        patch.object(databricks_digest.smtplib, "SMTP_SSL", return_value=smtp),
    ):
        databricks_digest.send_digest([first, second], "reddit-digest")

    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "to@gmail.com"
    assert message["From"] == "to@gmail.com"
    assert message.get_content().index("second") < message.get_content().index("first")
    smtp.login.assert_called_once_with("to@gmail.com", "app-password")


def test_save_posts_skips_empty_input():
    spark = MagicMock()
    databricks_digest.save_posts(spark, "catalog.schema.posts", [])
    spark.createDataFrame.assert_not_called()


def test_save_posts_merges_rows():
    spark = MagicMock()
    databricks_digest.save_posts(spark, "catalog.schema.posts", [make_post()])
    spark.createDataFrame.assert_called_once()
    spark.createDataFrame.return_value.createOrReplaceTempView.assert_called_once_with("new_digest_posts")


def test_parse_rss_entry_removes_prefix_and_uses_author():
    entry = feedparser.FeedParserDict(
        id="t3_post",
        title="Title",
        link="https://reddit.test/post",
        author="/u/author",
        published_parsed=datetime(2026, 1, 1, tzinfo=UTC).timetuple(),
    )
    post = databricks_digest.parse_rss_entry(entry, "Event")
    assert post.post_id == "post"
    assert post.author == "author"


def test_smoke_test_main_dry_run(capsys):
    smoke_test = load_smoke_test()
    with (
        patch.object(smoke_test, "load_dotenv"),
        patch.object(smoke_test, "fetch_posts", return_value=[make_post()]),
        patch.object(sys, "argv", ["smoke_test.py", "--dry-run", "--limit", "1"]),
    ):
        smoke_test.main()

    assert "Dry run complete" in capsys.readouterr().out


def test_smoke_test_main_sends_email():
    smoke_test = load_smoke_test()
    with (
        patch.object(smoke_test, "load_dotenv"),
        patch.object(smoke_test, "fetch_posts", return_value=[make_post()]),
        patch.object(smoke_test, "send_digest") as send_digest,
        patch.object(sys, "argv", ["smoke_test.py", "--limit", "1"]),
    ):
        smoke_test.main()

    send_digest.assert_called_once_with([make_post()], "local")


def test_smoke_test_main_reports_fetch_errors(capsys):
    smoke_test = load_smoke_test()
    with (
        patch.object(smoke_test, "load_dotenv"),
        patch.object(smoke_test, "fetch_posts", side_effect=RuntimeError("blocked")),
        patch.object(sys, "argv", ["smoke_test.py"]),
    ):
        try:
            smoke_test.main()
        except SystemExit as error:
            assert error.code == 1
        else:
            raise AssertionError("Expected SystemExit")
    assert "blocked" in capsys.readouterr().err


def test_databricks_main_handles_empty_run(monkeypatch):
    spark = MagicMock()
    spark_module = types.ModuleType("pyspark.sql")
    spark_module.SparkSession = MagicMock(builder=MagicMock(getOrCreate=MagicMock(return_value=spark)))
    monkeypatch.setitem(sys.modules, "pyspark.sql", spark_module)
    monkeypatch.setattr(databricks_digest, "fetch_posts", lambda: [])
    monkeypatch.setattr(databricks_digest, "load_new_posts", lambda *_: [])
    monkeypatch.setattr(sys, "argv", ["databricks_digest.py", "--digest-table", "table", "--secret-scope", "scope"])
    databricks_digest.main()


def test_databricks_main_sends_and_saves(monkeypatch):
    spark = MagicMock()
    spark_module = types.ModuleType("pyspark.sql")
    spark_module.SparkSession = MagicMock(builder=MagicMock(getOrCreate=MagicMock(return_value=spark)))
    monkeypatch.setitem(sys.modules, "pyspark.sql", spark_module)
    posts = [make_post()]
    monkeypatch.setattr(databricks_digest, "fetch_posts", lambda: posts)
    monkeypatch.setattr(databricks_digest, "load_new_posts", lambda *_: posts)
    monkeypatch.setattr(databricks_digest, "send_digest", MagicMock())
    monkeypatch.setattr(databricks_digest, "save_posts", MagicMock())
    monkeypatch.setattr(sys, "argv", ["databricks_digest.py", "--digest-table", "table", "--secret-scope", "scope"])
    databricks_digest.main()
