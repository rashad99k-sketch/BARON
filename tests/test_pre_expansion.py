import unittest
from unittest import mock

import numpy as np
import pandas as pd

import core.engine as engine

from core.engine import InstitutionalRadar, PreInstitutionalState


def _fresh_entry(side="BUY", strength="MEDIUM", analysis=None, **kw):
    entry = {
        "side": side,
        "strength": strength,
        "state": "DETECTED",
        "trade_type": "TREND",
        "reasons": [],
        "smart_money_bias": "NEUTRAL",
        "momentum_expansion": False,
        "momentum_decay": False,
        "continuation_strength": 0,
        "exhaustion_risk": 0,
        "analysis": analysis if analysis is not None else {},
    }
    entry.update(kw)
    return entry


def _df():
    return pd.DataFrame({
        "open": [100.0] * 25,
        "close": [100.0] * 25,
        "high": [101.0] * 25,
        "low": [99.0] * 25,
        "volume": [10.0] * 25,
    })


def _bull_df(n=260, final_body=2.0, final_vol_mult=4.0):
    """Realistic bull-trend OHLCV ending with a firm bullish candle + volume
    spike.  Used for EARLY_EXPANSION / indicator-evidence tests."""
    closes = np.linspace(100.0, 120.0, n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    vol = np.linspace(1000, 1200, n)
    closes[-1] += final_body
    opens[-1] = closes[-2]
    vol[-1] *= final_vol_mult
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                          "close": closes, "volume": vol})


def _bear_df(n=260, final_body=-2.0, final_vol_mult=4.0):
    """Bear-trend OHLCV ending with a firm bearish candle + volume spike."""
    closes = np.linspace(120.0, 100.0, n)
    opens = np.concatenate([[closes[0]], closes[:-1]])
    highs = np.maximum(opens, closes) + 0.5
    lows = np.minimum(opens, closes) - 0.5
    vol = np.linspace(1000, 1200, n)
    closes[-1] += final_body
    opens[-1] = closes[-2]
    vol[-1] *= final_vol_mult
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                          "close": closes, "volume": vol})


_NEUTRAL_FLOW = {
    "trend_expansion": False,
    "flow_bias": "NEUTRAL",
    "momentum_decay": False,
    "continuation_strength": 0,
    "exhaustion_risk": 0,
}


def _run_eval(entry, bos=(False, False), flow=None, promote=False):
    """Invoke the evidence evaluator in isolation (all live feeds patched)."""
    radar = InstitutionalRadar()
    if flow is None:
        flow = _NEUTRAL_FLOW
    with mock.patch.object(engine, "detect_bos", return_value=bos), \
         mock.patch.object(engine.MomentumFlowEngine, "analyze_momentum_flow", return_value=flow), \
         mock.patch.object(engine, "log_execution"), \
         mock.patch.object(radar, "_promote_to_queue") as prom:
        radar._evaluate_pre_expansion("BTC/USDT:USDT", entry, _df(), 100.0, 1.0, {})
    return radar, entry, prom


def _run_eval_df(entry, df, flow=None, price=None):
    """Invoke the evidence evaluator against a realistic df (used for the
    TradingView / zone-first / expansion-phase tests)."""
    radar = InstitutionalRadar()
    if flow is None:
        flow = _NEUTRAL_FLOW
    if price is None:
        price = float(df["close"].iloc[-1])
    atr = float(engine.compute_atr(df).iloc[-1]) if len(df) > 14 else price * 0.01
    with mock.patch.object(engine.MomentumFlowEngine, "analyze_momentum_flow", return_value=flow), \
         mock.patch.object(engine, "log_execution"), \
         mock.patch.object(radar, "_promote_to_queue") as prom:
        radar._evaluate_pre_expansion("BTC/USDT:USDT", entry, df, price, atr, {})
    return radar, entry, prom


