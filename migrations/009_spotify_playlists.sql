CREATE TABLE spotify_playlists (
    id          TEXT PRIMARY KEY,          -- Spotify playlist ID
    name        TEXT NOT NULL,
    url         TEXT NOT NULL,             -- external Spotify URL
    uri         TEXT NOT NULL,             -- spotify:playlist:... URI
    type        TEXT NOT NULL DEFAULT 'playlist',
    public      BOOLEAN NOT NULL DEFAULT TRUE,
    collaborative BOOLEAN NOT NULL DEFAULT FALSE,
    snapshot_id TEXT,                      -- Spotify snapshot ID for change detection
    tracks_total INTEGER NOT NULL DEFAULT 0,
    owner_id    TEXT NOT NULL,             -- Spotify owner user ID
    owner_name  TEXT,                      -- Spotify owner display name
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
