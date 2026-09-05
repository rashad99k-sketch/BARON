"""Early Institutional Radar — staggered/batched global scanner (G1-G4).

Locks in the radar layer on the REAL DeepScanner code paths:
  1. scan(force=True) NO LONGER blocks on a full serial 240-symbol pass; it
     processes one time-boxed batch and returns, so the main loop never parks
     inside discovery (G1: no long idle searching).
  2. The radar cycle fills across the 20-minute window via
     advance_radar_cycle() -> yields the full universe ranked, and seeds the
     TOP-N watchlist with symbols NEAR an institutional OB/zone (G2/G3).
  3. API safety: batch pacing, per-batch time-box, and a per-minute call
     budget so the exchange is never hammered (G4).
  4. Quiet vs moving market selection: strong displacement/zone symbols win.
"""
import importlib
import sys
import time
import types
import unittest

import numpy as np
import pandas as pd

LOGS: list = []
STATE: dict = {"calls": 0}


def _frame(sym: str) -> pd.DataFrame:
    n = 120
    if "MOVE" in sym:
        close = np.linspace(100.0, 130.0, n)
        vol = np.full(n, 1000.0)
        vol[-1] = 3000.0
    else:
        close = np.full(n, 100.0) + np.sin(np.arange(n) / 5.0) * 0.02
        vol = np.full(n, 1000.0)
    df = pd.DataFrame({
        "open": close - 0.2, "high": close + 0.4, "low": close - 0.4,
        "close": close, "volume": vol,
    })
    df.symbol = sym
    return df


def _ohlcv(sym, limit=120, htf=False):
    STATE["calls"] += 1
    return _frame(sym)


def _zones(sym, df, ob=None):
    price = float(df["close"].iloc[-1])
    if "MOVE" in sym:
        return {"buy_zones": [{"price": price * 0.999, "strength": 80}],
                "sell_zones": [{"price": price * 1.02, "strength": 20}]}
    return {"buy_zones": [{"price": price * 1.25, "strength": 5}],
            "sell_zones": [{"price": price * 1.30, "strength": 5}]}


def _install_stubs():
    core_mod = types.ModuleType("core")
    core_mod.__path__ = []

    engine = types.ModuleType("core.engine")
    engine.MEMORY = {"watchlist": {}, "pipeline": {}}
    engine.log_execution = lambda *a, **k: LOGS.append((a[0], str(a[1]) if len(a) > 1 else ""))
    engine.get_ohlcv_safe = _ohlcv
    engine.get_orderbook_cached = lambda *a, **k: {"bids": [[99, 10]], "asks": [[101, 5]]}
    engine.compute_atr = lambda df: pd.Series(np.full(len(df), 1.0), index=df.index)

    def _adx(df):
        sym = getattr(df, "symbol", "")
        v = 26.0 if "MOVE" in sym else 8.0
        return pd.Series(np.full(len(df), v), index=df.index)
    engine.compute_adx = _adx

    class _RF:
        def compute(self, df):
            sym = getattr(df, "symbol", "")
            if "MOVE" in sym:
                return {"triggered": True, "distance": 0.001, "signal": "BUY"}
            return {"triggered": False, "distance": 0.01, "signal": "NEUTRAL"}
    engine.RFEngine = lambda period=20, multiplier=3.5: _RF()
    engine.MomentumFlowEngine = types.SimpleNamespace(analyze_momentum_flow=lambda df: {
        "trend_expansion": "MOVE" in getattr(df, "symbol", ""),
        "flow_bias": "BUY" if "MOVE" in getattr(df, "symbol", "") else "NEUTRAL",
    })
    engine.SmartMoneyEngine = types.SimpleNamespace(analyze_smart_money=lambda df: {
        "smart_money_dominant": "MOVE" in getattr(df, "symbol", ""),
        "institutional_bias": "BUY" if "MOVE" in getattr(df, "symbol", "") else "NEUTRAL",
        "distribution_risk": 10,
    })
    engine.get_smart_zones = _zones

    msb_mod = types.ModuleType("core.msb_ob")
    for name, value in (
        ("analyze_msb", lambda *a, **k: {"error": "MSB_UNAVAILABLE", "zones": [], "msb_events": [], "market": None}),
        ("msb_context", lambda *a, **k: None),
        ("rank_zones", lambda zones, side: (None, None)),
        ("temporal_sequence", lambda *a, **k: None),
        ("LONG", 1), ("SHORT", -1),
        ("STATUS_ACTIVE", "ACTIVE"), ("STATUS_TOUCHED", "TOUCHED"),
        ("STATUS_MITIGATING", "MITIGATING"), ("STATUS_INVALIDATED", "INVALIDATED"),
        ("STATUS_EXPIRED", "EXPIRED"),
    ):
        setattr(msb_mod, name, value)
    core_mod.__path__ = [""]
    sys.modules["core"] = core_mod
    sys.modules["core.engine"] = engine
    sys.modules["core.msb_ob"] = msb_mod

    news_mod = types.ModuleType("news.service")
    class FakeNews:
        def assess(self, *args, **kwargs):
            return types.SimpleNamespace(
                available=False, bias="NEUTRAL", risk=0, direct_count=0, macro_event=False,
                as_dict=lambda: {"available": False, "risk": 0, "bias": "NEUTRAL", "headlines": []},
            )
    news_mod.NewsService = FakeNews

    def _news_state_for_side(assessment, side, risk_block=80.0):
        if assessment is None or not getattr(assessment, "available", False):
            return "NEWS_UNAVAILABLE"
        return "NEWS_NEUTRAL"
    news_mod.news_state_for_side = _news_state_for_side
    sys.modules["news.service"] = news_mod

    strategy_mod = types.ModuleType("strategy.engine")
    class FakeStrategy:
        def analyze(self, symbol, side, df, orderbook=None):
            return {"side": side, "price": float(df.close.iloc[-1]), "score": 6.0,
                    "narrative": {}, "narrative_score": 5.0, "intent_score": 70,
                    "intent_status": "NEUTRAL", "intent_details": {},
                    "smart_money": {"institutional_bias": side, "distribution_risk": 10,
                                    "accumulation_strength": 50},
                    "momentum": {"trend_expansion": False, "momentum_decay": False,
                                 "exhaustion_risk": 10, "continuation_strength": 50},
                    "narrative_score": 5.0}
    strategy_mod.StrategyEngine = FakeStrategy
    sys.modules["strategy.engine"] = strategy_mod


