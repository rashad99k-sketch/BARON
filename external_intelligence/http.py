from __future__ import annotations
import time
import threading
from typing import Optional
import requests

class BoundedHTTPClient:
    """Small, dependency-light HTTP client with timeout, cache and backoff."""
    def __init__(self, timeout: float = 6.0, cache_ttl: float = 300.0, user_agent: str = "ATOM-BOOT/1.0 contact=local"):
        self.timeout = max(1.0, float(timeout))
        self.cache_ttl = max(0.0, float(cache_ttl))
        self.headers = {"User-Agent": user_agent, "Accept": "application/json,text/html;q=0.9,*/*;q=0.8"}
        self._cache = {}
        self._lock = threading.RLock()

    def get(self, url: str, *, params: Optional[dict] = None, headers: Optional[dict] = None, cache_key: Optional[str] = None):
        key = cache_key or url + ("?" + str(sorted((params or {}).items())) if params else "")
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self.cache_ttl:
                return hit[1]
        last = None
        for attempt in range(3):
            try:
                h = dict(self.headers)
                if headers:
                    h.update(headers)
                r = requests.get(url, params=params, headers=h, timeout=self.timeout)
                r.raise_for_status()
                with self._lock:
                    self._cache[key] = (time.time(), r)
                return r
            except Exception as exc:
                last = exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        raise last
