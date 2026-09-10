import importlib
import importlib.util
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
    # Re-execute the engine under a PRIVATE name so the shared core.engine
    # identity (bound by portfolio.manager and the live-brain harnesses) is
    # never evicted / orphaned mid-suite.
    orig_engine = saved["core.engine"]
    if orig_engine is not None:
        spec = importlib.util.spec_from_file_location("_inst_queue_fresh_engine", orig_engine.__file__)
        engine = importlib.util.module_from_spec(spec)
        sys.modules["_inst_queue_fresh_engine"] = engine
        try:
            spec.loader.exec_module(engine)
        finally:
            sys.modules.pop("_inst_queue_fresh_engine", None)
    else:
        engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


class InstitutionalQueueHardeningTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()

    @classmethod
    def tearDownClass(cls):
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper

    @staticmethod
    def _df(causal=True):
        n = 50
        close = np.full(n, 100.0)
        open_ = np.full(n, 100.0)
        high = np.full(n, 101.0)
        low = np.full(n, 99.0)
        volume = np.full(n, 1000.0)
        if causal:
            i = 35
            open_[i], close[i], high[i], low[i] = 105, 100, 106, 99
            open_[i + 1], close[i + 1], high[i + 1], low[i + 1] = 100, 110, 111, 99
            open_[i + 2], close[i + 2], high[i + 2], low[i + 2] = 109, 112, 113, 108
            volume[i + 1] = 2500
            close[i + 3] = 112
            open_[i + 3], high[i + 3], low[i + 3] = 111, 113, 110
            for j in range(i + 4, n):
                open_[j] = close[j] = 112
                high[j], low[j] = 113, 111
        return pd.DataFrame({"timestamp": np.arange(n), "open": open_, "high": high,
                             "low": low, "close": close, "volume": volume})

    def test_paper_mode_is_safe_by_default(self):
        self.assertTrue(self.engine.PAPER_MODE)
        self.assertFalse(self.engine.MODE_LIVE)

    def test_random_wick_is_not_promoted_to_real_order_block(self):
        queue = self.engine.ExecutionQueue()
        score, quality = queue._evaluate_order_block(self._df(False), "BUY", 2.0)
        self.assertLess(score, 40)
        self.assertEqual(quality, self.engine.OrderBlockQuality.FAKE)

    def test_causal_displacement_order_block_scores_as_fresh(self):
        queue = self.engine.ExecutionQueue()
        score, quality = queue._evaluate_order_block(self._df(True), "BUY", 2.0)
        self.assertGreaterEqual(score, 80)
        self.assertEqual(quality, self.engine.OrderBlockQuality.FRESH)

    def test_ready_requires_institutional_gate(self):
        queue = self.engine.ExecutionQueue()
        cand = self.engine.ExecutionCandidate(
            symbol="TEST/USDT:USDT", side="BUY", price=100, entry_price=100,
            stop_loss=98, take_profit_1=102, take_profit_2=104, atr=1,
            df=self._df(True), ob={},
        )
        cand.confirmation_count = 2
        # Validate the ADX band the same way the queue does (forensic RC#4):
        # execution rejects sub-band ADX, so a READY candidate must be in-band.
        # With no in-band ADX the candidate would be blocked on ADX, not on the
        # composite, which is precisely the mismatch the fix removes.
        cand.latest_adx = 30.0
        cand.latest_adx_bounds = [16.0, 55.0]
        cand.zone_metrics = self.engine.ZoneMetrics(
            order_block_quality=50, zone_strength=50, liquidity_quality=50,
            institutional_confidence=50, structure_alignment=50,
            entry_timing=90, trend_alignment=90, risk_score=90,
            trigger_state="MSS_CONFIRMED")
        cand.priority_score = cand.zone_metrics.final_zone_score
        queue._update_state(cand, 100)
        self.assertNotEqual(cand.state, self.engine.ExecutionState.READY)

        # when the composite reaches ≥75 with a confirmed trigger (and ADX in
        # band), READY fires
        cand.zone_metrics = self.engine.ZoneMetrics(
            order_block_quality=90, zone_strength=90, liquidity_quality=80,
            institutional_confidence=85, structure_alignment=85,
            entry_timing=90, trend_alignment=90, risk_score=90,
            trigger_state="MSS_CONFIRMED")
        cand.priority_score = cand.zone_metrics.final_zone_score
        queue._update_state(cand, 100)
        self.assertEqual(cand.state, self.engine.ExecutionState.READY)


if __name__ == "__main__":
    unittest.main()
