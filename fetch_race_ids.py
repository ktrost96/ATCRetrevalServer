"""Collect every race (subsession) ID across the full history of the league.

An iRacing "race ID" is a subsession_id — the key you pass to results/get to
pull a race's finishing order. This walks every league season (retired ones
included) and gathers the subsession_id of every session that has results.

Output: data/race_ids.json
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from iracing_client import IRacingClient, IRacingError, load_config

OUTPUT_PATH = Path(__file__).resolve().parent / "data" / "race_ids.json"


def _as_sessions(payload) -> list[dict]:
    """season_sessions normally returns {'sessions': [...]}; be tolerant."""
    if isinstance(payload, dict):
        return payload.get("sessions", [])
    if isinstance(payload, list):
        return payload
    return []


_PLACEHOLDERS = {"", "client_id", "client_secret", "your-iracing-password"}


def main() -> int:
    config = load_config()
    league_id = config.get("leagueId")
    if not league_id:
        print("No leagueId set in config.json — nothing to fetch.")
        return 1

    creds = config.get("iracing", {})
    required = ("email", "password", "client_id", "client_secret")
    missing = [k for k in required if str(creds.get(k, "")).strip() in _PLACEHOLDERS]
    if missing:
        print(f"Missing iRacing credential(s) in config.json: {', '.join(missing)}")
        print("  iRacing now uses OAuth — you need a client_id/client_secret issued by")
        print("  iRacing (Password Limited Grant) in addition to your email/password.")
        return 1

    client = IRacingClient(
        creds["email"],
        creds["password"],
        creds["client_id"],
        creds["client_secret"],
        scope=creds.get("scope", "iracing.auth"),
    )

    print("Authenticating with iRacing (OAuth)…")
    client.authenticate()

    print(f"Fetching seasons for league {league_id}…")
    seasons_resp = client.get("league/seasons", league_id=league_id, retired=True)
    seasons = seasons_resp.get("seasons", []) if isinstance(seasons_resp, dict) else seasons_resp
    print(f"  {len(seasons)} season(s) found.")

    races: list[dict] = []
    seen: set[int] = set()
    sample_session: dict | None = None
    total_sessions = 0

    for season in seasons:
        season_id = season["season_id"]
        season_name = season.get("season_name", "")
        resp = client.get(
            "league/season_sessions",
            league_id=league_id,
            season_id=season_id,
            results_only=True,
        )
        sessions = _as_sessions(resp)
        count = 0
        for session in sessions:
            total_sessions += 1
            if sample_session is None:
                sample_session = session
            subsession_id = session.get("subsession_id")
            if not subsession_id or subsession_id in seen:
                continue
            seen.add(subsession_id)
            races.append(
                {
                    "subsession_id": subsession_id,
                    "season_id": season_id,
                    "season_name": season_name,
                    "launch_at": session.get("launch_at"),
                    "session_name": session.get("league_session_name")
                    or session.get("session_name"),
                }
            )
            count += 1
        print(f"  season {season_id} '{season_name}': {count} race(s)")
        time.sleep(0.5)  # be gentle with the API

    # Safety net: if we saw sessions but extracted no IDs, the field name may
    # differ from what we expect — surface the shape so we can adapt quickly.
    if not races and total_sessions and sample_session is not None:
        print("\nNo subsession_id found; sample session keys were:")
        print(f"  {sorted(sample_session.keys())}")

    races.sort(key=lambda r: (r.get("launch_at") or ""))

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "league_id": league_id,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(races),
        "subsession_ids": [r["subsession_id"] for r in races],
        "races": races,
    }
    with OUTPUT_PATH.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)

    print(f"\nDone. {len(races)} race ID(s) written to {OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IRacingError as exc:
        print(f"iRacing API error: {exc}")
        raise SystemExit(1)
