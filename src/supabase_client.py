"""Supabase (PostgREST) client for pushing iRacing race data to the site DB.

The ATC website's database is a Supabase Postgres exposed via PostgREST at
`<url>/rest/v1/<table>`. This local server is a trusted backend: it authenticates
with the **service-role key**, which bypasses Row-Level Security, so it can write
race rows. The public website reads the same tables with the anon key.

The service-role key is a secret — it lives only in the git-ignored config.json
and is never logged.
"""
from __future__ import annotations

from typing import Any

import requests


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

    # -- reads --------------------------------------------------------------
    def get_column_values(self, table: str, column: str, page_size: int = 1000) -> list[Any]:
        """Return every value of `column` in `table`, paging through PostgREST."""
        values: list[Any] = []
        offset = 0
        while True:
            resp = self._session.get(
                f"{self._base}/{table}",
                params={
                    "select": column,
                    "order": f"{column}.asc",
                    "limit": page_size,
                    "offset": offset,
                },
                timeout=30,
            )
            if resp.status_code != 200:
                raise SupabaseError(
                    f"GET {table}.{column} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            rows = resp.json()
            values.extend(row.get(column) for row in rows)
            if len(rows) < page_size:
                return values
            offset += page_size

    def get_existing_subsession_ids(self) -> set[int]:
        """The subsession IDs already stored in the races table."""
        return {v for v in self.get_column_values("races", "subsession_id") if isinstance(v, int)}

    # -- writes -------------------------------------------------------------
    def upsert_races(self, rows: list[dict[str, Any]]) -> int:
        """Insert/replace race rows keyed on subsession_id. Returns rows sent."""
        if not rows:
            return 0
        resp = self._session.post(
            f"{self._base}/races",
            params={"on_conflict": "subsession_id"},
            headers={
                "Content-Type": "application/json",
                "Prefer": "resolution=merge-duplicates,return=minimal",
            },
            json=rows,
            timeout=90,
        )
        if resp.status_code not in (200, 201, 204):
            raise SupabaseError(
                f"Upsert races -> HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return len(rows)
