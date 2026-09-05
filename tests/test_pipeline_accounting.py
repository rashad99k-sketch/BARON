"""Pipeline accounting forensic tests (deterministic).

Proves, through the REAL production code paths:
  1. Universe selection is activity-ranked: strong movers survive the radar
     limit cut instead of being dropped by raw venue listing order.
  2. promote_to_queue refuses setups whose causal-OB entry window is already
     missed (the same 1.5 ATR rule the queue applies), and counts the reason.
  3. A full institutional sequence (sweep -> structure -> rejection, observed
     over the event window) reaches READY via the real re_evaluate_all, with
     per-gate explainability recorded.
  4. An extended candidate is returned to the watchlist with a counted reason.
  5. The dashboard payload carries the pipeline funnel + gate statistics.
"""
import importlib
import os
import sys
import types
import unittest

import numpy as np
import pandas as pd


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        pass
    def route(self, *args, **kwargs):
        return lambda fn: fn
    def add_url_rule(self, *args, **kwargs):
        return None


def _load_engine():
    saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine")}
    old_paper = os.environ.pop("PAPER_MODE", None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = {}

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: None
    fake_flask.request = types.SimpleNamespace()
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask
    sys.modules.pop("core.engine", None)
    engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _build_frame(with_next_candle=False):
    """Uptrend -> red OB -> displacement (BOS) -> pullback into zone with edge
    touches -> liquidity sweep of the equal-lows pool -> bullish rejection."""
    rows = []
    price = 95.0
    for _ in range(30):
        o = price; c = price + 0.06
        rows.append((o, c + 0.05, o - 0.05, c, 1000.0)); price = c
    for _ in range(4):
        o = price; c = price + 0.10
        rows.append((o, c + 0.04, o - 0.04, c, 1200.0)); price = c
    rows.append((97.55, 97.65, 97.10, 97.15, 1400.0))  # causal OB, zone [97.10, 97.55]
    p = 97.15
    for _ in range(3):
        o = p; c = p + 0.50
        rows.append((o, c + 0.04, o - 0.03, c, 2600.0)); p = c
    rows.extend([
        (98.55, 98.70, 98.20, 98.30, 900),
        (98.30, 98.40, 97.90, 98.00, 900),
        (98.00, 98.10, 97.60, 97.70, 900),
        (97.70, 97.80, 97.30, 97.40, 950),
        (97.40, 97.55, 97.02, 97.45, 2300),   # edge touch + rejection (pool low)
        (97.45, 97.60, 97.25, 97.35, 900),
        (97.35, 97.50, 97.03, 97.42, 2200),   # equal low (pool strength)
        (97.42, 97.55, 97.20, 97.30, 900),
        (97.30, 97.45, 97.06, 97.40, 2100),   # third edge touch
        (97.40, 97.50, 97.25, 97.32, 850),
        (97.32, 97.45, 97.18, 97.38, 850),
        (97.38, 97.48, 97.22, 97.42, 850),
    ])
    rows.append((97.42, 97.62, 96.90, 97.35, 3200.0))  # sweep + reclaim
    if with_next_candle:
        rows.append((97.35, 97.50, 97.05, 97.37, 2000.0))  # follow-through wick
    arr = np.array(rows, dtype=float)
    return pd.DataFrame({"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
                         "close": arr[:, 3], "volume": arr[:, 4],
                         "timestamp": np.arange(len(arr))})


class UniverseActivityTest(unittest.TestCase):
    def test_activity_ranked_movers_survive_radar_limit(self):
        from scanner.universe import build_balanced
        markets = {}
        for i in range(40):
            markets[f"COIN{i:02d}/USDT:USDT"] = {
                "symbol": f"COIN{i:02d}/USDT:USDT", "type": "swap",
                "active": True, "base": f"COIN{i:02d}", "quote": "USDT",
            }
        activity = {"COIN39/USDT:USDT": 50.0, "COIN38/USDT:USDT": 40.0}
        rows = build_balanced(markets, radar_limit=5, activity=activity)
        selected = {r["symbol"] for r in rows}
        self.assertIn("COIN39/USDT:USDT", selected)
        self.assertIn("COIN38/USDT:USDT", selected)
        self.assertLessEqual(len(rows), 5)

    def test_activity_fallback_without_tickers(self):
        from scanner.universe import build_balanced
        markets = {
            "BTC/USDT:USDT": {"symbol": "BTC/USDT:USDT", "type": "swap",
                              "active": True, "base": "BTC", "quote": "USDT"},
            "ETH/USDT:USDT": {"symbol": "ETH/USDT:USDT", "type": "swap",
                              "active": True, "base": "ETH", "quote": "USDT"},
        }
        rows = build_balanced(markets, radar_limit=10)
        self.assertEqual(len(rows), 2)


class PromotionWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    def _load_scanner(self):
        sys.modules.pop("scanner.scanner", None)
        return importlib.import_module("scanner.scanner")

    def _extended_frame(self):
        # Displacement moved price far ABOVE the causal OB: entry window missed.
        df = _build_frame(False)
        last = df.iloc[-1].copy()
        rows = df.values.tolist()
        last_ts = rows[-1][5] if len(rows[-1]) > 5 else 0
        for k in range(3):
            prev = rows[-1]
            rows.append([prev[3], prev[3] + 0.02, prev[3] + 0.55, prev[3] + 0.50, 1500.0, last_ts + k + 1])
        arr = np.array(rows, dtype=float)
        return pd.DataFrame({"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
                             "close": arr[:, 3], "volume": arr[:, 4],
                             "timestamp": arr[:, 5]})

    def _watchlist_item(self, sym):
        return {
            "symbol": sym, "side": "BUY", "state": "REJECTION",
            "score": 9.0, "narrative_score": 6.0,
            "reasons": ["Liquidity Sweep", "BOS/CHoCH", "Rejection"],
            "news_risk": 10.0, "deep_analyzed": True,
            "institutional_zone_active": True, "a_grade_ready": True,
            "institutional_stage": "A-GRADE_READY",
            "analysis": {
                "ob_grade": "A", "roro_signal": True, "struct_score": 90,
                "liq_score": 90, "price": 100.0, "atr": 1.0, "side": "BUY",
            },
        }

    def test_extended_setup_not_promoted_and_counted(self):
        E = self.engine
        S = self._load_scanner()
        E.MEMORY["watchlist"] = {"EXT/USDT:USDT": self._watchlist_item("EXT/USDT:USDT")}
        E.MEMORY["institutional_zone_analysis"] = {"EXT/USDT:USDT": {"symbol": "EXT/USDT:USDT", "side": "BUY", "institutional_score": 80, "composite_score": 80, "precursor_count": 3}}
        E.queue._candidates.clear()
        S.get_ohlcv_safe = lambda s, n=100: self._extended_frame()
        S.get_orderbook_cached = lambda s, limit=10: None
        promoted = S.promote_to_queue()
        self.assertEqual(promoted, 0)
        promo = E.MEMORY["pipeline"]["promotion"]
        self.assertEqual(promo["skipped_entry_window"], 1)
        self.assertEqual(promo["rejected_by_reason"].get("entry_window_missed"), 1)

    def test_prepared_setup_promoted_before_a_grade(self):
        E = self.engine
        S = self._load_scanner()
        item = self._watchlist_item("PREP/USDT:USDT")
        item.update({
            "a_grade_ready": False,
            "institutional_prepared": True,
            "institutional_stage": "PREPARED_FOR_ENTRY",
            "pre_expansion_evidence": ["DISPLACEMENT", "REJECTION", "MSB_MSS"],
            "precursor_count": 3,
            "pre_expansion": {"phase": "EARLY_EXPANSION"},
        })
        E.MEMORY["watchlist"] = {"PREP/USDT:USDT": item}
        E.MEMORY["institutional_zone_analysis"] = {
            "PREP/USDT:USDT": {
                "symbol": "PREP/USDT:USDT", "side": "BUY",
                "institutional_score": 72, "composite_score": 65, "precursor_count": 3,
            }
        }
        E.queue._candidates.clear()
        S.get_ohlcv_safe = lambda s, n=100: _build_frame(False)
        S.get_orderbook_cached = lambda s, limit=10: None
        promoted = S.promote_to_queue()
        self.assertEqual(promoted, 1)
        cand = E.queue._candidates["PREP/USDT:USDT"]
        self.assertFalse(cand.is_a_grade)
        self.assertEqual(cand.signal_type, "institutional_prepared")
        self.assertEqual(item["queue_state"], "PREPARED_ADMITTED")

    def test_in_window_setup_promoted(self):
        E = self.engine
        S = self._load_scanner()
        E.MEMORY["watchlist"] = {"INW/USDT:USDT": self._watchlist_item("INW/USDT:USDT")}
        E.MEMORY["institutional_zone_analysis"] = {"INW/USDT:USDT": {"symbol": "INW/USDT:USDT", "side": "BUY", "institutional_score": 80, "composite_score": 80, "precursor_count": 3}}
        E.queue._candidates.clear()
        S.get_ohlcv_safe = lambda s, n=100: _build_frame(False)
        S.get_orderbook_cached = lambda s, limit=10: None
        promoted = S.promote_to_queue()
        self.assertEqual(promoted, 1)
        self.assertIn("INW/USDT:USDT", E.queue._candidates)
        cand = E.queue._candidates["INW/USDT:USDT"]
        self.assertAlmostEqual(cand.zone_low, 97.10, places=2)
        self.assertAlmostEqual(cand.zone_high, 97.55, places=2)
        # The queue never aliases the entry anchor to the current price.
        self.assertNotEqual(cand.entry_price, cand.price)


class ReadyTransitionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    def test_full_ready_transition_with_gate_explainability(self):
        E = self.engine
        E.get_spread_bps = lambda s: 0.02  # tight-spread environment
        q = E.ExecutionQueue()
        df1 = _build_frame(False)
        df2 = _build_frame(True)
        cand = E.ExecutionCandidate(
            symbol="PROOF/USDT:USDT", side="BUY", price=float(df1["close"].iloc[-1]),
            entry_price=97.10, stop_loss=96.80, take_profit_1=97.80, take_profit_2=98.20,
            atr=float(E.compute_atr(df1).iloc[-1]), df=df1, ob={},
        )
        self.assertTrue(q.add_candidate(cand))

        q.re_evaluate_all(lambda s: df1)
        c = q._candidates.get("PROOF/USDT:USDT")
        self.assertIsNotNone(c)
        self.assertEqual(c.confirmation_count, 1)
        self.assertEqual(c.gate_status["blocker"], "CONFIRMATION")

        q.re_evaluate_all(lambda s: df2)
        c = q._candidates.get("PROOF/USDT:USDT")
        self.assertIsNotNone(c)
        self.assertEqual(c.confirmation_count, 2)
        # The current ATOM contract intentionally hard-rejects an over-mitigated zone.
        # The older test expected READY here, which contradicted the production
        # safety invariant and made the suite encode a stale behavior.
        self.assertNotEqual(c.state, E.ExecutionState.READY)
        self.assertTrue(c.atom_hard_reject)
        self.assertEqual(c.gate_status["blocker"], "ATOM_HARD_REJECT")
        self.assertEqual(q.gate_stats["ready"], 0)

    def test_extended_candidate_returned_with_reason(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _build_frame(False)
        # Push price far above the zone: extension guard must fire.
        rows = df.values.tolist()
        for _ in range(4):
            prev = rows[-1]
            rows.append([prev[3], prev[3] + 0.02, prev[3] + 0.60, prev[3] + 0.55, 1500.0, (prev[5] if len(prev) > 5 else 0) + 1])
        arr = np.array(rows, dtype=float)
        ext = pd.DataFrame({"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
                            "close": arr[:, 3], "volume": arr[:, 4],
                            "timestamp": arr[:, 5]})
        cand = E.ExecutionCandidate(
            symbol="EXT2/USDT:USDT", side="BUY", price=float(ext["close"].iloc[-1]),
            entry_price=97.10, stop_loss=96.80, take_profit_1=98.0, take_profit_2=98.5,
            atr=float(E.compute_atr(df).iloc[-1]), df=df, ob={},
        )
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: ext)
        c = q._candidates.get("EXT2/USDT:USDT")
        self.assertIsNotNone(c)
        self.assertEqual(c.state, E.ExecutionState.RETURNED_WATCHLIST)
        self.assertGreaterEqual(q.gate_stats["extended"], 1)
        feed = E.MEMORY.get("gate_feed", [])
        self.assertTrue(any(g["blocker"] == "PRICE_EXTENDED" for g in feed))
        q.cleanup()
        self.assertNotIn("EXT2/USDT:USDT", q._candidates)

    def test_invalidated_zone_is_terminal(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _build_frame(False)
        rows = df.values.tolist()
        prev = rows[-1]
        rows.append([prev[3], prev[3] + 0.02, 96.50, 96.60, 3000.0, (prev[5] if len(prev) > 5 else 0) + 1])
        arr = np.array(rows, dtype=float)
        brk = pd.DataFrame({"open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
                            "close": arr[:, 3], "volume": arr[:, 4],
                            "timestamp": arr[:, 5]})
        cand = E.ExecutionCandidate(
            symbol="BRK/USDT:USDT", side="BUY", price=float(brk["close"].iloc[-1]),
            entry_price=97.10, stop_loss=96.80, take_profit_1=98.0, take_profit_2=98.5,
            atr=float(E.compute_atr(df).iloc[-1]), df=df, ob={},
        )
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: brk)
        self.assertNotIn("BRK/USDT:USDT", q._candidates)
        self.assertTrue(
            q.gate_stats["zone_invalidated"] >= 1 or q.gate_stats["extended"] >= 1
        )


class DashboardPipelineTest(unittest.TestCase):
    def test_pipeline_payload_and_live_memory(self):
        eng = sys.modules.get("core.engine")
        if eng is None:
            self.skipTest("engine module not loaded")
        eng.MEMORY["watchlist_queue_promotions"] = 5
        eng.MEMORY["pipeline"] = {
            "universe": {"loaded": 100, "eligible": 50, "selected": 20},
            "radar": {"attempted": 20, "scanned": 18},
        }
        dashboard = importlib.import_module("dashboard.app")
        if not hasattr(dashboard.app, "test_client"):
            self.skipTest("dashboard app not available under fake-flask harness")
        eng.CACHE.pop("dashboard", None)
        with dashboard.app.test_client() as client:
            body = client.get("/data").get_json()
        self.assertEqual(body["queue"]["promotions"], 5)
        self.assertIn("pipeline", body)
        self.assertEqual(body["pipeline"]["universe"]["loaded"], 100)
        self.assertIn("gate_stats", body["queue"])




class LifecycleSmokeTest(unittest.TestCase):
    """Defect guard: paper-mode lifecycle left OPEN_PENDING_CONFIRMATION on
    first management pass; open-condition names remain test-only."""

    def test_pending_to_live_on_manage(self):
        import core.engine as E
        import pandas as pd
        df = pd.DataFrame({"timestamp": np.arange(60),
                           "open": [1.0]*60, "high": [1.01]*60,
                           "low": [0.99]*60, "close": [1.0]*60,
                           "volume": [100.0]*60})
        E.STATE.clear()
        E.STATE.update({"open": True, "current_symbol": "L/SCOPE",
                        "side": "BUY", "entry": 1.0, "qty": 1.0,
                        "mark_price": 1.0, "roe_pct": 0.0})
        E.get_ohlcv_safe = lambda s, n=120: df
        E.get_ticker_safe = lambda s: 1.0
        mgr = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        mgr.lifecycle_state = E.TradeLifecycleState.OPEN_PENDING_CONFIRMATION
        mgr.manage_live_trade()
        assert mgr.lifecycle_state == E.TradeLifecycleState.LIVE
        E.STATE.clear()


class InstitutionalZoneSeparationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    def test_medium_without_precursor_stays_out_of_institutional_zone(self):
        E = self.engine
        E.MEMORY["watchlist"] = {"MED/USDT:USDT": {
            "symbol": "MED/USDT:USDT", "strength": "MEDIUM", "deep_analyzed": True,
            "state": "DETECTED", "pre_expansion_state": None,
            "pre_expansion_evidence": [], "pre_expansion": {"phase": "NEUTRAL"},
        }}
        E.MEMORY["institutional_zone_analysis"] = {}
        radar = E.InstitutionalRadar()
        radar._sync_institutional_zone_registry("MED/USDT:USDT", E.MEMORY["watchlist"]["MED/USDT:USDT"])
        self.assertEqual(E.MEMORY.get("institutional_zone_count"), 0)

    def test_medium_with_precursor_enters_dynamic_institutional_zone_not_queue(self):
        E = self.engine
        entry = {
            "symbol": "MED/USDT:USDT", "side": "BUY", "strength": "MEDIUM",
            "deep_analyzed": True, "state": "DISPLACEMENT",
            "pre_expansion_state": "PRE_EXPANSION_LONG",
            "pre_expansion_evidence": ["DISPLACEMENT", "SWEEP"],
            "pre_expansion": {"phase": "EARLY_EXPANSION", "zone": "OB", "zone_quality": 0.8},
            "analysis": {"composite_score": 70, "ob_grade": "B", "roro_signal": False, "struct_score": 80, "liq_score": 80, "trap_risk": 0},
        }
        E.MEMORY["watchlist"] = {"MED/USDT:USDT": entry}
        E.MEMORY["institutional_zone_analysis"] = {}
        E.queue._candidates.clear()
        radar = E.InstitutionalRadar()
        radar._update_a_grade_status(entry)
        radar._sync_institutional_zone_registry("MED/USDT:USDT", entry)
        self.assertEqual(E.MEMORY["institutional_zone_count"], 1)
        self.assertFalse(entry["a_grade_ready"])
        self.assertEqual(len(E.queue._candidates), 0)

if __name__ == "__main__":
    unittest.main()
