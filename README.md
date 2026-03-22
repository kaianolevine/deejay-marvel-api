# deejay-sets-api

FastAPI service providing:
* CRUD-style read endpoints for `sets` and `tracks`
* A `track_catalog` for normalized track matching + reconciliation
* Pipeline evaluation endpoints (list, summary, and write)
* Basic usage statistics endpoints
* An ingest endpoint that runs reconciliation and catalog upsert logic

## Local Development

### Prerequisites

* Python 3.11+
* `uv` installed

### Environment

Copy `.env.example` to `.env` and adjust values as needed.
In production, set `CORS_ORIGINS` to the specific Cloudflare Pages domain(s) rather than using the wildcard (`*`).

## Run the Server

API docs are available at `http://localhost:8000/docs`.

```bash
uv run uvicorn src.deejay_sets_api.main:app --reload
```

## Run Tests

```bash
uv run pytest --cov=src --cov-report=term-missing
```

## CI/CD

Every push to `main` runs CI (lint + tests).
Railway auto-deploys on push to `main`.
Feature flags control activation without deployment.
Flags are managed via `PATCH /v1/flags/{name}`.

## Versioning

This repo uses semantic-release for automated versioning.
Versions are determined automatically from commit messages
on merge to main:

- feat: → minor version bump (0.3.1 → 0.4.0)
- fix: → patch version bump (0.3.1 → 0.3.2)
- feat!: or BREAKING CHANGE → major bump (0.3.1 → 1.0.0)
- chore/docs/refactor/test/ci → no version bump

Never manually edit the version in pyproject.toml.
Never manually edit CHANGELOG.md.
Both are managed automatically on merge to main.

### Production Flag Rollback

Use flags for safe rollout and fast rollback without redeploying:

- Enable one flag change at a time via `PATCH /v1/flags/{name}`.
- Verify health immediately after change (API status, error logs, and endpoint behavior).
- If regressions appear, rollback by patching the same flag back to `enabled: false` (or `true` for previously disabled flags).
- Prefer changing ingest-related flags during low-traffic windows and monitor pipeline runs for 5-10 minutes after each change.
- Record each production flag flip in deployment notes (flag name, old/new value, timestamp, operator).

## Deployment Target

Designed for Railway.

## Authentication

Owner-based auth is implemented for now via a placeholder `get_current_owner` dependency.
Clerk JWT verification is planned for production.
