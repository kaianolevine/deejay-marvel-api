-- Migration 025: the wcs-writer role
--
-- Machines that write WCS sources and transcripts need a role of their own.
-- `wcs-admin` is a human role and carries `wcs.grants.write`, which decides
-- who may see what; a transcript ingester has no business holding it.
--
-- Least privilege here is not ceremony: the cogs that write WCS content are
-- the ones most likely to be given a broad role by default, and that is
-- exactly how a pipeline account ends up able to change access control.

INSERT INTO identity_roles (name, description) VALUES
  ('wcs-writer', 'Machine that writes WCS sources, transcripts and notes.')
ON CONFLICT (name) DO NOTHING;

INSERT INTO identity_role_scopes (role_name, scope) VALUES
  ('wcs-writer', 'wcs.notes.read'),
  ('wcs-writer', 'wcs.notes.write'),
  ('wcs-writer', 'wcs.sources.write'),
  ('wcs-writer', 'wcs.transcripts.write')
ON CONFLICT (role_name, scope) DO NOTHING;
