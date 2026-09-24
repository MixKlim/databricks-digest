import sys
import types
from datetime import UTC, date, datetime
from importlib.util import module_from_spec, spec_from_file_location
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

import feedparser
import pytest

from src import databricks_digest


def load_local_digest():
    """Load the local-digest script as a module for integration-style tests."""
    spec = spec_from_file_location("local_digest", "scripts/local_digest.py")
    module = module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_note(note_id: str = "abc", published_utc: float = 1_700_000_000) -> databricks_digest.ReleaseNote:
    """Build a representative release note for tests."""
    return databricks_digest.ReleaseNote(
        note_id=note_id,
        title="Databricks announcement",
        url=f"https://learn.microsoft.com/en-us/azure/databricks/release-notes/{note_id}",
        category="Azure Databricks",
        published_utc=published_utc,
        summary="A release note summary.",
    )


def make_entry(note_id: str, title: str, day: int) -> feedparser.FeedParserDict:
    """Build a Microsoft Learn feed entry."""
    entry = feedparser.FeedParserDict(
        id=note_id,
        title=title,
        link=f"https://learn.microsoft.com/en-us/azure/databricks/release-notes/{note_id}",
        summary="Summary",
        tags=[{"term": "Platform"}],
    )
    entry["published_parsed"] = datetime(2026, 1, day, tzinfo=UTC).timetuple()
    return entry


def test_fetch_release_notes_sorts_and_limits_feed_entries():
    """Verify feed entries are parsed, sorted by date, and limited."""
    feed = MagicMock(bozo=False, entries=[make_entry("old", "Old", 1), make_entry("new", "New", 2)])
    with patch.object(databricks_digest, "request_feed", return_value=feed):
        notes = databricks_digest.fetch_release_notes(1)

    assert [note.note_id for note in notes] == ["new"]
    assert notes[0].category == "Platform"


def test_fetch_release_notes_filters_to_single_local_publication_date():
    """Verify a start date without an end date selects one local publication date."""
    feed = MagicMock(
        bozo=False,
        entries=[
            make_entry("older", "Older", 13),
            make_entry("yesterday", "Yesterday", 14),
            make_entry("today", "Today", 15),
        ],
    )
    with patch.object(databricks_digest, "request_feed", return_value=feed):
        notes = databricks_digest.fetch_release_notes(start_date=date(2026, 1, 14))

    assert [note.note_id for note in notes] == ["yesterday"]


def test_fetch_release_notes_includes_future_publication_dates():
    """Verify feed discovery does not wait for a future publication date."""
    feed = MagicMock(bozo=False, entries=[make_entry("future", "Future", 25)])
    with patch.object(databricks_digest, "request_feed", return_value=feed):
        notes = databricks_digest.fetch_release_notes(start_date=date(2026, 1, 23), end_date=date(2026, 1, 25))

    assert [note.note_id for note in notes] == ["future"]


def test_fetch_release_notes_filters_to_inclusive_local_publication_date_range():
    """Verify both endpoints of a requested local publication date range are included."""
    feed = MagicMock(
        bozo=False,
        entries=[
            make_entry("before", "Before", 13),
            make_entry("start", "Start", 14),
            make_entry("end", "End", 15),
            make_entry("after", "After", 16),
        ],
    )
    with patch.object(databricks_digest, "request_feed", return_value=feed):
        notes = databricks_digest.fetch_release_notes(start_date=date(2026, 1, 14), end_date=date(2026, 1, 15))

    assert [note.note_id for note in notes] == ["end", "start"]


def test_fetch_release_notes_rejects_invalid_date_range():
    """Verify invalid range arguments fail before requesting the feed."""
    with patch.object(databricks_digest, "request_feed") as request:
        with pytest.raises(ValueError, match="earlier"):
            databricks_digest.fetch_release_notes(start_date=date(2026, 1, 15), end_date=date(2026, 1, 14))
        with pytest.raises(ValueError, match="required"):
            databricks_digest.fetch_release_notes(end_date=date(2026, 1, 14))
    request.assert_not_called()


