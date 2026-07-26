"""Curate one iRacing `results/get` document into the site's display schema.

A pure transform — no I/O. Both writers (the nightly sync and the one-shot
backfill) push documents through here so they produce identical rows.

One iRacing document becomes two things:

    curated_races          one header row     (key: subsession_id)
    curated_race_results   one row per driver (key: subsession_id + cust_id)

Contract details that are easy to get wrong — the authority is
`supabase/migrations/0004_curated_races.sql` in the ATCWeb repo:

* **Positions are 0-based in iRacing, 1-based here.** The winner finishes `0`
  upstream and must be stored as `1`. A `starting_position` of `-1` means "no
  qualifying" and becomes null.
* **`interval` passes through verbatim**, including `-1` (a lap or more down).
  It is ten-thousandths of a second behind the class leader, and the web app
  re-sorts on it, so it is never normalized here.
* **`status`, `penalty_points` and `adjusted_position` are never emitted.** They
  are owned by the web app's admin page and all three have DB defaults, so
  leaving them out of the payload is what makes a re-run refresh the baseline
  without clobbering a steward's work. Adding them here would silently undo
  penalties on the next sync.
* **Every row in a batch must carry the same keys** — PostgREST rejects an array
  whose objects differ in shape. Nullable fields are emitted as `None`, never
  dropped, which is why each builder returns one fixed dict literal.

Anything that can't be curated raises `SkipRace`. The curated schema declares
several columns NOT NULL, and a single bad row fails the whole batch insert, so
a suspect subsession is skipped in full (never half-written) and logged.
"""
from __future__ import annotations

from typing import Any

# The only simsession that matters. A subsession without one isn't a race.
_RACE_SIMSESSION = "race"

# Columns the curated schema declares NOT NULL.
_REQUIRED_RACE = ("subsession_id", "start_time", "track_name")
_REQUIRED_RESULT = ("subsession_id", "cust_id", "display_name", "finish_position")


class SkipRace(Exception):
    """This subsession can't be curated — the caller should skip it and log why.

    `permanent` marks the verdict as settled for good: a completed subsession
    that has no Race simsession will never grow one, so the caller can remember
    it and stop re-fetching it. Everything else (a missing NOT NULL field, a
    malformed payload) might be an upstream hiccup and is retried next run.
    """

    def __init__(self, reason: str, permanent: bool = False) -> None:
        super().__init__(reason)
        self.permanent = permanent


# -- small coercions --------------------------------------------------------
def _int(value: Any) -> int | None:
    """An int, or None. `bool` is excluded — it is an int subclass in Python."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    """A non-blank trimmed string, or None."""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _one_based(value: Any) -> int | None:
    """iRacing position -> curated position. 0-based upstream; -1 means unset."""
    number = _int(value)
    if number is None or number < 0:
        return None
    return number + 1


def _track_name(doc: dict) -> str | None:
    track = doc.get("track")
    nested = track.get("track_name") if isinstance(track, dict) else None
    return _text(nested) or _text(doc.get("track_name"))


def _strength_of_field(doc: dict) -> int | None:
    """Event SoF, falling back to the first car class. -1 means "not rated"."""
    sof = _int(doc.get("event_strength_of_field"))
    if sof is None or sof < 0:
        classes = doc.get("car_classes") or []
        first = classes[0] if classes and isinstance(classes[0], dict) else {}
        sof = _int(first.get("strength_of_field"))
    return sof if sof is not None and sof >= 0 else None


def _race_simsession(doc: dict) -> dict | None:
    """The 'Race' entry of session_results[] — Practice/Qualify are ignored."""
    for simsession in doc.get("session_results") or []:
        if not isinstance(simsession, dict):
            continue
        name = simsession.get("simsession_type_name")
        if isinstance(name, str) and name.strip().lower() == _RACE_SIMSESSION:
            return simsession
    return None


# -- the transform ----------------------------------------------------------
def curate_race(doc: dict) -> dict:
    """Build the `curated_races` header row. Raises SkipRace if unusable."""
    row = {
        "subsession_id": _int(doc.get("subsession_id")),
        "start_time": _text(doc.get("start_time")),
        "track_name": _track_name(doc),
        "series_name": _text(doc.get("series_name")),
        "season_label": _text(doc.get("league_season_name")),
        "_iracing_season_id": _int(doc.get("league_season_id")),
        "strength_of_field": _strength_of_field(doc),
        "num_drivers": _int(doc.get("num_drivers")),
        # season_id: omitted — the web app links races to its own seasons table.
        # status:    omitted — DB default 'provisional' on insert, untouched on
        #            update, so a re-sync can't demote a finalized race.
    }
    missing = [column for column in _REQUIRED_RACE if row[column] is None]
    if missing:
        raise SkipRace(f"missing required race field(s): {', '.join(missing)}")
    return row


def curate_results(doc: dict) -> list[dict]:
    """Build the `curated_race_results` rows. Raises SkipRace if unusable."""
    simsession = _race_simsession(doc)
    if simsession is None:
        raise SkipRace("no 'Race' simsession (practice/qualify only)", permanent=True)

    subsession_id = _int(doc.get("subsession_id"))
    rows: list[dict] = []
    seen: set[int] = set()

    for entry in simsession.get("results") or []:
        if not isinstance(entry, dict):
            continue
        cust_id = _int(entry.get("cust_id"))
        # One row per driver per race is a unique constraint upstream, and a
        # duplicate key inside a single batch makes Postgres reject the whole
        # statement ("cannot affect row a second time"), so drop repeats here.
        if cust_id is not None and cust_id in seen:
            continue

        row = {
            "subsession_id": subsession_id,
            "cust_id": cust_id,
            "display_name": _text(entry.get("display_name")),
            "car_name": _text(entry.get("car_name")),
            "car_class_name": _text(entry.get("car_class_name")),
            "finish_position": _one_based(entry.get("finish_position")),
            "starting_position": _one_based(entry.get("starting_position")),
            "laps_complete": _int(entry.get("laps_complete")),
            "interval_ten_thousandths": _int(entry.get("interval")),
            "incidents": _int(entry.get("incidents")),
            # penalty_points / adjusted_position: omitted — web-app owned.
        }
        missing = [column for column in _REQUIRED_RESULT if row[column] is None]
        if missing:
            raise SkipRace(
                f"driver entry missing required field(s): {', '.join(missing)}"
            )

        seen.add(row["cust_id"])
        rows.append(row)

    if not rows:
        raise SkipRace("'Race' simsession has no driver results", permanent=True)
    return rows


def curate(doc: Any) -> tuple[dict, list[dict]]:
    """Curate a full results document into (race_row, driver_rows).

    Raises SkipRace if the subsession isn't a race or is missing a NOT NULL
    field. Callers write the race row *before* its driver rows — the results
    table has an FK to curated_races.
    """
    if not isinstance(doc, dict):
        raise SkipRace("results document is not a JSON object")
    # Reject non-races first: it's the more accurate reason to report when a
    # practice-only subsession also happens to have a usable header.
    results = curate_results(doc)
    return curate_race(doc), results
