"""Fetch Azure Databricks release notes and send a daily Gmail digest."""

from __future__ import annotations

import argparse
import calendar
import html
import logging
import os
import re
import smtplib
import sqlite3
import ssl
import urllib.error
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

import feedparser
from google import genai
from google.genai import types

LOGGER = logging.getLogger("databricks-digest")
RELEASE_NOTES_FEED_URL = "https://learn.microsoft.com/en-us/azure/databricks/feed.xml"
FEED_USER_AGENT = "databricks-release-digest/1.0"
LOCAL_TIMEZONE = ZoneInfo("Europe/Amsterdam")
MICROSOFT_LOGO_URL = "https://logos-world.net/wp-content/uploads/2020/09/Microsoft-Logo-700x394.png"
DATABRICKS_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/6/63/Databricks_Logo.png"


@dataclass(frozen=True)
class ReleaseNote:
    """Represent one release note from the Microsoft Learn feed."""

    note_id: str
    title: str
    url: str
    category: str
    published_utc: float
    summary: str


def get_secret(scope: str, key: str) -> str:
    """Read a secret from the process environment."""
    environment_key = key.upper().replace("-", "_")
    environment_value = os.getenv(environment_key)
    if environment_value:
        return environment_value

    raise RuntimeError(f"Missing required environment variable: {environment_key}")


def fetch_release_notes(
    limit: int | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[ReleaseNote]:
    """Fetch release notes, optionally limited to a local publication date range."""
    if limit is not None and limit < 1:
        return []
    if end_date is not None and start_date is None:
        raise ValueError("start_date is required when end_date is provided.")
    if start_date is not None and end_date is not None and end_date < start_date:
        raise ValueError("end_date cannot be earlier than start_date.")
    LOGGER.info(
        "Fetching release notes from Microsoft Learn feed (limit=%s, start_date=%s, end_date=%s).",
        limit,
        start_date,
        end_date,
    )
    try:
        feed = request_feed(RELEASE_NOTES_FEED_URL)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"Microsoft Learn release-notes feed is unavailable ({error.code}).") from error
    if feed.bozo and not feed.entries:
        raise RuntimeError("Microsoft Learn returned an invalid release-notes feed.")

    notes = [parse_feed_entry(entry) for entry in feed.entries]
    LOGGER.info("Parsed %d release notes from Microsoft Learn feed.", len(notes))
    if start_date is not None:
        range_end = end_date or start_date
        notes = [note for note in notes if start_date <= release_date(note) <= range_end]
        LOGGER.info(
            "Filtered release notes to local publication date range %s through %s: %d notes.",
            start_date,
            range_end,
            len(notes),
        )
    unique_notes: dict[tuple[date, str], ReleaseNote] = {}
    for note in notes:
        deduplication_key = (release_date(note), " ".join(note.title.split()).casefold())
        unique_notes.setdefault(deduplication_key, note)
    if len(unique_notes) != len(notes):
        LOGGER.info("Removed %d duplicate release notes.", len(notes) - len(unique_notes))
    notes = list(unique_notes.values())
    return sorted(notes, key=lambda note: note.published_utc, reverse=True)[:limit]


def release_date(note: ReleaseNote) -> date:
    """Return a release note's publication date in the digest's local timezone."""
    return datetime.fromtimestamp(note.published_utc, LOCAL_TIMEZONE).date()


def request_feed(url: str) -> feedparser.FeedParserDict:
    """Fetch and parse an RSS or Atom feed over HTTPS with a bounded timeout."""
    if url != RELEASE_NOTES_FEED_URL:
        raise ValueError("Only the configured Microsoft Learn release-notes feed is allowed.")
    request = Request(url, headers={"User-Agent": FEED_USER_AGENT, "Accept": "application/rss+xml, application/xml"})
    with urlopen(request, timeout=30) as response:
        return feedparser.parse(response.read())


def parse_feed_entry(entry: feedparser.FeedParserDict) -> ReleaseNote:
    """Convert one Microsoft Learn feed item into a release-note record."""
    published = entry.get("published_parsed") or entry.get("updated_parsed")
    if published is None:
        raise ValueError(f"Release-note feed item {entry.get('id', '<unknown>')} has no publication date.")
    note_id = entry.get("id") or entry.get("link")
    url = entry.get("link")
    if not note_id or not url:
        raise ValueError("Release-note feed item is missing its ID or URL.")
    if not url.startswith("https://learn.microsoft.com/"):
        raise ValueError(f"Release-note feed item has an unexpected URL: {url}")
    categories = entry.get("tags", [])
    category = ", ".join(tag.get("term", "") for tag in categories if tag.get("term"))
    return ReleaseNote(
        note_id=note_id,
        title=entry.get("title", "Untitled release note"),
        url=url,
        category=category or "Azure Databricks",
        published_utc=float(calendar.timegm(published)),
        summary=entry.get("summary", "").strip(),
    )


