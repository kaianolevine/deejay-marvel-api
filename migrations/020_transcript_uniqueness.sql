-- Migration 020: enforce one-transcript-per-(owner_id, drive_file_id)
--
-- Cleans up orphan transcripts left from the rebuild's debugging phase
-- (failed extractions that wrote transcript rows before failing at
-- downstream validation), then adds the uniqueness constraint.
--
-- Per ADR-0004, the rebuild assumes one canonical source per Drive file
-- per owner. The cog's existing workflow moves processed files out of
-- the input folder; if a file reappears in input, it's an explicit
-- re-ingestion signal. The API treats duplicate POSTs to
-- /v1/wcs/transcripts as re-ingestions, not errors — implemented in
-- routers/wcs_notes.py::create_transcript.

-- Step 1: Delete orphan transcripts (no associated source).
-- These are debris from the rebuild's development phase; they have
-- no entities, attributions, or other downstream rows attached.
DELETE FROM wcs_transcripts t
WHERE NOT EXISTS (
    SELECT 1 FROM wcs_sources s WHERE s.transcript_id = t.id
);

-- Step 2: Add the uniqueness constraint. Will fail loudly if any
-- duplicates remain (which would indicate the cleanup above missed
-- something).
ALTER TABLE wcs_transcripts
    ADD CONSTRAINT uq_wcs_transcripts_owner_drive_file
    UNIQUE (owner_id, drive_file_id);
