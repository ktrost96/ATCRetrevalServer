"""Nightly sync: curate iRacing league races into the site database.

Flow:
  1. Fetch the full league subsession-ID history from iRacing.
  2. Ask Supabase which subsessions curated_races already holds.
  3. Pick this run's targets:
       * missing      — in the iRacing history, absent from curated_races, and
                        not already known to be a non-race (see state/)
       * provisional  — already stored, still `status = 'provisional'`, and
                        started within the refresh window (default 30 days)
     A race a steward has marked `final` is never re-fetched, and neither is an
     old provisional one — iRacing doesn't revise year-old results.
  4. For each target, fetch its results document and curate it (see curate.py).
  5. Upsert: the curated_races row first, then its curated_race_results rows.

This sync writes the **baseline only**. `status`, `penalty_points` and
`adjusted_position` are the web app's, and are deliberately absent from every
payload so a re-run refreshes names/positions/incidents while leaving applied
penalties and finalized statuses alone.

Strictly ADD-ONLY: rows are inserted or updated, never deleted. If Supabase
holds a subsession the iRacing history no longer reports (an "extra"), it is
left completely untouched — but the discrepancy is recorded in the run log so it
stays visible. Same for a driver who disappears from a re-fetched race: the row
stays, because `penalties` cascades off it.

Every run appends a block to logs/sync.log recording what it wrote and what it
skipped. Idempotent: a night with no new races re-pulls only recent provisional
races, and re-runs never duplicate (upserts merge on the natural keys).

    python src/sync_races.py                # the real thing
    python src/sync_races.py --dry-run      # curate 3 races, print the exact
    python src/sync_races.py --dry-run=10   # payloads, write NOTHING

A dry run is the safe way to check the pipeline against live iRacing data: it
authenticates, fetches, and curates for real, but makes no POST and touches no
log file. It also tolerates the curated tables not existing yet, so it works
before `0004_curated_races.sql` has been applied.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from curate import SkipRace, curate
from iracing_client import IRacingError, load_config
from fetch_race_ids import build_client, fetch_league_subsession_ids
from supabase_client import SupabaseClient, SupabaseError

_SB_PLACEHOLDERS = {"", "service_role_key", "your-service-role-key"}

# How far back to re-pull races still marked provisional. Old provisional races
# are settled in practice, so this keeps a quiet night to a couple of requests
# instead of the whole league history. Override with `refreshWindowDays`.
_DEFAULT_REFRESH_WINDOW_DAYS = 30

# How many races a bare `--dry-run` curates before stopping. Enough to eyeball
# the payloads without pulling the whole history from iRacing.
_DEFAULT_DRY_RUN_RACES = 3

# Curated rows are small (no payload blob), so batches can be generous. Races
# flush first, then the driver rows that reference them.
_RACE_BATCH = 50
_RESULT_BATCH = 500

# Persistent, append-only run log. Lives at the repo root (sibling of src/) and
# is git-ignored -- it's local operational history, not part of the codebase.
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "sync.log"

# Subsessions proven to have no Race simsession. Roughly a third of this
# league's history is practice-only, and none of them ever land in
# curated_races -- so without this note-to-self every run would re-download all
# of them forever. Safe to cache because a completed subsession's structure is
# immutable; safe to delete, it just costs one slow run to rebuild.
STATE_DIR = Path(__file__).resolve().parent.parent / "state"
NON_RACE_FILE = STATE_DIR / "non_races.json"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def append_log(text: str) -> None:
    """Append a block of text to the persistent run log (creating logs/ if needed)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def load_non_races() -> set[int]:
    """Subsession IDs already proven to have no race. Missing/corrupt file = empty."""
    try:
        data = json.loads(NON_RACE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    ids = data.get("subsession_ids", []) if isinstance(data, dict) else data
    return {value for value in ids if isinstance(value, int)}


def save_non_races(subsession_ids: set[int]) -> None:
    """Persist the non-race cache. Purely an optimisation — never fatal."""
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        NON_RACE_FILE.write_text(
            json.dumps(
                {
                    "_comment": (
                        "Subsessions with no 'Race' simsession, so the sync stops "
                        "re-fetching them. Delete this file to re-check them all."
                    ),
                    "updated": now_utc(),
                    "subsession_ids": sorted(subsession_ids),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"  WARNING: could not write {NON_RACE_FILE.name} ({exc}); continuing.")


def _cutoff_iso(days: int) -> str:
    """The start of the refresh window, as PostgREST-safe ISO-8601 UTC."""
    moment = datetime.now(timezone.utc) - timedelta(days=days)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _chunks(rows: list, size: int):
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def write_curated(
    supabase: SupabaseClient, pending: list[tuple[dict, list[dict]]]
) -> int:
    """Upsert a batch of curated races, then their driver rows. Returns row count.

    Order matters: curated_race_results has an FK to curated_races, so every
    parent in this batch must land before any child referencing it.
    """
    if not pending:
        return 0

    normalized_pending: list[tuple[dict, list[dict]]] = []
    for race, rows in pending:
        season_id = supabase.upsert_season_if_needed(
            race.pop("_iracing_season_id", None),
            race.get("season_label"),
        )
        if season_id is not None:
            race["season_id"] = season_id
        normalized_pending.append((race, rows))

    supabase.upsert_curated_races([race for race, _ in normalized_pending])
    results = [row for _, rows in normalized_pending for row in rows]
    for chunk in _chunks(results, _RESULT_BATCH):
        supabase.upsert_curated_race_results(chunk)
    return len(results)


def _print_dry_run(pending: list[tuple[dict, list[dict]]]) -> None:
    """Show the exact JSON a real run would POST, so it can be eyeballed.

    Worth checking here: finish_position is 1-based, starting_position is null
    where iRacing had -1, and none of status / penalty_points /
    adjusted_position appear anywhere.
    """
    bar = "=" * 64
    print(f"\n{bar}\nDRY RUN — nothing was written to Supabase.\n{bar}")
    for race, rows in pending:
        print("-" * 64)
        print("POST /rest/v1/curated_races?on_conflict=subsession_id")
        print(json.dumps(race, indent=2))
        shown = min(3, len(rows))
        print(
            "POST /rest/v1/curated_race_results?on_conflict=subsession_id,cust_id"
            f"  ({len(rows)} row(s), first {shown} shown)"
        )
        print(json.dumps(rows[:shown], indent=2))
    print(f"{bar}\nDRY RUN — nothing was written to Supabase.\n{bar}")


def _format_run_log(
    league_id: int,
    history_count: int,
    stored_count: int,
    new_races: list[dict],
    refreshed_races: list[dict],
    driver_rows: int,
    skipped: list[tuple[int, str]],
    extras: list[int],
    window_days: int,
    cached_non_races: int = 0,
    dry_run: bool = False,
    timestamp: str | None = None,
) -> str:
    """Render one run's summary block for the persistent log (and console)."""
    bar = "=" * 64
    rule = "-" * 64

    def stat(label: str, value: object) -> str:
        return f"  {label.ljust(32)}{value}"

    if dry_run:
        new_label = "New races (would upload):"
        refresh_label = "Provisional (would refresh):"
        rows_label = "Driver rows (would write):"
        new_heading = "New races that WOULD be uploaded"
        refresh_heading = "Provisional races that WOULD be refreshed"
    else:
        new_label = "New races uploaded:"
        refresh_label = "Provisional refreshed:"
        rows_label = "Driver rows written:"
        new_heading = "New races uploaded"
        refresh_heading = "Provisional races refreshed"

    lines = [
        bar,
        f"Sync {'DRY RUN' if dry_run else 'run'}: {timestamp or now_utc()}"
        f"  (league {league_id})",
        stat("iRacing history:", history_count),
        stat("Already in curated_races:", stored_count),
        stat(new_label, len(new_races)),
        stat(
            refresh_label,
            f"{len(refreshed_races)} (started within {window_days} days)",
        ),
        stat(rows_label, driver_rows),
        stat("Skipped (not curatable):", len(skipped)),
        stat("Known non-races (not fetched):", cached_non_races),
        stat("Extras (in DB, not on iRacing):", len(extras)),
    ]

    if new_races:
        lines.append(rule)
        lines.append(f"{new_heading}  (subsession_id | start_time | track_name):")
        for row in new_races:
            lines.append(
                f"  {row.get('subsession_id')} | "
                f"{row.get('start_time')} | {row.get('track_name')}"
            )

    if refreshed_races:
        lines.append(rule)
        lines.append(
            f"{refresh_heading} (baseline re-pulled; penalties and adjusted "
            "positions untouched):"
        )
        for row in refreshed_races:
            lines.append(
                f"  {row.get('subsession_id')} | "
                f"{row.get('start_time')} | {row.get('track_name')}"
            )

    if skipped:
        lines.append(rule)
        lines.append("Skipped subsessions (nothing written for these):")
        for subsession_id, reason in skipped:
            lines.append(f"  ! {subsession_id} - {reason}")

    lines.append(rule)
    if dry_run:
        lines.append("DRY RUN: nothing was written to Supabase.")
    if extras:
        lines.append(
            f"ADD-ONLY: no rows were deleted. {len(extras)} subsession(s) exist in "
            "Supabase but not in the iRacing history and were LEFT UNTOUCHED:"
        )
        for sid in extras:
            lines.append(f"  ! {sid}  (present in Supabase, absent from iRacing) - kept")
    else:
        lines.append("ADD-ONLY: no rows were deleted; no extra rows detected in Supabase.")
    lines.append(bar)
    return "\n".join(lines)


def build_supabase(config: dict) -> SupabaseClient:
    sb = config.get("supabase", {})
    url = str(sb.get("url", "")).strip()
    key = str(sb.get("service_role_key", "")).strip()
    if not url or key in _SB_PLACEHOLDERS:
        raise SupabaseError(
            "Missing supabase.url / supabase.service_role_key in config.json. "
            "Get the service_role key from Supabase -> Project Settings -> API."
        )
    return SupabaseClient(url, key)


def _parse_args(argv: list[str] | None) -> tuple[int, bool]:
    """Return the dry-run race limit and whether a full backfill was requested."""
    parser = argparse.ArgumentParser(description="Sync iRacing league races to Supabase.")
    parser.add_argument(
        "--dry-run",
        type=int,
        nargs="?",
        const=_DEFAULT_DRY_RUN_RACES,
        default=0,
        metavar="N",
        help=(
            f"Curate at most N races (default {_DEFAULT_DRY_RUN_RACES}) and print "
            "the payloads a real run would send. Writes nothing."
        ),
    )
    parser.add_argument(
        "--backfill",
        action="store_true",
        help="Treat every historical race as a target so the full league history is imported once.",
    )
    args = parser.parse_args(argv)
    return args.dry_run, args.backfill


def main(argv: list[str] | None = None) -> int:
    dry_run_limit, backfill = _parse_args(argv)
    dry_run = dry_run_limit > 0

    config = load_config()
    league_id = config.get("leagueId")
    if not league_id:
        print("No leagueId set in config.json.")
        return 1
    window_days = int(config.get("refreshWindowDays", _DEFAULT_REFRESH_WINDOW_DAYS))

    if dry_run:
        print(f"DRY RUN: curating at most {dry_run_limit} race(s); nothing will be written.")
    if backfill:
        print("BACKFILL: targeting the full league history for this run.")

    iracing = build_client(config)
    supabase = build_supabase(config)

    print("Authenticating with iRacing (OAuth)…")
    iracing.authenticate()

    print(f"Fetching league {league_id} subsession history from iRacing…")
    all_ids = fetch_league_subsession_ids(iracing, league_id)
    print(f"  iRacing history: {len(all_ids)} subsession(s).")

    print("Fetching stored subsession IDs from Supabase…")
    try:
        stored = supabase.get_curated_subsession_ids()
        # Recent provisional races get their baseline re-pulled; anything a
        # steward finalized (or older than the window) is left alone.
        provisional = supabase.get_provisional_subsession_ids(_cutoff_iso(window_days))
    except SupabaseError as exc:
        # A dry run is useful before 0004_curated_races.sql has been applied, so
        # treat unreadable tables as "empty" rather than failing. A real run must
        # still fail here — writing depends on those tables existing.
        if not dry_run:
            raise
        print(f"  WARNING: could not read the curated tables — {exc}")
        print("  Continuing the dry run as if the database were empty.")
        stored, provisional = set(), set()
    print(f"  Already in curated_races: {len(stored)}.")

    # Practice-only subsessions never land in curated_races, so without this
    # they would look "missing" and be re-downloaded on every single run.
    non_races = load_non_races()
    missing = [sid for sid in all_ids if sid not in stored and sid not in non_races]
    refresh = [sid for sid in all_ids if sid in stored and sid in provisional]
    if backfill:
        refresh = [sid for sid in all_ids if sid in stored and sid not in non_races]
    # Rows in Supabase the iRacing history no longer reports. We NEVER delete
    # these -- just record them so the discrepancy is visible in the log.
    extras = sorted(stored - set(all_ids))

    print(f"  Missing (to upload): {len(missing)}.")
    if backfill:
        print(f"  Backfill targets (all stored races): {len(refresh)}.")
    else:
        print(f"  Provisional within {window_days} days (to refresh): {len(refresh)}.")
    if non_races:
        print(f"  Known non-races skipped without fetching: {len(non_races)}.")
    if extras:
        print(
            f"  NOTE: {len(extras)} row(s) in Supabase are absent from the iRacing "
            "history — left untouched (add-only; nothing is ever deleted)."
        )

    new_races: list[dict] = []
    refreshed_races: list[dict] = []
    skipped: list[tuple[int, str]] = []
    newly_cached: set[int] = set()
    driver_rows = 0
    pending: list[tuple[dict, list[dict]]] = []
    targets = missing + refresh
    if dry_run:
        targets = targets[:dry_run_limit]

    for subsession_id in targets:
        try:
            race_row, result_rows = curate(iracing.get_subsession_result(subsession_id))
        except SkipRace as exc:
            # Not a race, or missing a NOT NULL column. Skipping the whole
            # subsession keeps a partial race out of the DB.
            skipped.append((subsession_id, str(exc)))
            print(f"  skipped {subsession_id}: {exc}")
            # Only a settled verdict is remembered; a transient upstream problem
            # must stay retryable.
            if exc.permanent:
                newly_cached.add(subsession_id)
            continue

        pending.append((race_row, result_rows))
        (refreshed_races if subsession_id in stored else new_races).append(race_row)

        if not dry_run and len(pending) >= _RACE_BATCH:
            driver_rows += write_curated(supabase, pending)
            pending = []
            print(f"  wrote {len(new_races) + len(refreshed_races)}/{len(targets)}…")

    if dry_run:
        driver_rows = sum(len(rows) for _, rows in pending)
        _print_dry_run(pending)
    else:
        driver_rows += write_curated(supabase, pending)
        if newly_cached:
            save_non_races(non_races | newly_cached)
            print(f"  Cached {len(newly_cached)} new non-race subsession(s).")

    report = _format_run_log(
        league_id,
        len(all_ids),
        len(stored),
        new_races,
        refreshed_races,
        driver_rows,
        skipped,
        extras,
        window_days,
        # Only the pre-existing cache was skipped without a fetch; anything
        # newly discovered this run is already counted under "Skipped".
        cached_non_races=len(non_races),
        dry_run=dry_run,
    )
    if dry_run:
        # Don't pollute the operational history with runs that wrote nothing.
        print(report)
        print("DRY RUN: nothing written, nothing logged.")
        return 0
    append_log(report)
    print(report)
    print(f"Run logged to {LOG_FILE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IRacingError, SupabaseError) as exc:
        # Record the failure in the same persistent log. Exception messages carry
        # only HTTP status / response text -- never the service-role key.
        append_log(f"{'=' * 64}\nSync run FAILED: {now_utc()} — {exc}\n{'=' * 64}")
        print(f"Error: {exc}")
        raise SystemExit(1)
