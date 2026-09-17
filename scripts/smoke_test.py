"""Run a local release-notes digest and optional Gmail delivery smoke test."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.databricks_digest import fetch_release_notes, send_digest  # noqa: E402


def main() -> None:
    """Fetch release notes for one publication date and optionally send a local digest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--pub-date",
        type=date.fromisoformat,
        required=True,
        help="Fetch notes published on this local calendar date (YYYY-MM-DD)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Optional maximum number of feed notes")
    parser.add_argument("--dry-run", action="store_true", help="Print matching notes without sending email")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        notes = fetch_release_notes(limit=args.limit, pub_date=args.pub_date)
    except (RuntimeError, ValueError) as error:
        parser.exit(1, f"Smoke test could not fetch release notes: {error}\n")
    print(f"Found {len(notes)} release notes published on {args.pub_date.isoformat()}.")
    for note in notes:
        print(f"[{note.category}] {note.title}\n  {note.url}")

    if args.dry_run:
        print("Dry run complete; no email sent.")
        return

    if not notes:
        print("No release notes found; no email sent.")
        return

    send_digest(notes, "local", pub_date=args.pub_date)
    print("Email sent successfully.")


if __name__ == "__main__":  # pragma: no cover
    main()
