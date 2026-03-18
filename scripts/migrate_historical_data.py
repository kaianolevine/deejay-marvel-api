from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from pathlib import Path
import sys
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deejay_sets_api.config import get_settings
from deejay_sets_api.database import get_db_session, get_engine
from deejay_sets_api.models import Set as DbSet
from deejay_sets_api.models import Track as DbTrack
from deejay_sets_api.models import Base
from deejay_sets_api.models import TrackCatalog as DbTrackCatalog
from deejay_sets_api.schemas import IngestTrack
from deejay_sets_api.services.reconciliation import reconcile_set_tracks


EXPECTED_SETS_BY_YEAR: dict[int, int] = {
    2016: 64,
    2017: 40,
    2018: 37,
    2019: 50,
    2020: 8,
    2021: 28,
    2022: 77,
    2023: 49,
    2024: 42,
    2025: 35,
    2026: 11,
}


class MigrationError(Exception):
    pass


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _parse_mmss_to_seconds(value: Any) -> int | None:
    s = _clean_str(value)
    if not s:
        return None

    parts = s.split(":")
    if len(parts) != 2:
        return None

    minutes_s, seconds_s = parts
    try:
        minutes = int(minutes_s)
        seconds = int(seconds_s)
    except ValueError:
        return None

    if seconds < 0 or seconds >= 60:
        # Be conservative; unexpected formats become null.
        return None
    return minutes * 60 + seconds


def _parse_time(value: Any) -> dt.time | None:
    s = _clean_str(value)
    if not s:
        return None
    parts = s.split(":")
    try:
        if len(parts) == 2:
            hour, minute = (int(p) for p in parts)
            second = 0
        elif len(parts) == 3:
            hour, minute, second = (int(p) for p in parts)
        else:
            return None
        return dt.time(hour=hour, minute=minute, second=second)
    except ValueError:
        return None


def _parse_float(value: Any) -> float | None:
    s = _clean_str(value)
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    s = _clean_str(value)
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _build_header_map(header_row: list[Any]) -> dict[str, int]:
    header_map: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        key = _clean_str(raw)
        if not key:
            continue
        header_map[key.lower()] = idx
    return header_map


def _get_cell(row: list[Any], idx: int | None) -> Any:
    if idx is None:
        return None
    if idx < 0 or idx >= len(row):
        return None
    return row[idx]


def _parse_track_rows(
    values: list[list[Any]],
) -> tuple[list[IngestTrack], int]:
    """
    Returns: (valid_track_objects, tracks_skipped_no_title_artist_count)
    """

    if not values:
        return [], 0
    header_row = values[0]
    header_map = _build_header_map(header_row)

    idx_label = header_map.get("label")
    idx_title = header_map.get("title")
    idx_remix = header_map.get("remix")
    idx_artist = header_map.get("artist")
    idx_comment = header_map.get("comment")
    idx_genre = header_map.get("genre")
    idx_length = header_map.get("length")
    idx_bpm = header_map.get("bpm")
    idx_year = header_map.get("year")
    idx_play_time = header_map.get("play_time")

    ingest_tracks: list[IngestTrack] = []
    skipped_no_title_artist = 0

    for row_position, row in enumerate(values[1:], start=1):
        title = _clean_str(_get_cell(row, idx_title))
        artist = _clean_str(_get_cell(row, idx_artist))
        if not title or not artist:
            skipped_no_title_artist += 1
            continue

        label = _clean_str(_get_cell(row, idx_label))
        remix = _clean_str(_get_cell(row, idx_remix))
        comment = _clean_str(_get_cell(row, idx_comment))
        genre = _clean_str(_get_cell(row, idx_genre))

        length_secs = _parse_mmss_to_seconds(_get_cell(row, idx_length))
        bpm = _parse_float(_get_cell(row, idx_bpm))
        release_year = _parse_int(_get_cell(row, idx_year))
        play_time = _parse_time(_get_cell(row, idx_play_time))

        ingest_tracks.append(
            IngestTrack(
                play_order=row_position,
                play_time=play_time,
                label=label,
                title=title,
                remix=remix,
                artist=artist,
                comment=comment,
                genre=genre,
                bpm=bpm,
                release_year=release_year,
                length_secs=length_secs,
            )
        )

    return ingest_tracks, skipped_no_title_artist


def _skip_folder(name: str) -> bool:
    return name.strip().lower() == "summary"


