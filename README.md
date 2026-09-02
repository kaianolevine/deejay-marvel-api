# kaianolevine-api

FastAPI service powering api.kaianolevine.com — sets, tracks, catalog
reconciliation, pipeline evaluations, feature flags, stats, live plays,
contact form handling (Brevo + Turnstile), and resume PDF proxy
(Google Drive).

## API Reference

All versioned routes are mounted under `/v1`. Interactive OpenAPI
documentation is the source of truth for request and response shapes:

- Live (production): https://api.kaianolevine.com/docs
- OpenAPI JSON:      https://api.kaianolevine.com/openapi.json
- Local (dev):       http://localhost:8000/docs

Route groups:

- `/v1/sets`, `/v1/sets/{id}`, `/v1/sets/{id}/tracks` — DJ sets and per-set tracks
- `/v1/tracks`, `/v1/tracks/{id}` — track catalog
- `/v1/catalog`, `/v1/catalog/{id}` — reconciled catalog entries
- `/v1/evaluations`, `/v1/evaluations/summary` — pipeline evaluation findings
- `/v1/flags`, `/v1/flags/{name}` — feature flags
- `/v1/stats/overview`, `/v1/stats/by-year`, `/v1/stats/top-artists`, `/v1/stats/top-tracks` — aggregate stats
- `/v1/spotify/playlists` — Spotify playlist catalog
- `/v1/live-plays`, `/v1/live-plays/recent` — VirtualDJ live play history
- `/v1/ingest` — set ingestion endpoint
- `/v1/prefect-webhook` — Prefect flow-run webhook
- `/v1/contact` — public contact form (CORS + Turnstile gated)
- `/v1/resume` — resume PDF proxy (Google Drive)
- `/v1/wcs/transcripts`, `/v1/wcs/notes`, `/v1/wcs/notes/all`, `/v1/wcs/notes/{id}` — WCS notes pipeline (legacy)
- `/v1/wcs/sources` — ingest WCS lesson extractions
- `/v1/wcs/wiki/concepts|techniques|patterns|drills/{slug}` — entity views
- `/v1/wcs/wiki/instructors/{slug}` — instructor views
- `/v1/wcs/wiki/sources/{id}` — source views
- `/v1/wcs/wiki/export` — bulk corpus export (for wiki-curator-cog)
- `/v1/wcs/me`, `/v1/wcs/admin/users`, `/v1/wcs/admin/grants`, `/v1/wcs/admin/notes/{id}/visibility` — WCS access control
- `/v1/wcs/admin/corrections/*`, `/v1/wcs/admin/additions/*` — input-layer overrides
- `/v1/wcs/admin/recompose/{source_id}` — manual composition trigger
- `/v1/wcs/admin/gaps/*` — curation gap-finding
- Unversioned meta routes: `/health` (liveness), `/version` (deployed version), `/` (redirects to `/docs`)

## Developer Setup

### Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```
- PostgreSQL (or use the `DATABASE_URL` from Railway for a shared dev DB)

### First-time setup
```bash
# 1. Install all dependencies including dev extras
uv sync --all-extras

# 2. Install pre-commit hooks
uv run pre-commit install

# 3. Copy env file and fill in values
cp .env.example .env
```

### Run the server
```bash
uv run uvicorn src.kaianolevine_api.main:app --reload
```

API docs available at http://localhost:8000/docs

### Run tests
```bash
# All tests (uses SQLite in-memory — no DATABASE_URL needed)
uv run pytest

# With coverage detail
uv run pytest --cov=src --cov-report=term-missing
```

### Lint, format, type check
```bash
uv run ruff check src/ tests/ --fix
uv run ruff format src/ tests/
uv run mypy src/
```

### Pre-commit (runs automatically on every commit)
```bash
# Run manually against all files
uv run pre-commit run --all-files
```

Hooks run on every `git commit`: ruff lint, ruff format, mock method checks, type annotation checks. If a hook fails the commit is blocked — ruff will auto-fix in place, then `git add .` and re-commit.

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

## Authentication and authorization

This service is a conformant enforcement point for the `identity`
contract. It binds four functions — verify, resolve, authorize,
emit_audit — once per request, in `src/kaianolevine_api/auth.py`. The
decision itself lives in `identity.policy`; what stays here is
configuration and the FastAPI adapters.

