"""Early Entry Confluence Intelligence tests.

Covers the advisory early-entry layer (core.early_entry_confluence) which
sits ON TOP of Roro + Atom to detect the START of a move out of a fresh,
liquidity-backed zone -- without becoming a hard gate and without touching
RORO / RF / Entry / Execution / Risk.

Proves:
  1. BUY early: price near/inside a FRESH demand zone + liquidity event + flow
     + positive confluence + RF BUY -> EARLY_ENTRY, PHASE=FIRST, small distance.
  2. SELL early: the symetric case -> EARLY_ENTRY, PHASE=FIRST.
  3. Late rejection: price far from the zone -> PHASE=LATE / SITOUT_LATE
     (REASON=TOO_FAR_FROM_ZONE), it is NO LONGER an early entry.
  4. Distance semantics: inside=0, near=small, far=large (in ATR units).
  5. No hard gate: weak/absent confluence only lowers confidence, never
     hard-rejects, and score_shift stays bounded.
  6. Zone freshness acts as the source-of-truth location: a STALE /
     OVER-MITIGATED zone suppresses the confluence even with strong signals.
  7. RF is part of the confluence (NOT a separate veto): rf alignment adds,
     misalignment reduces, but neither alone decides the action.
  8. BUY/SELL symmetry.
"""
import unittest

import numpy as np
import pandas as pd

from core.early_entry_confluence import (
    EarlyEntryConfluenceEngine,
    analyze_early_entry,
    evaluate_zone_distance,
    classify_distance_phase,
    PHASE_FIRST,
    PHASE_DEVELOPING,
    PHASE_LATE,
    ACTION_EARLY_ENTRY,
    ACTION_WAIT,
    ACTION_LATE,
    DIST_INSIDE,
    DIST_EDGE,
    DIST_FAR,
)


def _frame(n=250, base=100.0):
    """A controlled trending frame (closed-candle, deterministic)."""
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


def _atr(df):
    return float(df["close"].iloc[-1]) * 0.01


class EarlyEntryDistanceTest(unittest.TestCase):
    def test_inside_zone_is_zero(self):
        self.assertEqual(evaluate_zone_distance(100.0, 98.0, 102.0, 1.0), DIST_INSIDE)

    def test_near_zone_is_small_positive(self):
        d = evaluate_zone_distance(102.5, 98.0, 102.0, 1.0)
        self.assertGreater(d, 0.0)
        self.assertLessEqual(d, DIST_EDGE)

    def test_far_zone_is_large(self):
        d = evaluate_zone_distance(110.0, 98.0, 102.0, 1.0)
        self.assertGreater(d, DIST_FAR)

    def test_below_zone_symmetric(self):
        d = evaluate_zone_distance(95.0, 98.0, 102.0, 1.0)
        self.assertGreater(d, 0.0)

    def test_no_zone_yields_inf(self):
        self.assertEqual(evaluate_zone_distance(100.0, None, None, 1.0), float("inf"))

    def test_distance_phase_boundaries(self):
        self.assertEqual(classify_distance_phase(0.0), PHASE_FIRST)
        self.assertEqual(classify_distance_phase(DIST_EDGE), PHASE_FIRST)
        self.assertEqual(classify_distance_phase((DIST_EDGE + DIST_FAR) / 2.0), PHASE_DEVELOPING)
        self.assertEqual(classify_distance_phase(DIST_FAR + 0.1), PHASE_LATE)


