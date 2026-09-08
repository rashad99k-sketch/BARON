import importlib
import sys
import types
import unittest
import pandas as pd
import numpy as np


def _install_stubs():
    # Always install deterministic dependency boundaries. The test must not depend
    # on which earlier test happened to import core.engine.
    core_mod = types.ModuleType("core")
    core_mod.__path__ = []
    engine = types.ModuleType("core.engine")
    class FakeExchange:
        def __init__(self):
            self.markets = {
                "BTC/USDT:USDT": {"base": "BTC", "type": "swap", "active": True},
                "GOLD(XAU)/USDT:USDT": {"base": "GOLD(XAU)", "type": "swap", "active": True},
                "OILWTI/USDT:USDT": {"base": "OILWTI", "type": "swap", "active": True},
                "AAPL/USDT:USDT": {"base": "AAPL", "type": "swap", "active": True},
                "SP500/USDT:USDT": {"base": "SP500", "type": "swap", "active": True},
            }
        def load_markets(self):
            return self.markets
    engine.ex = FakeExchange()
    engine.MEMORY = {"watchlist": {}}
    engine.log_execution = lambda *a, **k: None
    engine.RFEngine = lambda period=20, multiplier=3.5: types.SimpleNamespace(
        compute=lambda df: {"triggered": False, "distance": 0.002, "signal": "BUY"}
    )
    engine.MomentumFlowEngine = types.SimpleNamespace(analyze_momentum_flow=lambda df: {
        "trend_expansion": True, "flow_bias": "BUY"
    })
    engine.SmartMoneyEngine = types.SimpleNamespace(analyze_smart_money=lambda df: {
        "smart_money_dominant": True, "institutional_bias": "BUY", "distribution_risk": 10
    })
    engine.compute_atr = lambda df: pd.Series(np.full(len(df), 1.0), index=df.index)
    engine.compute_adx = lambda df: pd.Series(np.full(len(df), 25.0), index=df.index)
    engine.get_smart_zones = lambda sym, df, ob=None: {
        "buy_zones": [{"price": float(df.close.iloc[-1]) * 0.999, "strength": 80}],
        "sell_zones": [{"price": float(df.close.iloc[-1]) * 1.001, "strength": 70}],
    }
    engine.get_orderbook_cached = lambda *a, **k: {"bids": [[99, 10]], "asks": [[101, 5]]}
    engine.get_ohlcv_safe = lambda *a, **k: _df()

    class FakeInstitutionalRadar:
        def __init__(self):
            self.calls = []
        def _update_a_grade_status(self, entry):
            entry["a_grade_ready"] = False
            entry["a_grade_reasons"] = []
            return False
        def _sync_institutional_zone_registry(self, symbol, entry):
            registry = engine.MEMORY.setdefault("institutional_zone_analysis", {})
            if entry.get("pre_expansion_state") and entry.get("institutional_precursor_evidence"):
                registry[symbol] = {
                    "symbol": symbol, "side": entry.get("side"),
                    "strength": entry.get("strength"), "state": "INSTITUTIONAL_WATCH",
                    "precursor_evidence": list(entry.get("institutional_precursor_evidence", [])),
                }
                entry["institutional_zone_active"] = True
            else:
                registry.pop(symbol, None)
                entry["institutional_zone_active"] = False
            engine.MEMORY["institutional_zone_count"] = len(registry)
    engine.InstitutionalRadar = FakeInstitutionalRadar
    msb_mod = types.ModuleType("core.msb_ob")
    msb_mod.analyze_msb = lambda df, symbol: {"error": "MSB_UNAVAILABLE", "zones": [], "msb_events": [], "market": None}
    msb_mod.LONG = 1
    msb_mod.SHORT = -1
    msb_mod.STATUS_ACTIVE = "ACTIVE"
    msb_mod.STATUS_TOUCHED = "TOUCHED"
    msb_mod.STATUS_MITIGATING = "MITIGATING"
    msb_mod.STATUS_INVALIDATED = "INVALIDATED"
    msb_mod.STATUS_EXPIRED = "EXPIRED"
    msb_mod.msb_context = lambda *a, **k: None
    msb_mod.rank_zones = lambda zones, side: (None, None)
    msb_mod.temporal_sequence = lambda *a, **k: None
    core_mod.__path__ = [""]
    sys.modules["core"] = core_mod
    sys.modules["core.engine"] = engine
    sys.modules["core.msb_ob"] = msb_mod

    news_mod = types.ModuleType("news.service")
    class FakeNews:
        def assess(self, *args, **kwargs):
            return types.SimpleNamespace(
                available=False, bias="NEUTRAL", risk=0,
                direct_count=0, macro_event=False,
                as_dict=lambda: {"available": False, "risk": 0, "bias": "NEUTRAL", "headlines": []}
            )
    news_mod.NewsService = FakeNews
    def _fake_news_state_for_side(assessment, side, risk_block=80.0):
        if assessment is None or not getattr(assessment, "available", False):
            return "NEWS_UNAVAILABLE"
        if float(getattr(assessment, "risk", 0.0) or 0.0) >= risk_block:
            return "NEWS_RISK"
        return "NEWS_NEUTRAL"
    news_mod.news_state_for_side = _fake_news_state_for_side
    sys.modules["news.service"] = news_mod

    strategy_mod = types.ModuleType("strategy.engine")
    class FakeStrategy:
        def analyze(self, symbol, side, df, orderbook=None):
            return {
                "side": side, "price": float(df.close.iloc[-1]), "score": 7.0 if side == "BUY" else 5.0,
                "narrative": {"sweep": True, "choch_bos": True, "retest": True, "rejection": True, "displacement": True},
                "narrative_score": 6.0, "intent_score": 70, "intent_status": "ACCUMULATION",
                "intent_details": {}, "smart_money": {"institutional_bias": side, "distribution_risk": 10, "accumulation_strength": 80},
                "momentum": {"trend_expansion": True, "momentum_decay": False, "exhaustion_risk": 10, "continuation_strength": 80},
            }
    strategy_mod.StrategyEngine = FakeStrategy
    sys.modules["strategy.engine"] = strategy_mod


