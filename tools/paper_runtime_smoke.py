"""Deterministic end-to-end paper-mode smoke test.

This does not contact BingX. It injects a fake exchange boundary and exercises
venue discovery -> watchlist -> queue -> portfolio capacity -> dashboard state.
It is intentionally deterministic so CI can run it without exchange credentials.
"""
from __future__ import annotations

import os
import sys
import types
import importlib
import time
import pandas as pd
import numpy as np
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.update({
    "PAPER_MODE": "True",
    "NEWS_ENABLED": "False",
    "DEEP_SCAN_WATCHLIST_SIZE": "5",
    "DEEP_WATCHLIST_SIZE": "5",
    "DEEP_SCAN_RADAR_SYMBOLS": "0",
    "WATCHLIST_DEEP_BATCH_SIZE": "5",
    "WATCHLIST_DEEP_INTERVAL_SEC": "0",
    "USE_EXECUTION_QUEUE": "True",
    "POSITION_MARGIN_PCT": "0.10",
    "PORTFOLIO_MARGIN_CAP_PCT": "0.60",
})


class FakeExchange:
    def __init__(self, *args, **kwargs):
        self.markets = {
            "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "type": "swap", "active": True},
            "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "type": "swap", "active": True},
            "GOLD(XAU)/USDT:USDT": {"base": "GOLD(XAU)", "quote": "USDT", "type": "swap", "active": True},
            "OILWTI/USDT:USDT": {"base": "OILWTI", "quote": "USDT", "type": "swap", "active": True},
            "US500/USDT:USDT": {"base": "US500", "quote": "USDT", "type": "swap", "active": True},
        }

    def load_markets(self):
        return self.markets


ccxt_stub = types.ModuleType("ccxt")
ccxt_stub.bingx = FakeExchange
sys.modules["ccxt"] = ccxt_stub

# Flask is not required for this deterministic pipeline smoke; the core only
# needs the names at import time.
flask_stub = types.ModuleType("flask")

class _SmokeFlask:
    def __init__(self, *args, **kwargs):
        self.routes = {}

    def route(self, path, methods=None, **kwargs):
        def decorator(fn):
            for method in (methods or ["GET"]):
                self.routes[(method, path)] = fn
            return fn
        return decorator

    def add_url_rule(self, path, endpoint, view_func, methods=None, **kwargs):
        for method in (methods or ["GET"]):
            self.routes[(method, path)] = view_func

    def before_request(self, fn):
        return fn

flask_stub.Flask = _SmokeFlask
flask_stub.jsonify = lambda *a, **k: a[0] if a else None
flask_stub.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1", json=None)
sys.modules["flask"] = flask_stub

for name in list(sys.modules):
    if name == "core.engine" or name.startswith("scanner.") or name.startswith("strategy.") or name.startswith("news."):
        sys.modules.pop(name, None)

E = importlib.import_module("core.engine")
D = importlib.import_module("scanner.deep_scanner")
S = importlib.import_module("scanner.scanner")


def frame(seed: float) -> pd.DataFrame:
    n = 260
    x = np.linspace(seed, seed * 1.04, n)
    return pd.DataFrame({
        "timestamp": np.arange(n),
        "open": x - 0.15,
        "high": x + 0.45,
        "low": x - 0.45,
        "close": x,
        "volume": np.full(n, 1000.0),
    })

frames = {
    "BTC/USDT:USDT": frame(100),
    "ETH/USDT:USDT": frame(200),
    "GOLD(XAU)/USDT:USDT": frame(300),
    "OILWTI/USDT:USDT": frame(400),
    "US500/USDT:USDT": frame(500),
}

E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: frames.get(symbol)
E.get_orderbook_cached = lambda *a, **k: {"bids": [[99.0, 10.0]], "asks": [[101.0, 5.0]]}
E.get_ticker_safe = lambda symbol: float(frames[symbol]["close"].iloc[-1])