class EarlyEntryEngineTest(unittest.TestCase):
    def _near_demand(self, side="BUY"):
        """Frame with a demand zone just below current price whose tail candle
        swept sell-side liquidity (low dipped through the zone) then recovered
        above it, so the early-entry sweep+reclaim is a real FIRST scenario."""
        df = _frame()
        price = float(df["close"].iloc[-1])
        atr = _atr(df)
        # demand zone just below current price -> price inside/at top edge
        zone_low = price - 1.2 * atr
        zone_high = price - 0.2 * atr
        # force the tail low through the zone bottom then recover above it
        df.iloc[-2, df.columns.get_loc("low")] = zone_low - 0.8 * atr
        df.iloc[-2, df.columns.get_loc("close")] = price - 1.0 * atr
        df.iloc[-2, df.columns.get_loc("open")] = price - 0.9 * atr
        df.iloc[-2, df.columns.get_loc("high")] = price - 0.4 * atr
        df.iloc[-1, df.columns.get_loc("low")] = zone_low - 0.3 * atr
        df.iloc[-1, df.columns.get_loc("close")] = price
        df.iloc[-1, df.columns.get_loc("open")] = price - 0.4 * atr
        df.iloc[-1, df.columns.get_loc("high")] = price + 0.4 * atr
        df.iloc[-1, df.columns.get_loc("volume")] = 4000.0
        return df, price, atr, zone_low, zone_high

    def test_buy_early_entry(self):
        df, price, atr, zl, zh = self._near_demand("BUY")
        r = analyze_early_entry(df, "BUY", zone_low=zl, zone_high=zh,
                                zone_type="DEMAND", zone_freshness="FRESH",
                                liquidity_support="STRONG", rf_signal="BUY")
        self.assertEqual(r.evidence.phase, PHASE_FIRST)
        self.assertLessEqual(r.evidence.distance_from_zone, DIST_EDGE)
        self.assertEqual(r.action, ACTION_EARLY_ENTRY)
        self.assertGreaterEqual(r.confidence, 60)
        self.assertTrue(r.evidence.rf_aligned)

    def test_sell_early_entry_symmetric(self):
        # Supply zone just at/above current price.
        df = _frame()
        price = float(df["close"].iloc[-1])
        atr = _atr(df)
        zone_low = price - 0.1 * atr
        zone_high = price + 1.0 * atr
        r = analyze_early_entry(df, "SELL", zone_low=zone_low, zone_high=zone_high,
                                zone_type="SUPPLY", zone_freshness="FRESH",
                                liquidity_support="STRONG", rf_signal="SELL")
        self.assertEqual(r.evidence.phase, PHASE_FIRST)
        self.assertEqual(r.action, ACTION_EARLY_ENTRY)
        self.assertTrue(r.evidence.rf_aligned)
        self.assertGreaterEqual(r.confidence, 60)

    def test_too_far_from_zone_is_late(self):
        df = _frame()
        price = float(df["close"].iloc[-1])
        atr = _atr(df)
        # price far above the demand zone -> not early
        zone_low = price - 10.0 * atr
        zone_high = price - 8.0 * atr
        r = analyze_early_entry(df, "BUY", zone_low=zone_low, zone_high=zone_high,
                                zone_type="DEMAND", zone_freshness="FRESH",
                                liquidity_support="STRONG", rf_signal="BUY")
        self.assertEqual(r.evidence.phase, PHASE_LATE)
        self.assertEqual(r.action, ACTION_LATE)
        self.assertGreater(r.evidence.distance_from_zone, DIST_FAR)
        self.assertIn("TOO_FAR_FROM_ZONE", r.log_line)

    def test_no_hard_gate_weak_confluence(self):
        # Far but not late-beyond-limit + no confluence -> WAIT, never REJECT.
        df = _frame()
        price = float(df["close"].iloc[-1])
        atr = _atr(df)
        zone_low = price - 1.2 * atr
        zone_high = price - 1.0 * atr
        r = analyze_early_entry(df, "BUY", zone_low=zone_low, zone_high=zone_high,
                                zone_type="DEMAND", zone_freshness="FRESH",
                                liquidity_support="WEAK", rf_signal="SELL")
        # confidence bounded in [0,100] and score_shift within accessory bounds
        self.assertGreaterEqual(r.confidence, 0.0)
        self.assertLessEqual(r.confidence, 100.0)
        self.assertGreaterEqual(r.score_shift, -5.0)
        self.assertLessEqual(r.score_shift, 5.0)
        # RF misalignment should not hard-veto; action is a soft WAIT / LATE.
        self.assertIn(r.action, (ACTION_WAIT, ACTION_LATE))

    def test_stale_zone_suppresses_confluence(self):
        df, price, atr, zl, zh = self._near_demand("BUY")
        r = analyze_early_entry(df, "BUY", zone_low=zl, zone_high=zh,
                                zone_type="DEMAND", zone_freshness="OVER_MITIGATED",
                                liquidity_support="STRONG", rf_signal="BUY")
        # A poor zone is the source-of-truth: even with strong signals, the
        # layer must not label this as a high-quality early long.
        self.assertNotEqual(r.action, ACTION_EARLY_ENTRY)

    def test_rf_misalignment_reduces_confidence(self):
        df, price, atr, zl, zh = self._near_demand("BUY")
        r_bad = analyze_early_entry(df, "BUY", zone_low=zl, zone_high=zh,
                                    zone_type="DEMAND", zone_freshness="FRESH",
                                    liquidity_support="STRONG", rf_signal="SELL")
        r_good = analyze_early_entry(df, "BUY", zone_low=zl, zone_high=zh,
                                     zone_type="DEMAND", zone_freshness="FRESH",
                                     liquidity_support="STRONG", rf_signal="BUY")
        self.assertLess(r_bad.confidence, r_good.confidence)
        self.assertFalse(r_bad.evidence.rf_aligned)
        self.assertTrue(r_good.evidence.rf_aligned)

    def test_evidence_schema_present(self):
        df, price, atr, zl, zh = self._near_demand("BUY")
        r = analyze_early_entry(df, "BUY", zone_low=zl, zone_high=zh,
                                zone_type="DEMAND", zone_freshness="FRESH",
                                liquidity_support="STRONG", rf_signal="BUY")
        ev = r.to_dict()["evidence"]
        for key in ("detected", "direction", "phase", "zone_type",
                    "zone_freshness", "liquidity_support", "liquidity_event",
                    "crossover_stage", "indicators_aligned", "structure_aligned",
                    "displacement", "distance_from_zone", "confidence"):
            self.assertIn(key, ev)

    def test_buy_sell_symmetric_direction(self):
        df = _frame()
        price = float(df["close"].iloc[-1])
        atr = _atr(df)
        zl, zh = price - 0.1 * atr, price + 1.0 * atr
        rb = analyze_early_entry(df, "BUY", zone_low=zl - 2 * atr, zone_high=zh - 2 * atr,
                                 zone_type="DEMAND", zone_freshness="FRESH",
                                 liquidity_support="WEAK", rf_signal="BUY")
        rs = analyze_early_entry(df, "SELL", zone_low=zl, zone_high=zh,
                                 zone_type="SUPPLY", zone_freshness="FRESH",
                                 liquidity_support="WEAK", rf_signal="SELL")
        self.assertEqual(rb.evidence.direction, "LONG")
        self.assertEqual(rs.evidence.direction, "SHORT")


if __name__ == "__main__":
    unittest.main()
