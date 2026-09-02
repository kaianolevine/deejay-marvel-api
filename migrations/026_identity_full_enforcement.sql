-- Migration 026: scopes for every guarded endpoint, and humans backfilled again
--
-- Prepares for enforcing a scope on all 45 authenticated endpoints. Two things
-- have to be true before that deploy, and both are done here.
--
-- 1. EVERY human has a principal. Migration 023 backfilled the profiles that
--    existed then. Anyone who signed up since has a wcs_user_profiles row and
--    no principal, and would lose access to all 43 human-facing endpoints on
--    the same deploy. This re-runs the backfill for whatever is missing.
--
-- 2. The scope vocabulary covers the endpoints. Two scopes are new:
--    config.flags.write (PATCH /v1/flags/{name}) and wcs.embeddings.write
--    (POST /v1/wcs/embeddings/refresh).
--
-- Additive and idempotent. Safe to run before the code that uses it.

-- ---------------------------------------------------------------------------
-- New scopes
-- ---------------------------------------------------------------------------

-- The bulk export returns every source regardless of per-source visibility,
-- so it gets a scope of its own. Mapping it to wcs.notes.read would hand the
-- full corpus, private sources included, to every wcs-reader — which is every
-- signed-in user. The old gate (machine callers only) was cruder but tighter,
-- and a migration must not quietly widen what it replaces.
INSERT INTO identity_roles (name, description) VALUES
  ('corpus-reader', 'May read the full WCS corpus unfiltered by visibility.')
ON CONFLICT (name) DO NOTHING;

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('corpus-reader', 'wcs.notes.read'),
  ('corpus-reader', 'wcs.corpus.read'),
  ('wcs-admin',     'wcs.corpus.read')
ON CONFLICT (role_name, scope) DO NOTHING;

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('wcs-admin',  'wcs.sources.write'),
  ('wcs-admin',  'wcs.transcripts.write'),
  ('wcs-admin',  'wcs.embeddings.write'),
  ('wcs-admin',  'config.flags.write'),
  ('wcs-writer', 'wcs.embeddings.write')
ON CONFLICT (role_name, scope) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Backfill: every WCS human becomes a principal
-- ---------------------------------------------------------------------------
-- Same shape as migration 023's block, re-run for rows added since. Written as
-- a LEFT JOIN rather than relying on ON CONFLICT so it is obvious what it
-- targets: profiles with no matching principal.

INSERT INTO identity_principals (kind, issuer, subject, display_name, email, created_at)
SELECT
  'human',
  'https://clerk.kaianolevine.com',
  p.user_id,
  p.display_name,
  NULLIF(p.email, ''),
  p.created_at
FROM wcs_user_profiles p
LEFT JOIN identity_principals ip
  ON ip.issuer = 'https://clerk.kaianolevine.com' AND ip.subject = p.user_id
WHERE ip.id IS NULL
ON CONFLICT (issuer, subject) DO NOTHING;

-- is_admin remains the source for the initial grant. It stops being the
-- authority once the admin endpoints move to scope checks; this is the last
-- migration that reads it.
INSERT INTO identity_principal_roles (principal_id, role_name, granted_by)
SELECT ip.id, 'wcs-admin', 'migration_026'
FROM wcs_user_profiles p
JOIN identity_principals ip
  ON ip.issuer = 'https://clerk.kaianolevine.com' AND ip.subject = p.user_id
WHERE p.is_admin IS TRUE
ON CONFLICT (principal_id, role_name) DO NOTHING;

INSERT INTO identity_principal_roles (principal_id, role_name, granted_by)
SELECT ip.id, 'wcs-reader', 'migration_026'
FROM wcs_user_profiles p
JOIN identity_principals ip
  ON ip.issuer = 'https://clerk.kaianolevine.com' AND ip.subject = p.user_id
ON CONFLICT (principal_id, role_name) DO NOTHING;