def _markets(n_move: int, n_quiet: int) -> dict:
    m = {}
    for i in range(n_move):
        base = f"MOVE{i:03d}"
        m[f"{base}/USDT:USDT"] = {"base": base, "quote": "USDT", "type": "swap", "active": True}
    for i in range(n_quiet):
        base = f"QUIET{i:03d}"
        m[f"{base}/USDT:USDT"] = {"base": base, "quote": "USDT", "type": "swap", "active": True}
    return m


class FakeExchange:
    def __init__(self, markets):
        self.markets = markets

    def load_markets(self):
        return self.markets

    def fetch_tickers(self):
        return {}


class EarlyInstitutionalRadarTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {name: sys.modules.get(name) for name in
                      ("core", "core.engine", "core.msb_ob", "news.service",
                       "strategy.engine", "scanner.deep_scanner")}
        _install_stubs()
        sys.modules.pop("scanner.deep_scanner", None)
        importlib.invalidate_caches()
        module = importlib.import_module("scanner.deep_scanner")
        cls.DeepScanner = module.DeepScanner

    @classmethod
    def tearDownClass(cls):
        for name, module in cls._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        sys.modules.pop("core.msb_ob", None)

    def setUp(self):
        STATE["calls"] = 0
        LOGS.clear()
        sys.modules["core.engine"].MEMORY = {"watchlist": {}, "pipeline": {}}

    def _scanner(self, markets, watchlist=60, **kw):
        scanner = self.DeepScanner(max_symbols=watchlist)
        scanner.exchange = FakeExchange(markets)
        scanner._market_loader = lambda: markets
        for k, v in kw.items():
            setattr(scanner, k, v)
        return scanner

    def _drive_to_completion(self, scanner, max_steps=200):
        scanner.scan(force=True)
        steps = 0
        while scanner.radar_cycle is not None:
            scanner.advance_radar_cycle(force=True)
            steps += 1
            assert steps < max_steps, "radar cycle failed to converge"
        return steps

    def test_cycle_seeds_60_near_ob_symbols_within_window(self):
        scanner = self._scanner(_markets(100, 160), watchlist=60,
                                radar_max_calls_per_min=100000)
        scanner.radar_symbols = 0
        scanner.radar_batch_size = 20
        steps = self._drive_to_completion(scanner)
        # 260 rows in batches of 20 -> 13 advances, not one blocking pass.
        self.assertEqual(steps, 12)
        watch = scanner.last_result
        self.assertEqual(len(watch), 60)
        self.assertTrue(all(w["near_ob"] for w in watch))
        self.assertTrue(all("MOVE" in w["symbol"] for w in watch))
        self.assertEqual(scanner.status["radar_cycle"], "COMPLETE")
        pipe = sys.modules["core.engine"].MEMORY["pipeline"]["radar"]
        self.assertEqual(pipe["attempted"], 260)
        self.assertEqual(pipe["scanned"], 260)
        # Completion elapsed stays inside the 20-minute cycle budget.
        self.assertLess(scanner.stats["last_discovery"] - scanner.last_scan, 1.0)

    def test_scan_never_blocks_on_full_universe(self):
        scanner = self._scanner(_markets(120, 120), watchlist=60)
        scanner.radar_symbols = 0
        scanner.radar_batch_size = 20
        scanner.scan(force=True)
        self.assertIsNotNone(scanner.radar_cycle)
        self.assertEqual(scanner.radar_cycle["cursor"], 20)
        self.assertLess(scanner.radar_cycle["cursor"], 240)
        self.assertEqual(scanner.radar_cycle["api_calls"], 20)
        self.assertIn("cycle=CYCLING", [m for m, _ in LOGS if m.startswith("[DEEP] Discovery")][-1])

    def test_batch_pacing_and_per_minute_budget_protect_api(self):
        markets = _markets(40, 0)
        scanner = self._scanner(markets)
        scanner.radar_symbols = 0
        scanner.radar_batch_size = 10
        scanner.radar_batch_interval = 3600.0  # pacing forces one batch per tick
        scanner.scan(force=True)
        self.assertEqual(scanner.radar_cycle["cursor"], 10)
        calls_after_scan = STATE["calls"]
        scanner.advance_radar_cycle()  # NOT force -> deferred by pacing
        self.assertEqual(STATE["calls"], calls_after_scan)
        self.assertEqual(scanner.radar_cycle["cursor"], 10)

        # Per-minute budget: a bursted history defers the forced batch too.
        scanner.radar_max_calls_per_min = 5
        scanner._radar_api_call_times = [time.time()] * 60
        scanner.advance_radar_cycle(force=True)
        self.assertEqual(scanner.radar_cycle["cursor"], 10)
        self.assertTrue(any("rate budget hit" in m for m, _ in LOGS))

    def test_timebox_guarantees_progress_one_row_per_tick(self):
        scanner = self._scanner(_markets(30, 0))
        scanner.radar_symbols = 0
        scanner.radar_batch_size = 10
        scanner.radar_time_budget_sec = 0.0  # hard time-box: 1 row per batch
        scanner.scan(force=True)
        self.assertEqual(scanner.radar_cycle["cursor"], 1)
        scanner.advance_radar_cycle(force=True)
        self.assertEqual(scanner.radar_cycle["cursor"], 2)

    def test_moving_market_selected_quiet_filtered(self):
        scanner = self._scanner(_markets(60, 60), watchlist=60,
                                radar_max_calls_per_min=100000)
        scanner.radar_symbols = 0
        scanner.radar_batch_size = 24
        self._drive_to_completion(scanner)
        watch = scanner.last_result
        self.assertEqual(len(watch), 60)
        self.assertTrue(all("MOVE" in w["symbol"] for w in watch))
        self.assertTrue(all(w["near_ob"] for w in watch))
        # Only the moving-market rows are flagged near an institutional zone.
        radar = scanner.last_radar
        self.assertEqual(sum(1 for r in radar if r["near_ob"]), 60)
        self.assertTrue(all(not r["near_ob"] for r in radar if "QUIET" in r["symbol"]))
        # The summary log carries scanned/accepted/near_ob counters.
        self.assertTrue(any("scanned=120" in m for m, _ in LOGS if "cycle complete" in m))

    def test_legacy_radar_pass_contract_preserved(self):
        scanner = self._scanner(_markets(5, 5))
        scanner.radar_symbols = 0
        rows = scanner._discover()
        radar = scanner._radar(rows)
        self.assertEqual(len(radar), 10)
        first = radar[0]
        for key in ("radar_score", "ob_proximity", "near_ob", "radar_rank", "prox_side"):
            self.assertIn(key, first)
        self.assertTrue(all(r["near_ob"] for r in radar if "MOVE" in r["symbol"]))
        self.assertFalse(any(r["near_ob"] for r in radar if "QUIET" in r["symbol"]))

    def test_discovery_failure_keeps_watchlist_live(self):
        scanner = self._scanner(_markets(20, 20), watchlist=60)
        scanner.radar_symbols = 0
        scanner.radar_batch_size = 50
        top1 = scanner.scan(force=True)
        self.assertEqual(len(top1), 40)
        self.assertEqual(len(sys.modules["core.engine"].MEMORY["watchlist"]), 40)
        before = dict(sys.modules["core.engine"].MEMORY["watchlist"])
        scanner._market_loader = lambda: (_ for _ in ()).throw(TimeoutError("provider timeout"))
        top2 = scanner.scan(force=True)
        self.assertEqual(len(top2), 40)
        self.assertEqual(set(sys.modules["core.engine"].MEMORY["watchlist"]), set(before))
        self.assertEqual(scanner.status["watchlist"], "PRESERVED_DEGRADED")


if __name__ == "__main__":
    unittest.main()