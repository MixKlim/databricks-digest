# Databricks Release Notes Digest

[![CI/CD](https://github.com/MixKlim/databricks-digest/actions/workflows/databricks.yml/badge.svg)](https://github.com/MixKlim/databricks-digest/actions/workflows/databricks.yml)
[![Coverage Status](https://raw.githubusercontent.com/mixklim/databricks-digest/main/reports/coverage/coverage-badge.svg?dummy=8484744)](https://raw.githubusercontent.com/mixklim/databricks-digest/main/reports/coverage/index.html)

Scheduled Databricks Asset Bundle that reads the official [Azure Databricks release-notes RSS feed](https://learn.microsoft.com/en-us/azure/databricks/feed.xml) and sends a Gmail digest. Microsoft documents this feed on the [Azure Databricks release-notes page](https://learn.microsoft.com/en-us/azure/databricks/release-notes/).

## Databricks setup

1. Create a Unity Catalog schema for the state table. The defaults are catalog `workspace` and schema `default`; override them with bundle variables if needed.
2. Create a Databricks secret scope named `databricks-digest` and add these keys:
   - `recipient-email`: the Gmail address used to send and receive the digest.
   - `smtp-app-password`: a Gmail app password, not the normal account password.
3. Install the Databricks CLI and authenticate to the workspace.
4. Authenticate with the configured Databricks CLI profile and deploy:

   ```text
   databricks bundle deploy -t dev -p mixklim
   ```

The job runs daily at 08:00 CET/CEST using the `Europe/Amsterdam` timezone and stores processed release-note IDs in `<catalog>.<schema>.databricks_release_notes`. It sends no email when there are no new feed items.

The feed client uses the fixed Microsoft Learn HTTPS endpoint, a bounded 30-second timeout, a descriptive user agent, and validates every returned link before including it in email. The Delta state table makes delivery idempotent across daily runs and feed refreshes.

## Local checks

Install the development dependencies with UV, then run:

```text
uv sync --dev
uv run pre-commit run --all-files
uv run pytest
```

### Local email smoke test

This fetches real release notes from Microsoft Learn and can send the matching window through Gmail without requiring PySpark. Use a Gmail app password, not your normal Gmail password. Copy `.env.example` to `.env`, fill in the values, then run:

```powershell
Copy-Item .env.example .env
# Edit .env and replace the placeholder values.
uv sync --dev
uv run python scripts/smoke_test.py --pub-date 2026-01-01 --dry-run
uv run python scripts/smoke_test.py --pub-date 2026-01-01
```

`--pub-date` selects one local publication date in `YYYY-MM-DD` format. The first command prints matching notes without sending mail. The second sends the matching notes and does not update the Delta state table. The email uses inline styling and text-based Microsoft Learn/Databricks brand lockups so it remains presentable when mail clients block external images. Unit tests cover feed parsing, date filtering, URL validation, state deduplication, and email composition:

```text
make test
```

### Full Databricks test

To test the actual Spark/Delta watermark and secret-scope path, deploy the development target and start the job manually:

```powershell
databricks bundle deploy -t dev -p mixklim
databricks bundle run -t dev -p mixklim databricks_digest
```

Check the run output and the recipient inbox. A second run with no new release notes should report `No new Azure Databricks release notes found; no email sent.`

## GitHub Actions

Configure the `production` environment with:

- Repository/environment variable `DATABRICKS_HOST`.
- Repository/environment secret `DATABRICKS_TOKEN` for the Databricks service principal.

The workflow validates on pull requests and deploys the `prod` bundle after a push to `main`.

The Gmail address and SMTP credentials are never GitHub Actions variables; they remain in the Databricks secret scope and are read only at job runtime.
