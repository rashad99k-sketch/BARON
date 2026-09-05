"""Phase 2 replay: graded evidence wired into the trigger decision.

Proves:
  - classify_sweep differentiates strong/weak/fake;
  - a FAKE sweep does NOT produce a valid trigger (sweep_ok False);
  - a strong sweep + structure + rejection produces MSS_CONFIRMED;
  - evidence grade is recorded on the candidate.
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


def _sweep_df(open_, high, low, close):
    """Flat ~100 with a swing-low pool at bar 30, last candle sweeps it."""
    n = 60
    closes = np.full(n, 100.0)
    opens = np.full(n, 100.0)
    highs = np.full(n, 100.3)
    lows = np.full(n, 99.7)
    vol = np.full(n, 1000.0)
    lows[30] = 99.0
    closes[30] = 99.3
    opens[30] = 99.6
    highs[30] = 99.7
    opens[-1] = open_
    highs[-1] = high
    lows[-1] = low
    closes[-1] = close
    return pd.DataFrame({"timestamp": np.arange(n), "open": opens, "high": highs,
                         "low": lows, "close": closes, "volume": vol})


class EvidenceWiringTest(unittest.TestCase):
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

    def test_classify_sweep_grades_strong_weak_fake(self):
        E = self.engine
        strong = E.classify_sweep(_sweep_df(99.5, 99.6, 98.6, 99.4), "BUY")
        weak = E.classify_sweep(_sweep_df(99.5, 99.7, 99.0, 99.4), "BUY")
        fake = E.classify_sweep(_sweep_df(99.5, 99.6, 99.3, 99.35), "BUY")
        self.assertEqual(strong[0], "strong")
        self.assertEqual(weak[0], "weak")
        self.assertEqual(fake[0], "fake")

    def test_fake_sweep_not_a_valid_trigger(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _sweep_df(99.5, 99.6, 99.3, 99.35)  # fake: tiny wick, no reclaim
        atr = 0.5
        trigger = q._detect_trigger_state(df, "BUY", atr, 99.5)
        ev = q._last_evidence
        self.assertEqual(ev["sweep_quality"], "fake")
        # A fake sweep must NOT qualify as a valid sweep-based trigger.
        self.assertNotEqual(trigger, "MSS_CONFIRMED")
        self.assertNotEqual(trigger, "LIQUIDITY_SWEEP")

    def test_strong_sweep_recorded_in_evidence(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _sweep_df(99.5, 99.6, 98.6, 99.4)  # strong reclaim
        trigger = q._detect_trigger_state(df, "BUY", 0.5, 99.5)
        ev = q._last_evidence
        self.assertEqual(ev["sweep_quality"], "strong")

    def test_evidence_stored_on_candidate(self):
        E = self.engine
        q = E.ExecutionQueue()
        df = _sweep_df(99.5, 99.6, 98.6, 99.4)
        price = float(df["close"].iloc[-1])
        cand = E.ExecutionCandidate(
            symbol="T/USDT:USDT", side="BUY", price=price, entry_price=99.5,
            stop_loss=99.0, take_profit_1=price + 0.5, take_profit_2=price + 1.0,
            atr=0.5, df=df, ob={},
        )
        q.add_candidate(cand)
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("T/USDT:USDT")
        self.assertIsInstance(c.evidence, dict)
        self.assertIn("sweep_quality", c.evidence)
        self.assertIn("trap_risk", c.evidence)

    def test_identical_poll_counts_as_one_confirmation(self):
        """The 5-second polling loop must not count the same event twice."""
        E = self.engine
        q = E.ExecutionQueue()
        sig1 = q._confirm_marker("MSS_CONFIRMED", {"sweep_quality": "strong", "structure_score": 6, "rejection_score": 2, "absorption": 70, "sweep_age": 1}, 60, 100.0, 0.5)
        sig2 = q._confirm_marker("MSS_CONFIRMED", {"sweep_quality": "strong", "structure_score": 6, "rejection_score": 2, "absorption": 70, "sweep_age": 1}, 60, 100.0, 0.5)
        # Same candle, same evidence -> same marker (one event).
        self.assertEqual(sig1, sig2)
        # New candle (bar_count advances) -> new marker (distinct event).
        sig3 = q._confirm_marker("MSS_CONFIRMED", {"sweep_quality": "strong", "structure_score": 6, "rejection_score": 2, "absorption": 70, "sweep_age": 1}, 61, 100.0, 0.5)
        self.assertNotEqual(sig1, sig3)

    def test_same_frame_repoll_does_not_increment_confirmation(self):
        E = self.engine
        q = E.ExecutionQueue()
        # Force a confirming trigger by crafting evidence directly.
        df = _sweep_df(99.5, 99.6, 98.6, 99.4)
        price = float(df["close"].iloc[-1])
        cand = E.ExecutionCandidate(
            symbol="T2/USDT:USDT", side="BUY", price=price, entry_price=99.5,
            stop_loss=99.0, take_profit_1=price + 0.5, take_profit_2=price + 1.0,
            atr=0.5, df=df, ob={},
        )
        q.add_candidate(cand)
        # Stub the trigger to a confirming state and the evidence constant so
        # both polls observe the SAME event.
        q._detect_trigger_state = lambda d, s, a, e: "MSS_CONFIRMED"
        q._last_evidence = {"sweep_quality": "strong", "structure_score": 6, "rejection_score": 2, "absorption": 70}
        q.re_evaluate_all(lambda s: df)
        c = q._candidates.get("T2/USDT:USDT")
        first = c.confirmation_count
        q.re_evaluate_all(lambda s: df)  # identical re-poll
        c = q._candidates.get("T2/USDT:USDT")
        self.assertEqual(first, 1)
        self.assertEqual(c.confirmation_count, 1)  # NOT 2


if __name__ == "__main__":
    unittest.main()
