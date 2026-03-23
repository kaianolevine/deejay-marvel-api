-- Remove columns added to tracks in migration 004
ALTER TABLE tracks DROP COLUMN IF EXISTS played_at;
ALTER TABLE tracks DROP COLUMN IF EXISTS source;
ALTER TABLE tracks DROP COLUMN IF EXISTS needs_review;

-- Revert set_id to NOT NULL
ALTER TABLE tracks ALTER COLUMN set_id SET NOT NULL;

-- Revert FK back to CASCADE
ALTER TABLE tracks DROP CONSTRAINT IF EXISTS tracks_set_id_fkey;
ALTER TABLE tracks ADD CONSTRAINT tracks_set_id_fkey
  FOREIGN KEY (set_id) REFERENCES sets(id) ON DELETE CASCADE;

-- Drop indexes added in 004
DROP INDEX IF EXISTS ix_tracks_played_at;
DROP INDEX IF EXISTS ix_tracks_needs_review;

-- Create standalone live_plays table
CREATE TABLE IF NOT EXISTS live_plays (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  owner_id TEXT NOT NULL,
  played_at TIMESTAMPTZ NOT NULL,
  title TEXT NOT NULL,
  artist TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT uq_live_plays_owner_title_artist_played_at UNIQUE (owner_id, title, artist, played_at)
);

CREATE INDEX IF NOT EXISTS ix_live_plays_played_at ON live_plays(played_at);
CREATE INDEX IF NOT EXISTS ix_live_plays_owner_id ON live_plays(owner_id);

