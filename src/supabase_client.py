"""Supabase (PostgREST) client for pushing curated iRacing data to the site DB.

The ATC website's database is a Supabase Postgres exposed via PostgREST at
`<url>/rest/v1/<table>`. This local server is a trusted backend: it authenticates
with the **service-role key**, which bypasses Row-Level Security, so it can write
race rows without needing a policy of its own. The public website reads the
curated tables (and the cust_id-free `race_results` view) with the anon key.

The service-role key is a secret — it lives only in the git-ignored config.json
and is never logged.

**Writes are upserts and there is no DELETE anywhere in this module.** That is
load-bearing, not just caution: `penalties` carries an `ON DELETE CASCADE` FK to
`curated_race_results`, so deleting a driver's result row would silently destroy
that driver's steward penalty history.
"""
from __future__ import annotations

import re
from typing import Any, Iterator

import requests

CURATED_RACES = "curated_races"
CURATED_RACE_RESULTS = "curated_race_results"
SEASONS = "seasons"


class SupabaseError(RuntimeError):
    """Raised when a Supabase REST call fails."""


class SupabaseClient:
    def __init__(self, url: str, api_key: str) -> None:
        self._base = url.rstrip("/") + "/rest/v1"
        self._session = requests.Session()
        self._session.headers.update(
            {
                "apikey": api_key,
                "Authorization": f"Bearer {api_key}",
            }
        )

    def upsert_season_if_needed(self, iracing_season_id: int | None, season_label: str | None) -> str | None:
        """Ensure a season row exists for the given iRacing season id.

        Returns the season UUID if a row was created or matched, otherwise None.
        """
        if iracing_season_id is None:
            return None

        existing = self._session.get(
            f"{self._base}/{SEASONS}",
            params={
                "select": "id",
                "iracing_season_id": f"eq.{iracing_season_id}",
                "limit": "1",
            },
            timeout=60,
        )
        if existing.status_code != 200:
            raise SupabaseError(
                f"GET seasons(iracing_season_id={iracing_season_id}) -> HTTP {existing.status_code}: {existing.text[:300]}"
            )

        rows = existing.json()
        if rows:
            return rows[0]["id"]

        label = (season_label or f"Season {iracing_season_id}").strip()
        number_hint = None
        if label:
            match = re.search(r"(\d+)", label)
            if match:
                number_hint = int(match.group(1))

        payload: dict[str, Any] = {
            "iracing_season_id": iracing_season_id,
            "name": label,
            "number": number_hint if number_hint is not None else iracing_season_id,
        }

        if payload.get("number") is not None:
            conflict = self._session.get(
                f"{self._base}/{SEASONS}",
                params={
                    "select": "id",
                    "number": f"eq.{payload['number']}",
                    "limit": "1",
                },
                timeout=60,
            )
            if conflict.status_code == 200 and conflict.json():
                payload["number"] = 1000000 + iracing_season_id

        resp = self._session.post(
            f"{self._base}/{SEASONS}",
            headers={
                "Content-Type": "application/json",
                "Prefer": "return=representation",
            },
            json=[payload],
            timeout=90,
        )
        if resp.status_code not in (200, 201, 204):
            raise SupabaseError(
                f"Insert season(iracing_season_id={iracing_season_id}) -> HTTP {resp.status_code}: {resp.text[:300]}"
            )

        body = resp.json()
        return body[0]["id"] if isinstance(body, list) and body else None

    # -- reads --------------------------------------------------------------
    def select(
        self,
        table: str,
        columns: str,
        filters: dict[str, str] | None = None,
        order: str | None = None,
        page_size: int = 1000,
    ) -> Iterator[dict[str, Any]]:
        """Yield every row of `table`, paging through PostgREST.

        `filters` are raw PostgREST predicates, e.g. {"status": "eq.provisional"}.
        """
        offset = 0
        while True:
            params: dict[str, Any] = {
                "select": columns,
                "order": order or columns.split(",")[0] + ".asc",
                "limit": page_size,
                "offset": offset,
            }
            params.update(filters or {})
            resp = self._session.get(f"{self._base}/{table}", params=params, timeout=60)
            if resp.status_code != 200:
                raise SupabaseError(
                    f"GET {table}({columns}) -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            rows = resp.json()
            yield from rows
            if len(rows) < page_size:
                return
            offset += page_size

    def get_curated_subsession_ids(self) -> set[int]:
        """Subsession IDs already present in curated_races."""
        return {
            value
            for row in self.select(CURATED_RACES, "subsession_id")
            if isinstance(value := row.get("subsession_id"), int)
        }

    def get_provisional_subsession_ids(self, since: str) -> set[int]:
        """Provisional races that started on/after `since` (ISO-8601 UTC).

        These are the races whose baseline is still worth re-pulling: nobody has
        finalized them yet, and they're recent enough that iRacing may still
        correct the result. Finalized races are never re-fetched.
        """
        rows = self.select(
            CURATED_RACES,
            "subsession_id",
            filters={"status": "eq.provisional", "start_time": f"gte.{since}"},
        )
        return {
            value for row in rows if isinstance(value := row.get("subsession_id"), int)
        }

    # -- writes -------------------------------------------------------------
    def _upsert(self, table: str, rows: list[dict[str, Any]], on_conflict: str) -> int:
        """POST rows with merge-duplicates. Returns the number of rows sent.

        Columns absent from the payload keep their DB default on insert and are
        left untouched on conflict — that is exactly how `status`,
        `penalty_points` and `adjusted_position` survive a re-sync.
        """
        if not rows:
            return 0
        resp = self._session.post(
            f"{self._base}/{table}",
            params={"on_conflict": on_conflict},
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=rows,
            timeout=90,
        )
        if resp.status_code not in (200, 201, 204):
            raise SupabaseError(
                f"Upsert {table} ({len(rows)} row(s)) -> "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return len(rows)

    def upsert_curated_races(self, rows: list[dict[str, Any]]) -> int:
        """Upsert race header rows. Must run BEFORE their driver rows (FK)."""
        return self._upsert(CURATED_RACES, rows, "subsession_id")

    def upsert_curated_race_results(self, rows: list[dict[str, Any]]) -> int:
        """Upsert per-driver rows. Their parent race must already exist."""
        return self._upsert(CURATED_RACE_RESULTS, rows, "subsession_id,cust_id")