def _df():
    n = 80
    close = np.linspace(100, 120, n)
    return pd.DataFrame({
        "open": close - 0.5,
        "high": close + 1,
        "low": close - 1,
        "close": close,
        "volume": np.full(n, 1000.0),
    })


class DeepScannerRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._saved = {name: sys.modules.get(name) for name in ("core", "core.engine", "core.msb_ob", "news.service", "strategy.engine", "scanner.deep_scanner")}
        _install_stubs()
        # Reload the scanner after dependency stubs are installed so test order
        # cannot leak a previously imported production module.
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

    def test_radar_does_not_zero_out_when_radar_symbols_is_unset(self):
        scanner = self.DeepScanner(max_symbols=5)
        scanner.radar_symbols = 0  # backwards-compatible "scan all discovered rows"
        rows = scanner._discover()
        radar = scanner._radar(rows)
        self.assertEqual(len(radar), 5)
        self.assertTrue({r["asset_class"] for r in radar} >= {"CRYPTO", "GOLD", "OIL", "INDEX", "STOCK"})

    def test_zone_keys_are_consistent(self):
        scanner = self.DeepScanner(max_symbols=1)
        zones = scanner._zone_context("BTC/USDT:USDT", _df())
        self.assertIn("buy_zone", zones)
        self.assertIn("sell_zone", zones)
        self.assertNotIn("buy_near", zones)
        self.assertNotIn("sell_near", zones)

    def test_watchlist_seed_is_dynamic(self):
        scanner = self.DeepScanner(max_symbols=3)
        scanner.radar_symbols = 0
        top = scanner.scan(force=True)
        self.assertEqual(len(top), 3)
        self.assertEqual(len(sys.modules["core.engine"].MEMORY["watchlist"]), 3)

    def test_provider_failure_does_not_erase_existing_watchlist(self):
        scanner = self.DeepScanner(max_symbols=3)
        scanner.radar_symbols = 0
        scanner.scan(force=True)
        before = dict(sys.modules["core.engine"].MEMORY["watchlist"])
        scanner._market_loader = lambda: (_ for _ in ()).throw(TimeoutError("provider timeout"))
        scanner.scan(force=True)
        after = sys.modules["core.engine"].MEMORY["watchlist"]
        self.assertEqual(set(after), set(before))
        self.assertEqual(scanner.status["universe"], "PROVIDER_FAILURE")
        self.assertIn(scanner.status["watchlist"], {"PRESERVED_DEGRADED", "UNAVAILABLE"})

    def test_radar_symbols_is_optional_filter(self):
        scanner = self.DeepScanner(max_symbols=5)
        scanner.radar_symbols = 0
        scanner.radar_symbol_override = ["AAPL/USDT:USDT", "SP500/USDT:USDT"]
        rows = scanner._discover()
        self.assertEqual({r["symbol"] for r in rows}, {"AAPL/USDT:USDT", "SP500/USDT:USDT"})

    def test_medium_precursor_is_promoted_immediately_to_institutional_zone(self):
        E = sys.modules["core.engine"]
        scanner = self.DeepScanner(max_symbols=1)
        scanner.strategy.analyze = lambda symbol, side, df, orderbook=None: {
            "side": side, "price": float(df.close.iloc[-1]), "score": 6.0,
            "narrative": {
                "sweep": True, "choch_bos": True, "retest": True,
                "rejection": True, "displacement": True,
            },
            "narrative_score": 6.0, "intent_score": 70,
            "intent_status": "ACCUMULATION", "intent_details": {},
            "smart_money": {"institutional_bias": side, "distribution_risk": 10, "accumulation_strength": 80},
            "momentum": {"trend_expansion": True, "momentum_decay": False,
                         "exhaustion_risk": 10, "continuation_strength": 80},
        }
        # Keep the candidate MEDIUM: no queue admission is being tested here.
        scanner._fvg_context = lambda df, side: {"present": False, "distance": 999, "ifvg_present": False, "ifvg_penalty": 0.0, "ifvg_blocking": False}
        E.get_smart_zones = lambda sym, df, ob=None: {
            "buy_zones": [{"price": float(df.close.iloc[-1]) * 0.999, "strength": 80}],
            "sell_zones": [{"price": float(df.close.iloc[-1]) * 1.001, "strength": 70}],
        }
        E.get_orderbook_cached = lambda *a, **k: {"bids": [[99, 20]], "asks": [[101, 5]]}
        E.MEMORY["institutional_zone_analysis"] = {}
        row = scanner._analyze_symbol({"symbol": "BTC/USDT:USDT", "asset_class": "CRYPTO", "radar_score": 0.0, "last_update": 0.0})
        self.assertIsNotNone(row)
        self.assertEqual(row["strength"], "MEDIUM")
        self.assertTrue(row.get("institutional_zone_active"))
        self.assertIn("BTC/USDT:USDT", E.MEMORY["institutional_zone_analysis"])
        self.assertFalse(row.get("a_grade_ready", False))

    def test_medium_single_precursor_starts_institutional_analysis_but_not_prepared(self):
        E = sys.modules["core.engine"]
        scanner = self.DeepScanner(max_symbols=1)
        scanner.strategy.analyze = lambda symbol, side, df, orderbook=None: {
            "side": side, "price": float(df.close.iloc[-1]), "score": 6.0,
            "narrative": {"displacement": True},
            "narrative_score": 5.0, "intent_score": 60, "intent_status": "NEUTRAL",
            "intent_details": {},
            "smart_money": {"institutional_bias": "NEUTRAL", "distribution_risk": 10, "accumulation_strength": 50},
            "momentum": {"trend_expansion": False, "momentum_decay": False,
                         "exhaustion_risk": 10, "continuation_strength": 40},
        }
        scanner._fvg_context = lambda df, side: {"present": False, "distance": 999, "ifvg_present": False, "ifvg_penalty": 0.0, "ifvg_blocking": False}
        E.get_smart_zones = lambda sym, df, ob=None: {"buy_zones": [], "sell_zones": []}
        E.get_orderbook_cached = lambda *a, **k: {"bids": [[99, 5]], "asks": [[101, 5]]}
        E.MEMORY["institutional_zone_analysis"] = {}
        row = scanner._analyze_symbol({"symbol": "BTC/USDT:USDT", "asset_class": "CRYPTO", "radar_score": 0.0, "last_update": 0.0})
        self.assertIsNotNone(row)
        self.assertEqual(row["strength"], "MEDIUM")
        self.assertTrue(row.get("institutional_zone_active", False))
        self.assertIn("BTC/USDT:USDT", E.MEMORY["institutional_zone_analysis"])
        self.assertFalse(row.get("institutional_prepared", False))

    def test_medium_in_watchlist_swept_to_institutional_without_batch_turn(self):
        # A MEDIUM symbol that is deep-analyzed but NOT part of this cycle's
        # rotating batch must still reach Institutional Zone Analysis in the
        # same monitor tick: zero wait for its batch turn.
        E = sys.modules["core.engine"]
        scanner = self.DeepScanner(max_symbols=4)
        sym = "ETH/USDT:USDT"
        entry = {
            "symbol": sym, "asset_class": "CRYPTO", "score": 6.0,
            "strength": "MEDIUM", "deep_analyzed": True, "side": "SELL",
            "price": 3000.0, "state": "REJECTION",
            "pre_expansion_state": "PRE_EXPANSION_SHORT",
            "pre_expansion_evidence": ["BOS", "REJECTION"],
            "institutional_precursor_evidence": ["BOS", "REJECTION"],
            "pre_expansion": {"phase": "EARLY_EXPANSION",
                              "indicator_alignment": "BEARISH"},
            "last_update": 0.0,
        }
        E.MEMORY["watchlist"] = {sym: entry}
        E.MEMORY["institutional_zone_analysis"] = {}
        # No batch entries at all -> nothing is re-analyzed this cycle.
        scanner.watch_batch_size = 0
        updated = scanner.monitor_watchlist(force=True)
        self.assertEqual(len(updated), 0, "no symbol was deep-re-analyzed this tick")
        self.assertEqual(E.MEMORY["watchlist_deep_analyzed"], 0)
        # Yet the MEDIUM symbol is in Institutional Zone Analysis immediately.
        self.assertTrue(entry.get("institutional_zone_active"))
        self.assertIn(sym, E.MEMORY["institutional_zone_analysis"])
        self.assertEqual(E.MEMORY["institutional_zone_count"], 1)

    def test_zero_evidence_medium_still_routed_to_institutional_resolver(self):
        # MEDIUM without institutional precursor evidence is STILL handed to
        # the institutional resolver in the same tick (latency-free routing);
        # the resolver's quality gate keeps it out of the registry.
        E = sys.modules["core.engine"]

        class SpyRadar:
            def __init__(self):
                self.synced = []

            def _update_a_grade_status(self, entry):
                entry["a_grade_ready"] = False
                entry["a_grade_reasons"] = []

            def _sync_institutional_zone_registry(self, symbol, entry):
                self.synced.append(symbol)
                registry = E.MEMORY.setdefault("institutional_zone_analysis", {})
                if entry.get("pre_expansion_state") and entry.get("institutional_precursor_evidence"):
                    registry[symbol] = {"symbol": symbol, "strength": entry.get("strength")}
                else:
                    registry.pop(symbol, None)
                entry["institutional_zone_active"] = bool(registry.get(symbol))
                E.MEMORY["institutional_zone_count"] = len(registry)

        E.InstitutionalRadar = SpyRadar
        scanner = self.DeepScanner(max_symbols=4)
        sym = "WTI/USDT:USDT"
        entry = {
            "symbol": sym, "asset_class": "OIL", "score": 5.5,
            "strength": "MEDIUM", "deep_analyzed": True, "side": "BUY",
            "price": 100.0, "state": "DETECTED", "last_update": 0.0,
        }
        E.MEMORY["watchlist"] = {sym: entry}
        E.MEMORY["institutional_zone_analysis"] = {}
        scanner.watch_batch_size = 0
        scanner.monitor_watchlist(force=True)
        spy = scanner._institutional_radar
        self.assertIsInstance(spy, SpyRadar)
        self.assertIn(sym, spy.synced, "MEDIUM was routed to the resolver this tick")
        self.assertFalse(entry.get("institutional_zone_active"),
                         "quality gate: zero-evidence MEDIUM stays out of the registry")
        self.assertEqual(E.MEMORY["institutional_zone_count"], 0)


