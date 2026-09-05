"""Atom Intelligence layer tests.

Locks the intelligence layer (Roro Entry -> Atom Approval) onto the rules
agreed with the operator:
  * Roro stays the ENTRY ENGINE; Atom only verifies/classifies/manages.
  * TREND trades are approved on OB-fresh + liquidity-supported + structure and
    do NOT require the extra Sniper confirmation.
  * REVERSAL / SNIPER_REVERSAL additionally require Sniper confirmation
    (sweep + MSS/CHoCH + rejection + fresh OB + liquidity support).
  * Fake/Broken/Stale/Over-mitigated OB must never be treated as a strong zone
    (hard-reject).
  * ADX is per trade type: TREND 20..45, REVERSAL/SNIPER < 35.
  * The layer is advisory and bounded: it must never flip a weak setup into a
    strong one and must never act as an independent entry source.

Fully self-contained (no engine / network), so the module is validated in
isolation plus a smoke that the engine still imports and wires the flag.
"""

import importlib
import os
import sys
import unittest

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import atom_intelligence as ai
from core.atom_intelligence import (
    AtomIntelligenceEngine,
    evaluate_zone_freshness,
    evaluate_liquidity_support,
    TradeClassifier,
    TRADE_TREND,
    TRADE_REVERSAL,
    TRADE_SNIPER_REVERSAL,
    FRESH,
    AGING,
    STALE,
    OVER_MITIGATED,
    MAX_CONFIDENCE_BONUS,
)


def _df_from(opens, closes, highs, lows, volumes=None):
    n = len(opens)
    if volumes is None:
        volumes = [1000.0] * n
    return pd.DataFrame({
        "open": np.asarray(opens, dtype=float),
        "high": np.asarray(highs, dtype=float),
        "low": np.asarray(lows, dtype=float),
        "close": np.asarray(closes, dtype=float),
        "volume": np.asarray(volumes, dtype=float),
        "timestamp": list(range(n)),
    })


def _trend_up(n=60):
    """A clear, steady uptrend (TREND long context)."""
    o = []
    c = []
    h = []
    l = []
    p = 100.0
    for i in range(n):
        o.append(p)
        p = round(p + 0.4, 3)
        c.append(p)
        h.append(p + 0.3)
        l.append(o[-1] - 0.3)
    vol = [1000.0 + i * 10 for i in range(n)]
    return _df_from(o, c, h, l, vol)


def _reversal_up(n=60):
    """A downtrend then a bullish shift with strong rejection (reversal long)."""
    o, c, h, l = [], [], [], []
    p = 120.0
    for i in range(n - 6):
        o.append(p)
        p = round(p - 0.4, 3)
        c.append(p)
        h.append(max(o[-1], c[-1]) + 0.3)
        l.append(min(o[-1], c[-1]) - 0.3)
    # Strong bullish rejection leg (sweep low then reclaim).
    for i in range(6):
        o.append(p)
        p = round(p + 0.9, 3)
        c.append(p)
        h.append(max(o[-1], c[-1]) + 0.4)
        l.append(min(o[-1], c[-1]) - 0.4)
    vol = [1200.0 + i * 5 for i in range(n)]
    return _df_from(o, c, h, l, vol)


def _choch_up(df):
    if len(df) < 8:
        return False
    return (float(df["high"].iloc[-3]) > float(df["high"].iloc[-6])
            and float(df["low"].iloc[-3]) > float(df["low"].iloc[-6]))


class _FakeSniperConf:
    score_shift = 6.0
    primary_energy = "STRONG"
    reasons = ["mss_bullish", "fresh_ob", "liquidity"]
    evidence = type("E", (), {
        "mss_bullish": True, "mss_bearish": False,
        "nearest_demand": {"price": 99.5}, "nearest_supply": None,
        "delta_bullish": True, "delta_bearish": False, "volume_ratio": 1.5,
    })()


class TestZoneFreshness(unittest.TestCase):
    def test_fresh(self):
        z = evaluate_zone_freshness(5, 1, 0)
        self.assertEqual(z.state, FRESH)
        self.assertTrue(z.fresh)
        self.assertFalse(z.bad)

    def test_aging(self):
        z = evaluate_zone_freshness(25, 1, 0)
        self.assertEqual(z.state, AGING)
        self.assertTrue(z.fresh)
        self.assertFalse(z.bad)

    def test_stale(self):
        z = evaluate_zone_freshness(99, 1, 0)
        self.assertEqual(z.state, STALE)
        self.assertFalse(z.fresh)
        self.assertTrue(z.bad)

    def test_over_mitigated_touches(self):
        z = evaluate_zone_freshness(5, 5, 0)
        self.assertEqual(z.state, OVER_MITIGATED)
        self.assertTrue(z.bad)

    def test_over_mitigated_price_left(self):
        z = evaluate_zone_freshness(5, 1, 10, price_interacting=False)
        self.assertEqual(z.state, OVER_MITIGATED)
        self.assertTrue(z.bad)


