from __future__ import annotations

import time

import requests

from .base import DataError

_session = requests.Session()
_session.headers["User-Agent"] = "quantdesk-paper/1.0"


def get_json(url: str, params: dict | None = None, headers: dict | None = None,
             timeout: float = 15.0, retries: int = 3):
    last = None
    for attempt in range(retries):
        try:
            r = _session.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code == 429 or r.status_code >= 500:
                last = DataError(f"HTTP {r.status_code} from {url}")
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code >= 400:
                raise DataError(f"HTTP {r.status_code} from {url}: {r.text[:200]}")
            return r.json()
        except requests.RequestException as e:
            last = DataError(f"network error for {url}: {e}")
            time.sleep(min(2 ** attempt, 8))
    raise last or DataError(f"failed: {url}")