class SummaryRenderer(HTMLParser):
    """Convert trusted feed markup into a small safe subset for email clients."""

    allowed_tags = {"a", "strong", "b", "em", "i", "p", "br", "ul", "ol", "li"}
    block_tags = {"p", "li"}

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.html_parts: list[str] = []
        self.text_parts: list[str] = []
        self.ignored_depth = 0
        self.link_open = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
            return
        if self.ignored_depth:
            return
        if tag == "a":
            href = dict(attrs).get("href")
            absolute_url = urljoin(self.base_url, href or "")
            parsed_url = urlparse(absolute_url)
            if parsed_url.scheme == "https" and parsed_url.netloc == "learn.microsoft.com":
                self.html_parts.append(f'<a href="{html.escape(absolute_url, quote=True)}">')
                self.link_open = True
            return
        if tag in self.allowed_tags:
            self.html_parts.append(f"<{tag}>")
        if tag == "br":
            self.text_parts.append("\n")
        elif tag in self.block_tags:
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth = max(0, self.ignored_depth - 1)
            return
        if self.ignored_depth:
            return
        if tag == "a" and self.link_open:
            self.html_parts.append("</a>")
            self.link_open = False
        elif tag in self.allowed_tags and tag != "br":
            self.html_parts.append(f"</{tag}>")
        if tag in self.block_tags:
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.ignored_depth:
            return
        self.html_parts.append(html.escape(data))
        self.text_parts.append(data)


def render_summary(summary: str, base_url: str) -> tuple[str, str]:
    """Return sanitized HTML and readable plain text for a feed summary."""
    renderer = SummaryRenderer(base_url)
    renderer.feed(summary)
    renderer.close()
    plain_summary = " ".join(renderer.text_parts).split()
    readable_text = re.sub(r"\s+([.,!?;:])", r"\1", " ".join(plain_summary))
    return "".join(renderer.html_parts).strip(), readable_text


def summarize_digest(
    items: Sequence[ReleaseNote],
    client: genai.Client | None = None,
    secret_scope: str | None = None,
) -> list[str]:
    """Summarize each release-note item as one concise, actionable bullet sentence."""
    if not items:
        return []

    if client is None:
        api_key = get_secret(secret_scope, "gemini-api-key") if secret_scope else os.environ["GEMINI_API_KEY"]
        client = genai.Client(api_key=api_key)

    digest_items = []
    for item in items:
        _, readable_summary = render_summary(item.summary, item.url)
        digest_items.append(f"- [{item.category}] {item.title}: {readable_summary or 'No details provided.'}")

    response = client.models.generate_content(
        model="gemini-3.5-flash-lite",
        contents=(
            "Summarize each of the following Azure Databricks release-note items as exactly one short "
            "bullet sentence, preserving the input order. Keep the wording professional and "
            "developer-focused. Prioritize practical impact, migration or compatibility concerns, "
            "and concrete actions developers should take. Return exactly one line per item, each "
            "starting with '- '. Do not include category labels or bracketed tags, and do not add "
            "headings or introductory text.\n\n" + "\n".join(digest_items)
        ),
        config=types.GenerateContentConfig(
            system_instruction="You are a pragmatic technical release-notes analyst.",
            temperature=0.0,
            max_output_tokens=1000,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        ),
    )

    summaries = [
        re.sub(
            r"^(?:\[[^\]]+\]\s*)+",
            "",
            re.sub(r"^(?:[-*]|\d+[.)])\s*", "", " ".join(line.split())),
        )
        for line in (response.text or "").splitlines()
        if line.strip()
    ]
    if not summaries:
        raise RuntimeError("Gemini returned an empty digest summary.")
    if len(summaries) != len(items):
        raise RuntimeError("Gemini returned an unexpected number of digest summaries.")
    return summaries


