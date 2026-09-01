-- Migration 023: identity principal store
--
-- Installs the principal store defined by the `identity` repo
-- (sql/principal-store.sql) into this ecosystem's database, and makes
-- api-kaianolevine-com the first conformant enforcement point.
--
-- One store per ecosystem, not shared at runtime: deejaytools-com gets its
-- own instance of this same shape. They share the schema, never the rows.
--
-- Tables are prefixed `identity_` in the default schema rather than living in
-- a Postgres schema of their own, because this service's test suite runs
-- against SQLite in-memory and SQLite has no CREATE SCHEMA.
--
-- This migration is additive. It does not touch wcs_user_profiles,
-- wcs_note_grants, or any owner_id column: existing authorization keeps
-- working unchanged while the identity path is wired alongside it.

-- ---------------------------------------------------------------------------
-- Core tables
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS identity_issuers (
  issuer        TEXT PRIMARY KEY,
  display_name  TEXT NOT NULL DEFAULT '',
  jwks_url      TEXT NOT NULL,
  enabled       BOOLEAN NOT NULL DEFAULT TRUE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_roles (
  name          TEXT PRIMARY KEY
                  CHECK (name ~ '^[a-z][a-z0-9-]*$'),
  description   TEXT NOT NULL DEFAULT '',
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS identity_role_scopes (
  role_name     TEXT NOT NULL REFERENCES identity_roles(name) ON DELETE CASCADE,
  scope         TEXT NOT NULL
                  CHECK (scope ~ '^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){2}$'),
  PRIMARY KEY (role_name, scope)
);

CREATE TABLE IF NOT EXISTS identity_principals (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  kind          TEXT NOT NULL CHECK (kind IN ('human', 'machine')),
  issuer        TEXT NOT NULL REFERENCES identity_issuers(issuer),
  subject       TEXT NOT NULL,
  display_name  TEXT NOT NULL DEFAULT '',
  email         TEXT,
  status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'suspended')),
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at  TIMESTAMPTZ,
  UNIQUE (issuer, subject)
);

CREATE INDEX IF NOT EXISTS idx_identity_principals_issuer_subject
  ON identity_principals(issuer, subject);
CREATE INDEX IF NOT EXISTS idx_identity_principals_kind
  ON identity_principals(kind);

CREATE TABLE IF NOT EXISTS identity_principal_roles (
  principal_id  UUID NOT NULL REFERENCES identity_principals(id) ON DELETE CASCADE,
  role_name     TEXT NOT NULL REFERENCES identity_roles(name) ON DELETE RESTRICT,
  granted_by    TEXT NOT NULL DEFAULT '',
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, role_name)
);

CREATE TABLE IF NOT EXISTS identity_explicit_grants (
  principal_id  UUID NOT NULL REFERENCES identity_principals(id) ON DELETE CASCADE,
  scope         TEXT NOT NULL
                  CHECK (scope ~ '^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*){2}$'),
  resource      TEXT NOT NULL,
  granted_by    TEXT NOT NULL DEFAULT '',
  granted_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (principal_id, scope, resource)
);

CREATE TABLE IF NOT EXISTS identity_audit_events (
  event_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  occurred_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  enforcement_point  TEXT NOT NULL,
  -- No foreign key: an audit event must survive deletion of the principal it
  -- describes, or the trail erases itself exactly when it matters most.
  principal_id       UUID,
  principal_kind     TEXT CHECK (principal_kind IN ('human', 'machine')),
  issuer             TEXT,
  subject            TEXT,
  scope              TEXT NOT NULL,
  resource           TEXT,
  allowed            BOOLEAN NOT NULL,
  reason             TEXT NOT NULL,
  request_id         TEXT
);

CREATE INDEX IF NOT EXISTS idx_identity_audit_occurred_at
  ON identity_audit_events(occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_principal
  ON identity_audit_events(principal_id, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_identity_audit_enforcement_point
  ON identity_audit_events(enforcement_point, occurred_at DESC);

-- ---------------------------------------------------------------------------
-- Seed: this ecosystem's issuer and role vocabulary
-- ---------------------------------------------------------------------------

INSERT INTO identity_issuers (issuer, display_name, jwks_url)
VALUES (
  'https://clerk.kaianolevine.com',
  'MiniAppPolis (cogs + api)',
  'https://clerk.kaianolevine.com/.well-known/jwks.json'
)
ON CONFLICT (issuer) DO NOTHING;

INSERT INTO identity_roles (name, description) VALUES
  ('wcs-admin',       'Human administrator of the WCS knowledge base.'),
  ('wcs-reader',      'Human with baseline WCS read access.'),
  ('pipeline-writer', 'Machine that reports pipeline evaluations and findings.'),
  ('catalog-ingest',  'Machine that ingests DJ set, track and play data.')
ON CONFLICT (name) DO NOTHING;

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('wcs-admin',       'wcs.notes.read'),
  ('wcs-admin',       'wcs.notes.write'),
  ('wcs-admin',       'wcs.grants.write'),
  ('wcs-admin',       'wcs.sources.write'),
  ('wcs-reader',      'wcs.notes.read'),
  ('pipeline-writer', 'pipeline.evaluations.write'),
  ('pipeline-writer', 'pipeline.findings.write'),
  ('catalog-ingest',  'catalog.sets.write'),
  ('catalog-ingest',  'catalog.tracks.write'),
  ('catalog-ingest',  'catalog.plays.write')
ON CONFLICT (role_name, scope) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Backfill: existing WCS humans become principals
-- ---------------------------------------------------------------------------
-- wcs_user_profiles is already a principal table in all but name — Clerk sub
-- as primary key, plus one boolean role. This lifts those rows into the
-- store without deleting them: the profile table stays the system of record
-- for WCS-specific display data, identity_principals becomes the system of
-- record for who may do what.

INSERT INTO identity_principals (kind, issuer, subject, display_name, email, created_at)
SELECT
  'human',
  'https://clerk.kaianolevine.com',
  p.user_id,
  p.display_name,
  NULLIF(p.email, ''),
  p.created_at
FROM wcs_user_profiles p
ON CONFLICT (issuer, subject) DO NOTHING;

-- is_admin was the entire role model. It becomes exactly one role grant.
INSERT INTO identity_principal_roles (principal_id, role_name, granted_by)
SELECT ip.id, 'wcs-admin', 'migration_023'
FROM wcs_user_profiles p
JOIN identity_principals ip
  ON ip.issuer = 'https://clerk.kaianolevine.com' AND ip.subject = p.user_id
WHERE p.is_admin IS TRUE
ON CONFLICT (principal_id, role_name) DO NOTHING;

INSERT INTO identity_principal_roles (principal_id, role_name, granted_by)
SELECT ip.id, 'wcs-reader', 'migration_023'
FROM wcs_user_profiles p
JOIN identity_principals ip
  ON ip.issuer = 'https://clerk.kaianolevine.com' AND ip.subject = p.user_id
WHERE p.is_admin IS NOT TRUE
ON CONFLICT (principal_id, role_name) DO NOTHING;

-- Existing per-note grants carry over as instance-level grants.
INSERT INTO identity_explicit_grants (principal_id, scope, resource, granted_by, granted_at)
SELECT ip.id, 'wcs.notes.read', g.note_id::text, g.granted_by, g.granted_at
FROM wcs_note_grants g
JOIN identity_principals ip
  ON ip.issuer = 'https://clerk.kaianolevine.com' AND ip.subject = g.user_id
ON CONFLICT (principal_id, scope, resource) DO NOTHING;
