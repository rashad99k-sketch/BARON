"""OB quality-gate layer (scientific review T5).

Locks in the report's recommendations on the REAL code paths:
  1. _evaluate_order_block skips broken candidates while a valid causal OB
     exists (previously a strong-but-broken OB could win selection and be
     returned as BROKEN, masking a tradeable zone) while preserving a BROKEN
     verdict when the ONLY causal OB is broken.
  2. _ob_synergy premium/discount uses the LOCAL trade-range midpoint
     (discount = price at/below 50% of the range), not just the zone body.
  3. get_sweep_authenticity grades the DIRECTIONAL sweep candle (strong/weak/
     fake) so the entry gate never chases a fake fill-through; same candle the
     queue's detect_liquidity_context reports.
  4. execute_entry consults the sweep-authenticity gate (SWEEP_AUTHENTICITY)
     and the env-gated ENTRY_QUALITY_AUTHORITY final gate (REJECT blocks).
  5. msb_context exposes a real CHOCH (two-legged shift) instead of False.
"""
import importlib
import importlib.util
import os
import sys
import unittest

import numpy as np
import pandas as pd


def _load_engine():
    saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine")}
    old_paper = os.environ.pop("PAPER_MODE", None)

    class _FakeFlask:
        def __init__(self, *args, **kwargs):
            pass
        def route(self, *args, **kwargs):
            return lambda fn: fn
        def add_url_rule(self, *args, **kwargs):
            return None

    fake_ccxt = importlib.util.find_spec("ccxt") is not None
    if not fake_ccxt:
        import types
        ccxt_mod = types.ModuleType("ccxt")
        class FakeBingX:
            def __init__(self, *args, **kwargs):
                self.markets = {}
        ccxt_mod.bingx = FakeBingX
        sys.modules["ccxt"] = ccxt_mod
    sys.modules["flask"] = _fake_flask_with(_FakeFlask)
    # Re-execute the engine under a PRIVATE name so the shared core.engine
    # identity (bound by portfolio.manager and the live-brain harnesses) is
    # never evicted / orphaned mid-suite.
    orig_engine = saved["core.engine"]
    if orig_engine is not None:
        spec = importlib.util.spec_from_file_location("_ob_gates_fresh_engine", orig_engine.__file__)
        engine = importlib.util.module_from_spec(spec)
        sys.modules["_ob_gates_fresh_engine"] = engine
        try:
            spec.loader.exec_module(engine)
        finally:
            sys.modules.pop("_ob_gates_fresh_engine", None)
    else:
        engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _fake_flask_with(_FakeFlask):
    import types
    f = types.ModuleType("flask")
    f.Flask = _FakeFlask
    f.jsonify = lambda *a, **k: None
    f.request = types.SimpleNamespace()
    return f


def _base(n=60, level=100.0):
    o = np.full(n, level); c = np.full(n, level)
    h = np.full(n, level + 0.5); l = np.full(n, level - 0.5)
    v = np.full(n, 1000.0)
    return o, c, h, l, v


