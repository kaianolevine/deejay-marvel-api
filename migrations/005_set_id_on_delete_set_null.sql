ALTER TABLE tracks
  DROP CONSTRAINT IF EXISTS tracks_set_id_fkey;

ALTER TABLE tracks
  ADD CONSTRAINT tracks_set_id_fkey
  FOREIGN KEY (set_id)
  REFERENCES sets(id)
  ON DELETE SET NULL;