def _load_collection(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _get_google_api():
    # Defer the import so `uv run python -c "import scripts.migrate_historical_data"`
    # works even if kaiano-common-utils isn't installed in your local environment.
    try:
        from kaiano.google import GoogleAPI  # type: ignore
    except ModuleNotFoundError as e:  # pragma: no cover
        print(
            "❌ Missing dependency: `kaiano.google.GoogleAPI`.\n"
            "Install `kaiano-common-utils` and try again.\n"
            "If you use uv, run:\n"
            "  uv sync\n",
            file=sys.stderr,
        )
        raise MigrationError(
            "kaiano.google.GoogleAPI not found. Ensure kaiano-common-utils is installed "
            "and GOOGLE_CREDENTIALS_JSON is set."
        ) from e

    return GoogleAPI.from_env()


@dataclass
class YearStats:
    expected: int
    imported: int = 0
    skipped_already_imported: int = 0
    failed: int = 0
    tracks: int = 0


async def _find_set_by_source_file(session: AsyncSession, source_file: str) -> DbSet | None:
    stmt = select(DbSet).where(DbSet.source_file == source_file)
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _run() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", required=True, help="Path to deejay_set_collection.json")
    parser.add_argument("--year", type=int, default=None, help="Only process sets from this year")
    args = parser.parse_args()

    collection = _load_collection(args.collection)
    try:
        settings = get_settings()
    except Exception as e:
        # Provide a clearer message than the raw pydantic validation error.
        if "DATABASE_URL" in str(e):
            print(
                "❌ DATABASE_URL is required but not set.\n"
                "Create a .env file in the repo root (copy from .env.example) "
                "or export DATABASE_URL before running this script.",
                file=sys.stderr,
            )
            raise
        raise

    repo_root = Path(__file__).resolve().parents[1]
    default_credentials_path = repo_root / "credentials.json"

    creds_raw = os.getenv("GOOGLE_CREDENTIALS_JSON")
    creds_path = os.getenv("GOOGLE_CREDENTIALS_JSON_PATH")
    if not creds_raw and not creds_path and default_credentials_path.exists():
        creds_path = str(default_credentials_path)

    # Support multiline creds by reading JSON from a file path instead of embedding it in .env.
    if creds_path and not creds_raw:
        try:
            creds_raw = open(creds_path, "r", encoding="utf-8").read()
        except OSError as e:
            raise MigrationError(
                f"GOOGLE_CREDENTIALS_JSON_PATH is set but the file can't be read: {creds_path}"
            ) from e

    if not creds_raw:
        raise MigrationError(
            "Google service-account credentials are required.\n"
            "Set GOOGLE_CREDENTIALS_JSON (single-line JSON) or GOOGLE_CREDENTIALS_JSON_PATH,\n"
            "or place the service-account JSON at the repo root as `credentials.json`."
        )

    try:
        json.loads(creds_raw)
    except json.JSONDecodeError as e:
        if creds_path:
            # If the .env loader partially broke GOOGLE_CREDENTIALS_JSON, re-read from file.
            try:
                creds_raw = open(creds_path, "r", encoding="utf-8").read()
                json.loads(creds_raw)
                # Continue; creds_raw has been replaced.
            except Exception as e2:  # pragma: no cover
                raise MigrationError(
                    "GOOGLE_CREDENTIALS_JSON (or GOOGLE_CREDENTIALS_JSON_PATH) is not valid JSON."
                ) from e2
        if default_credentials_path.exists():
            try:
                creds_raw = default_credentials_path.read_text(encoding="utf-8")
                json.loads(creds_raw)
            except Exception as e2:  # pragma: no cover
                raise MigrationError("Credentials file is present but is not valid JSON.") from e2
        else:
            raise MigrationError(
                "GOOGLE_CREDENTIALS_JSON is set but is not valid JSON.\n"
                "Important: python-dotenv cannot parse multiline JSON values inside .env.\n"
                "Place the service-account JSON at the repo root as `credentials.json`."
            ) from e

    # kaiano.google.GoogleAPI.from_env() reads credentials from the
    # GOOGLE_CREDENTIALS_JSON environment variable, so we populate it
    # from the file/env we validated above.
    os.environ["GOOGLE_CREDENTIALS_JSON"] = creds_raw

    # Ensure DB schema exists (safe to call on already-migrated DB).
    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    year_stats: dict[int, YearStats] = {
        year: YearStats(expected=expected)
        for year, expected in EXPECTED_SETS_BY_YEAR.items()
    }

    total_tracks_skipped_no_title_artist = 0

    sets_failed: list[tuple[str, str]] = []

    sets_skipped_already_imported = 0
    sets_imported_this_run = 0
    sets_failed_count = 0
    total_tracks_imported_this_run = 0

    google_api = _get_google_api()

    # Run all imports in a single async session, committing per-set.
    session_gen = get_db_session()
    session = await anext(session_gen)
    try:
        # Fail fast if the DB hostname can't be resolved or reached.
        try:
            await session.execute(select(1))
        except socket.gaierror as e:
            db_url = os.getenv("DATABASE_URL", "")
            print(
                "❌ Cannot resolve DATABASE_URL hostname from this machine.\n"
                "Your DATABASE_URL host likely points to an internal/private network (example: "
                "`*.railway.internal`).\n\n"
                "Fix: update DATABASE_URL to a publicly reachable Postgres endpoint, "
                "or run this script in the same environment/VPC as the DB.",
                file=sys.stderr,
            )
            if db_url:
                host = urlparse(db_url).hostname
                print(f"DATABASE_URL host: {host}", file=sys.stderr)
            raise MigrationError("Database hostname cannot be resolved") from e

        for folder in collection.get("folders", []):
            folder_name = str(folder.get("name", "")).strip()
            if _skip_folder(folder_name):
                continue

            try:
                year = int(folder_name)
            except ValueError:
                # Unknown folder; ignore.
                continue

            if args.year is not None and year != args.year:
                continue

            if year not in year_stats:
                continue

            for item in folder.get("items", []):
                label = str(item.get("label", "")).strip()
                if not label:
                    continue

                try:
                    if await _find_set_by_source_file(session, label) is not None:
                        print(f"⏭️ Skipping (already imported): {label}")
                        year_stats[year].skipped_already_imported += 1
                        sets_skipped_already_imported += 1
                        continue

                    set_date = dt.date.fromisoformat(str(item.get("date")))
                    venue = str(item.get("title"))

                    spreadsheet_id = str(item.get("spreadsheet_id"))
                    if not spreadsheet_id:
                        raise MigrationError("Missing spreadsheet_id in collection JSON")

                    print(f"▶ Processing: {label}")
                    print(f"  Reading spreadsheet_id: {spreadsheet_id}")

                    # Pull A:Z and parse track data after the header.
                    values = google_api.sheets.read_values(spreadsheet_id, "A:Z")
                    if not values or len(values) <= 1:
                        print(f"⚠️ Empty sheet: {label}")
                        # Spec: log and skip; do not treat as a failed set.
                        continue

                    print(f"  Read {len(values) - 1} track rows (incl. empty rows before parsing).")

                    ingest_tracks, skipped_no_title_artist = _parse_track_rows(values)
                    total_tracks_skipped_no_title_artist += skipped_no_title_artist

                    db_set = DbSet(
                        owner_id=settings.OWNER_ID,
                        set_date=set_date,
                        venue=venue,
                        source_file=label,
                    )
                    session.add(db_set)
                    await session.flush()

                    await reconcile_set_tracks(
                        session=session,
                        owner_id=settings.OWNER_ID,
                        set_id=db_set.id,
                        set_date=set_date,
                        tracks=ingest_tracks,
                    )

                    await session.commit()

                    year_stats[year].imported += 1
                    sets_imported_this_run += 1
                    year_stats[year].tracks += len(ingest_tracks)
                    total_tracks_imported_this_run += len(ingest_tracks)

                    print(f"✅ Imported: {label} — {len(ingest_tracks)} tracks")
                except Exception as e:
                    await session.rollback()
                    sets_failed_count += 1
                    year_stats[year].failed += 1
                    sets_failed.append((label, str(e)))
                    print(f"❌ Failed: {label}: {e}")

        # Verification queries after import.
        # Catalog stats
        total_catalog_entries = (
            await session.execute(
                select(func.count(DbTrackCatalog.id)).where(
                    DbTrackCatalog.owner_id == settings.OWNER_ID
                )
            )
        ).scalar_one()

        confidence_rows = await session.execute(
            select(DbTrackCatalog.confidence, func.count(DbTrackCatalog.id)).where(
                DbTrackCatalog.owner_id == settings.OWNER_ID
            ).group_by(DbTrackCatalog.confidence)
        )
        confidence_counts = {row[0]: row[1] for row in confidence_rows.all()}

        total_tracks = (
            await session.execute(
                select(func.count(DbTrack.id)).where(DbTrack.owner_id == settings.OWNER_ID)
            )
        ).scalar_one()

        dq_rows = await session.execute(
            select(DbTrack.data_quality, func.count(DbTrack.id)).where(
                DbTrack.owner_id == settings.OWNER_ID
            ).group_by(DbTrack.data_quality)
        )
        dq_counts = {row[0]: row[1] for row in dq_rows.all()}

        def pct(n: int) -> int:
            if total_tracks == 0:
                return 0
            return int(round((n / total_tracks) * 100))

        def conf_pct(n: int) -> int:
            if total_catalog_entries == 0:
                return 0
            return int(round((n / total_catalog_entries) * 100))

        # Build report
        total_sets_in_source = sum(EXPECTED_SETS_BY_YEAR.values())

        lines: list[str] = []
        lines.append("================================================")
        lines.append("HISTORICAL MIGRATION REPORT")
        lines.append("================================================")
        lines.append("")
        lines.append("SUMMARY")
        lines.append(f"  Total sets in source:             {total_sets_in_source:3d}")
        lines.append(f"  Sets imported this run:           {sets_imported_this_run:3d}")
        lines.append(f"  Sets skipped (already imported):  {sets_skipped_already_imported:3d}")
        lines.append(f"  Sets failed:                      {sets_failed_count:3d}")
        lines.append(
            f"  Total tracks imported this run:   {total_tracks_imported_this_run:3d}"
        )
        lines.append(
            f"  Tracks skipped (no title/artist): {total_tracks_skipped_no_title_artist:3d}"
        )
        lines.append("")
        lines.append("BY YEAR")
        lines.append("  Year | Expected | Imported | Skipped | Failed | Tracks")
        lines.append("  -----+----------+----------+---------+--------+-------")

        years_to_report = sorted(EXPECTED_SETS_BY_YEAR.keys())
        for year in years_to_report:
            st = year_stats[year]
            lines.append(
                f"  {year} |"
                f" {st.expected:9d} |"
                f" {st.imported:9d} |"
                f" {st.skipped_already_imported:8d} |"
                f" {st.failed:6d} |"
                f" {st.tracks:5d}"
            )

        lines.append("")
        lines.append("SPOT CHECK")
        lines.append('  Flag any year where imported count differs from expected by more')
        lines.append('  than 2 with: "⚠️ MISMATCH: {year} expected {n} got {m}"')
        lines.append("")

        if args.year is None:
            processed_years = years_to_report
        else:
            processed_years = [args.year]

        mismatches: list[str] = []
        for year in processed_years:
            st = year_stats[year]
            if abs(st.imported - st.expected) > 2:
                mismatches.append(f"⚠️ MISMATCH: {year} expected {st.expected} got {st.imported}")

        if mismatches:
            lines.extend(mismatches)
        else:
            lines.append("  None")
        lines.append("CATALOG STATS (query from database after import)")
        lines.append(f"  Total catalog entries:   {total_catalog_entries:3d}")
        lines.append(
            f"  Confidence low:          {confidence_counts.get('low', 0):3d}  ({conf_pct(confidence_counts.get('low', 0)):2d}%)"
        )
        lines.append(
            f"  Confidence medium:       {confidence_counts.get('medium', 0):3d}  ({conf_pct(confidence_counts.get('medium', 0)):2d}%)"
        )
        lines.append(
            f"  Confidence high:         {confidence_counts.get('high', 0):3d}  ({conf_pct(confidence_counts.get('high', 0)):2d}%)"
        )
        lines.append("")
        lines.append("DATA QUALITY (query from database after import)")
        lines.append(
            f"  Tracks minimal  (title+artist only): {dq_counts.get('minimal', 0):3d}  ({pct(dq_counts.get('minimal', 0)):2d}%)"
        )
        lines.append(
            f"  Tracks partial:                      {dq_counts.get('partial', 0):3d}  ({pct(dq_counts.get('partial', 0)):2d}%)"
        )
        lines.append(
            f"  Tracks complete:                     {dq_counts.get('complete', 0):3d}  ({pct(dq_counts.get('complete', 0)):2d}%)"
        )
        lines.append("")
        lines.append("FAILED SETS")
        if not sets_failed:
            lines.append("  None")
        else:
            for label, err in sets_failed:
                lines.append(f"  - {label}: {err}")
        lines.append("")

        report = "\n".join(lines)
        print(report)

        report_path = "scripts/migration_report.txt"
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)
    finally:
        await session_gen.aclose()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    try:
        main()
    except MigrationError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)

