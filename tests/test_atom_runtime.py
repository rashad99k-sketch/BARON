"""Runtime integration tests for the Atom Intelligence Layer (A-F scenarios).

Loads the real engine (with stubbed ccxt/flask as the existing tests do) and
exercises the Roro Entry -> Atom Approval pipeline end to end:

  A  TREND valid         : Roro timing + BOS + fresh zone + liquidity support
                           -> TRADE_TYPE=TREND, READY without extra Sniper.
  B  REVERSAL valid      : sweep + MSS/CHoCH + rejection + fresh OB + liquidity
                           -> TRADE_TYPE=REVERSAL, READY only with Sniper confirm.
  C  Fake/Stale OB       : Roro timing present but OB weak / zone over-mitigated
                           -> atom_hard_reject, never READY.
  D  TREND pullback      : a pullback WITH the impulse must classify TREND (not
                           REVERSAL) and management holds (TREND_RIDE).
  E  Liquidity grab      : genuine grab + liquidity support -> not rejected, held.
  F  Exhaustion / thesis : distribution/exhaustion -> aggressive profit-lock.

Notes on seams: the heavy market internals (sweep detection, OB scoring, ADX
realism on tiny synthetic frames) are stubbed so the ONLY variable under test is
the ATOM intelligence integration + READY-gate differentiation. The ATOM logic
itself (classification, freshness, liquidity, sniper-for-reversal, approval) is
the real implementation.
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
    saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine", "core.sniper_enrichment")}
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
    # Re-execute the engine under a private name so its module globals are
    # isolated WITHOUT evicting the shared `core.engine` identity that the
    # live-brain harnesses (and portfolio.manager) bind at import time.
    orig_engine = saved["core.engine"]
    if orig_engine is not None:
        spec = importlib.util.spec_from_file_location("_atom_fresh_engine", orig_engine.__file__)
        engine = importlib.util.module_from_spec(spec)
        sys.modules["_atom_fresh_engine"] = engine
        try:
            spec.loader.exec_module(engine)
        finally:
            sys.modules.pop("_atom_fresh_engine", None)
    else:
        engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _frame(o, c, h, l, vol):
    n = len(o)
    return pd.DataFrame({"timestamp": np.arange(n), "open": np.asarray(o, dtype=float),
                         "high": np.asarray(h, dtype=float), "low": np.asarray(l, dtype=float),
                         "close": np.asarray(c, dtype=float), "volume": np.asarray(vol, dtype=float)})


def _trend_df(n=60, start=100.0, step=0.4):
    o, c, h, l = [], [], [], []
    p = start
    for _ in range(n):
        o.append(p)
        p = round(p + step, 3)
        c.append(p)
        h.append(max(o[-1], c[-1]) + 0.3)
        l.append(min(o[-1], c[-1]) - 0.3)
    vol = [1000.0 + i * 10 for i in range(n)]
    return _frame(o, c, h, l, vol)


def _reversal_df(n=60):
    o, c, h, l = [], [], [], []
    p = 120.0
    for _ in range(n - 6):
        o.append(p)
        p = round(p - 0.4, 3)
        c.append(p)
        h.append(max(o[-1], c[-1]) + 0.3)
        l.append(min(o[-1], c[-1]) - 0.3)
    for _ in range(6):
        o.append(p)
        p = round(p + 0.9, 3)
        c.append(p)
        h.append(max(o[-1], c[-1]) + 0.4)
        l.append(min(o[-1], c[-1]) - 0.4)
    vol = [1200.0 + i * 5 for i in range(n)]
    return _frame(o, c, h, l, vol)


def _build_fake_sniper(engine, mss):
    """Fake SniperEnrichmentEngine that returns controlled evidence, so the real
    ATOM reversal-sniper confirmation is exercised deterministically."""
    from core.sniper_enrichment import SniperConfluence, SniperEvidence, LONG, SHORT

    class _FakeSniperEngine:
        def __init__(self, *a, **k):
            pass

        def analyze(self, df, symbol="UNKNOWN", side=None):
            s = str(side).upper()
            si = LONG if s in ("BUY", "LONG") else SHORT
            ev = SniperEvidence(
                mss_bullish=(si == LONG and mss),
                mss_bearish=(si == SHORT and mss),
                nearest_demand={"price": float(df["close"].iloc[-1])} if si == LONG else None,
                nearest_supply={"price": float(df["close"].iloc[-1])} if si == SHORT else None,
                delta_bullish=(si == LONG), delta_bearish=(si == SHORT),
                volume_ratio=1.5, sweep_buy=(si == LONG), sweep_sell=(si == SHORT),
            )
            return SniperConfluence(side=si, score_shift=6.0, evidence=ev)

    return _FakeSniperEngine


class _QueueHarness:
    def __init__(self, engine, df, side, trigger, ob_score=85, ob_grade="A",
                 zone_age=5, zone_touches=1, struct=6, mss=False, sweep=True):
        self.engine = engine
        self.df = df if df is not None else _trend_df()
        self.side = side
        # Realistic, in-range ADX so the per-trade-type ADX gate uses a sane value.
        engine.compute_adx = lambda dfx, period=14: pd.Series([25.0] * len(dfx))
        # Stub the sniper engine with controlled evidence.
        engine.SniperEnrichmentEngine = _build_fake_sniper(engine, mss)
        atr = 1.0 if trigger == "BOS_CONFIRMED" else 2.0

        q = engine.ExecutionQueue(max_size=10, re_eval_interval=0.0)
        cand = engine.ExecutionCandidate(
            symbol="ATOMX", side=side, price=float(self.df["close"].iloc[-1]),
            entry_price=float(self.df["close"].iloc[-1]), stop_loss=95.0,
            take_profit_1=120.0, take_profit_2=140.0, atr=atr, df=self.df,
            ob={}, zone_low=99.0, zone_high=101.0, zone_origin_bar=len(self.df) - zone_age,
            zone_created_at=0.0, zone_touches=zone_touches,
            ob_cfg={}, institutional_score=50.0,
        )
        cand.state = engine.ExecutionState.DISCOVERED
        # Force the OB grade/score so the ATOM layer sees the intended causal OB
        # (mirrors _select_strong_ob grading; kept deterministic for the test).
        cand.ob_grade = ob_grade
        cand.ob_cfg = {}
        cand.strong_ob_present = ob_grade in ("A+", "A")

        def _detect_trigger_state(*a, **k):
            ev = {"sweep_quality": "weak" if sweep else "none", "structure_valid": True,
                  "structure_score": struct, "rejection_or_displacement": True,
                  "ob_sweep_aligned": sweep, "ob_bos_aligned": (trigger == "BOS_CONFIRMED"),
                  "ob_fvg_after_displacement": True, "ob_pd_aligned": True,
                  "trap_risk": 20, "response": 70, "absorption": 60}
            q._last_evidence = ev
            return trigger

        q._detect_trigger_state = _detect_trigger_state
        q._find_causal_ob_zone = lambda *a, **k: (99.0, 101.0, len(self.df) - zone_age, zone_touches)
        q._evaluate_order_block = lambda *a, **k: (ob_score, "STRONG")
        q._evaluate_zone_strength = lambda *a, **k: 95.0
        q._evaluate_liquidity = lambda *a, **k: (90.0, {})
        q._evaluate_institutional = lambda *a, **k: 92.0
        q._evaluate_structure = lambda *a, **k: (struct, "MSS")
        q._evaluate_timing = lambda *a, **k: 95.0
        q._evaluate_trend_alignment = lambda *a, **k: 92.0
        q._evaluate_risk = lambda *a, **k: 20.0
        q._select_strong_ob = lambda *a, **k: {"grade": ob_grade, "score": ob_score,
                                               "freshness": zone_age, "displacement_atr": 1.0}
        q._update_zone_lifecycle = lambda *a, **k: False
        q._is_extended = lambda *a, **k: False
        q._is_order_block_broken = lambda *a, **k: False
        # Roro institutional entry passes so the A-grade fast-confirm path
        # (min_confirmations=1) is reachable; the ATOM layer still decides.
        engine.check_institutional_entry = lambda *a, **k: (True, "INSTITUTIONAL_SNIPER", "test")
        q.add_candidate(cand)
        q.re_evaluate_all(lambda sym: self.df)
        self.cand = q._candidates[cand.symbol]
        self.q = q


class TestREADYGateDifferentiation(unittest.TestCase):
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

    def test_A_trend_valid_reaches_ready_without_sniper(self):
        E = self.engine
        h = _QueueHarness(E, _trend_df(), "BUY", "BOS_CONFIRMED",
                          ob_score=85, ob_grade="A", zone_age=5, struct=6)
        c = h.cand
        self.assertEqual(c.trade_type, "TREND")
        self.assertTrue(c.atom_approved)
        self.assertFalse(c.atom_hard_reject)
        # TREND never requires (and never runs) the sniper confirmation, so we
        # must NOT fake a confirmed flag on it — this is the semantics fix.
        self.assertFalse(c.atom_intel.get("sniper_required"))
        self.assertFalse(c.atom_sniper_confirmed)
        self.assertEqual(c.state, E.ExecutionState.READY)

    def test_B_reversal_with_sniper_reaches_ready(self):
        E = self.engine
        h = _QueueHarness(E, _reversal_df(), "BUY", "MSS_CONFIRMED",
                          ob_score=85, ob_grade="A", zone_age=5, struct=7, mss=True)
        c = h.cand
        self.assertEqual(c.trade_type, "REVERSAL")
        self.assertTrue(c.atom_sniper_confirmed)
        self.assertTrue(c.atom_approved)
        self.assertEqual(c.state, E.ExecutionState.READY)

    def test_B2_reversal_without_sniper_not_ready(self):
        E = self.engine
        h = _QueueHarness(E, _reversal_df(), "BUY", "MSS_CONFIRMED",
                          ob_score=85, ob_grade="A", zone_age=5, struct=7, mss=False, sweep=False)
        c = h.cand
        self.assertEqual(c.trade_type, "REVERSAL")
        self.assertFalse(c.atom_sniper_confirmed)
        self.assertFalse(c.atom_approved)
        self.assertNotEqual(c.state, E.ExecutionState.READY)

    def test_C_fake_ob_never_ready(self):
        E = self.engine
        h = _QueueHarness(E, _trend_df(), "BUY", "BOS_CONFIRMED",
                          ob_score=25, ob_grade="FAKE", zone_age=5, struct=6)
        c = h.cand
        self.assertTrue(c.atom_hard_reject)
        self.assertFalse(c.atom_approved)
        self.assertNotEqual(c.state, E.ExecutionState.READY)

    def test_C2_stale_zone_never_ready(self):
        E = self.engine
        h = _QueueHarness(E, _trend_df(), "BUY", "BOS_CONFIRMED",
                          ob_score=80, ob_grade="A", zone_age=99, struct=6)
        c = h.cand
        self.assertTrue(c.atom_hard_reject)  # stale zone -> not a strong zone
        self.assertFalse(c.atom_approved)
        self.assertNotEqual(c.state, E.ExecutionState.READY)


class TestManagementClassification(unittest.TestCase):
    """D/E/F: management must distinguish TREND continuation & healthy pullback
    (hold) from distribution / exhaustion (profit-lock / exit), and never treat
    a TREND pullback or genuine grab as a reversal fail."""

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

    def test_D_trend_pullback_holds(self):
        E = self.engine
        sm = E.TradeStateMachine()
        state = sm.update({"banker_pressure": 55, "retailer_pressure": 45,
                           "distribution_risk": 20, "accumulation_strength": 55},
                          {"momentum_health": 65, "continuation_strength": 80,
                           "exhaustion_risk": 10, "climax_risk": 10,
                           "trend_expansion": False, "momentum_decay": False},
                          25, "STRONG_TREND")
        self.assertEqual(state, "TREND_RIDE")  # hold, not a failure
        self.assertTrue(sm.should_delay_tp1())
        self.assertFalse(sm.should_aggressive_profit_lock())

    def test_E_liquidity_grab_with_support_holds(self):
        E = self.engine
        sm = E.TradeStateMachine()
        state = sm.update({"banker_pressure": 70, "retailer_pressure": 30,
                           "distribution_risk": 15, "accumulation_strength": 70},
                          {"momentum_health": 55, "continuation_strength": 70,
                           "exhaustion_risk": 20, "climax_risk": 20,
                           "trend_expansion": False, "momentum_decay": False},
                          25, "EXPANSION")
        self.assertIn(state, ("ACCUMULATION", "TREND_RIDE"))
        self.assertFalse(sm.should_hard_exit())  # not dumped immediately

    def test_F_exhaustion_thesis_failure_profit_locks(self):
        E = self.engine
        sm = E.TradeStateMachine()
        state = sm.update({"banker_pressure": 35, "retailer_pressure": 65,
                           "distribution_risk": 80, "accumulation_strength": 20},
                          {"momentum_health": 40, "continuation_strength": 40,
                           "exhaustion_risk": 85, "climax_risk": 80,
                           "trend_expansion": False, "momentum_decay": True},
                          28, "DISTRIBUTION")
        self.assertIn(state, ("DISTRIBUTION", "EXHAUSTION", "LIQUIDITY_EXHAUSTION", "PROFIT_DEFENSE"))
        self.assertTrue(sm.should_aggressive_profit_lock())  # lock/exit with reason
        self.assertIn(sm.get_patience_level(), ("LOW", "MEDIUM"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
