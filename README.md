# Databricks Reddit Digest

Scheduled Databricks Asset Bundle that fetches new `News` and `Event` link-flair posts from [r/databricks](https://www.reddit.com/r/databricks/) and sends a Gmail digest.

## Databricks setup

1. Create a Unity Catalog schema for the state table. The defaults are catalog `workspace` and schema `default`; override them with bundle variables if needed.
2. Create a Databricks secret scope named `reddit-digest` and add these keys:
   - `recipient-email`: the Gmail address used to send and receive the digest.
   - `smtp-app-password`: a Gmail app password, not the normal account password.
3. Install the Databricks CLI and authenticate to the workspace.
4. Authenticate with the configured Databricks CLI profile and deploy:

   ```text
   databricks bundle deploy -t dev -p mixklim
   ```

The job runs daily at 08:00 CET/CEST using the `Europe/Paris` timezone and stores processed Reddit IDs in `<catalog>.<schema>.databricks_digest_posts`. It sends no email when there are no new matching posts. Reddit's public feed is limited to its latest 100 posts, so the job should run at least daily to avoid gaps during unusually high activity.

## Local checks

Install the development dependencies with UV, then run:

```text
uv sync --dev
uv run pre-commit run --all-files
uv run pytest
```

### Local email smoke test

This tests the live Reddit fetch and Gmail SMTP delivery without requiring PySpark. Use a Gmail app password, not your normal Gmail password. Copy `.env.example` to `.env`, fill in the values, then run:

```powershell
Copy-Item .env.example .env
# Edit .env and replace the placeholder values.
uv sync --dev
uv run python scripts/smoke_test.py --dry-run --limit 10
uv run python scripts/smoke_test.py
```

The first command fetches and prints matching posts without sending mail. The second sends all matching posts in the selected Reddit page and does not update the Delta state table. Use `--limit 10` to reduce the number of posts included. The unit tests cover the filtering, deduplication, and email composition without sending mail:

The fetcher uses Reddit's JSON feed first and attempts flair-filtered RSS from `old.reddit.com` when Reddit blocks or rate-limits the JSON endpoint. Reddit may return a bot or login challenge instead of RSS; in that case, use Reddit API OAuth credentials or run from a network where Reddit's public feed is available.

```text
make test
```

### Full Databricks test

To test the actual Spark/Delta watermark and secret-scope path, deploy the development target and start the job manually:

```powershell
databricks bundle deploy -t dev -p mixklim
databricks bundle run -t dev -p mixklim databricks_digest
```

Check the run output and the recipient inbox. A second run with no new matching Reddit posts should report `No new News or Event posts found; no email sent.`

## GitHub Actions

Configure the `production` environment with:

- Repository/environment variable `DATABRICKS_HOST`.
- Repository/environment secret `DATABRICKS_TOKEN` for the Databricks service principal.

The workflow validates on pull requests and deploys the `prod` bundle after a push to `main`.

The Gmail address and SMTP credentials are never GitHub Actions variables; they remain in the Databricks secret scope and are read only at job runtime.
