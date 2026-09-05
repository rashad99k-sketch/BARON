from __future__ import annotations
import os, re, time, threading
from typing import Dict, Iterable
from .http import BoundedHTTPClient
from .providers import SECProvider, OpenInsiderProvider, FinvizProvider
from .fusion import IntelligenceFusion

class ExternalIntelligenceService:
    def __init__(self):
        self.enabled = os.getenv("EXTERNAL_INTELLIGENCE_ENABLED", "true").strip().lower() in {"1","true","yes","on"}
        self.interval = max(60.0, float(os.getenv("EXTERNAL_INTELLIGENCE_INTERVAL_SEC", "600")))
        self.alert_score = float(os.getenv("EXTERNAL_INTELLIGENCE_ALERT_SCORE", "70"))
        self.timeout = float(os.getenv("EXTERNAL_INTELLIGENCE_TIMEOUT_SEC", "6"))
        self.cache_ttl = float(os.getenv("EXTERNAL_INTELLIGENCE_CACHE_TTL_SEC", "300"))
        ua = os.getenv("SEC_USER_AGENT", "ATOM-BOOT/1.0 contact=local")
        self.client = BoundedHTTPClient(self.timeout, self.cache_ttl, ua)
        self.sec = SECProvider(self.client)
        self.insider = OpenInsiderProvider(self.client)
        self.finviz = FinvizProvider(self.client)
        self.fusion = IntelligenceFusion(self.alert_score)
        self._last_run = 0.0
        self._lock = threading.RLock()
        self._snapshots: Dict[str, dict] = {}

    @staticmethod
    def ticker_from_symbol(symbol: str) -> str:
        raw = str(symbol or "").upper().strip()
        raw = raw.split(":")[0]
        raw = raw.replace("/USDT", "").replace("-USDT", "")
        return re.sub(r"[^A-Z0-9.\-]", "", raw)

    def scan(self, symbols: Iterable[str], *, force: bool = False) -> Dict[str, dict]:
        if not self.enabled:
            return {}
        now = time.time()
        with self._lock:
            if not force and now - self._last_run < self.interval:
                return dict(self._snapshots)
            out = {}
            for symbol in symbols:
                ticker = self.ticker_from_symbol(symbol)
                if not ticker or len(ticker) > 8:
                    continue
                try:
                    f = self.finviz.quote(ticker)
                    i = self.insider.latest(ticker)
                    s = self.sec.recent_filings(ticker)
                    snap = self.fusion.fuse(ticker, f, i, s).to_dict()
                    snap["symbol"] = symbol
                    snap["scanned_at"] = now
                    out[ticker] = snap
                except Exception as exc:
                    out[ticker] = {"symbol": symbol, "ticker": ticker, "score": 0, "direction": "NEUTRAL", "data_quality": "ERROR", "error": str(exc), "scanned_at": now}
            self._snapshots = out
            self._last_run = now
            return dict(out)

    def top(self, limit=10):
        return sorted(self._snapshots.values(), key=lambda x: float(x.get("score", 0)), reverse=True)[:limit]
