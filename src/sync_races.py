"""Nightly sync: push any iRacing league races missing from the site database.

Flow:
  1. Fetch the full league subsession-ID history from iRacing.
  2. GET the subsession IDs already stored in Supabase.
  3. Diff -> the missing subsessions.
  4. For each missing subsession, fetch its full results from iRacing.
  5. Upsert the new race rows into Supabase (keyed on subsession_id).

This sync is strictly ADD-ONLY: it inserts/updates rows keyed on subsession_id
and NEVER deletes. If Supabase holds a subsession the iRacing history no longer
reports (an "extra"), it is left completely untouched -- but the discrepancy is
recorded in the run log so it stays visible.

Every run appends a block to logs/sync.log recording what it uploaded
(subsession_id, start_time, track_name) and any extras it detected.

Idempotent: a night with no new races is a no-op, and re-runs never duplicate
(the upsert merges on subsession_id).
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from iracing_client import IRacingError, load_config
from fetch_race_ids import build_client, fetch_league_subsession_ids
from supabase_client import SupabaseClient, SupabaseError

_SB_PLACEHOLDERS = {"", "service_role_key", "your-service-role-key"}
_BATCH_SIZE = 25  # races per upsert request (each row carries a full results doc)

# Persistent, append-only run log. Lives at the repo root (sibling of src/) and
# is git-ignored -- it's local operational history, not part of the codebase.
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "sync.log"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _append_log(text: str) -> None:
    """Append a block of text to the persistent run log (creating logs/ if needed)."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _build_race_row(league_id: int, doc: dict) -> dict:
    """Map one iRacing results/get document to a `races` row."""
    track = doc.get("track") or {}
    return {
        "subsession_id": doc.get("subsession_id"),
        "league_id": league_id,
        "start_time": doc.get("start_time"),
        "track_name": track.get("track_name"),
        "results": doc,
    }


def _format_run_log(
    league_id: int,
    history_count: int,
    existing_count: int,
    uploaded: list[dict],
    extras: list[int],
    timestamp: str | None = None,
) -> str:
    """Render one run's summary block for the persistent log (and console)."""
    bar = "=" * 64
    lines = [
        bar,
        f"Sync run: {timestamp or _now_utc()}  (league {league_id})",
        f"  iRacing history:       {history_count}",
        f"  Already in Supabase:   {existing_count}",
        f"  Uploaded this run:     {len(uploaded)}",
        f"  Extras (in DB, not on iRacing): {len(extras)}",
    ]

    if uploaded:
        lines.append("-" * 64)
        lines.append("New races uploaded  (subsession_id | start_time | track_name):")
        for row in uploaded:
            lines.append(
                f"  {row.get('subsession_id')} | "
                f"{row.get('start_time')} | {row.get('track_name')}"
            )

    lines.append("-" * 64)
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


def _build_supabase(config: dict) -> SupabaseClient:
    sb = config.get("supabase", {})
    url = str(sb.get("url", "")).strip()
    key = str(sb.get("service_role_key", "")).strip()
    if not url or key in _SB_PLACEHOLDERS:
        raise SupabaseError(
            "Missing supabase.url / supabase.service_role_key in config.json. "
            "Get the service_role key from Supabase -> Project Settings -> API."
        )
    return SupabaseClient(url, key)


def main() -> int:
    config = load_config()
    league_id = config.get("leagueId")
    if not league_id:
        print("No leagueId set in config.json.")
        return 1

    iracing = build_client(config)
    supabase = _build_supabase(config)

    print("Authenticating with iRacing (OAuth)…")
    iracing.authenticate()

    print(f"Fetching league {league_id} subsession history from iRacing…")
    all_ids = fetch_league_subsession_ids(iracing, league_id)
    print(f"  iRacing history: {len(all_ids)} subsession(s).")

    print("Fetching existing subsession IDs from Supabase…")
    existing = supabase.get_existing_subsession_ids()
    print(f"  Already in DB: {len(existing)}.")

    missing = [sid for sid in all_ids if sid not in existing]
    # Rows in Supabase the iRacing history no longer reports. We NEVER delete
    # these -- just record them so the discrepancy is visible in the log.
    extras = sorted(existing - set(all_ids))
    print(f"  Missing (to push): {len(missing)}.")
    if extras:
        print(
            f"  NOTE: {len(extras)} row(s) in Supabase are absent from the iRacing "
            "history — left untouched (add-only; nothing is ever deleted)."
        )

    # Fetch + upsert the missing races, collecting what actually landed so the
    # log reflects only rows a successful upsert accepted.
    uploaded: list[dict] = []
    batch: list[dict] = []
    for subsession_id in missing:
        batch.append(_build_race_row(league_id, iracing.get_subsession_result(subsession_id)))
        if len(batch) >= _BATCH_SIZE:
            supabase.upsert_races(batch)
            uploaded.extend(batch)
            print(f"  pushed {len(uploaded)}/{len(missing)}…")
            batch = []
    if batch:
        supabase.upsert_races(batch)
        uploaded.extend(batch)

    report = _format_run_log(league_id, len(all_ids), len(existing), uploaded, extras)
    _append_log(report)
    print(report)
    print(f"Run logged to {LOG_FILE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (IRacingError, SupabaseError) as exc:
        # Record the failure in the same persistent log. Exception messages carry
        # only HTTP status / response text -- never the service-role key.
        _append_log(f"{'=' * 64}\nSync run FAILED: {_now_utc()} — {exc}\n{'=' * 64}")
        print(f"Error: {exc}")
        raise SystemExit(1)