def test_fetch_release_notes_removes_duplicate_titles_on_same_date():
    """Verify duplicate feed representations of one release are returned once."""
    first = make_entry("first", "Same release", 14)
    duplicate = make_entry("duplicate", "  SAME   RELEASE ", 14)
    feed = MagicMock(bozo=False, entries=[first, duplicate])
    with patch.object(databricks_digest, "request_feed", return_value=feed):
        notes = databricks_digest.fetch_release_notes(start_date=date(2026, 1, 14))

    assert [note.note_id for note in notes] == ["first"]


def test_fetch_release_notes_reports_http_errors():
    """Verify feed HTTP failures are reported without retrying unboundedly."""
    error = HTTPError("url", 429, "Too many requests", {}, None)
    with patch.object(databricks_digest, "request_feed", side_effect=error):
        with pytest.raises(RuntimeError, match="429"):
            databricks_digest.fetch_release_notes()


def test_fetch_release_notes_returns_empty_for_nonpositive_limit():
    """Verify an empty result avoids an unnecessary network request."""
    with patch.object(databricks_digest, "request_feed") as request:
        assert databricks_digest.fetch_release_notes(0) == []
    request.assert_not_called()


def test_fetch_release_notes_rejects_invalid_empty_feed():
    """Verify malformed empty feeds fail closed."""
    with patch.object(databricks_digest, "request_feed", return_value=MagicMock(bozo=True, entries=[])):
        with pytest.raises(RuntimeError, match="invalid"):
            databricks_digest.fetch_release_notes()


def test_request_feed_fetches_configured_url():
    """Verify the feed request uses the configured URL and parser."""
    response = MagicMock()
    response.__enter__.return_value = BytesIO(b"<rss />")
    feed = feedparser.FeedParserDict(bozo=False, entries=[])
    with (
        patch.object(databricks_digest, "urlopen", return_value=response) as open_url,
        patch.object(databricks_digest.feedparser, "parse", return_value=feed),
    ):
        assert databricks_digest.request_feed(databricks_digest.RELEASE_NOTES_FEED_URL) is feed
    assert open_url.call_args.kwargs["timeout"] == 30


def test_parse_feed_entry_rejects_missing_date_and_identity():
    """Verify incomplete feed entries are rejected."""
    entry = feedparser.FeedParserDict(
        id="missing-date", link="https://learn.microsoft.com/en-us/azure/databricks/release-notes/x"
    )
    with pytest.raises(ValueError, match="no publication date"):
        databricks_digest.parse_feed_entry(entry)
    entry["published_parsed"] = datetime(2026, 1, 1, tzinfo=UTC).timetuple()
    del entry["id"]
    del entry["link"]
    with pytest.raises(ValueError, match="missing its ID"):
        databricks_digest.parse_feed_entry(entry)


def test_parse_feed_entry_uses_default_category_and_title():
    """Verify entries without optional metadata get useful defaults."""
    entry = make_entry("defaults", "", 1)
    del entry["tags"]
    del entry["title"]
    note = databricks_digest.parse_feed_entry(entry)
    assert note.category == "Azure Databricks"
    assert note.title == "Untitled release note"


def test_request_feed_rejects_unconfigured_urls():
    """Verify the feed client cannot be redirected to an arbitrary URL."""
    with pytest.raises(ValueError, match="configured"):
        databricks_digest.request_feed("https://example.test/feed.xml")


def test_parse_feed_entry_rejects_non_microsoft_urls():
    """Verify email links are restricted to Microsoft Learn."""
    entry = make_entry("bad", "Bad", 1)
    entry["link"] = "https://example.test/bad"
    with pytest.raises(ValueError, match="unexpected URL"):
        databricks_digest.parse_feed_entry(entry)


def test_parse_feed_entry_uses_updated_date_when_published_is_missing():
    """Verify updated dates provide a stable fallback for feed items."""
    entry = make_entry("updated", "Updated", 1)
    del entry["published_parsed"]
    entry["updated_parsed"] = datetime(2026, 1, 3, tzinfo=UTC).timetuple()
    assert databricks_digest.parse_feed_entry(entry).published_utc == datetime(2026, 1, 3, tzinfo=UTC).timestamp()


