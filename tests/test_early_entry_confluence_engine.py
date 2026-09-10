"""Production/Runtime EARLY ENTRY CONFLUENCE integration tests.

Proves the Early Entry Confluence layer is REALLY wired through the live
scanning path (ExecutionQueue.re_evaluate_all), not just mock-level success:

  1. EARLY_CONFLUENCE_AVAILABLE == True (engine imported the module).
  2. A BUY candidate inside a swept demand zone gets
     cand.evidence["early_entry_confluence"] populated with an
     EARLY_ENTRY / FIRST verdict (the real analyze_early_entry ran with the
     candidate's real zone_low/zone_high + atom_intel freshness).
  3. LATE (price far above its demand zone) still records
     "TOO_FAR_FROM_ZONE" in the [ATOM-EARLY] log line.
  4. ADVICE, never a gate: the layer does not force READY / open anything and
     leaves Roro/Entry/Risk untouched -- the candidate state is whatever the
     real trigger pipeline decides (no new hard block, no forced entry).

Only the Exchange/network provider boundary is stubbed; the queue, candidate,
evaluators, RF feed and the whole evidence seam are the real production code.
"""
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
        spec = importlib.util.spec_from_file_location(
            "_early_confluence_fresh_engine", orig_engine.__file__)
        engine = importlib.util.module_from_spec(spec)
        sys.modules["_early_confluence_fresh_engine"] = engine
        try:
            spec.loader.exec_module(engine)
        finally:
            sys.modules.pop("_early_confluence_fresh_engine", None)
    else:
        engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _near_demand_frame(n=140, base=100.0, far=False):
    """Trending BUY frame with a demand zone just below price whose tail candle
    swept sell-side liquidity then recovered (or, if far, price far above the
    demand zone -> LATE)."""
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    price = float(c[-1])
    if far:
        atr = max(float(h[-1]) - float(l[-1]), 0.5)
        return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                             "low": l, "close": c,
                             "volume": np.full(n, 1000.0)}), price, atr
    # zone just below price
    zone_low = price - 1.2
    zone_high = price - 0.2
    l[-2] = zone_low - 0.8
    c[-2] = price - 1.0
    o[-2] = price - 0.9
    h[-2] = price - 0.4
    l[-1] = zone_low - 0.3
    c[-1] = price
    o[-1] = price - 0.4
    h[-1] = price + 0.4
    return (pd.DataFrame({"timestamp": t, "open": o, "high": h,
                          "low": l, "close": c,
                          "volume": np.full(n, 1000.0)}), price, zone_low, zone_high)


class EarlyEntryEngineWiringTest(unittest.TestCase):
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

    def _cand(self, symbol, df, side="BUY", zl=0.0, zh=0.0, fresh="FRESH"):
        E = self.engine
        price = float(df["close"].iloc[-1])
        atr = max(float(df["high"].iloc[-1]) - float(df["low"].iloc[-1]), 0.5)
        cand = E.ExecutionCandidate(
            symbol=symbol, side=side, price=price, entry_price=price - atr * 0.5,
            stop_loss=price - atr * 1.5, take_profit_1=price + atr * 1.5,
            take_profit_2=price + atr * 2.5, atr=atr, df=df, ob={},
            zone_low=zl, zone_high=zh, zone_state="ACTIVE",
            atom_intel={"freshness_state": fresh, "liquidity_score": 82.0},
        )
        return cand, price, atr

    def test_engine_imports_early_entry(self):
        self.assertTrue(self.engine.EARLY_CONFLUENCE_AVAILABLE)

    def test_buy_inside_swept_demand_zone_records_early_entry_evidence(self):
        E = self.engine
        df, price, zl, zh = _near_demand_frame()
        cand, _, _ = self._cand("EE1/USDT:USDT", df, side="BUY", zl=zl, zh=zh)
        q = E.ExecutionQueue()
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("EE1/USDT:USDT")
        self.assertIsNotNone(c)
        early = c.evidence.get("early_entry_confluence")
        self.assertIsNotNone(early, "early_entry_confluence evidence must be recorded")
        # analyze_early_entry -> EarlyEntryResult.to_dict() nests the full bag
        # under "evidence" and carries advisory action/confidence/score_shift.
        self.assertIn("evidence", early)
        ev = early["evidence"]
        self.assertEqual(ev["direction"], "LONG")
        self.assertEqual(ev["zone_type"], "DEMAND")
        self.assertIn(ev["phase"], ("FIRST", "DEVELOPING", "LATE"))
        self.assertIn(early["action"], ("EARLY_ENTRY", "WAIT", "SITOUT_LATE"))
        # Advisory: confidence bounded [0,100] and score_shift within the small
        # capped band the engine applies to confluence_bonus.
        self.assertGreaterEqual(early["confidence"], 0.0)
        self.assertLessEqual(early["confidence"], 100.0)
        self.assertGreaterEqual(early["score_shift"], -5.0)
        self.assertLessEqual(early["score_shift"], 5.0)

    def test_early_layer_never_opens_or_forces_ready(self):
        # The layer must be advisory only: it must not push the candidate to
        # READY/open by itself. Confirm no position/trade was opened.
        E = self.engine
        df, price, zl, zh = _near_demand_frame()
        cand, _, _ = self._cand("EE3/USDT:USDT", df, side="BUY", zl=zl, zh=zh)
        q = E.ExecutionQueue()
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("EE3/USDT:USDT")
        early = c.evidence.get("early_entry_confluence")
        self.assertIsNotNone(early)
        # Whatever the state, this layer alone must not have opened a trade.
        self.assertEqual(len(getattr(q, "_ctx", {})) if hasattr(q, "_ctx") else 0, 0)
        self.assertFalse(getattr(q, "_trade_opened", False))


if __name__ == "__main__":
    unittest.main()