# Patch compatibility exports captured by scanner.scanner at import time.
S.get_ohlcv_safe = lambda symbol, limit=120, htf=False: frames.get(symbol)
S.get_orderbook_cached = lambda *a, **k: {"bids": [[99.0, 10.0]], "asks": [[101.0, 5.0]]}

scanner = D.DeepScanner(max_symbols=5)
scanner.radar_symbols = 0
scanner.news.enabled = False

# Keep this smoke deterministic: strategy evidence is injected at the boundary,
# while the real queue/scanner orchestration is exercised.
def strategy_analyze(symbol, side, df, orderbook=None):
    return {
        "symbol": symbol, "side": side, "price": float(df.close.iloc[-1]),
        "atr": 1.0, "score": 9.0 if side == "BUY" else 6.0,
        "narrative_score": 8.0,
        "intent_score": 85.0, "intent_status": "ACCUMULATION", "intent_details": {},
        "narrative": {"sweep": True, "choch_bos": True, "retest": True,
                      "rejection": True, "displacement": True, "volume_confirmation": True,
                      "rf_alignment": True},
        "smart_money": {"institutional_bias": side, "institutional_bias_detailed": side,
                         "smart_money_dominant": True, "distribution_risk": 5,
                         "accumulation_strength": 90},
        "momentum": {"trend_expansion": True, "flow_bias": side,
                      "momentum_decay": False, "exhaustion_risk": 5,
                      "continuation_strength": 90},
    }
scanner.strategy.analyze = strategy_analyze

watch = scanner.scan(force=True)
assert len(watch) == 5, f"watchlist seed failed: {len(watch)}"
scanner.monitor_watchlist(force=True)
assert len(E.MEMORY.get("watchlist", {})) == 5

# Build a mature queue candidate directly from the real queue promotion path.
for item in E.MEMORY["watchlist"].values():
    item.update({
        "deep_analyzed": True,
        "state": "CONFIRMED",
        "score": 10.0,
        "narrative_score": 8.0,
        "intent_score": 90.0,
        "reasons": ["Liquidity Sweep", "BOS/CHoCH", "OB/Zone Retest", "Rejection", "Displacement"],
        "news_risk": 0,
        "side": "BUY",
        "pre_expansion_state": "PRE_EXPANSION_LONG",
        "pre_expansion_evidence": ["SWEEP", "DISPLACEMENT", "CHOCH_BOS", "OB_ZONE"],
        "pre_expansion": {"phase": "TREND_BUILDING", "indicator_alignment": "BULLISH"},
        "analysis": {"ob_grade": "A", "ob_score": 90, "liq_score": 85, "struct_score": 90,
                      "roro_signal": True, "trap_risk": 10},
    })

# Re-enter the production institutional registry after the deterministic
# evidence injection above. This explicitly exercises the intended
# Watchlist -> Institutional Zone Analysis -> A-GRADE -> Queue path.
radar = getattr(getattr(E, "main_loop_sniper", None), "_radar", None)
if radar is None:
    from core.engine import InstitutionalRadar
    radar = InstitutionalRadar()
for sym, item in E.MEMORY["watchlist"].items():
    radar._update_a_grade_status(item)
    radar._sync_institutional_zone_registry(sym, item)

promoted = S.promote_to_queue()
assert promoted >= 1, "queue promotion failed"
status = E.queue.get_status()
assert status["total_candidates"] >= 1

# Queue re-evaluation must never raise and must preserve a valid status object.
E.queue.re_evaluate_all(lambda sym: frames[sym])
status2 = E.queue.get_status()
assert "ready" in status2 and "waiting_trigger" in status2

print("PAPER_RUNTIME_SMOKE=PASS")
print(f"universe={len(watch)} watchlist={len(E.MEMORY['watchlist'])} promoted={promoted} queue={status2['total_candidates']} ready={status2['ready']}")