@contextmanager
def release_notes_db(database_path: Path) -> Iterator[sqlite3.Connection]:
    """Open the SQLite digest state database, creating its schema when needed."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    try:
        with connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS release_notes (
                    note_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    note_url TEXT NOT NULL,
                    category TEXT NOT NULL,
                    published_utc REAL NOT NULL,
                    summary TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    processed_at TEXT
                )"""
            )
            yield connection
    finally:
        connection.close()


def capture_release_notes(database_path: Path, notes: list[ReleaseNote]) -> None:
    """Upsert fetched feed items while preserving their delivery state."""
    if not notes:
        LOGGER.info("No release notes discovered.")
        return
    rows = [(note.note_id, note.title, note.url, note.category, note.published_utc, note.summary) for note in notes]
    with release_notes_db(database_path) as connection:
        connection.executemany(
            """INSERT INTO release_notes
                (note_id, title, note_url, category, published_utc, summary)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(note_id) DO UPDATE SET
                    title = excluded.title,
                    note_url = excluded.note_url,
                    category = excluded.category,
                    published_utc = excluded.published_utc,
                    summary = excluded.summary,
                    last_seen_at = CURRENT_TIMESTAMP""",
            rows,
        )
    LOGGER.info("Captured %d release notes in %s.", len(rows), database_path)


def load_pending_release_notes(database_path: Path) -> list[ReleaseNote]:
    """Load captured release notes that have not been delivered yet."""
    with release_notes_db(database_path) as connection:
        rows = connection.execute(
            """SELECT note_id, title, note_url, category, published_utc, summary
            FROM release_notes
            WHERE processed_at IS NULL
            ORDER BY published_utc DESC"""
        ).fetchall()
    return [ReleaseNote(row[0], row[1], row[2], row[3], float(row[4]), row[5] or "") for row in rows]


def save_release_notes(database_path: Path, notes: list[ReleaseNote]) -> None:
    """Mark successfully delivered release notes as processed."""
    if not notes:
        return
    with release_notes_db(database_path) as connection:
        connection.executemany(
            "UPDATE release_notes SET processed_at = CURRENT_TIMESTAMP WHERE note_id = ?",
            [(note.note_id,) for note in notes],
        )
    LOGGER.info("Marked %d release notes delivered in %s.", len(notes), database_path)


