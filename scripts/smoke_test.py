"""Run a local release-notes digest and optional Gmail delivery smoke test."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.databricks_digest import fetch_release_notes, send_digest  # noqa: E402


def main() -> None:
    """Fetch one day's release notes and optionally send a local digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days-ago",
        type=int,
        default=1,
        help="Fetch notes from this many local calendar days ago through today (default: 1)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of feed notes")
    parser.add_argument("--dry-run", action="store_true", help="Print matching notes without sending email")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        notes = fetch_release_notes(limit=args.limit, days_ago=args.days_ago)
    except (RuntimeError, ValueError) as error:
        parser.exit(1, f"Smoke test could not fetch release notes: {error}\n")
    print(f"Found {len(notes)} release notes in the requested date window.")
    for note in notes:
        print(f"[{note.category}] {note.title}\n  {note.url}")

    if args.dry_run:
        print("Dry run complete; no email sent.")
        return

    if not notes:
        print("No release notes found; no email sent.")
        return

    send_digest(notes, "local")
    print("Email sent successfully.")


if __name__ == "__main__":  # pragma: no cover
    main()
