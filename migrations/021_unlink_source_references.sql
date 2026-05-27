-- Migration 021: unlink wcs_source_references from wcs_instructors.
--
-- References to people mentioned in lesson content are too sparse for
-- reliable identity resolution (first-name-only is the common case;
-- "Robert" cannot be safely matched to "RobertRoyston" without
-- additional context). The references layer now stores raw mention
-- strings without trying to link them to canonical instructor
-- records. Definitions and attributions remain linked — those carry
-- higher-signal "attributed teaching" semantics where the LLM
-- explicitly named a teaching authority.
--
-- This migration:
-- 1. Adds wcs_source_references.referenced_name (NOT NULL after backfill).
-- 2. Backfills referenced_name from the existing instructor's canonical_name.
-- 3. Drops the wcs_source_references.instructor_id FK column.
-- 4. Cleans up wcs_instructors rows that only existed because of references:
--    rows with zero definitions, zero attributions, and that aren't
--    in any source's instructors_raw.

-- Step 1: Add the new column.
ALTER TABLE wcs_source_references
    ADD COLUMN IF NOT EXISTS referenced_name TEXT;

-- Step 2: Backfill referenced_name from the linked instructor's canonical_name.
UPDATE wcs_source_references r
SET referenced_name = i.canonical_name
FROM wcs_instructors i
WHERE r.instructor_id = i.id
  AND r.referenced_name IS NULL;

-- Step 3: Enforce NOT NULL on the new column now that it's backfilled.
ALTER TABLE wcs_source_references
    ALTER COLUMN referenced_name SET NOT NULL;

-- Step 4: Drop the FK column.
ALTER TABLE wcs_source_references
    DROP COLUMN instructor_id;

-- Step 5: Clean up orphaned reference-only instructors. An instructor is
-- "reference-only" if it has no definitions, no attributions, and is not
-- named in any source's instructors_raw array. These rows existed only
-- because the previous composition logic created them from references.
DELETE FROM wcs_instructors i
WHERE NOT EXISTS (
    SELECT 1 FROM wcs_entity_definitions d WHERE d.instructor_id = i.id
)
  AND NOT EXISTS (
    SELECT 1 FROM wcs_source_attributions a WHERE a.instructor_id = i.id
  )
  AND NOT EXISTS (
    SELECT 1 FROM wcs_sources s WHERE i.canonical_name = ANY(s.instructors_raw)
  );