def send_digest(notes: list[ReleaseNote], scope: str, pub_date: date) -> None:
    """Format release notes as plain text and HTML, then send them by Gmail."""
    LOGGER.info("Preparing digest email for %d release notes using secret scope %s.", len(notes), scope)
    recipient = get_secret(scope, "recipient-email")
    password = get_secret(scope, "smtp-app-password")
    date_label = pub_date.strftime("%Y-%m-%d")
    sorted_notes = sorted(notes, key=lambda item: item.published_utc)
    llm_summaries = None
    try:
        llm_summaries = summarize_digest(sorted_notes, secret_scope=scope)
    except Exception:  # noqa: BLE001
        LOGGER.warning("LLM digest summary unavailable; continuing without it.", exc_info=True)

    message = EmailMessage()
    message["Subject"] = f"Azure Databricks Release Notes | {date_label}"
    message["From"] = recipient
    message["To"] = recipient
    lines = [f"New Azure Databricks release notes: {len(notes)}", ""]
    if llm_summaries:
        lines.extend(["LLM summary:", *[f"**- {summary}**" for summary in llm_summaries], ""])
    for note in sorted_notes:
        _, plain_summary = render_summary(note.summary, note.url)
        lines.append(f"[{note.category}] {note.title}")
        lines.append(note.url)
        if plain_summary:
            lines.append(plain_summary)
        lines.append("")
    message.set_content("\n".join(lines))

    cards = []
    for note in sorted_notes:
        safe_category = html.escape(note.category)
        safe_title = html.escape(note.title)
        safe_url = html.escape(note.url, quote=True)
        safe_summary, _ = render_summary(note.summary, note.url)
        summary = (
            f'<div style="color:#4b5563;font-size:14px;line-height:1.6;margin:0 0 14px;">{safe_summary}</div>'
            if safe_summary
            else ""
        )
        cards.append(
            f"""
            <article style="background:#ffffff;border:1px solid #d9e2ec;border-radius:4px;
                            margin:0 0 16px;padding:18px 20px;">
                <div style="color:#52606d;font-size:12px;font-weight:700;margin-bottom:10px;">
                    Microsoft Learn &middot; {safe_category}
                </div>
                <h2 style="color:#102a43;font-size:19px;line-height:1.3;margin:7px 0 12px;">
                    <a href="{safe_url}" style="color:#102a43;text-decoration:none;">{safe_title}</a>
                </h2>
                {summary}
                <a href="{safe_url}" style="color:#006dcc;font-size:13px;font-weight:700;text-decoration:none;">
                    Read release note &rarr;
                </a>
            </article>
            """
        )
    llm_summary_html = (
        '<section style="background:#eef6fc;border-left:4px solid #0078d4;margin:0 0 20px;padding:16px 18px;">'
        '<div style="color:#0078d4;font-size:12px;font-weight:700;margin-bottom:7px;">LLM SUMMARY</div>'
        '<ul style="color:#243b53;font-size:14px;line-height:1.6;margin:0;padding-left:20px;">'
        + "".join(f"<li><strong>{html.escape(summary)}</strong></li>" for summary in llm_summaries)
        + "</ul>"
        "</section>"
        if llm_summaries
        else ""
    )
    html_content = f"""
    <!DOCTYPE html>
    <html>
            <body style="background:#e8eef3;margin:0;padding:30px 12px;font-family:'Segoe UI',Arial,sans-serif;">
                <main style="background:#f7f9fb;border-top:5px solid #0078d4;margin:0 auto;max-width:680px;
                                 padding:0 22px 22px;">
                    <header style="background:#ffffff;border-bottom:1px solid #d9e2ec;
                                   margin:0 -22px 24px;padding:25px 26px 23px;">
                            <div style="font-size:13px;font-weight:700;letter-spacing:.3px;margin-bottom:18px;">
                               <img src="{MICROSOFT_LOGO_URL}" alt="Microsoft" width="112"
                                   style="display:inline-block;height:auto;margin:0 14px 0 0;vertical-align:middle;">
                               <span style="color:#b8c5d1;margin-right:14px;vertical-align:middle;">|</span>
                               <img src="{DATABRICKS_LOGO_URL}" alt="Databricks" width="132"
                                   style="display:inline-block;height:auto;vertical-align:middle;">
                        </div>
                        <div style="color:#0078d4;font-size:11px;font-weight:800;
                                    letter-spacing:1.8px;text-transform:uppercase;">
                            Official documentation digest
                        </div>
                        <h1 style="color:#102a43;font-family:Georgia,'Times New Roman',serif;
                                   font-size:31px;font-weight:700;line-height:1.12;margin:8px 0 9px;">
                Azure Databricks Release Notes
            </h1>
                        <p style="color:#627d98;font-size:14px;line-height:1.5;margin:0;">
              {html.escape(date_label)} &middot; {len(notes)} new release notes
            </p>
          </header>
                    {llm_summary_html}
                    {"".join(cards)}
                    <footer style="border-top:1px solid #d9e2ec;color:#627d98;font-size:12px;
                                   line-height:1.5;padding:16px 4px 4px;text-align:center;">
                        Curated from the official Microsoft Learn Azure Databricks documentation feed
          </footer>
        </main>
      </body>
    </html>
    """
    message.add_alternative(html_content, subtype="html")

    context = ssl.create_default_context()
    LOGGER.info("Connecting to Gmail SMTP.")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
        smtp.login(recipient, password)
        smtp.send_message(message)
    LOGGER.info("Digest email delivered successfully.")


def main() -> None:
    """Fetch release notes, email the digest, and persist delivery state."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--state-db",
        type=Path,
        default=Path(os.getenv("DIGEST_STATE_DB", "data/release_notes.sqlite3")),
        help="Path to the SQLite file used to persist sent release notes.",
    )
    args = parser.parse_args()
    LOGGER.info("Starting digest job with state database %s.", args.state_db)
    discovered_notes = fetch_release_notes()
    capture_release_notes(args.state_db, discovered_notes)
    pending_notes = load_pending_release_notes(args.state_db)
    if not pending_notes:
        LOGGER.info("No new release notes found; skipping email delivery.")
        print("No new Azure Databricks release notes found; no email sent.")
        return
    digest_date = datetime.now(LOCAL_TIMEZONE).date()
    send_digest(pending_notes, "github", pub_date=digest_date)
    save_release_notes(args.state_db, pending_notes)
    LOGGER.info("Digest job completed successfully with %d release notes.", len(pending_notes))
    print(f"Sent digest containing {len(pending_notes)} release notes.")


if __name__ == "__main__":  # pragma: no cover
    main()
