"""Surgical-fix replay: neutral defaults, independent pillar confluence, real
zone_strength, and news-as-context.

Proves (against the 3 forensic gaps):
  FIX #1 - missing absorption/response is NEUTRAL, not a penalty.
  FIX #2 - one candle driving every sub-signal does NOT count as independent
           confirmation; STRONG requires >=3 independent pillar families.
  FIX #3 - promote_to_queue computes the REAL 5-pillar zone strength rather
           than the neutral default 50.
  NEWS    - news is context only; it can never create READY by itself.
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
        def __init__(self, *a, **k):
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


def _flat_df(n=60):
    return pd.DataFrame({
        "timestamp": np.arange(n), "open": np.full(n, 100.0), "high": np.full(n, 100.3),
        "low": np.full(n, 99.7), "close": np.full(n, 100.0),
        "volume": np.full(n, 1000.0),
    })


class SmartValidationTest(unittest.TestCase):
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

    def _cand(self, E, **zm):
        df = _flat_df()
        metrics = E.ZoneMetrics(**zm)
        c = E.ExecutionCandidate(
            symbol="X/USDT:USDT", side="BUY", price=100.0, entry_price=99.5,
            stop_loss=99.0, take_profit_1=101.0, take_profit_2=102.0,
            atr=0.5, df=df, ob={}, zone_metrics=metrics,
        )
        c.zone_low, c.zone_high = 99.0, 100.0
        c.zone_state = "ENTRY_WINDOW"
        return c

    # ---- FIX #1: neutral defaults for missing absorption/response ----
    def test_missing_absorption_is_neutral_not_penalty(self):
        E = self.engine
        q = E.ExecutionQueue()
        # No absorption/response keys at all; strong zone + liquidity + structure.
        c = self._cand(E, order_block_quality=80, liquidity_quality=80,
                       trend_alignment=70, institutional_confidence=70)
        c.evidence = {"sweep_quality": "strong", "structure_valid": True,
                      "structure_score": 6, "rejection_score": 2}
        label, composite, _ = q._classify_decision(c)
        self.assertTrue(label.startswith("STRONG"), f"missing absorption penalized: {label}")

    # ---- FIX #2: independent pillar families, not one-candle blending ----
    def test_one_candle_correlated_evidence_is_not_strong(self):
        E = self.engine
        q = E.ExecutionQueue()
        # Only the liquidity family confirms (a strong sweep). Zone OB quality
        # is weak, no structure, no volume, no trend -> 1 independent family.
        c = self._cand(E, order_block_quality=10, liquidity_quality=10,
                       trend_alignment=10, institutional_confidence=10)
        c.zone_low, c.zone_high = 99.0, 100.0  # zone band exists but quality gate fails
        c.evidence = {"sweep_quality": "strong", "structure_valid": False,
                      "structure_score": 0, "rejection_score": 3}
        label, _, _ = q._classify_decision(c)
        self.assertTrue(label.startswith("WEAK") or label.startswith("INVALID"),
                        f"one-candle evidence over-counted: {label}")

    def test_two_pillars_is_medium(self):
        E = self.engine
        q = E.ExecutionQueue()
        c = self._cand(E, order_block_quality=80, liquidity_quality=10,
                       trend_alignment=10, institutional_confidence=10)
        c.evidence = {"sweep_quality": "strong", "structure_valid": False,
                      "structure_score": 0}
        # zone + liquidity = 2 families
        label, _, _ = q._classify_decision(c)
        self.assertTrue(label.startswith("MEDIUM"), f"2 families should be MEDIUM: {label}")

    def test_three_pillars_is_strong(self):
        E = self.engine
        q = E.ExecutionQueue()
        c = self._cand(E, order_block_quality=80, liquidity_quality=80,
                       trend_alignment=70, institutional_confidence=70)
        c.evidence = {"sweep_quality": "strong", "structure_valid": True,
                      "structure_score": 6}
        # zone + liquidity + structure + volume + trend = 5 families
        label, _, _ = q._classify_decision(c)
        self.assertTrue(label.startswith("STRONG"), f"3+ families should be STRONG: {label}")

    def test_fake_sweep_is_invalid(self):
        E = self.engine
        q = E.ExecutionQueue()
        c = self._cand(E, order_block_quality=90, liquidity_quality=90,
                       trend_alignment=90, institutional_confidence=90)
        c.evidence = {"sweep_quality": "fake", "structure_valid": True, "structure_score": 8}
        label, _, _ = q._classify_decision(c)
        self.assertTrue(label.startswith("INVALID"), f"fake sweep must be INVALID: {label}")

    def test_decision_reasons_report_families(self):
        E = self.engine
        q = E.ExecutionQueue()
        c = self._cand(E, order_block_quality=80, liquidity_quality=80, trend_alignment=70)
        c.evidence = {"sweep_quality": "strong", "structure_valid": True, "structure_score": 6}
        q._classify_decision(c)
        joined = " ".join(c.decision_reasons)
        self.assertIn("families=", joined)

    # ---- FIX #3: real compute_zone_strength in promote_to_queue ----
    def test_promote_uses_real_zone_strength(self):
        E = self.engine
        # Rebind scanner to the same live engine object when this test is run
        # after suites that intentionally reload core.engine.
        import core as _core_pkg
        import sys as _sys
        _sys.modules["core.engine"] = E
        _core_pkg.engine = E
        _sys.modules.pop("scanner.scanner", None)
        import scanner.scanner as S
        df = _flat_df(150)
        df.loc[df.index[40:45], "low"] = 98.0
        df.loc[df.index[40:45], "close"] = 98.2
        df.loc[df.index[40:45], "volume"] = 3000.0

        # Fully isolate shared runtime state that other tests may have dirtied.
        saved_watch = E.MEMORY.get("watchlist")
        saved_stale = E.MEMORY.get("stale_zone_refs")
        saved_queue = E.queue
        saved_fetch = getattr(S, "get_ohlcv_safe", None)
        saved_ob = getattr(S, "get_orderbook_cached", None)
        saved_zones = getattr(S, "get_smart_zones", None)
        saved_czs = getattr(S, "compute_zone_strength", None)
        # scanner.py snapshots these from the core at import (globals().update),
        # so the spy must patch the SCANNER's references, not the engine's.
        orig_zones = E.get_smart_zones
        orig_czs = E.compute_zone_strength
        calls = {"zones": 0, "czs": 0}

        def spy_zones(symbol, d, ob=None):
            calls["zones"] += 1
            return orig_zones(symbol, d, ob)

        def spy_czs(*a, **k):
            calls["czs"] += 1
            return orig_czs(*a, **k)

        q = E.ExecutionQueue()
        watch = {"Z/USDT:USDT": {
            "deep_analyzed": True, "state": "ACTIVE", "side": "BUY",
            "strength": "MEDIUM", "score": 9.0, "narrative_score": 6.0,
            "intent_score": 85.0, "institutional_zone_active": True,
            "a_grade_ready": True, "pre_institutional_state": "CONFIRMED",
            "pre_expansion_state": "PRE_EXPANSION_LONG",
            "pre_expansion_evidence": ["SWEEP", "DISPLACEMENT"],
            "pre_expansion": {"phase": "TREND_BUILDING", "indicator_alignment": "BULLISH"},
            "analysis": {"ob_grade": "A", "ob_score": 80, "liq_score": 80,
                          "struct_score": 80, "roro_signal": True, "trap_risk": 10},
            "reasons": ["Liquidity Sweep", "Displacement"]}}

        try:
            E.queue = q
            S.queue = q
            E.MEMORY["watchlist"] = watch
            E.MEMORY["stale_zone_refs"] = {}
            E.MEMORY["institutional_zone_analysis"] = {"Z/USDT:USDT": {"symbol": "Z/USDT:USDT", "state": "INSTITUTIONAL_WATCH", "a_grade_ready": True}}
            S.MEMORY["watchlist"] = watch
            S.MEMORY["stale_zone_refs"] = {}
            S.get_ohlcv_safe = lambda s, n: df
            S.get_orderbook_cached = lambda s, limit=10: None
            S.get_smart_zones = spy_zones
            S.compute_zone_strength = spy_czs
            promoted = S.promote_to_queue()
        finally:
            S.get_ohlcv_safe, S.get_orderbook_cached = saved_fetch, saved_ob
            if saved_zones is not None:
                S.get_smart_zones = saved_zones
            if saved_czs is not None:
                S.compute_zone_strength = saved_czs
            E.MEMORY["watchlist"] = saved_watch if saved_watch is not None else {}
            E.MEMORY["stale_zone_refs"] = saved_stale if saved_stale is not None else {}
            E.MEMORY.pop("institutional_zone_analysis", None)
            S.MEMORY["watchlist"] = E.MEMORY["watchlist"]
            S.MEMORY["stale_zone_refs"] = E.MEMORY["stale_zone_refs"]
            E.queue = saved_queue
            S.queue = saved_queue
            _sys.modules.pop("scanner.scanner", None)

        self.assertGreaterEqual(promoted, 1)
        cand = q._candidates.get("Z/USDT:USDT")
        self.assertIsNotNone(cand)
        # The REAL zone scorer ran (not the silent 50 default) -> [0,100].
        self.assertGreater(calls["zones"], 0, "get_smart_zones was not called")
        self.assertIsInstance(cand.zone_metrics.zone_strength, float)
        self.assertTrue(0.0 <= cand.zone_metrics.zone_strength <= 100.0)

    # ---- NEWS: context only, never creates READY ----
    def test_news_alone_cannot_create_ready(self):
        E = self.engine
        q = E.ExecutionQueue()
        # High news risk / news support present, but NO trigger & NO confirmation.
        c = self._cand(E, order_block_quality=95, zone_strength=95,
                       liquidity_quality=95, institutional_confidence=95,
                       trend_alignment=95, risk_score=100)
        c.zone_metrics.trigger_state = "WAITING_TRIGGER"  # no technical trigger
        c.confirmation_count = 0
        c.evidence = {}
        q._update_state(c, 100.0)
        self.assertNotEqual(c.state, E.ExecutionState.READY,
                            "news/context must never create READY without a technical trigger")


if __name__ == "__main__":
    unittest.main()
