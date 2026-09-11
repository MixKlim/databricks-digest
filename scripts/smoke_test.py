"""Run a local Reddit fetch and optional Gmail delivery smoke test."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.databricks_digest import fetch_posts, send_digest  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="Number of recent Reddit posts to inspect")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print posts without sending email")
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        posts = fetch_posts(limit=args.limit)
    except RuntimeError as error:
        parser.exit(1, f"Smoke test could not fetch Reddit posts: {error}\n")
    print(f"Found {len(posts)} matching News/Event posts.")
    for post in posts:
        print(f"[{post.flair}] {post.title}\n  {post.url}")

    if args.dry_run:
        print("Dry run complete; no email sent.")
        return

    send_digest(posts, "local")
    print("Email sent successfully.")


if __name__ == "__main__":  # pragma: no cover
    main()