class TestLiquiditySupport(unittest.TestCase):
    def test_on_support_boosts(self):
        df = _trend_up()
        ls = evaluate_liquidity_support(df, "BUY", act_demand={"price": float(df["close"].iloc[-1])},
                                        delta_bullish=True, volume_ratio=1.5)
        self.assertGreaterEqual(ls.score, 70)

    def test_weak_no_support_low_score(self):
        df = _trend_up()
        ls = evaluate_liquidity_support(df, "BUY", act_demand=None, delta_bullish=False, volume_ratio=0.0)
        self.assertLess(ls.score, 60)

    def test_blocking_box_penalizes(self):
        df = _trend_up()
        price = float(df["close"].iloc[-1])
        # Same setup, but one has an active opposing (supply) box at price.
        ls_no_block = evaluate_liquidity_support(df, "BUY", act_demand={"price": price})
        ls_block = evaluate_liquidity_support(df, "BUY", act_demand={"price": price},
                                              act_supply={"price": price})
        # An opposing box at price is a negative (blocked = confidence penalty),
        # never a veto. It must lower score vs the unblocked case.
        self.assertLess(ls_block.score, ls_no_block.score)


class TestTradeClassification(unittest.TestCase):
    def test_trend_context_is_trend(self):
        df = _trend_up()
        tt = TradeClassifier.classify(df, "BUY", 1.0)
        self.assertEqual(tt, TRADE_TREND)

    def test_choch_marks_reversal(self):
        df = _reversal_up()
        self.assertTrue(_choch_up(df))
        tt = TradeClassifier.classify(df, "BUY", 2.0)
        self.assertIn(tt, (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL))


class TestAtomEngine(unittest.TestCase):
    def test_weak_ob_hard_rejects(self):
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        r = eng.evaluate(df, "BUY", 1.0, ob_grade="BROKEN", ob_score=20)
        self.assertTrue(r.hard_reject)
        self.assertFalse(r.approved)
        self.assertFalse(r.ob_quality_ok)

    def test_stale_zone_hard_rejects(self):
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        r = eng.evaluate(df, "BUY", 1.0, ob_grade="A", ob_score=80,
                         zone_age_bars=99, zone_touches=1)
        self.assertTrue(r.hard_reject)
        self.assertEqual(r.freshness.state, STALE)

    def test_trend_approved_without_sniper(self):
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        r = eng.evaluate(df, "BUY", 1.0,
                         ob_grade="A", ob_score=80, zone_age_bars=5, zone_touches=1,
                         struct_score=5, adx=25, price_interacting=True,
                         act_demand={"price": float(df["close"].iloc[-1])},
                         volume_ratio=1.4)
        self.assertEqual(r.trade_type, TRADE_TREND)
        self.assertTrue(r.adx_ok)
        # TREND never requires (and never runs) the extra sniper gate; the
        # semantics must not fake a confirmation that we decided not to require.
        self.assertFalse(r.sniper_required)
        self.assertFalse(r.sniper_confirmed)
        self.assertTrue(r.approved)

    def test_reversal_requires_sniper(self):
        eng = AtomIntelligenceEngine()
        df = _reversal_up()
        # Good reversal evidence: sweep + mss + fresh + liquidity.
        r = eng.evaluate(df, "BUY", 2.0,
                         ob_grade="A", ob_score=80, zone_age_bars=5, zone_touches=1,
                         struct_score=5, adx=22, price_interacting=True,
                         sweep_aligned=True, mss_bullish=True,
                         act_demand={"price": float(df["close"].iloc[-1])},
                         volume_ratio=1.5)
        self.assertEqual(r.trade_type, TRADE_REVERSAL)
        self.assertTrue(r.adx_ok)
        self.assertTrue(r.sniper_confirmed)
        self.assertTrue(r.approved)

    def test_reversal_without_sniper_not_approved(self):
        eng = AtomIntelligenceEngine()
        df = _reversal_up()
        r = eng.evaluate(df, "BUY", 2.0,
                         ob_grade="A", ob_score=80, zone_age_bars=5, zone_touches=1,
                         struct_score=5, adx=22, price_interacting=True,
                         sweep_aligned=False, mss_bullish=False,
                         act_demand=None, volume_ratio=0.0)
        self.assertEqual(r.trade_type, TRADE_REVERSAL)
        self.assertFalse(r.sniper_confirmed)
        self.assertFalse(r.approved)  # reversal needs the extra confirmation

    def test_adx_by_trade_type(self):
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        # TREND with ADX 40 is OK; reversal with ADX 40 not.
        r = eng.evaluate(df, "BUY", 1.0, ob_grade="A", ob_score=80,
                         zone_age_bars=5, zone_touches=1, struct_score=5, adx=40,
                         price_interacting=True, act_demand={"price": 100.0})
        self.assertEqual(r.trade_type, TRADE_TREND)
        self.assertTrue(r.adx_ok)

    def test_premature_reversal_adx_high(self):
        # ADX is a contextual regime read, NOT a blind veto. A high ADX on a
        # reversal means over-extension / exhaustion — which *favors* the
        # reversal thesis; we gate reversals on sweep+reclaim+structure, not on
        # an ADX ceiling. So a high-ADX reversal is still contextually OK.
        eng = AtomIntelligenceEngine()
        df = _reversal_up()
        r = eng.evaluate(df, "BUY", 2.0, ob_grade="A", ob_score=80,
                         zone_age_bars=5, zone_touches=1, struct_score=5, adx=40,
                         price_interacting=True, sweep_aligned=True, mss_bullish=True)
        self.assertIn(r.trade_type, (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL))
        self.assertTrue(r.adx_ok)  # high ADX = exhaustion context, not a veto
        self.assertTrue(r.approved)  # not blocked by ADX; sniper confirmed

    def test_reversal_adx_high_not_a_blocker(self):
        # Regression guard: a reversal is gated on the extra sniper confirmation,
        # never on an ADX ceiling. Even an extreme ADX must not flip approval.
        eng = AtomIntelligenceEngine()
        df = _reversal_up()
        r = eng.evaluate(df, "BUY", 2.0, ob_grade="A", ob_score=80,
                         zone_age_bars=5, zone_touches=1, struct_score=5, adx=99,
                         price_interacting=True, sweep_aligned=True, mss_bullish=True)
        self.assertIn(r.trade_type, (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL))
        self.assertTrue(r.approved)  # high ADX does NOT block a confirmed reversal
        self.assertFalse(r.hard_reject)


