"""Collect every race (subsession) ID across the full history of the league.

An iRacing "race ID" is a subsession_id — the key you pass to results/get to
pull a race's finishing order. This walks every league season (retired ones
included) and returns the subsession_id of every session that has results.

Nothing is persisted to disk: the IDs are returned in memory as a list[int] for
the caller (the nightly sync) to diff against the remote database.
"""
from __future__ import annotations

from iracing_client import IRacingClient, IRacingError, load_config

_PLACEHOLDERS = {"", "client_id", "client_secret", "your-iracing-password"}


def _as_sessions(payload) -> list[dict]:
    """season_sessions normally returns {'sessions': [...]}; be tolerant."""
    if isinstance(payload, dict):
        return payload.get("sessions", [])
    if isinstance(payload, list):
        return payload
    return []


def fetch_league_subsession_ids(client: IRacingClient, league_id: int) -> list[int]:
    """Return the sorted, de-duplicated subsession IDs for the whole league history."""
    seasons_resp = client.get("league/seasons", league_id=league_id, retired=True)
    seasons = seasons_resp.get("seasons", []) if isinstance(seasons_resp, dict) else seasons_resp

    subsession_ids: set[int] = set()
    for season in seasons:
        resp = client.get(
            "league/season_sessions",
            league_id=league_id,
            season_id=season["season_id"],
            results_only=True,
        )
        for session in _as_sessions(resp):
            subsession_id = session.get("subsession_id")
            if isinstance(subsession_id, int) and subsession_id > 0:
                subsession_ids.add(subsession_id)
    return sorted(subsession_ids)


def build_client(config: dict) -> IRacingClient:
    """Construct an authenticated-capable client from config, or raise if creds are missing."""
    creds = config.get("iracing", {})
    required = ("email", "password", "client_id", "client_secret")
    missing = [k for k in required if str(creds.get(k, "")).strip() in _PLACEHOLDERS]
    if missing:
        raise IRacingError(
            f"Missing iRacing credential(s) in config.json: {', '.join(missing)}. "
            "iRacing OAuth needs email, password, client_id and client_secret."
        )
    return IRacingClient(
        creds["email"],
        creds["password"],
        creds["client_id"],
        creds["client_secret"],
        scope=creds.get("scope", "iracing.auth"),
    )


def main() -> int:
    config = load_config()
    league_id = config.get("leagueId")
    if not league_id:
        print("No leagueId set in config.json — nothing to fetch.")
        return 1

    client = build_client(config)
    print("Authenticating with iRacing (OAuth)…")
    client.authenticate()

    print(f"Fetching subsession IDs for league {league_id}…")
    ids = fetch_league_subsession_ids(client, league_id)
    print(f"{len(ids)} subsession ID(s):")
    print(ids)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except IRacingError as exc:
        print(f"iRacing API error: {exc}")
        raise SystemExit(1)
