"""Minimal iRacing /data API client for the ATC Retrieval Server.

iRacing authenticates via OAuth 2.0 (the legacy email/password /auth endpoint
now returns 405). This uses the "Password Limited Grant" — the grant intended
for unattended clients acting on behalf of a registered user. It needs four
secrets from config.json: the user's email + password AND an OAuth client_id +
client_secret issued by iRacing.

Two iRacing-specific details this handles:

1. Secret masking — both client_secret and password are sent as
   base64(sha256(secret + identifier.strip().lower())), where client_secret is
   keyed by client_id and password is keyed by the username (email).
2. The "link" indirection — every /data endpoint responds with a signed S3 link
   you must follow for the real payload (large payloads arrive as numbered
   chunk files).

Credentials are read from config.json at runtime and are never logged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import requests

BASE_URL = "https://members-ng.iracing.com"
TOKEN_URL = "https://oauth.iracing.com/oauth2/token"
DEFAULT_SCOPE = "iracing.auth"
# Code lives in src/; config lives in the sibling config/ dir at the repo root.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "config.json"

# Refresh the access token this many seconds before it actually expires.
_EXPIRY_BUFFER = 60


class IRacingError(RuntimeError):
    """Raised when the iRacing API returns something we can't recover from."""


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _mask(secret: str, identifier: str) -> str:
    """iRacing's masking: base64(sha256(secret + identifier.strip().lower()))."""
    normalized = identifier.strip().lower()
    digest = hashlib.sha256(f"{secret}{normalized}".encode("utf-8")).digest()
    return base64.b64encode(digest).decode("utf-8")


class IRacingClient:
    def __init__(
        self,
        email: str,
        password: str,
        client_id: str,
        client_secret: str,
        scope: str = DEFAULT_SCOPE,
    ) -> None:
        self._email = email
        self._password = password
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "ATCRetrevalServer/0.1"})
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry = 0.0  # epoch seconds

    # -- auth ---------------------------------------------------------------
    def authenticate(self) -> None:
        """Get an access token via the password_limited grant (once, at start)."""
        self._token_request(
            {
                "grant_type": "password_limited",
                "client_id": self._client_id,
                "client_secret": _mask(self._client_secret, self._client_id),
                "username": self._email,
                "password": _mask(self._password, self._email),
                "scope": self._scope,
            }
        )

    def _refresh(self) -> None:
        if not self._refresh_token:
            self.authenticate()
            return
        self._token_request(
            {
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "client_secret": _mask(self._client_secret, self._client_id),
                "refresh_token": self._refresh_token,
            }
        )

    def _token_request(self, data: dict[str, str]) -> None:
        resp = self._session.post(
            TOKEN_URL,
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        if resp.status_code != 200:
            raise IRacingError(
                f"Token request ({data['grant_type']}) failed: "
                f"HTTP {resp.status_code}: {resp.text[:300]}"
            )
        body = resp.json()
        token = body.get("access_token")
        if not token:
            raise IRacingError(f"Token response missing access_token: {resp.text[:300]}")
        self._access_token = token
        self._refresh_token = body.get("refresh_token", self._refresh_token)
        self._token_expiry = time.time() + int(body.get("expires_in", 600))

    def _ensure_token(self) -> None:
        if self._access_token is None:
            self.authenticate()
        elif time.time() >= self._token_expiry - _EXPIRY_BUFFER:
            try:
                self._refresh()
            except IRacingError:
                self.authenticate()

    # -- core GET with link + chunk indirection -----------------------------
    def get(self, endpoint: str, **params: Any) -> Any:
        """GET a /data endpoint and return the fully-resolved payload."""
        data = self._raw_get(endpoint, params)
        if isinstance(data, dict) and "link" in data:
            data = self._follow_link(data["link"])
        if isinstance(data, dict) and data.get("chunk_info"):
            return self._download_chunks(data["chunk_info"])
        return data

    # -- high-level data helpers -------------------------------------------
    def get_subsession_result(
        self, subsession_id: int, include_licenses: bool = False
    ) -> dict:
        """Return the full results document for a single subsession.

        This is the complete record for one race ID: every session segment
        (practice / qualify / race), each driver's finishing position, incidents,
        laps completed, best lap, etc. Wraps the /data/results/get endpoint.
        """
        return self.get(
            "results/get",
            subsession_id=subsession_id,
            include_licenses=include_licenses,
        )

    def _raw_get(self, endpoint: str, params: dict[str, Any], _retry: bool = True) -> Any:
        self._ensure_token()
        url = f"{BASE_URL}/data/{endpoint.lstrip('/')}"
        resp = self._session.get(
            url,
            params=self._clean(params),
            headers={"Authorization": f"Bearer {self._access_token}"},
            timeout=30,
        )
        if resp.status_code == 401 and _retry:
            self._access_token = None  # force a fresh token
            return self._raw_get(endpoint, params, _retry=False)
        if resp.status_code == 429:
            self._sleep_for_rate_limit(resp)
            return self._raw_get(endpoint, params, _retry=_retry)
        if resp.status_code != 200:
            raise IRacingError(f"GET {endpoint} -> HTTP {resp.status_code}: {resp.text[:200]}")
        self._respect_rate_limit(resp)
        return resp.json()

    def _follow_link(self, link: str) -> Any:
        # The signed link is pre-authorized; no bearer token needed.
        resp = self._session.get(link, timeout=60)
        if resp.status_code != 200:
            raise IRacingError(f"Following data link -> HTTP {resp.status_code}")
        return resp.json()

    def _download_chunks(self, chunk_info: dict[str, Any]) -> list[Any]:
        base = chunk_info.get("base_download_url", "")
        rows: list[Any] = []
        for name in chunk_info.get("chunk_file_names", []):
            resp = self._session.get(base + name, timeout=60)
            resp.raise_for_status()
            rows.extend(resp.json())
        return rows

    # -- rate limiting ------------------------------------------------------
    @staticmethod
    def _respect_rate_limit(resp: requests.Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is not None and reset is not None and int(remaining) <= 1:
            wait = max(0, int(reset) - int(time.time())) + 1
            time.sleep(min(wait, 30))

    @staticmethod
    def _sleep_for_rate_limit(resp: requests.Response) -> None:
        reset = resp.headers.get("x-ratelimit-reset")
        wait = max(1, int(reset) - int(time.time())) + 1 if reset else 5
        time.sleep(min(wait, 60))

    @staticmethod
    def _clean(params: dict[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in params.items():
            if value is None:
                continue
            out[key] = "true" if value is True else "false" if value is False else value
        return out
