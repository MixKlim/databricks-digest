"""Fetch new r/databricks News/Event posts and send a Gmail digest."""

from __future__ import annotations

import argparse
import calendar
import json
import os
import smtplib
import ssl
import urllib.error
from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import feedparser

REDDIT_URL = "https://www.reddit.com/r/databricks/new.json"
REDDIT_RSS_URL = "https://old.reddit.com/r/databricks/search.rss"
ALLOWED_FLAIRS = frozenset({"News", "Event"})
LOCAL_TIMEZONE = ZoneInfo("Europe/Paris")


@dataclass(frozen=True)
class RedditPost:
    post_id: str
    title: str
    url: str
    author: str
    flair: str
    created_utc: float
    selftext: str


def get_secret(scope: str, key: str) -> str:
    """Read a Databricks secret, with env vars available for local tests."""
    environment_key = key.upper().replace("-", "_")
    environment_value = os.getenv(environment_key)
    if environment_value:
        return environment_value

    dbutils = globals().get("dbutils")
    if dbutils is None:
        try:
            from databricks.sdk.runtime import dbutils as runtime_dbutils
        except ImportError:
            runtime_dbutils = None
        dbutils = runtime_dbutils
    if dbutils is None:
        raise RuntimeError(f"Missing secret {key}; set {environment_key} locally or use Databricks secrets.")
    return dbutils.secrets.get(scope=scope, key=key)


def fetch_posts(limit: int = 100) -> list[RedditPost]:
    try:
        return fetch_json_posts(limit)
    except Exception as error:
        if not isinstance(error, urllib.error.HTTPError) or error.code not in {403, 429}:
            raise
        return fetch_rss_posts(limit)


def request_json(url: str) -> Any:
    request = Request(
        url,
        headers={"User-Agent": os.getenv("REDDIT_USER_AGENT", "databricks-digest/1.0")},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_json_posts(limit: int) -> list[RedditPost]:
    query = urlencode({"limit": limit, "raw_json": 1})
    payload = request_json(f"{REDDIT_URL}?{query}")

    posts = []
    for item in payload["data"]["children"]:
        data = item["data"]
        flair = data.get("link_flair_text")
        if flair not in ALLOWED_FLAIRS:
            continue
        posts.append(
            RedditPost(
                post_id=data["id"],
                title=data["title"],
                url=f"https://www.reddit.com{data['permalink']}",
                author=data.get("author") or "[deleted]",
                flair=flair,
                created_utc=float(data["created_utc"]),
                selftext=data.get("selftext") or "",
            )
        )
    return posts


def fetch_rss_posts(limit: int) -> list[RedditPost]:
    posts_by_id: dict[str, RedditPost] = {}
    per_flair_limit = max(1, limit // len(ALLOWED_FLAIRS))
    for flair in sorted(ALLOWED_FLAIRS):
        query = urlencode({"q": f'flair:"{flair}"', "restrict_sr": 1, "sort": "new", "limit": per_flair_limit})
        try:
            payload = request_xml(f"{REDDIT_RSS_URL}?{query}")
        except urllib.error.HTTPError as error:
            raise RuntimeError(
                f"Reddit RSS is unavailable ({error.code}). Try again later or provide Reddit API access."
            ) from error
        for entry in payload.entries:
            post = parse_rss_entry(entry, flair)
            posts_by_id[post.post_id] = post
    return sorted(posts_by_id.values(), key=lambda post: post.created_utc, reverse=True)[:limit]


def request_xml(url: str) -> feedparser.FeedParserDict:
    request = Request(
        url,
        headers={"User-Agent": os.getenv("REDDIT_USER_AGENT", "databricks-digest/1.0")},
    )
    with urlopen(request, timeout=30) as response:
        feed = feedparser.parse(response.read())
    if feed.bozo and not feed.entries:
        raise RuntimeError("Reddit returned a non-RSS page instead of the RSS feed, likely a bot or login challenge.")
    return feed


def parse_rss_entry(entry: feedparser.FeedParserDict, flair: str) -> RedditPost:
    post_id = entry.id.removeprefix("t3_")
    title = entry.title
    url = entry.link
    author = entry.get("author", "").removeprefix("/u/")
    created_utc = float(calendar.timegm(entry.published_parsed))
    return RedditPost(post_id, title, url, author or "[deleted]", flair, created_utc, "")


def load_new_posts(spark: Any, table_name: str, posts: list[RedditPost]) -> list[RedditPost]:
    spark.sql(
        f"""CREATE TABLE IF NOT EXISTS {table_name} (
            post_id STRING,
            title STRING,
            post_url STRING,
            flair STRING,
            created_utc DOUBLE,
            processed_at TIMESTAMP
        ) USING DELTA"""
    )
    known_ids = {row.post_id for row in spark.sql(f"SELECT post_id FROM {table_name}").collect()}
    return [post for post in posts if post.post_id not in known_ids]


def send_digest(posts: list[RedditPost], scope: str) -> None:
    recipient = get_secret(scope, "recipient-email")
    password = get_secret(scope, "smtp-app-password")
    date_label = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d")

    message = EmailMessage()
    message["Subject"] = f"r/databricks News and Event digest - {date_label}"
    message["From"] = recipient
    message["To"] = recipient
    lines = [f"New r/databricks posts with News or Event flair: {len(posts)}", ""]
    for post in sorted(posts, key=lambda item: item.created_utc):
        lines.append(f"[{post.flair}] {post.title}")
        lines.append(f"{post.url} (u/{post.author})")
        lines.append("")
    message.set_content("\n".join(lines))

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(recipient, password)
        smtp.send_message(message)


def save_posts(spark: Any, table_name: str, posts: list[RedditPost]) -> None:
    rows = [(post.post_id, post.title, post.url, post.flair, post.created_utc) for post in posts]
    if not rows:
        return
    new_posts = spark.createDataFrame(rows, ["post_id", "title", "post_url", "flair", "created_utc"])
    new_posts.createOrReplaceTempView("new_digest_posts")
    spark.sql(
        f"""MERGE INTO {table_name} AS target
        USING new_digest_posts AS source
        ON target.post_id = source.post_id
        WHEN NOT MATCHED THEN INSERT (post_id, title, post_url, flair, created_utc, processed_at)
        VALUES (source.post_id, source.title, source.post_url, source.flair, source.created_utc, current_timestamp())"""
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--digest-table", required=True)
    parser.add_argument("--secret-scope", required=True)
    args = parser.parse_args()

    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    new_posts = load_new_posts(spark, args.digest_table, fetch_posts())
    if not new_posts:
        print("No new News or Event posts found; no email sent.")
        return
    send_digest(new_posts, args.secret_scope)
    save_posts(spark, args.digest_table, new_posts)
    print(f"Sent digest containing {len(new_posts)} posts.")


if __name__ == "__main__":  # pragma: no cover
    main()
