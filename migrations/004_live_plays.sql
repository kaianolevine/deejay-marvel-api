-- Make set_id nullable to support orphaned live history plays
ALTER TABLE tracks ALTER COLUMN set_id DROP NOT NULL;

-- Add live history columns to tracks
ALTER TABLE tracks
  ADD COLUMN IF NOT EXISTS played_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'csv',
  ADD COLUMN IF NOT EXISTS needs_review BOOLEAN NOT NULL DEFAULT false;

-- Backfill existing rows
UPDATE tracks SET source = 'csv' WHERE source IS NULL;

-- Indexes
CREATE INDEX IF NOT EXISTS ix_tracks_played_at ON tracks(played_at);
CREATE INDEX IF NOT EXISTS ix_tracks_needs_review ON tracks(needs_review) WHERE needs_review = true;

-- Feature flag for live plays
INSERT INTO feature_flags (owner_id, name, enabled, description)
VALUES
  ('dev-owner', 'flags.deejay_api.live_plays_enabled', true,
   'Enable live play history ingest and recent plays endpoint')
ON CONFLICT (name) DO NOTHING;