def test_render_summary_converts_feed_html_to_safe_readable_content():
    """Verify feed paragraphs and relative links render as readable email content."""
    summary = (
        "<p>Genie Agents use <strong>attached sources</strong>.</p>"
        '<p>See <a href="/azure/databricks/genie-agents/set-up">Create and manage a Genie Agent</a>.</p>'
        '<script><b>alert("ignored")</b></script><style>body { color: red; }</style><a>Unlinked text</a>'
    )
    rendered_html, rendered_text = databricks_digest.render_summary(
        summary, "https://learn.microsoft.com/en-us/azure/databricks/release-notes/product/2026/september"
    )

    assert "<strong>attached sources</strong>" in rendered_html
    assert 'href="https://learn.microsoft.com/azure/databricks/genie-agents/set-up"' in rendered_html
    assert "<script>" not in rendered_html
    assert "Genie Agents use attached sources." in rendered_text
    assert "Create and manage a Genie Agent" in rendered_text


def test_render_summary_removes_external_links():
    """Verify summary links cannot point email readers to untrusted hosts."""
    rendered_html, rendered_text = databricks_digest.render_summary(
        '<p>Read <a href="https://example.test/phishing">the note</a>.</p>',
        databricks_digest.RELEASE_NOTES_FEED_URL,
    )

    assert "example.test" not in rendered_html
    assert "the note" in rendered_html
    assert rendered_text == "Read the note."


def test_summarize_digest_builds_one_bullet_per_item_prompt():
    """Verify each digest item produces one concise actionable bullet."""
    client = MagicMock()
    client.models.generate_content.return_value.text = "- Check runtime compatibility."

    summary = databricks_digest.summarize_digest([make_note()], client=client)

    assert summary == ["Check runtime compatibility."]
    request = client.models.generate_content.call_args.kwargs
    assert request["model"] == "gemini-3.5-flash-lite"
    assert "one short bullet sentence" in request["contents"]
    assert "Databricks announcement" in request["contents"]
    assert "Do not include category labels or bracketed tags" in request["contents"]


def test_summarize_digest_removes_model_category_tags():
    """Verify model-generated category tags are removed from bullet sentences."""
    client = MagicMock()
    client.models.generate_content.return_value.text = "- [aibi, dashboards] Update dashboards with local metric views."

    summary = databricks_digest.summarize_digest([make_note()], client=client)

    assert summary == ["Update dashboards with local metric views."]


def test_summarize_digest_handles_empty_items():
    """Verify no model request is made for an empty digest."""
    client = MagicMock()

    assert databricks_digest.summarize_digest([], client=client) == []
    client.models.generate_content.assert_not_called()


def test_summarize_digest_creates_gemini_client_from_environment(monkeypatch):
    """Verify the summarizer can create its default Gemini client."""
    client = MagicMock()
    client.models.generate_content.return_value.text = "Use the new API."
    gemini = MagicMock()
    gemini.Client.return_value = client
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    with patch.object(databricks_digest.genai, "Client", gemini.Client):
        assert databricks_digest.summarize_digest([make_note()]) == ["Use the new API."]
    gemini.Client.assert_called_once_with(api_key="test-key")


def test_summarize_digest_reads_gemini_key_from_databricks_secret():
    """Verify the summarizer uses the configured secret scope in a job."""
    client = MagicMock()
    client.models.generate_content.return_value.text = "Use the new API."
    gemini = MagicMock()
    gemini.Client.return_value = client
    with (
        patch.object(databricks_digest, "get_secret", return_value="secret-key") as get_secret,
        patch.object(databricks_digest.genai, "Client", gemini.Client),
    ):
        assert databricks_digest.summarize_digest([make_note()], secret_scope="databricks-digest") == [
            "Use the new API."
        ]
    get_secret.assert_called_once_with("databricks-digest", "gemini-api-key")
    gemini.Client.assert_called_once_with(api_key="secret-key")