class PreExpansionPromotionTest(unittest.TestCase):
    """Tests A-D: when a Watchlist asset is promoted to priority institutional
    analysis based on early institutional evidence."""

    def test_a_medium_displacement_promoted(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")

    def test_b_medium_rejection_promoted(self):
        entry = _fresh_entry(side="SELL", strength="MEDIUM", analysis={
            "displacement": False, "rejection": True, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_SHORT")

    def test_c_medium_multiple_reasons_promoted(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 50,
            "liq_score": 80, "ob_grade": "A", "roro_signal": False,
            "vol_state": "neutral", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        evidence = entry.get("pre_expansion_evidence", [])
        self.assertIn("DISPLACEMENT", evidence)
        self.assertIn("OB_ZONE", evidence)
        self.assertIn("SWEEP", evidence)

    def test_d_medium_alone_not_promoted(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={})
        _, entry, _ = _run_eval(entry)
        self.assertNotIn("pre_expansion_state", entry)


class PreExpansionNeverOpensTest(unittest.TestCase):
    """Test E: PRE_EXPANSION promotion must never queue or open a trade."""

    def test_e_pre_expansion_never_opens(self):
        for hypothesis, bos in [
            ("PRE_EXPANSION_LONG", (False, False)),
            ("PRE_EXPANSION_SHORT", (False, False)),
        ]:
            entry = _fresh_entry(side="BUY" if hypothesis == "PRE_EXPANSION_LONG" else "SELL",
                                 strength="MEDIUM", analysis={
                "displacement": True, "rejection": False, "struct_score": 80,
                "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
                "vol_state": "expansion", "trap_risk": 0,
            })
            _, entry, promote = _run_eval(entry, bos=bos)
            self.assertEqual(entry.get("pre_expansion_state"), hypothesis)
            promote.assert_not_called()

    def test_e_conflict_never_opens(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        }, smart_money_bias="SHORT")
        _, entry, promote = _run_eval(entry, bos=(False, True))
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_CONFLICT")
        promote.assert_not_called()


class PreExpansionHypothesisTest(unittest.TestCase):
    """Tests F-H: directional hypothesis preservation and conflict handling."""

    def test_f_long_hypothesis_preserved(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        self.assertEqual(entry.get("pre_expansion_evidence"), ["BOS", "DISPLACEMENT"])

    def test_g_short_hypothesis_preserved(self):
        entry = _fresh_entry(side="SELL", strength="MEDIUM", analysis={
            "displacement": False, "rejection": True, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_SHORT")
        self.assertEqual(entry.get("pre_expansion_evidence"), ["BOS", "REJECTION"])

    def test_h_conflict_does_not_enter(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        }, smart_money_bias="SHORT")
        _, entry, promote = _run_eval(entry, bos=(False, True))
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_CONFLICT")
        promote.assert_not_called()


class PreExpansionInvalidationTest(unittest.TestCase):
    """Test I: invalidation demotes a promoted candidate back to monitoring."""

    def test_i_invalidation_demotes(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")

        entry["analysis"] = dict(entry["analysis"], trap_risk=85)
        _, entry, _ = _run_eval(entry)
        self.assertNotIn("pre_expansion_state", entry)
        self.assertIn("pre_expansion_invalidated_time", entry)


class PreExpansionSchedulingTest(unittest.TestCase):
    """Test J: promoted candidates get higher priority and faster re-analysis."""

    def _promoted_entry(self):
        e = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        e["pre_expansion_state"] = "PRE_EXPANSION_LONG"
        return e

    def _plain_entry(self):
        return _fresh_entry(side="BUY", strength="MEDIUM", analysis={})

    def test_j_priority_boosted(self):
        radar = InstitutionalRadar()
        watchlist = {
            "A/USDT": self._promoted_entry(),
            "B/USDT": self._plain_entry(),
        }
        priorities = radar._calculate_priorities(watchlist)
        self.assertGreater(priorities["A/USDT"], priorities["B/USDT"])

    def test_j_interval_shortened(self):
        radar = InstitutionalRadar()
        promoted = self._promoted_entry()
        plain = self._plain_entry()
        watchlist = {"A/USDT": promoted, "B/USDT": plain}
        priorities = radar._calculate_priorities(watchlist)
        interval_a = radar._get_update_interval("A/USDT", promoted, priorities)
        interval_b = radar._get_update_interval("B/USDT", plain, priorities)
        self.assertLess(interval_a, interval_b)


class PreExpansionStrongUnchangedTest(unittest.TestCase):
    """Test K: STRONG entries keep existing authority; evaluator only adds the
    monitoring overlay and never mutates the entry-gating pre_institutional_state."""

    def test_k_strong_state_machine_untouched(self):
        entry = _fresh_entry(side="BUY", strength="STRONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        entry["pre_institutional_state"] = "BUILDING"
        _, entry, _ = _run_eval(entry)
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        self.assertEqual(entry.get("pre_institutional_state"), "BUILDING")

    def test_k_strong_weak_no_evidence_no_pre_expansion(self):
        entry = _fresh_entry(side="BUY", strength="WEAK", analysis={
            "displacement": False, "rejection": False, "struct_score": 50,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "neutral", "trap_risk": 0,
        })
        _, entry, _ = _run_eval(entry)
        self.assertNotIn("pre_expansion_state", entry)


class PreExpansionNoRegressionScanTest(unittest.TestCase):
    """Test L: the promotion path must never call the queue/entry authority and
    only ever route entries through the existing PRE_ENTRY_READY -> execute_entry
    decision. Guards the integration contract at the source level."""

    def test_l_enum_has_priority_state_and_no_direct_entry_calls(self):
        self.assertTrue(hasattr(PreInstitutionalState, "INSTITUTIONAL_WATCH"))
        import pathlib
        src = pathlib.Path("core/engine.py").read_text(encoding="utf-8")
        # PRE_EXPANSION promotion must never call the queue authority directly.
        i_start = src.find("def _evaluate_pre_expansion")
        i_end = src.find("def _calculate_acceleration")
        body = src[i_start:i_end]
        self.assertNotIn("_promote_to_queue(", body)
        self.assertNotIn("execute_entry(", body)
        # The radar now terminates at Institutional Zone Analysis.
        self.assertIn('_sync_institutional_zone_registry(symbol, entry)', src)
        self.assertNotIn('self._promote_to_queue(symbol, entry)', src)


class _Clock:
    """Controllable monotonic clock so timestamps in the temporal sim are
    deterministic and strictly increasing."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class PreExpansionTemporalProgressionTest(unittest.TestCase):
    """Temporal regression: models the PORTAL-style progression
    MEDIUM -> early evidence -> PRE_EXPANSION -> confirmation -> expansion/STRONG -> entry.

    Simulates one asset evolving across multiple radar updates (T1..T4) driving
    the real evidence evaluator and the existing entry-gating state machine.
    """

    def _t1_entry(self):
        return _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
            "ob_distance": 0.001,
        })

    def test_portal_temporal_progression(self):
        clock = _Clock(1000.0)
        radar = InstitutionalRadar()
        state_machine = engine.PreInstitutionalStateMachine()
        entry = self._t1_entry()
        promote = mock.Mock()
        radar._promote_to_queue = promote

        def step(analysis=None, bos=(False, False), flow=None, df=None):
            frame = df if df is not None else _df()
            px = float(frame["close"].iloc[-1])
            atr = float(engine.compute_atr(frame).iloc[-1]) if len(frame) > 14 else px * 0.01
            with mock.patch.object(engine.time, "time", clock), \
                 mock.patch.object(engine, "detect_bos", return_value=bos), \
                 mock.patch.object(engine.MomentumFlowEngine, "analyze_momentum_flow",
                                   return_value=flow or _NEUTRAL_FLOW), \
                 mock.patch.object(engine, "log_execution"):
                if analysis is not None:
                    entry["analysis"] = analysis
                radar._evaluate_pre_expansion("BTC/USDT:USDT", entry, frame, px, atr, {})

        # ---- T1: MEDIUM + DISPLACEMENT + TREND -----------------------------
        step()
        # Assertion 5: no premature entry at T1.
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        self.assertEqual(entry.get("pre_institutional_state", "IDLE"), "IDLE")
        promote.assert_not_called()
        pre_time = entry.get("pre_expansion_time")
        self.assertIsNotNone(pre_time)
        self.assertEqual(pre_time, 1000.0)

        # ---- T2: momentum/volume/structure evidence improves ----------------
        clock.advance(200)
        step(analysis={
            "displacement": True, "rejection": False, "struct_score": 85,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0, "ob_distance": 0.001,
        }, flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 60, "exhaustion_risk": 10,
        })
        # Still under accelerated institutional analysis, hypothesis intact.
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        promote.assert_not_called()

        # Assertion 7: priority/cadence boost is active while PRE_EXPANSION is on.
        watchlist = {"BTC/USDT:USDT": entry}
        priorities = radar._calculate_priorities(watchlist)
        interval = radar._get_update_interval("BTC/USDT:USDT", entry, priorities)
        self.assertGreaterEqual(priorities["BTC/USDT:USDT"], 40)
        self.assertLessEqual(interval, 8)

        # ---- T3: MSS/BOS + required institutional confirmation --------------
        # The EXISTING authority (state machine) becomes eligible for PRE_ENTRY_READY
        # purely from score/acceleration/ob-distance; PRE_EXPANSION did not cause it.
        # _update_symbol writes the machine's returned state back to the entry,
        # so we drive the full IDLE -> ... -> PRE_ENTRY_READY walk the same way.
        clock.advance(200)
        entry["ob_distance"] = 0.001  # for the CONFIRMED gate
        entry["pre_institutional_state"] = state_machine.update(
            "BTC/USDT:USDT", entry, 95, 5)                          # IDLE -> WATCH
        self.assertEqual(entry["pre_institutional_state"], "WATCH")
        entry["pre_institutional_state"] = state_machine.update(
            "BTC/USDT:USDT", entry, 95, 5)                          # WATCH -> MONITORING
        self.assertEqual(entry["pre_institutional_state"], "MONITORING")
        entry["pre_institutional_state"] = state_machine.update(
            "BTC/USDT:USDT", entry, 95, 3)                          # MONITORING -> BUILDING
        self.assertEqual(entry["pre_institutional_state"], "BUILDING")
        entry["pre_institutional_state"] = state_machine.update(
            "BTC/USDT:USDT", entry, 85, 1)                          # BUILDING -> CONFIRMED
        self.assertEqual(entry["pre_institutional_state"], "CONFIRMED")
        new_state = state_machine.update(
            "BTC/USDT:USDT", entry, 85, 0)                          # CONFIRMED -> PRE_ENTRY_READY
        self.assertEqual(new_state, "PRE_ENTRY_READY")
        # Assertion 4: entry authority unchanged -> eligibility comes from the
        # machine's CONFIRMED gate, not from PRE_EXPANSION.
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        self.assertEqual(entry.get("pre_institutional_state"), "CONFIRMED")

        # ---- T3->T4 early expansion: zone breaks + volume expands + bullish
        # structure => EARLY_EXPANSION phase begins (before the STRONG stage).
        clock.advance(200)
        step(analysis={
            "displacement": True, "rejection": False, "struct_score": 60,
            "liq_score": 85, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0, "ob_distance": 0.0005,
        }, flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 5,
        }, df=_bull_df())
        early_time = entry["pre_expansion"]["early_expansion_time"]
        self.assertIsNotNone(early_time)
        self.assertEqual(entry["pre_expansion"]["phase"], "EARLY_EXPANSION")
        # PORTAL: ideal entry window is at/around EARLY_EXPANSION, which the
        # existing authority would confirm — PRE_EXPANSION still never opens.
        promote.assert_not_called()

        # ---- T4: STRONG / expansion / shock occurs -------------------------
        # Strength naturally becomes STRONG at a later stage (t=1600).
        clock.t = 1600.0
        strong_time = clock.t
        self.assertEqual(strong_time, 1600.0)

        # PORTAL ORDERING: pre_expansion < early_expansion < strong.
        self.assertLess(entry["pre_expansion_time"], early_time)
        self.assertLess(early_time, strong_time)
        # Assertion 1: PRE_EXPANSION timestamp < STRONG timestamp.
        self.assertLess(entry["pre_expansion_time"], strong_time)
        # Assertion 2: PRE_EXPANSION occurred before the major expansion.
        self.assertLess(entry["pre_expansion_time"], clock.t)
        # Assertion 6: STRONG was NOT manufactured at T1; it is a later stage.
        self.assertEqual(self._t1_entry()["strength"], "MEDIUM")
        self.assertLessEqual(entry.get("pre_expansion_confidence", 0.0), 100.0)

        # ---- Assertion 8: invalidation still demotes -------------------------
        clock.advance(100)
        step(analysis={
            "displacement": False, "rejection": False, "struct_score": 40,
            "liq_score": 30, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "neutral", "trap_risk": 90, "ob_distance": 0.001,
        })
        self.assertNotIn("pre_expansion_state", entry)
        self.assertIn("pre_expansion_invalidated_time", entry)
        # PRE_EXPANSION never queued or opened anything at any stage.
        promote.assert_not_called()


class ZoneFirstAnalysisTest(unittest.TestCase):
    """Requirements 2 & 4: zone-first institutional zone analysis and
    immediate promotion of MEDIUM + meaningful evidence."""

    def _bully(self, **analysis):
        base = {
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 85, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0, "ob_distance": 0.001,
        }
        base.update(analysis)
        return _fresh_entry(side="BUY", strength="MEDIUM", analysis=base)

    def test_medium_ob_immediate_zone_analysis(self):
        entry = self._bully()
        _, entry, prom = _run_eval_df(entry, _bull_df(),
                                     flow={"trend_expansion": True, "flow_bias": "BUY",
                                           "momentum_decay": False, "continuation_strength": 70,
                                           "exhaustion_risk": 5})
        # Immediate promotion: MEDIUM + institutional evidence, no STRONG needed.
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        rich = entry["pre_expansion"]
        self.assertEqual(rich["zone"], "OB")
        self.assertGreaterEqual(rich["zone_quality"], 0.5)
        self.assertEqual(rich["zone_verdict"], "ACCUMULATION")
        prom.assert_not_called()

    def test_zone_quality_mapping(self):
        for grade, expected in [("A+", 1.0), ("A", 0.85), ("B", 0.6), ("NONE", 0.0)]:
            e = self._bully(ob_grade=grade)
            if grade == "NONE":
                e["analysis"]["liq_score"] = 50
                e["analysis"]["displacement"] = False
                e["analysis"]["struct_score"] = 50
                e["analysis"]["ob_distance"] = 0.5  # far from any zone
                e["analysis"].pop("zones", None)
            _, e, _ = _run_eval_df(e, _bull_df())
            self.assertEqual(e["pre_expansion"]["zone_quality"], expected, grade)
            self.assertEqual(e["pre_expansion"]["zone_quality"], expected, grade)

    def test_volume_inside_zone(self):
        e = self._bully(vol_state="expansion")
        _, e, _ = _run_eval_df(e, _bull_df())
        self.assertEqual(e["pre_expansion"]["volume_in_zone"], 1.0)
        e2 = self._bully(vol_state="neutral")
        _, e2, _ = _run_eval_df(e2, _bull_df())
        self.assertEqual(e2["pre_expansion"]["volume_in_zone"], 0.0)

    def test_liquidity_around_zone(self):
        e = self._bully(liq_score=90)
        _, e, _ = _run_eval_df(e, _bull_df())
        self.assertGreaterEqual(e["pre_expansion"]["liquidity_around_zone"], 0.8)

    def test_fvg_imbalance_evidence(self):
        e = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 60, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        }, reasons=["FVG", "Imbalance", "Displacement"])
        _, e, _ = _run_eval_df(e, _bull_df())
        self.assertEqual(e.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        ev = e.get("pre_expansion_evidence", [])
        self.assertIn("FVG", ev)
        self.assertIn("IMBALANCE", ev)


class IndicatorTransitionTest(unittest.TestCase):
    """Requirements 3 & 6: TradingView evidence engine remembers indicator
    state transitions (weak -> improving -> aligned -> early expansion)."""

    def test_indicator_transition_memory(self):
        radar = InstitutionalRadar()
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "normal", "trap_risk": 0,
        })
        flow = {"trend_expansion": False, "flow_bias": "NEUTRAL",
                "momentum_decay": False, "continuation_strength": 20,
                "exhaustion_risk": 10}
        # First pass: neutral/build phase.
        with mock.patch.object(engine.MomentumFlowEngine, "analyze_momentum_flow", return_value=flow), \
             mock.patch.object(engine, "log_execution"):
            radar._evaluate_pre_expansion("X/USDT:USDT", entry, _df(), 100.0, 1.0, {})
        self.assertEqual(len(entry["indicator_history"]), 1)
        # Second pass: transition into EARLY_EXPANSION with improving evidence.
        flow2 = {"trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
                 "continuation_strength": 80, "exhaustion_risk": 5}
        with mock.patch.object(engine.MomentumFlowEngine, "analyze_momentum_flow", return_value=flow2), \
             mock.patch.object(engine, "log_execution"):
            radar._evaluate_pre_expansion("X/USDT:USDT", entry, _bull_df(),
                                          float(_bull_df()["close"].iloc[-1]), 1.0, {})
        self.assertGreaterEqual(len(entry["indicator_history"]), 2)
        self.assertIn("phase", entry["indicator_history"][-1])
        self.assertEqual(entry["pre_expansion"]["phase"], "EARLY_EXPANSION")

    def test_bullish_pre_expansion_aggregates_indicators(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "normal", "trap_risk": 0,
        })
        _, entry, _ = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 5})
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        ev = entry.get("pre_expansion_evidence", [])
        self.assertIn("VWMA_TREND", ev)
        self.assertIn("SMC_MSS", ev)
        self.assertEqual(entry["pre_expansion"]["indicator_alignment"], "BULLISH")

    def test_bearish_pre_expansion(self):
        entry = _fresh_entry(side="SELL", strength="MEDIUM",
                             smart_money_bias="SHORT", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "normal", "trap_risk": 0,
        })
        _, entry, _ = _run_eval_df(entry, _bear_df(), flow={
            "trend_expansion": True, "flow_bias": "SELL", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 5})
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_SHORT")
        self.assertEqual(entry["pre_expansion"]["indicator_alignment"], "BEARISH")


class EarlyExpansionPhaseTest(unittest.TestCase):
    """Requirement 5 & objective 10: detect the BEGINNING of expansion, not the
    late-stage move; never chase OVEREXTENDED / EXHAUSTION."""

    def test_early_expansion_classified_entryable(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 60,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, prom = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 5})
        self.assertEqual(entry["pre_expansion"]["phase"], "EARLY_EXPANSION")
        self.assertIsNotNone(entry["pre_expansion"].get("early_expansion_time"))
        # Even at EARLY_EXPANSION, PRE_EXPANSION itself does not open a trade.
        prom.assert_not_called()

    def test_late_expansion_rejected_when_overextended(self):
        # Far beyond VWAP + huge body => OVEREXTENDED (late-stage) => not entryable.
        closes = np.concatenate([np.linspace(100, 105, 255), np.array([180.0])])
        opens = np.concatenate([[closes[0]], closes[:-1]])
        over = pd.DataFrame({
            "open": opens, "high": opens + 2, "low": opens - 2,
            "close": closes, "volume": np.linspace(1000, 5000, 256),
        })
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0,
        })
        _, entry, prom = _run_eval_df(entry, over)
        self.assertIn(entry["pre_expansion"]["phase"], ("OVEREXTENDED", "EXHAUSTION"))
        prom.assert_not_called()

    def test_exhaustion_chase_prevention(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "exhaustion", "trap_risk": 0,
        }, exhaustion_risk=80, momentum_decay=True)
        _, entry, prom = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": False, "flow_bias": "NEUTRAL", "momentum_decay": True,
            "continuation_strength": 10, "exhaustion_risk": 80})
        self.assertEqual(entry["pre_expansion"]["phase"], "EXHAUSTION")
        prom.assert_not_called()


class ConflictProtectionTest(unittest.TestCase):
    """Requirement 8: zone bullish but TradingView evidence bearish -> conflict,
    no entry; keep monitoring."""

    def test_zone_indicator_conflict_no_entry(self):
        # OB/zone bullish (A grade, side BUY) but momentum flow bearish.
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="SHORT", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "normal", "trap_risk": 0,
        })
        _, entry, prom = _run_eval_df(entry, _bear_df(), flow={
            "trend_expansion": True, "flow_bias": "SELL", "momentum_decay": False,
            "continuation_strength": 60, "exhaustion_risk": 5})
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_CONFLICT")
        prom.assert_not_called()
        # Still monitored (rich snapshot persists, no demote).
        self.assertIn("zone_verdict", entry["pre_expansion"])


class AuthorityUnchangedTest(unittest.TestCase):
    """Requirement 9: existing institutional entry authority remains the sole
    decider; PRE_EXPANSION never mutates the entry-gating state."""

    def test_entry_authority_untouched_by_zone_analysis(self):
        entry = _fresh_entry(side="BUY", strength="MEDIUM",
                             smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0,
        })
        entry["pre_institutional_state"] = "BUILDING"
        _, entry, prom = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 5})
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        self.assertEqual(entry.get("pre_institutional_state"), "BUILDING")
        prom.assert_not_called()
        self.assertEqual(entry["pre_expansion"]["zone_verdict"], "ACCUMULATION")


class LiveDataDepthTest(unittest.TestCase):
    """F1: the live institutional path must source enough OHLCV for the
    EMA200 / HTF evidence, and too-shallow feeds must be gated as unavailable
    instead of silently trusting absent signals."""

    def test_evidence_depth_constant_supports_ema200_and_htf(self):
        # EMA200 needs >= 200 bars; the HTF EMA200-slope proxy needs a 21-bar
        # window on top (>= 210). The constant used on the live path must cover
        # both.
        self.assertGreaterEqual(engine.INSTITUTIONAL_OHLCV_DEPTH, 210)

    def test_htf_ema200_fire_with_full_live_depth(self):
        radar = engine.InstitutionalRadar()
        df = _bull_df(n=engine.INSTITUTIONAL_OHLCV_DEPTH)
        atr = float(engine.compute_atr(df).iloc[-1])
        res = radar._compute_tv_indicators(df, atr)
        self.assertTrue(res["data_depth"]["ema200"])
        self.assertTrue(res["data_depth"]["htf"])
        self.assertTrue(res["tv"]["htf"]["available"])
        self.assertTrue(res["tv"]["ema"].get("htf_available"))
        self.assertIn("HTF_TREND", res["ipa"]["bull"])
        self.assertIn("EMA_TREND", res["ipa"]["bull"])

    def test_htf_ema200_gated_unavailable_when_shallow(self):
        # A shallow live feed (exchange cap) must be explicitly unusable,
        # never counted as neutral/valid evidence.
        radar = engine.InstitutionalRadar()
        res = radar._compute_tv_indicators(_df(), 1.0)
        self.assertFalse(res["data_depth"]["htf"])
        self.assertFalse(res["tv"]["htf"]["available"])
        self.assertFalse(res["tv"]["ema"].get("htf_available"))
        self.assertNotIn("HTF_TREND", res["ipa"]["bull"])
        self.assertNotIn("EMA_TREND", res["ipa"]["bull"])
        self.assertEqual(res["alignment"], "NEUTRAL")
        self.assertFalse(res["data_depth"]["ema200"])

    def test_live_institutional_path_requests_evidence_depth(self):
        # The institutional radar path must actually ASK for the deep feed, not
        # just compute on the legacy 100-bar snapshot.
        radar = engine.InstitutionalRadar()
        entry = _fresh_entry(side="BUY", strength="MEDIUM", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 50, "ob_grade": "NONE", "roro_signal": False,
            "vol_state": "expansion", "trap_risk": 0,
        })
        calls = []
        deep = _bull_df(n=engine.INSTITUTIONAL_OHLCV_DEPTH)
        shallow = _bull_df(n=100)
        # Live OHLCV frames carry a timestamp column; is_valid_dataframe
        # requires it, so mirror the real feed on both frames.
        for frame in (deep, shallow):
            frame["timestamp"] = np.arange(len(frame)).astype(float)

        def fake_get_ohlcv(symbol, limit, htf=False):
            calls.append(limit)
            return deep if limit == engine.INSTITUTIONAL_OHLCV_DEPTH else shallow

        with mock.patch.object(engine, "get_ohlcv_safe", side_effect=fake_get_ohlcv), \
             mock.patch.object(engine, "get_orderbook_cached", return_value={}), \
             mock.patch.object(engine.InstitutionalIntentEngine, "detect",
                               return_value=(10, "NEUTRAL", {})), \
             mock.patch.object(engine, "log_execution"):
            radar._update_symbol("BTC/USDT:USDT", entry)
        self.assertIn(100, calls)
        self.assertIn(engine.INSTITUTIONAL_OHLCV_DEPTH, calls)
        # The deep feed actually reaches the evidence layer (HTF available).
        self.assertTrue(entry["pre_expansion"]["data_depth"]["htf"])


class FreshMomentumInvalidationTest(unittest.TestCase):
    """F2: invalidation/demotion must use momentum/exhaustion computed on this
    tick, never the values frozen at watchlist-entry creation."""

    def _healthy_entry(self):
        return _fresh_entry(side="BUY", strength="MEDIUM",
                            smart_money_bias="LONG", analysis={
            "displacement": True, "rejection": False, "struct_score": 80,
            "liq_score": 80, "ob_grade": "A", "roro_signal": True,
            "vol_state": "expansion", "trap_risk": 0,
        })

    def test_fresh_exhaustion_demotes_immediately(self):
        entry = self._healthy_entry()
        # Stale entry-level snapshot stays healthy on purpose.
        entry["momentum_expansion"] = True
        entry["momentum_decay"] = False
        entry["exhaustion_risk"] = 0
        _, entry, prom = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 5})
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        prom.assert_not_called()

        # Same tick of the SAME entry: fresh flow collapses. No watchlist
        # re-creation -> fresh exhaustion_risk (80) must demote immediately.
        _, entry, prom = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": False, "flow_bias": "NEUTRAL",
            "momentum_decay": True, "continuation_strength": 10,
            "exhaustion_risk": 80})
        self.assertNotIn("pre_expansion_state", entry)
        self.assertEqual(entry["pre_expansion"]["phase"], "INVALIDATED")
        self.assertIn("pre_expansion_invalidated_time", entry)
        prom.assert_not_called()

    def test_stale_entry_exhaustion_no_longer_demotes(self):
        # Negative control: a high exhaustion frozen into the entry at
        # watchlist-creation must NOT demote a currently-healthy candidate.
        entry = self._healthy_entry()
        entry["momentum_expansion"] = False
        entry["momentum_decay"] = True
        entry["exhaustion_risk"] = 90
        _, entry, prom = _run_eval_df(entry, _bull_df(), flow={
            "trend_expansion": True, "flow_bias": "BUY", "momentum_decay": False,
            "continuation_strength": 80, "exhaustion_risk": 10})
        self.assertEqual(entry.get("pre_expansion_state"), "PRE_EXPANSION_LONG")
        prom.assert_not_called()


if __name__ == "__main__":
    unittest.main()