class OrderbookSideBoostRuntimeTest(unittest.TestCase):
    """End-to-end proof that the deep scanner reads the orderbook sides.

    A crafted limit-order book is fed through the REAL ``_analyze_symbol``
    pipeline; the scanner must stamp ONLY the side the book actually favors
    (LOB Imbalance reason + a score boost) and must stay silent for the side
    it does not support — and fully silent on a balanced book.
    """

    @classmethod
    def setUpClass(cls):
        cls._saved = {name: sys.modules.get(name) for name in ("core", "core.engine", "core.msb_ob",
                                                               "news.service", "strategy.engine",
                                                               "scanner.deep_scanner")}
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

    def _scan_row(self, book):
        scanner = self.DeepScanner(max_symbols=1)
        # side-agnostic strategy so the orderbook is the ONLY differentiator
        scanner.strategy.analyze = lambda symbol, side, df, orderbook=None: {
            "side": side, "price": float(df.close.iloc[-1]), "score": 6.0,
            "narrative": {}, "narrative_score": 5.0, "intent_score": 70,
            "intent_status": "NEUTRAL", "intent_details": {},
            "smart_money": {"institutional_bias": side, "distribution_risk": 10,
                            "accumulation_strength": 50},
            "momentum": {"trend_expansion": False, "momentum_decay": False,
                         "exhaustion_risk": 10, "continuation_strength": 50},
        }
        sys.modules["core.engine"].get_orderbook_cached = lambda *a, **k: book
        entry = {"symbol": "BTC/USDT:USDT", "asset_class": "CRYPTO",
                 "radar_score": 0.0, "last_update": 0.0, "source": "test"}
        row = scanner._analyze_symbol(entry)
        self.assertIsNotNone(row, "deep analysis must complete on a crafted book")
        return row

    @staticmethod
    def _book(bids_qty, asks_qty, mid=100.0):
        bids = [[mid - (i + 1) * 0.1, bids_qty[i]] for i in range(len(bids_qty))]
        asks = [[mid + (i + 1) * 0.1, asks_qty[i]] for i in range(len(asks_qty))]
        return {"bids": bids, "asks": asks}

    def test_buy_heavy_book_stamps_buy_side_only(self):
        row = self._scan_row(self._book([20] * 5, [4] * 5))
        self.assertEqual(row["side"], "BUY")
        self.assertIn("LOB Imbalance", row["reasons"])
        self.assertGreater(row["orderbook_imbalance"], 0.15)
        self.assertGreaterEqual(row["score"], 6.0)

    def test_sell_heavy_book_stamps_sell_side_only(self):
        row = self._scan_row(self._book([4] * 5, [20] * 5))
        self.assertEqual(row["side"], "SELL")
        self.assertIn("LOB Imbalance", row["reasons"])
        self.assertLess(row["orderbook_imbalance"], -0.15)
        self.assertGreaterEqual(row["score"], 6.0)

    def test_balanced_book_stamps_neither_side(self):
        row = self._scan_row(self._book([10] * 5, [10] * 5))
        self.assertNotIn("LOB Imbalance", row["reasons"])
        self.assertAlmostEqual(row["orderbook_imbalance"], 0.0, places=4)

    def test_zero_depth_imbalance_never_stamped(self):
        # A book of empty top-5 is treated as neutral regardless of deep levels.
        row = self._scan_row(self._book([0] * 5, [0] * 5))
        self.assertNotIn("LOB Imbalance", row["reasons"])
        self.assertEqual(row["orderbook_imbalance"], 0.0)

    def test_exact_threshold_plus_015_stamps_buy(self):
        row = self._scan_row(self._book([115], [85]))
        self.assertEqual(row["side"], "BUY")
        self.assertIn("LOB Imbalance", row["reasons"])

    def test_exact_threshold_minus_015_stamps_sell(self):
        row = self._scan_row(self._book([85], [115]))
        self.assertEqual(row["side"], "SELL")
        self.assertIn("LOB Imbalance", row["reasons"])

    def test_just_below_threshold_stamps_nothing(self):
        row = self._scan_row(self._book([114], [87]))  # (114-87)/201 ~ +0.134 < 0.15
        self.assertNotIn("LOB Imbalance", row["reasons"])


if __name__ == "__main__":
    unittest.main()