def test_summarize_digest_rejects_empty_model_response():
    """Verify empty model output is reported instead of silently delivered."""
    client = MagicMock()
    client.models.generate_content.return_value.text = ""

    with pytest.raises(RuntimeError, match="empty digest summary"):
        databricks_digest.summarize_digest([make_note()], client=client)


def test_summarize_digest_rejects_wrong_item_count():
    """Verify one missing bullet fails closed instead of mislabeling items."""
    client = MagicMock()
    client.models.generate_content.return_value.text = "- Use the new API."

    with pytest.raises(RuntimeError, match="unexpected number"):
        databricks_digest.summarize_digest([make_note("one"), make_note("two")], client=client)


def test_summary_renderer_ignores_nested_script_state():
    """Verify nested ignored tags cannot leak markup or script text into email."""
    renderer = databricks_digest.SummaryRenderer(databricks_digest.RELEASE_NOTES_FEED_URL)
    renderer.handle_starttag("script", [])
    renderer.handle_starttag("strong", [])
    renderer.handle_data("ignored")
    renderer.handle_endtag("strong")
    renderer.handle_endtag("script")
    renderer.handle_starttag("br", [])

    assert renderer.html_parts == ["<br>"]
    assert renderer.text_parts == ["\n"]


def test_get_secret_reads_environment(monkeypatch):
    """Verify secrets can be provided through environment variables."""
    monkeypatch.setenv("RECIPIENT_EMAIL", "user@example.com")
    assert databricks_digest.get_secret("scope", "recipient-email") == "user@example.com"


def test_get_secret_reads_dbutils(monkeypatch):
    """Verify secrets fall back to Databricks dbutils."""
    monkeypatch.delenv("SOME_KEY", raising=False)
    dbutils = MagicMock()
    dbutils.secrets.get.return_value = "secret-value"
    monkeypatch.setattr(databricks_digest, "dbutils", dbutils, raising=False)
    assert databricks_digest.get_secret("scope", "some-key") == "secret-value"
    dbutils.secrets.get.assert_called_once_with(scope="scope", key="some-key")


