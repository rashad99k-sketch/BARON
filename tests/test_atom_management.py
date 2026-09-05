"""Phase-2 tests: verify the Atom trade_type (TREND / REVERSAL / SNIPER_REVERSAL)
is threaded through to open-position management and that management reacts by
type — including the new CORRECTION semantic (bank a partial + protect the
remainder) versus the existing PULLBACK (hold fully) and DISTRIBUTION (de-risk).

These tests mirror the test_position_management_phase1 harness: they drive the
Real `_apply_dynamic_profit_and_exit_rules` with controlled inputs and stub the
real broker side effects (close_partial / close_position_full).
"""

import os
import unittest

import core.engine as E  # noqa: E402  (real engine)


def _continuation(cont=0.8, conf=0.85):
    return E.ContinuationEvaluation(
        continuation_probability=cont, trend_strength=cont, exhaustion_probability=0.1,
        reclaim_risk=0.2, counter_pressure=0.1, confidence=conf,
        reasons=["x"], should_hold=cont >= 0.62, hold_quality="GOOD",
    )


class DynamicTypeRecognitionTest(unittest.TestCase):
    """SNIPER_REVERSAL must be a first-class management type, reachable both
    from the Atom trade_type label and from the classification string."""

    def _profile(self, trade_type="", classification="TREND"):
        return E.DynamicPositionProfile("BTC/USDT:USDT", "BUY", 100.0, 1.0,
                                        classification=classification,
                                        trade_type=trade_type)

    def test_sniper_reversal_recognized_from_trade_type(self):
        p = self._profile(trade_type="SNIPER_REVERSAL")
        self.assertEqual(p.trade_type, "SNIPER_REVERSAL")

    def test_sniper_reversal_recognized_from_classification(self):
        p = self._profile(classification="SNIPER_REVERSAL")
        self.assertEqual(p.trade_type, "SNIPER_REVERSAL")

    def test_trend_reversal_preserved(self):
        self.assertEqual(self._profile(trade_type="TREND").trade_type, "TREND")
        self.assertEqual(self._profile(trade_type="REVERSAL").trade_type, "REVERSAL")

    def test_legacy_sniper_still_breakout(self):
        # The legacy "SNIPER" classification (non-reversal) must keep mapping to
        # BREAKOUT — only SNIPER_REVERSAL is the reversal-specific type.
        p = self._profile(classification="SNIPER")
        self.assertEqual(p.trade_type, "BREAKOUT")


class AtomTypeThreadingTest(unittest.TestCase):
    """The Atom classification must flow into the open-trade profile via
    _ensure_position_profile (called at OPEN inside execute_entry)."""

    def setUp(self):
        self._manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        self._manager.position_profile = None
        E.STATE["open"] = False

    def _thread(self, trade_type="", classification="INSTITUTIONAL_SNIPER"):
        return self._manager._ensure_position_profile(
            "BTC/USDT:USDT", 100.0, "BUY", 1.0, classification=classification,
            trade_type=trade_type)

    def test_atom_sniper_reversal_threads_into_profile(self):
        prof = self._thread(trade_type="SNIPER_REVERSAL")
        self.assertEqual(prof.trade_type, "SNIPER_REVERSAL")

    def test_atom_trend_threads_into_profile(self):
        self.assertEqual(self._thread(trade_type="TREND").trade_type, "TREND")

    def test_atom_reversal_threads_into_profile(self):
        self.assertEqual(self._thread(trade_type="REVERSAL").trade_type, "REVERSAL")


class CorrectionDetectionTest(unittest.TestCase):
    """_is_correction must separate CORRECTION from PULLBACK / DISTRIBUTION /
    hard-failure."""

    def setUp(self):
        self._manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)

    def _corr(self, **kw):
        return self._manager._is_correction(**kw)

    def test_healthy_pullback_is_not_correction(self):
        # shallow dip, structure aligned, continuation intact -> HOLD
        self.assertFalse(self._corr(
            trade_state="HEALTHY_PULLBACK", cont=0.75, dist_risk=10,
            exhaustion_risk=15, momentum_decay=False, structure_aligned=True))

    def test_strong_trend_is_not_correction(self):
        self.assertFalse(self._corr(
            trade_state="TREND_RIDE", cont=0.85, dist_risk=10,
            exhaustion_risk=10, momentum_decay=False, structure_aligned=True))

    def test_distribution_is_not_correction(self):
        # high distribution risk -> defense / profit-lock, not a correction
        self.assertFalse(self._corr(
            trade_state="DISTRIBUTION", cont=0.4, dist_risk=70,
            exhaustion_risk=30, momentum_decay=True, structure_aligned=False))

    def test_exhaustion_is_not_correction(self):
        self.assertFalse(self._corr(
            trade_state="EXHAUSTION", cont=0.4, dist_risk=20,
            exhaustion_risk=80, momentum_decay=True, structure_aligned=False))

    def test_hard_failure_is_not_correction(self):
        self.assertFalse(self._corr(
            trade_state="MOMENTUM_COLLAPSE", cont=0.2, dist_risk=30,
            exhaustion_risk=30, momentum_decay=True, structure_aligned=False))

    def test_deep_pullback_with_faded_continuation_is_correction(self):
        # deeper/weaker dip: structure off-kilter, continuation faded, momentum
        # decaying, but NOT a confirmed distribution/exhaustion/failure.
        self.assertTrue(self._corr(
            trade_state="HEALTHY_PULLBACK", cont=0.5, dist_risk=20,
            exhaustion_risk=25, momentum_decay=True, structure_aligned=False))