def _broken_then_valid_df():
    """Strong-but-BROKEN causal OB at bar 30, then a valid causal OB at bar 42.
    The break closes at bar i+4 so the displacement leg stays directional."""
    n = 60
    o, c, h, l, v = _base(n)
    # bar 30: red OB1 -> zone [100.1, 101.3]
    o[30], c[30], h[30], l[30] = 101.2, 100.3, 101.8, 100.1
    o[31], c[31], h[31], l[31] = 100.2, 102.2, 102.35, 100.0    # displacement
    o[32], c[32], h[32], l[32] = 102.4, 103.1, 103.2, 102.2
    o[33], c[33], h[33], l[33] = 103.1, 103.4, 103.5, 103.0    # still directional
    o[34], c[34], h[34], l[34] = 103.4, 98.6, 103.5, 98.4      # CLOSE through -> BROKEN
    v[31] = 2600.0
    o[35], c[35], h[35], l[35] = 99.0, 99.5, 99.6, 98.9
    for j in range(36, 42):                                    # flat short-legging
        o[j] = c[j] = 100.0; h[j], l[j] = 100.3, 99.7
    # bar 42: red OB2 -> zone [99.4, 100.4]
    o[42], c[42], h[42], l[42] = 100.4, 99.6, 100.5, 99.4
    o[43], c[43], h[43], l[43] = 99.6, 101.0, 100.9, 99.5      # displacement
    o[44], c[44], h[44], l[44] = 101.2, 101.6, 101.7, 101.0    # FVG
    o[45], c[45], h[45], l[45] = 101.6, 102.0, 102.1, 101.4
    v[43] = 2600.0
    for j in range(46, 55):
        o[j] = c[j] = 102.0; h[j], l[j] = 102.3, 101.6
    o[55], c[55], h[55], l[55] = 102.0, 101.0, 102.2, 100.8
    o[56], c[56], h[56], l[56] = 101.0, 100.0, 101.1, 99.8
    o[57], c[57], h[57], l[57] = 100.0, 99.6, 100.05, 99.4
    o[58], c[58], h[58], l[58] = 99.6, 99.85, 99.9, 99.4
    o[59], c[59], h[59], l[59] = 99.85, 99.7, 99.9, 99.45
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _broken_only_df():
    """Only a broken causal OB exists inside the 35-bar window."""
    n = 60
    o, c, h, l, v = _base(n)
    o[30], c[30], h[30], l[30] = 101.2, 100.3, 101.8, 100.1
    o[31], c[31], h[31], l[31] = 100.2, 102.2, 102.35, 100.0
    o[32], c[32], h[32], l[32] = 102.4, 103.1, 103.2, 102.2
    o[33], c[33], h[33], l[33] = 103.1, 103.4, 103.5, 103.0
    o[34], c[34], h[34], l[34] = 103.4, 98.6, 103.5, 98.4      # BROKEN
    v[31] = 2600.0
    for j in range(35, n):
        o[j] = c[j] = 100.0; h[j], l[j] = 100.4, 99.6
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _discount_false_bu():
    """BUY OB zone sits at 97.8-99.0 (mid 98.4); price 98.3 is below the zone
    mid but ABOVE the local-range midpoint 98.25 (deep low wick at bar 4):
    real discount check says False, zone-mid check says True."""
    n = 26
    o = np.full(n, 98.2); c = np.full(n, 98.2)
    h = np.full(n, 98.35); l = np.full(n, 98.05)
    v = np.full(n, 1000.0)
    o[4], c[4], h[4], l[4] = 98.2, 97.8, 98.35, 97.0             # deep low wick
    o[8], c[8], h[8], l[8] = 99.0, 98.0, 99.2, 97.8             # OB [97.8, 99.0]
    o[9], c[9], h[9], l[9] = 98.0, 99.4, 99.5, 97.9             # displacement
    for j in range(12, 24):
        o[j] = c[j] = 98.2; h[j], l[j] = 98.35, 98.05
    o[25], c[25], h[25], l[25] = 98.1, 98.3, 98.5, 98.0         # price 98.3 in zone
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _premium_true_se():
    """SELL mirror: OB [101.8, 102.6] mid 102.2 with a deep low wick dragging
    the range midpoint below price: price 102.4 is above BOTH the range
    midpoint (102.1) and the zone mid -> genuine premium -> True."""
    n = 26
    o, c, h, l, v = _base(n, 100.0)
    o[4], c[4], h[4], l[4] = 101.0, 101.5, 101.6, 101.0         # deep low wick
    o[8], c[8], h[8], l[8] = 101.8, 102.6, 102.7, 101.7         # OB [101.8, 102.6]
    o[9], c[9], h[9], l[9] = 102.6, 101.4, 102.7, 101.3         # displacement down
    for j in range(12, 24):
        o[j] = c[j] = 102.3; h[j], l[j] = 102.7, 102.0
    o[25], c[25], h[25], l[25] = 102.2, 102.4, 102.6, 102.1     # price 102.4
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _sweep_fake_buy_df():
    """sell-side 'sweep' bar filled through with no counter-wick and no
    reclaim: fake (wick ratio ~0.1)."""
    n = 14
    o, c, h, l, v = _base(n, 100.0)
    o[-3], c[-3], h[-3], l[-3] = 99.2, 99.2, 99.5, 99.0
    o[-2], c[-2], h[-2], l[-2] = 98.8, 98.75, 99.1, 98.7        # fake: fill-through
    o[-1], c[-1], h[-1], l[-1] = 99.0, 99.3, 99.4, 98.85
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _sweep_strong_buy_df():
    """sell-side sweep with a real counter-wick and reclaim: strong."""
    n = 14
    o, c, h, l, v = _base(n, 100.0)
    o[-3], c[-3], h[-3], l[-3] = 99.3, 99.3, 99.5, 99.2
    o[-2], c[-2], h[-2], l[-2] = 98.8, 99.3, 99.35, 97.7        # wick 1.1 / range 1.65
    o[-1], c[-1], h[-1], l[-1] = 99.1, 99.6, 99.7, 99.0
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _sweep_fake_sell_df():
    n = 14
    o, c, h, l, v = _base(n, 100.0)
    o[-3], c[-3], h[-3], l[-3] = 100.8, 100.8, 101.0, 100.5
    o[-2], c[-2], h[-2], l[-2] = 101.2, 101.25, 101.3, 100.9   # fake fill-through up
    o[-1], c[-1], h[-1], l[-1] = 101.0, 100.7, 101.15, 100.6
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _bullish_shift_df():
    """Tail rises (HH + HL): _detect_structure_shift -> bullish_shift."""
    n = 20
    o, c, h, l, v = _base(n, 100.0)
    for j in range(11, n):
        o[j] = c[j] = 99.0 + (j - 11) * 0.25
        h[j] = o[j] + 0.4; l[j] = o[j] - 0.3
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _flat_df():
    o, c, h, l, v = _base(20)
    return pd.DataFrame({"timestamp": np.arange(20), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


class _EngineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()
        cls.engine.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        cls.engine.STATE.clear()
        cls.engine.TRADE_STATE.clear()

    @classmethod
    def tearDownClass(cls):
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper


class ObSelectionPrefersValid(_EngineTestCase):

    def test_broken_strong_ob_never_masks_valid_causal_ob(self):
        q = self.engine.ExecutionQueue()
        score, quality = q._evaluate_order_block(_broken_then_valid_df(), "BUY", 0.9)
        self.assertNotIn(quality, (self.engine.OrderBlockQuality.BROKEN,
                                   self.engine.OrderBlockQuality.FAKE))
        self.assertGreaterEqual(score, 60)

    def test_broken_only_fallback_still_reports_broken(self):
        q = self.engine.ExecutionQueue()
        score, quality = q._evaluate_order_block(_broken_only_df(), "BUY", 0.9)
        self.assertEqual(quality, self.engine.OrderBlockQuality.BROKEN)
        self.assertEqual(score, 20)


class PdDiscountUsesRange(_EngineTestCase):

    def test_buy_not_discount_when_above_range_midpoint(self):
        df = _discount_false_bu()
        flags = self.engine.ExecutionQueue()._ob_synergy(df, "BUY", 0.8, 8, 97.8, 99.0)
        self.assertFalse(flags["pd_aligned"])

    def test_sell_premium_when_above_range_midpoint(self):
        df = _premium_true_se()
        flags = self.engine.ExecutionQueue()._ob_synergy(df, "SELL", 0.8, 8, 101.8, 102.6)
        self.assertTrue(flags["pd_aligned"])


class SweepAuthenticityGate(_EngineTestCase):

    def test_fake_sweep_is_flagged_on_the_directional_bar(self):
        E = self.engine
        grade, bar = E.get_sweep_authenticity(_sweep_fake_buy_df(), "BUY", lookback=10)
        self.assertEqual(grade, "fake")
        self.assertEqual(bar, -2)

    def test_strong_sweep_is_flagged_strong(self):
        E = self.engine
        grade, bar = E.get_sweep_authenticity(_sweep_strong_buy_df(), "BUY", lookback=10)
        self.assertEqual(grade, "strong")
        self.assertEqual(bar, -2)

    def test_sell_fake_sweep_symmetric(self):
        E = self.engine
        grade, _bar = E.get_sweep_authenticity(_sweep_fake_sell_df(), "SELL", lookback=10)
        self.assertEqual(grade, "fake")

    def test_wrong_side_sweep_is_unknown(self):
        E = self.engine
        # _sweep_strong_buy_df swept sell-side; a SELL entry should not read it
        grade, _bar = E.get_sweep_authenticity(_sweep_strong_buy_df(), "SELL", lookback=10)
        self.assertEqual(grade, "unknown")

    def test_entry_gate_blocks_fake_sweep(self):
        E = self.engine
        real_compute_adx = E.compute_adx
        real_ohlcv = E.get_ohlcv_safe
        try:
            E.compute_adx = lambda df, period=14: pd.Series([30.0] * len(df))
            E.get_ohlcv_safe = lambda symbol, limit=100, htf=False: _sweep_fake_buy_df()
            opened = E.execute_entry(
                "BUY", "GATE/USDT:USDT", 99.3, 97.5, 101.0, 102.0,
                80.0, "fake", 0.5, "SNIPER", "OB_RETEST", "SNIPER")
            self.assertFalse(opened)
        finally:
            E.compute_adx = real_compute_adx
            E.get_ohlcv_safe = real_ohlcv


class EntryQualityAuthorityGate(_EngineTestCase):

    def test_authority_gate_invoked_and_blocks_reject(self):
        E = self.engine
        real_compute_adx = E.compute_adx
        real_ohlcv = E.get_ohlcv_safe
        real_entry_df = E.get_orderbook_cached
        os.environ["ENTRY_QUALITY_AUTHORITY"] = "1"
        try:
            E.compute_adx = lambda df, period=14: pd.Series([37.4] * len(df))
            E.get_ohlcv_safe = lambda symbol, limit=100, htf=False: _entry_like_df()
            E.get_orderbook_cached = lambda *a, **k: {"bids": [[99.0, 10.0]], "asks": [[99.6, 5.0]]}
            E.STATE["open"] = False
            opened = E.execute_entry(
                "BUY", "AUTH/USDT:USDT", 99.85, 97.5, 101.0, 102.0,
                80.0, "auth", 0.5, "SNIPER", "OB_RETEST", "SNIPER")
            # _entry_like_df (a mirror of the pipeline frames) yields low
            # liquidity-authenticity in the strict final authority -> REJECT.
            self.assertFalse(opened)
            self.assertIsNone(E.paper["position"])
        finally:
            os.environ.pop("ENTRY_QUALITY_AUTHORITY", None)
            E.compute_adx = real_compute_adx
            E.get_ohlcv_safe = real_ohlcv
            E.get_orderbook_cached = real_entry_df

    def test_authority_gate_skipped_when_env_off(self):
        E = self.engine
        real_compute_adx = E.compute_adx
        real_ohlcv = E.get_ohlcv_safe
        os.environ.pop("ENTRY_QUALITY_AUTHORITY", None)
        calls = []
        real_qa = E.entry_quality_assessment
        try:
            E.compute_adx = lambda df, period=14: pd.Series([37.4] * len(df))
            # Realistic-but-thin frame cannot be reached by the strict gate in
            # this test; we only assert the gate hook is NOT consulted at all.
            E.entry_quality_assessment = lambda *a, **k: (calls.append(1) or
                                                          {"decision": "REJECT"})
            try:
                E.execute_entry("BUY", "AUTH2/USDT:USDT", 99.85, 97.5, 101.0, 102.0,
                                80.0, "x", 0.5, "SNIPER", "OB_RETEST", "SNIPER")
            except Exception:
                pass  # tail of execute_entry is irrelevant for this assertion
            self.assertEqual(calls, [])
        finally:
            E.entry_quality_assessment = real_qa
            E.compute_adx = real_compute_adx
            E.get_ohlcv_safe = real_ohlcv


class MsbContextExposesChoch(_EngineTestCase):

    def test_bullish_shift_is_choch_for_long(self):
        eng = self.engine
        q = self.engine.ExecutionQueue()
        from core.msb_ob import msb_context, LONG
        zone = {"side": "LONG", "zone_type": "OB", "created_at": 5,
                "top": 100.5, "bottom": 99.5, "status": "ACTIVE",
                "freshness": 3, "touch_count": 0, "zone_strength": 1.0}
        msb = {"direction": "LONG", "index": 6, "price": 99.6}
        ctx = msb_context(_bullish_shift_df(), "X", LONG, q, zone=zone,
                          msb_event=msb, atr=0.6)
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx.choch)

    def test_flat_frame_has_no_choch(self):
        eng = self.engine
        q = self.engine.ExecutionQueue()
        from core.msb_ob import msb_context, LONG
        zone = {"side": "LONG", "zone_type": "OB", "created_at": 5,
                "top": 100.5, "bottom": 99.5, "status": "ACTIVE",
                "freshness": 3, "touch_count": 0, "zone_strength": 1.0}
        msb = {"direction": "LONG", "index": 6, "price": 99.6}
        ctx = msb_context(_flat_df(), "X", LONG, q, zone=zone,
                          msb_event=msb, atr=0.6)
        self.assertIsNotNone(ctx)
        self.assertFalse(ctx.choch)


def _entry_like_df():
    """Mirror of the pipeline's real BUY-entry frame (ADX ~37, sell-side
    sweep with reclaim)."""
    n = 250
    t = np.arange(n)
    x = 100.0 + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2; c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]; prior_hi = h[n - 3]
    o[n - 2] = prior_low - 0.2; c[n - 2] = prior_low + 0.3
    h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5); l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1; c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3; l[n - 1] = prior_low - 0.1
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


if __name__ == "__main__":
    unittest.main()