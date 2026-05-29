-- Migration 022: collapse co-instructor duplicate attribution/definition rows.
--
-- Background: prior to the composer change in this same release, sources
-- with multiple instructors in instructors_raw had ONE row per (entity,
-- instructor) pair written by the extraction composer. This produced
-- byte-identical duplicate rows differing only in instructor_id.
--
-- Starting with this release, the composer writes ONE row per extracted
-- item with instructor_id = NULL; co-taught attribution is derived at
-- read time from wcs_sources.instructors_raw.
--
-- This migration collapses existing extraction-origin duplicates to one
-- row per content tuple, setting instructor_id = NULL on the survivor.
-- Operator-added rows (origin != 'extraction') and single-instructor
-- sources are left untouched.

-- Step 1: Collapse duplicate attribution rows on multi-instructor sources.
-- Use a CTE to identify the survivor in each (source_id, entity_id,
-- attribution_kind, raw_term, position, ... content tuple) group, then
-- delete the non-survivors. Finally, NULL the surviving rows' instructor_id.

WITH multi_instructor_sources AS (
    SELECT id FROM wcs_sources
    WHERE array_length(instructors_raw, 1) > 1
),
candidate_rows AS (
    SELECT
        id,
        source_id,
        entity_id,
        attribution_kind,
        raw_term,
        position,
        prose,
        COALESCE(mistake_text, '') AS mistake_text_norm,
        COALESCE(correction_text, '') AS correction_text_norm,
        COALESCE(drill_goal, '') AS drill_goal_norm,
        COALESCE(drill_steps, ARRAY[]::text[]) AS drill_steps_norm,
        ROW_NUMBER() OVER (
            PARTITION BY
                source_id, entity_id, attribution_kind, raw_term, position,
                prose,
                COALESCE(mistake_text, ''),
                COALESCE(correction_text, ''),
                COALESCE(drill_goal, ''),
                COALESCE(drill_steps, ARRAY[]::text[])
            ORDER BY created_at, id
        ) AS rn
    FROM wcs_source_attributions
    WHERE origin = 'extraction'
      AND source_id IN (SELECT id FROM multi_instructor_sources)
)
DELETE FROM wcs_source_attributions
WHERE id IN (SELECT id FROM candidate_rows WHERE rn > 1);

UPDATE wcs_source_attributions
SET instructor_id = NULL
WHERE origin = 'extraction'
  AND source_id IN (SELECT id FROM wcs_sources WHERE array_length(instructors_raw, 1) > 1)
  AND instructor_id IS NOT NULL;

-- Step 2: Same for wcs_entity_definitions.

WITH multi_instructor_sources AS (
    SELECT id FROM wcs_sources
    WHERE array_length(instructors_raw, 1) > 1
),
candidate_rows AS (
    SELECT
        id,
        ROW_NUMBER() OVER (
            PARTITION BY source_id, entity_id, term, definition, position
            ORDER BY created_at, id
        ) AS rn
    FROM wcs_entity_definitions
    WHERE origin = 'extraction'
      AND source_id IN (SELECT id FROM multi_instructor_sources)
)
DELETE FROM wcs_entity_definitions
WHERE id IN (SELECT id FROM candidate_rows WHERE rn > 1);

UPDATE wcs_entity_definitions
SET instructor_id = NULL
WHERE origin = 'extraction'
  AND source_id IN (SELECT id FROM wcs_sources WHERE array_length(instructors_raw, 1) > 1)
  AND instructor_id IS NOT NULL;

-- Step 3: Add a comment to document the column semantic. Postgres column
-- comments are visible in psql via \d+ and in introspection tools.

COMMENT ON COLUMN wcs_source_attributions.instructor_id IS
    'Optional per-row instructor attribution. Extraction-origin rows always '
    'set NULL; instructor identity is derived from the parent source.instructors_raw '
    'at read time. Operator-added rows MAY set this to attribute a row to a '
    'specific instructor (e.g., a direct quote).';

COMMENT ON COLUMN wcs_entity_definitions.instructor_id IS
    'Optional per-row instructor attribution. Extraction-origin rows always '
    'set NULL; instructor identity is derived from the parent source.instructors_raw '
    'at read time. Operator-added rows MAY set this to attribute a row to a '
    'specific instructor.';