class CorrectionManagementTest(unittest.TestCase):
    """Rule 3 must bank a partial + protect the remainder for a TREND/SNIPER
    trade in CORRECTION, and must NOT fire for a healthy pullback."""

    def setUp(self):
        self._manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        E.STATE["open"] = False
        E.STATE["tp1_hit"] = False
        E.STATE["runner_mode"] = False
        E.STATE["synthetic_sl"] = 0.0
        E.STATE["trail_stop"] = 0.0
        E.STATE["dynamic_reversal_exit_done"] = False
        E.STATE["dynamic_exhaustion_protect_done"] = False
        E.STATE["dynamic_partial_done"] = False
        E.STATE["position_health"] = 60.0
        E.STATE["position_action"] = "HOLD"
        E.STATE["position_confidence"] = 0.7
        E.STATE["market_regime"] = "EXPANSION"
        self._broker = E.close_position_full
        self._partial = E.close_partial
        self._dec = E.log_position_decision
        E.close_position_full = lambda: True
        E.close_partial = lambda ratio: None
        E.log_position_decision = lambda *a, **k: None

    def tearDown(self):
        E.close_position_full = self._broker
        E.close_partial = self._partial
        E.log_position_decision = self._dec
        E.STATE["open"] = False

    def _profile(self, trade_type="TREND"):
        self._manager.position_profile = E.DynamicPositionProfile(
            "BTC/USDT:USDT", "BUY", 100.0, 1.0, classification="INSTITUTIONAL_SNIPER",
            trade_type=trade_type, asset_class="CRYPTO")

    def _run(self, *, trade_state, cont, dist_risk, exh_risk, mom_decay,
             structure_aligned, roe=3.0, trade_type="TREND"):
        self._profile(trade_type=trade_type)
        continuation = _continuation(cont=cont)
        return self._manager._apply_dynamic_profit_and_exit_rules(
            symbol="BTC/USDT:USDT", mark_price=103.0, atr=1.0, side="BUY", entry=100.0,
            roe=roe, trade_state=trade_state,
            smart_money={"distribution_risk": dist_risk, "banker_pressure": 50},
            momentum={"momentum_health": 60, "exhaustion_risk": exh_risk,
                      "momentum_decay": mom_decay, "continuation_strength": 55},
            continuation_eval=continuation, structure_aligned=structure_aligned)

    def test_trend_correction_banks_partial_and_runs(self):
        # TREND entering a CORRECTION at a profit target -> partial 50% + runner
        # + breakeven (protect remainder), NOT a full hold.
        closed = self._run(
            trade_state="HEALTHY_PULLBACK", cont=0.5, dist_risk=20, exh_risk=25,
            mom_decay=True, structure_aligned=False, roe=3.0, trade_type="TREND",
        )
        self.assertFalse(closed)                      # not a full exit
        self.assertTrue(E.STATE["dynamic_partial_done"])  # partial banked
        self.assertTrue(E.STATE["tp1_hit"])
        self.assertTrue(E.STATE["runner_mode"])
        self.assertEqual(E.STATE["synthetic_sl"], 100.0)  # breakeven guard

    def test_sniper_reversal_in_correction_banks_partial(self):
        closed = self._run(
            trade_state="HEALTHY_PULLBACK", cont=0.5, dist_risk=20, exh_risk=25,
            mom_decay=True, structure_aligned=False, roe=3.0, trade_type="SNIPER_REVERSAL",
        )
        self.assertFalse(closed)
        self.assertTrue(E.STATE["dynamic_partial_done"])

    def test_healthy_pullback_does_not_bank(self):
        # Strong continuation + aligned structure = hold fully, no partial.
        closed = self._run(
            trade_state="HEALTHY_PULLBACK", cont=0.75, dist_risk=10, exh_risk=15,
            mom_decay=False, structure_aligned=True, roe=3.0, trade_type="TREND",
        )
        self.assertFalse(closed)
        self.assertFalse(E.STATE["dynamic_partial_done"])

    def test_strong_trend_does_not_bank(self):
        closed = self._run(
            trade_state="TREND_RIDE", cont=0.85, dist_risk=10, exh_risk=10,
            mom_decay=False, structure_aligned=True, roe=3.0, trade_type="TREND",
        )
        self.assertFalse(closed)
        self.assertFalse(E.STATE["dynamic_partial_done"])

    def test_correction_below_profit_target_does_not_bank(self):
        # Correction but not yet at the ATR profit target -> no premature partial.
        closed = self._run(
            trade_state="HEALTHY_PULLBACK", cont=0.5, dist_risk=20, exh_risk=25,
            mom_decay=True, structure_aligned=False, roe=0.5, trade_type="TREND",
        )
        self.assertFalse(closed)
        self.assertFalse(E.STATE["dynamic_partial_done"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
