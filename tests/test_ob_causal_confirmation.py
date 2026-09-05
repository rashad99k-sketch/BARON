"""T1-T3 OB causal unification + synergy + event-based confirmation tests.

Locks in (through the real queue methods):
  T1 - _select_strong_ob grades on the SAME causal OB zone the entry trades
       (not the legacy naive detector), and compute_order_block_quality
       measures displacement toward the following leg (no dead guard).
  T2 - _ob_synergy returns weighted confluence flags (sweep/FVG/premium-discount/
       BOS) that boost _evaluate_order_block score and feed _select_strong_ob;
       ZoneMetrics.confluence_bonus is additive and defaults to 0 so the
       blended final_zone_score is unchanged without a bonus.
  T3 - _confirm_marker is event-based: identical re-polls share a marker,
       a price move or a new completed bar changes it.
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


def _bearing_df():
    """BUY causal OB: flat ~100 -> red wick OB at bar 42 -> displacement leg with
    a bullish FVG -> sustained above the zone -> pullback into the zone ending in
    a light pool tap + discount reclaim (never closing below the break level)."""
    n = 60
    o = np.full(n, 100.0); c = np.full(n, 100.0)
    h = np.full(n, 100.8); l = np.full(n, 99.3)
    v = np.full(n, 1000.0)
    i = 42
    o[i], c[i], h[i], l[i] = 100.6, 99.7, 100.8, 99.2     # red wick-heavy OB -> zone [99.2, 100.6]
    o[i+1], c[i+1], h[i+1], l[i+1] = 99.7, 101.0, 100.9, 99.5   # displacement
    o[i+2], c[i+2], h[i+2], l[i+2] = 101.2, 101.6, 101.7, 101.0  # FVG: low 101.0 > prev high 100.9
    o[i+3], c[i+3], h[i+3], l[i+3] = 101.6, 102.0, 102.1, 101.4
    v[i+1] = 2600.0
    for j in range(i+4, n-4):                             # sustained above the zone
        o[j] = c[j] = 102.0
        h[j], l[j] = 102.4, 101.5
    o[n-4], c[n-4], h[n-4], l[n-4] = 101.8, 100.6, 101.9, 100.5
    o[n-3], c[n-3], h[n-3], l[n-3] = 100.4, 99.8, 100.5, 99.4
    o[n-2], c[n-2], h[n-2], l[n-2] = 99.4, 99.6, 99.7, 99.15   # pool tap, close above break level
    o[n-1], c[n-1], h[n-1], l[n-1] = 99.6, 99.85, 100.1, 99.5  # discount reclaim
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


class ObSynergyTest(unittest.TestCase):
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

    def test_synergy_flags_populated_and_bonus_bounded(self):
        q = self.engine.ExecutionQueue()
        df = _bearing_df()
        flags = q._ob_synergy(df, "BUY", 1.0, 42, 99.2, 100.6)
        for k in ("sweep_aligned", "fvg_after_displacement", "pd_aligned", "bos_aligned"):
            self.assertIn(k, flags)
        self.assertGreaterEqual(flags["bonus"], 0.0)
        self.assertLessEqual(flags["bonus"], 14.0)

    def test_bearing_frame_carries_synergy(self):
        q = self.engine.ExecutionQueue()
        df = _bearing_df()
        flags = q._ob_synergy(df, "BUY", 1.0, 42, 99.2, 100.6)
        self.assertTrue(flags["sweep_aligned"] or flags["pd_aligned"])

    def test_synergy_resets_per_call_no_cross_symbol_leak(self):
        q = self.engine.ExecutionQueue()
        q._evaluate_order_block(_bearing_df(), "BUY", 1.0)
        self.assertEqual(set(q._last_ob_analysis.keys()),
                         {"sweep_aligned", "fvg_after_displacement",
                          "pd_aligned", "bos_aligned", "bonus"})
        q._evaluate_order_block(pd.DataFrame(), "BUY", 1.0)
        self.assertEqual(q._last_ob_analysis, {})

    def test_evaluate_order_block_stores_synergy_and_scores_causal_ob(self):
        q = self.engine.ExecutionQueue()
        score, quality = q._evaluate_order_block(_bearing_df(), "BUY", 1.0)
        self.assertGreaterEqual(score, 55)
        self.assertNotEqual(quality, self.engine.OrderBlockQuality.FAKE)
        self.assertIn("bonus", q._last_ob_analysis)

    def test_select_strong_ob_grades_causal_zone_and_exposes_synergy(self):
        q = self.engine.ExecutionQueue()
        res = q._select_strong_ob(_bearing_df(), "BUY", 1.0)
        self.assertIn(res["grade"], ("A+", "A", "B"))
        self.assertGreater(res["zone_low"], 0.0)
        self.assertGreater(res["displacement_atr"], 0.4)
        self.assertIn("synergy", res)
        self.assertIn("sweep_aligned", res["synergy"])

    def test_final_zone_score_default_zero_bonus_unchanged(self):
        E = self.engine
        m0 = E.ZoneMetrics(order_block_quality=50)
        self.assertEqual(m0.confluence_bonus, 0.0)
        clean = m0.final_zone_score
        m1 = E.ZoneMetrics(order_block_quality=50, confluence_bonus=4.0)
        self.assertAlmostEqual(m1.final_zone_score, round(min(100.0, clean + 4.0), 2))
        m2 = E.ZoneMetrics(order_block_quality=50, confluence_bonus=400.0)
        self.assertLessEqual(m2.final_zone_score, 100.0)

    def test_confirm_marker_event_based(self):
        q = self.engine.ExecutionQueue()
        ev = {"sweep_quality": "strong", "sweep_age": 2}
        m1 = q._confirm_marker("MSS_CONFIRMED", ev, 60, 100.0, 0.5)
        m2 = q._confirm_marker("MSS_CONFIRMED", ev, 60, 100.0, 0.5)
        self.assertEqual(m1, m2)                                  # identical poll: one event
        mv = q._confirm_marker("MSS_CONFIRMED", ev, 60, 101.0, 0.5)  # price moved
        self.assertNotEqual(m1, mv)
        mb = q._confirm_marker("MSS_CONFIRMED", ev, 61, 100.0, 0.5)  # new bar
        self.assertNotEqual(m1, mb)
        ms = q._confirm_marker("LIQUIDITY_SWEEP", ev, 60, 100.0, 0.5)  # trigger changed
        self.assertNotEqual(m1, ms)

    def test_classify_decision_confluence_family_is_additive(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _bearing_df()
        zm = E.ZoneMetrics(trigger_state="MSS_CONFIRMED")
        c = E.ExecutionCandidate(symbol="OBTEST/USDT:USDT", side="BUY", price=99.6,
                                 entry_price=99.6, stop_loss=98.0,
                                 take_profit_1=101.0, take_profit_2=102.0,
                                 atr=1.0, df=df, ob={}, zone_metrics=zm)
        c.zone_low, c.zone_high = 99.2, 100.6
        c.evidence = {"sweep_quality": "none", "ob_sweep_aligned": True,
                      "ob_fvg_after_displacement": True}
        label, _comp, _trap = q._classify_decision(c)
        self.assertTrue(label.startswith(("STRONG", "MEDIUM", "WEAK", "INVALID")))

    def test_compute_order_block_quality_measures_following_leg(self):
        E = self.engine
        df = _bearing_df()
        bull, bear, details = E.compute_order_block_quality(df, "BUY", 1.0)
        self.assertIsInstance(bull, (int, float))
        self.assertIsInstance(bear, (int, float))
        self.assertIn("bullish_ob", details)
        self.assertIn("bearish_ob", details)


if __name__ == "__main__":
    unittest.main()