### Credentials

One `Authorization: Bearer <credential>` header carries either of two
credential types, routed structurally by dot count:

- **Clerk session JWTs** (people) — two dots. RS256, verified in process
  against the issuer's JWKS document. The `sub` claim is the principal
  subject.
- **Named machine keys** (cogs) — anything else. An opaque per-machine
  string, compared in constant time against the keys this service holds
  in its own environment. The matched key *names* the caller, so no
  machine name is ever asserted in a request. Verification is local:
  nothing leaves the process.

There is no fallback between the two paths. A credential that fails the
path it routed to is rejected, not retried against the other. Clerk M2M
opaque tokens and `CLERK_SECRET_KEY` were removed outright — see
ecosystem-standards ADR-008 and CD-019.

Required environment variables:

- `CLERK_ISSUER` and `CLERK_JWKS_URL` — or `CLERK_ISSUERS`, a JSON array
  of `{issuer, jwks_url}`, when more than one Clerk tenant is trusted.
- `<MACHINE_NAME>_API_KEY` — one per machine declared in
  `identity_registry.MACHINES` (`deejay-cog` → `DEEJAY_COG_API_KEY`).
  Doppler-managed. Key material is never stored in the database, hashed
  or otherwise; the principal store holds names and grants only.

Machine principals and their roles are declared in code and reconciled
into the store at boot. Adding a cog is a reviewed code change, not a
row inserted by hand.

### Route coverage

Every route is in exactly one of three states:

- **Scope-guarded** (51) — `Depends(require_scope("<domain>.<resource>.<action>"))`.
  Roles are named bundles of scopes; a suspended principal is denied
  before roles are consulted.
- **Authenticated-only** (3) — a verified credential, no scope. This is
  the whole list: `POST /v1/wcs/me` (where a person first becomes known,
  so it cannot require a principal in order to grant one), `GET /v1/wcs/me`
  (reads only the caller's own profile), and `GET /v1/identity/whoami`
  (reports what verify and resolve saw, and authorizes nothing).
- **Public** (22) — see below.

Every decision is audited, allow and deny alike, with the enforcement
point, principal, scope, outcome and reason.

### Public surface

Deliberately unauthenticated, per API-008 / DOC-011:

| Routes | Why |
|---|---|
| `GET /v1/sets`, `/v1/sets/{id}`, `/v1/sets/{id}/tracks`, `/v1/tracks`, `/v1/tracks/{id}`, `/v1/catalog`, `/v1/catalog/{id}` | Public catalog reads backing the website. |
| `GET /v1/stats/overview`, `/v1/stats/by-year`, `/v1/stats/top-artists`, `/v1/stats/top-tracks`, `/v1/spotify/playlists`, `/v1/live-plays/recent` | Public aggregate reads backing the website. |
| `GET /v1/evaluations`, `/v1/evaluations/summary`, `/v1/flags` | Pipeline Health and feature flags — read-only, no per-owner content. |
| `POST /v1/contact` | Contact form. CORS + Turnstile gated rather than credential gated. |
| `GET /v1/resume` | Public resume proxy. |
| `POST /v1/prefect-webhook` | Prefect flow state callbacks. Reviewed and accepted as unauthenticated. |
| `GET /health`, `GET /version`, `GET /` | Platform endpoints. `/` redirects to `/docs`. |

Client parity is maintained with `mini_app_polis.api.KaianoApiClient`,
which derives the caller's own key variable from its machine name and
sets the same header this module reads. The two must change in the same
release (AUTH-002) — a mismatch 401s every cog at once.

## Observability

Three-layer observability, aligned with the ecosystem standard:

- **Sentry** — unhandled exceptions and FastAPI integration, initialized
  in `main.py` `lifespan` when `SENTRY_DSN_API` is set.
- **Structured logs** — emitted via the shared logger from
  `mini_app_polis.logger` (install name `common-python-utils`); consistent
  JSON format and emoji-prefixed lifecycle lines across the ecosystem.
- **Healthchecks.io** — external uptime probes hit `/health` (public
  liveness endpoint, no auth, no DB access).