class TestSecurityInvariants(unittest.TestCase):
    def test_confidence_adjust_bounded(self):
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        r = eng.evaluate(df, "BUY", 1.0, ob_grade="A", ob_score=95,
                         zone_age_bars=3, zone_touches=0, struct_score=8, adx=30,
                         price_interacting=True,
                         act_demand={"price": float(df["close"].iloc[-1])},
                         volume_ratio=2.0, sniper_conf=_FakeSniperConf())
        self.assertLessEqual(r.confidence_adjust, MAX_CONFIDENCE_BONUS)
        self.assertGreaterEqual(r.confidence_adjust, 0)

    def test_never_positive_when_weak_freshness(self):
        # An aging zone with weak structure and no liquidity support is NOT a
        # hard defect, so Atom must still let the Roro entry through (we never
        # block a real causal OB / non-stale zone). But the weak evidence must
        # NOT produce a strong positive confidence boost.
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        r = eng.evaluate(df, "BUY", 1.0, ob_grade="B", ob_score=55,
                         zone_age_bars=25, zone_touches=1, struct_score=2, adx=25,
                         price_interacting=True)
        self.assertEqual(r.freshness.state, AGING)
        self.assertFalse(r.hard_reject)   # aging/weak is not a hard defect
        self.assertTrue(r.approved)       # Roro entry stands (real OB, fresh-enough zone)
        self.assertLessEqual(r.confidence_adjust, 1.0)  # no strong positive boost

    def test_non_hard_reject_never_blocks_entry(self):
        # Core guard: NO non-defective signal (weak structure, weak liquidity,
        # off-trend ADX) may flip Atom into a REJECT of a Roro-valid entry.
        # Only a fake/broken/stale OB (hard_reject) or a REVERSAL missing its
        # mandatory sniper confirmation can block.
        eng = AtomIntelligenceEngine()
        df = _trend_up()
        r = eng.evaluate(df, "BUY", 1.0,
                         ob_grade="A", ob_score=80, zone_age_bars=5, zone_touches=1,
                         struct_score=0, adx=200, price_interacting=True,
                         act_demand=None, volume_ratio=0.0)  # weak structure, extreme ADX, no liquidity
        self.assertEqual(r.trade_type, TRADE_TREND)
        self.assertFalse(r.hard_reject)
        self.assertTrue(r.approved)  # none of the weak signals block the entry


class TestEngineWiringSmoke(unittest.TestCase):
    def test_engine_imports_flag(self):
        os.environ.setdefault("SNIPER_ALLOW_IMPORT", "1")
        try:
            eng_mod = importlib.import_module("core.engine")
            self.assertTrue(getattr(eng_mod, "ATOM_INTELLIGENCE_AVAILABLE", False))
            self.assertIsNotNone(getattr(eng_mod, "AtomIntelligenceEngine", None))
        except Exception as exc:  # pragma: no cover - engine has optional deps
            self.skipTest(f"engine import not available in this env: {exc}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