def test_get_secret_requires_runtime_or_environment(monkeypatch):
    """Verify missing local and Databricks secret providers raise an error."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    monkeypatch.delattr(databricks_digest, "dbutils", raising=False)
    with patch.dict(sys.modules, {"databricks": None, "databricks.sdk": None, "databricks.sdk.runtime": None}):
        with pytest.raises(RuntimeError, match="MISSING_KEY"):
            databricks_digest.get_secret("scope", "missing-key")


def test_load_new_release_notes_excludes_processed_ids():
    """Verify stored note IDs are excluded from new results."""
    spark = MagicMock()
    spark.sql.return_value.collect.return_value = [MagicMock(note_id="known")]
    notes = databricks_digest.load_new_release_notes(
        spark, "catalog.schema.notes", [make_note("known"), make_note("new")]
    )
    assert [note.note_id for note in notes] == ["new"]
    spark.sql.assert_any_call("SELECT note_id FROM catalog.schema.notes")


def test_capture_release_notes_merges_by_stable_feed_id():
    """Verify discovery persists feed metadata with an idempotent merge key."""
    spark = MagicMock()
    databricks_digest.capture_release_notes(spark, "catalog.schema.notes", [make_note("future")])

    spark.createDataFrame.assert_called_once_with(
        [
            (
                "future",
                "Databricks announcement",
                "https://learn.microsoft.com/en-us/azure/databricks/release-notes/future",
                "Azure Databricks",
                1_700_000_000,
                "A release note summary.",
            )
        ],
        ["note_id", "title", "note_url", "category", "published_utc", "summary"],
    )
    assert "WHEN MATCHED THEN UPDATE SET" in spark.sql.call_args.args[0]
    assert "WHEN NOT MATCHED THEN INSERT" in spark.sql.call_args.args[0]


def test_load_pending_release_notes_maps_unprocessed_rows():
    """Verify pending captured rows are converted back to release notes."""
    spark = MagicMock()
    spark.sql.return_value.collect.return_value = [
        MagicMock(
            note_id="future",
            title="Future",
            note_url="https://learn.microsoft.com/en-us/azure/databricks/release-notes/future",
            category="Platform",
            published_utc=1_700_000_000,
            summary="Summary",
        )
    ]

    notes = databricks_digest.load_pending_release_notes(spark, "catalog.schema.notes")

    assert notes == [
        databricks_digest.ReleaseNote(
            note_id="future",
            title="Future",
            url="https://learn.microsoft.com/en-us/azure/databricks/release-notes/future",
            category="Platform",
            published_utc=1_700_000_000,
            summary="Summary",
        )
    ]
    assert "WHERE processed_at IS NULL" in spark.sql.call_args.args[0]


def test_send_digest_uses_gmail_secrets_and_escapes_html():
    """Verify delivery uses secrets, chronological ordering, and escaped HTML."""
    first = make_note("first", 2)
    second = make_note("second", 1)
    second = databricks_digest.ReleaseNote(**{**second.__dict__, "title": "<Important>"})
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    with (
        patch.object(databricks_digest, "get_secret", side_effect=["to@gmail.com", "app-password"]),
        patch.object(
            databricks_digest,
            "summarize_digest",
            return_value=["Review the updated APIs.", "Update clients."],
        ),
        patch.object(databricks_digest.smtplib, "SMTP_SSL", return_value=smtp),
    ):
        databricks_digest.send_digest([first, second], "release-notes-digest", pub_date=date(2026, 1, 14))

    message = smtp.send_message.call_args.args[0]
    assert message["To"] == "to@gmail.com"
    assert "Azure Databricks Release Notes" in message["Subject"]
    plain_content = message.get_body(preferencelist=("plain",)).get_content()
    html_content = message.get_body(preferencelist=("html",)).get_content()
    assert "**- Review the updated APIs.**" in plain_content
    assert "**- Update clients.**" in plain_content
    assert "<li><strong>Review the updated APIs.</strong></li>" in html_content
    assert "<li><strong>Update clients.</strong></li>" in html_content
    assert plain_content.index("second") < plain_content.index("first")
    assert "&lt;Important&gt;" in html_content
    assert databricks_digest.MICROSOFT_LOGO_URL in html_content
    assert databricks_digest.DATABRICKS_LOGO_URL in html_content
    smtp.login.assert_called_once_with("to@gmail.com", "app-password")


def test_send_digest_sends_items_when_llm_summary_fails():
    """Verify LLM failures do not prevent the release-note email from sending."""
    smtp = MagicMock()
    smtp.__enter__.return_value = smtp
    with (
        patch.object(databricks_digest, "get_secret", side_effect=["to@gmail.com", "app-password"]),
        patch.object(databricks_digest, "summarize_digest", side_effect=RuntimeError("model unavailable")),
        patch.object(databricks_digest.smtplib, "SMTP_SSL", return_value=smtp),
    ):
        databricks_digest.send_digest([make_note()], "release-notes-digest", pub_date=date(2026, 1, 14))

    message = smtp.send_message.call_args.args[0]
    plain_content = message.get_body(preferencelist=("plain",)).get_content()
    assert "Databricks announcement" in plain_content
    assert "LLM summary:" not in plain_content


def test_save_release_notes_skips_empty_input():
    """Verify saving an empty list performs no DataFrame write."""
    spark = MagicMock()
    databricks_digest.save_release_notes(spark, "catalog.schema.notes", [])
    spark.createDataFrame.assert_not_called()


def test_save_release_notes_merges_rows():
    """Verify release notes are converted to a temporary Spark view."""
    spark = MagicMock()
    databricks_digest.save_release_notes(spark, "catalog.schema.notes", [make_note()])
    spark.createDataFrame.assert_called_once()
    spark.createDataFrame.return_value.createOrReplaceTempView.assert_called_once_with("delivered_release_notes")
    assert "WHEN MATCHED THEN UPDATE SET processed_at" in spark.sql.call_args.args[0]


def test_local_digest_main_dry_run(capsys):
    """Verify the local-digest dry-run path does not send email."""
    local_digest = load_local_digest()
    with (
        patch.object(local_digest, "load_dotenv"),
        patch.object(local_digest, "fetch_release_notes", return_value=[make_note()]),
        patch.object(sys, "argv", ["local_digest.py", "--start-date", "2026-09-16", "--dry-run"]),
    ):
        local_digest.main()
    assert "Dry run complete" in capsys.readouterr().out


def test_local_digest_main_sends_email():
    """Verify the local digest sends the representative release note."""
    local_digest = load_local_digest()
    with (
        patch.object(local_digest, "load_dotenv"),
        patch.object(local_digest, "fetch_release_notes", return_value=[make_note()]) as fetch_notes,
        patch.object(local_digest, "send_digest") as send_digest,
        patch.object(
            sys,
            "argv",
            ["local_digest.py", "--start-date", "2026-09-16", "--end-date", "2026-09-18"],
        ),
    ):
        local_digest.main()
    fetch_notes.assert_called_once_with(
        limit=None,
        start_date=date(2026, 9, 16),
        end_date=date(2026, 9, 18),
    )
    send_digest.assert_called_once()


def test_local_digest_main_reports_fetch_errors():
    """Verify local-digest fetch errors are reported through argparse."""
    local_digest = load_local_digest()
    with (
        patch.object(local_digest, "load_dotenv"),
        patch.object(local_digest, "fetch_release_notes", side_effect=RuntimeError("invalid")),
        patch.object(sys, "argv", ["local_digest.py", "--start-date", "2026-09-16"]),
    ):
        with pytest.raises(SystemExit) as error:
            local_digest.main()
    assert error.value.code == 1


def test_local_digest_main_skips_empty_email(capsys):
    """Verify an empty date window does not attempt email delivery."""
    local_digest = load_local_digest()
    with (
        patch.object(local_digest, "load_dotenv"),
        patch.object(local_digest, "fetch_release_notes", return_value=[]),
        patch.object(local_digest, "send_digest") as send_digest,
        patch.object(sys, "argv", ["local_digest.py", "--start-date", "2026-09-16"]),
    ):
        local_digest.main()
    send_digest.assert_not_called()
    assert "no email sent" in capsys.readouterr().out


def test_databricks_main_handles_empty_run(monkeypatch):
    """Verify the Databricks entry point skips email when no notes are new."""
    spark = MagicMock()
    spark_module = types.ModuleType("pyspark.sql")
    spark_module.SparkSession = MagicMock(builder=MagicMock(getOrCreate=MagicMock(return_value=spark)))
    monkeypatch.setitem(sys.modules, "pyspark.sql", spark_module)
    monkeypatch.setattr(databricks_digest, "fetch_release_notes", lambda **_: [])
    monkeypatch.setattr(databricks_digest, "load_new_release_notes", lambda *_: [])
    monkeypatch.setattr(sys, "argv", ["databricks_digest.py", "--digest-table", "table", "--secret-scope", "scope"])
    databricks_digest.main()


def test_databricks_main_sends_and_saves(monkeypatch):
    """Verify the Databricks entry point delivers and persists new notes."""
    spark = MagicMock()
    spark_module = types.ModuleType("pyspark.sql")
    spark_module.SparkSession = MagicMock(builder=MagicMock(getOrCreate=MagicMock(return_value=spark)))
    monkeypatch.setitem(sys.modules, "pyspark.sql", spark_module)
    notes = [make_note()]
    monkeypatch.setattr(databricks_digest, "fetch_release_notes", lambda **_: notes)
    monkeypatch.setattr(databricks_digest, "capture_release_notes", MagicMock())
    monkeypatch.setattr(databricks_digest, "load_pending_release_notes", lambda *_: notes)
    monkeypatch.setattr(databricks_digest, "send_digest", MagicMock())
    monkeypatch.setattr(databricks_digest, "save_release_notes", MagicMock())
    monkeypatch.setattr(sys, "argv", ["databricks_digest.py", "--digest-table", "table", "--secret-scope", "scope"])
    databricks_digest.main()
