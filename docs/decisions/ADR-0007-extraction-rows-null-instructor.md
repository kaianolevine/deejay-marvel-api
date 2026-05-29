# ADR-0007 — Extraction rows write NULL instructor_id; co-instructor derived at read time

**Date:** 2026-05-28
**Status:** Accepted
**Related:** ADR-0002 (entity substrate), ADR-0003 (versioned extractions and corrections)

## Context

The composer originally wrote one attribution / definition row per `(entity, instructor)` pair, iterating over each name in `source.instructors_raw` and writing a row per instructor. The intent was to allow per-instructor pages to filter content by `instructor_id == X`.

The reality is that LLM extraction does not disambiguate which co-instructor in a co-taught class originated each teaching point. In any class taught by Kaiano + Amy, the extracted content is jointly owned by both; there is no per-row signal of attribution. The composer's per-instructor loop therefore produced byte-identical duplicate rows differing only in `instructor_id`.

Symptoms surfaced on the source-detail page: a Kaiano + Amy lesson rendered every teaching point twice, every definition twice. Frontend dedup was added as a temporary fix, then made unnecessary by this composer change.

The conceptual question the duplication forced was: what does `instructor_id` on an extraction row actually mean? Three honest answers:

1. For a single-instructor source, `instructor_id` is redundant with `source.instructors_raw[0]`.
2. For a co-taught source, no per-row attribution exists in the source data; the column has no truth to carry.
3. For an operator-added row (e.g., "Kate specifically said X"), the column genuinely carries information.

Only case 3 justifies the column. Cases 1 and 2 should derive attribution from `source.instructors_raw` at read time.

## Decision

**Extraction-origin rows write `instructor_id = NULL` unconditionally.** The composer's four extraction loops (entities-taught, definitions, mistakes, competition-notes in `services/wcs_composition.py`) no longer iterate over `default_instructor_ids`; each writes exactly one row per extracted item.

**The `instructor_id` column remains on `wcs_source_attributions` and `wcs_entity_definitions`.** It is nullable, documented as deprecated for extraction-origin rows. Operator-added rows (origin != 'extraction') may still set it deliberately — for example, attributing a quote to a specific instructor in an otherwise co-taught class. This use case has no current write surface, but the column is preserved to support a future quotes row type or operator-curation feature.

**Co-instructor identity is derived at read time** from `source.instructors_raw`. The instructor wiki-view query (`services/wcs_wiki.get_instructor_view`) and the renderer's `_collect_entity_teachers` / `export_attributions_for_instructor` helpers all match on either `instructor_id == X` (for operator-set rows) OR `instructor_id IS NULL AND source.instructors_raw contains X` (for co-taught extraction rows).

**Migration 022 collapsed existing duplicates.** For multi-instructor sources only, content-identical extraction-origin rows were merged to a single row with `instructor_id` set to NULL. Single-instructor extraction rows were left untouched; their per-row `instructor_id` remains populated. This produces a benign cross-release inconsistency (older single-instructor rows have `instructor_id` set; newer ones write NULL) that no current consumer is sensitive to.

## Consequences

**Easier:**

- Co-taught lessons render correctly without dedup logic at every consumer.
- The semantic of `instructor_id` becomes coherent: it means "this specific instructor said this specific thing," set only when that's genuinely known.
- The Q&A agent's pgvector retrieval no longer sees double-weighted text for co-taught content.
- The wiki renderer's deterministic property is preserved — same substrate state still yields byte-identical output.

**Harder:**

- Consumers must implement the "either column OR source.instructors_raw" matching pattern for instructor-keyed queries. There are three: the API instructor view, the curator renderer, and a future operator UI for instructor pages. The pattern is small but it's three places.
- The pre-022 / post-022 inconsistency on single-instructor extraction rows is irreversible without re-running extraction. Future audits that compare "is `instructor_id` set?" across the corpus will see two regimes.

**Trade-offs accepted:**

- We do not record per-row attribution in co-taught classes even when the original instruction was clear in the recording. The extraction LLM doesn't surface this distinction reliably; recording it on a per-row basis would be a fiction. The right way to preserve "Kate said X verbatim" is a future quotes row type with a dedicated speaker field, not a column repurposed from a different intent.
- We accept that the column lives on as a deprecated-but-retained field. This is preferable to dropping it (which would lock out the future quotes use case) or repurposing it (which would invite ambiguity about what it means).

## Implementation

- `services/wcs_composition.py`: four extraction-write loops collapse to single-row writes with `instructor_id=None`.
- `services/wcs_wiki.py:get_instructor_view`: query broadened to OR on (column match, NULL + source.instructors_raw overlap). Source-side filter happens in Python to keep the query dialect-agnostic (tests run against SQLite where `instructors_raw` is a JSON column without array operators).
- `models.py`: `comment=` added to both `instructor_id` columns documenting the NULL-on-extraction semantic.
- `migrations/022_collapse_coinstructor_duplicates.sql`: idempotent SQL that finds content-identical duplicate extraction rows on multi-instructor sources, deletes all but one per group, and NULLs the survivor's `instructor_id`.
- Frontend dedup helpers in `website-astro-wcs/src/components/SourceDetail.tsx` remain as defensive code; after this change they are no-ops but cost nothing and protect against regression.
