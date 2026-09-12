"""Fetch new r/databricks News/Event posts and send a Gmail digest."""

from __future__ import annotations

import argparse
import calendar
import html
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
LOCAL_TIMEZONE = ZoneInfo("Europe/Amsterdam")


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
    """Fetch News and Event posts, falling back to RSS when Reddit JSON is blocked."""
    try:
        return fetch_json_posts(limit)
    except Exception as error:
        if not isinstance(error, urllib.error.HTTPError) or error.code not in {403, 429}:
            raise
        return fetch_rss_posts(limit)


def request_json(url: str) -> Any:
    """Fetch and decode a JSON response from the specified URL."""
    request = Request(
        url,
        headers={"User-Agent": os.getenv("REDDIT_USER_AGENT", "databricks-digest/1.0")},
    )
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_json_posts(limit: int) -> list[RedditPost]:
    """Fetch Reddit JSON posts and retain only allowed flairs."""
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
    """Fetch, deduplicate, sort, and limit Reddit posts from RSS feeds."""
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
    """Fetch and parse an RSS or Atom response from the specified URL."""
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
    """Convert one parsed RSS entry into a Reddit post record."""
    post_id = entry.id.removeprefix("t3_")
    title = entry.title
    url = entry.link
    author = entry.get("author", "").removeprefix("/u/")
    created_utc = float(calendar.timegm(entry.published_parsed))
    return RedditPost(post_id, title, url, author or "[deleted]", flair, created_utc, "")


def load_new_posts(spark: Any, table_name: str, posts: list[RedditPost]) -> list[RedditPost]:
    """Create the digest table if needed and exclude posts already stored in it."""
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
    """Format posts as plain text and HTML, then send them using Databricks secrets."""
    recipient = get_secret(scope, "recipient-email")
    password = get_secret(scope, "smtp-app-password")
    date_label = datetime.now(LOCAL_TIMEZONE).strftime("%Y-%m-%d")
    sorted_posts = sorted(posts, key=lambda item: item.created_utc)

    message = EmailMessage()
    message["Subject"] = f"r/databricks News and Event digest - {date_label}"
    message["From"] = recipient
    message["To"] = recipient
    lines = [f"New r/databricks posts with News or Event flair: {len(posts)}", ""]
    for post in sorted_posts:
        lines.append(f"[{post.flair}] {post.title}")
        lines.append(f"{post.url} (u/{post.author})")
        lines.append("")
    message.set_content("\n".join(lines))

    cards = []
    for post in sorted_posts:
        flair_color = "#ff4500" if post.flair == "News" else "#7193ff"
        safe_flair = html.escape(post.flair)
        safe_title = html.escape(post.title)
        safe_url = html.escape(post.url, quote=True)
        safe_author = html.escape(post.author)
        cards.append(
            f"""
            <article style="background:#ffffff;border:1px solid #d6d6d6;border-radius:4px;
                            margin:0 0 16px;padding:18px 20px;">
                <div style="color:#878a8c;font-size:12px;font-weight:700;margin-bottom:10px;">
                    r/databricks <span style="color:#a7a9aa;font-weight:400;">&middot; posted by u/{safe_author}</span>
                </div>
                <div style="color:{flair_color};font-size:11px;font-weight:700;letter-spacing:.6px;
                            text-transform:uppercase;">{safe_flair}</div>
                <h2 style="color:#1a1a1b;font-size:19px;line-height:1.3;margin:7px 0 12px;">
                    <a href="{safe_url}" style="color:#1a1a1b;text-decoration:none;">{safe_title}</a>
                </h2>
                <a href="{safe_url}" style="color:#ff4500;font-size:13px;font-weight:700;text-decoration:none;">
                    View post &rarr;
                </a>
            </article>
            """
        )
    html_content = f"""
    <!DOCTYPE html>
    <html>
      <body style="background:#dae0e6;margin:0;padding:28px 12px;font-family:Arial,sans-serif;">
        <main style="background:#f6f7f8;border-top:4px solid #ff4500;margin:0 auto;max-width:640px;
                 padding:0 18px 18px;">
          <header style="background:#ffffff;border-bottom:1px solid #d6d6d6;margin:0 -18px 22px;padding:24px;">
            <div style="color:#ff4500;font-size:12px;font-weight:800;letter-spacing:1.5px;">r/DATABRICKS</div>
            <h1 style="color:#1a1a1b;font-size:28px;line-height:1.15;margin:9px 0 8px;">News &amp; Events Digest</h1>
            <p style="color:#7c7c7c;font-size:14px;margin:0;">
              {html.escape(date_label)} &middot; {len(posts)} new posts
            </p>
          </header>
          {"".join(cards)}
          <footer style="color:#878a8c;font-size:12px;padding:8px 4px;text-align:center;">
            Curated from r/databricks &middot; Delivered by Reddit digest
          </footer>
        </main>
      </body>
    </html>
    """
    message.add_alternative(html_content, subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(recipient, password)
        smtp.send_message(message)


def save_posts(spark: Any, table_name: str, posts: list[RedditPost]) -> None:
    """Merge newly delivered posts into the Databricks Delta table."""
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
    """Fetch new posts, email the digest, and record delivered posts."""
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
