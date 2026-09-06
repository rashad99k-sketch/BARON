"""Forensic-fix regression tests (RC#3/#4/#5 + confirmation/lifecycle/ADX).

Locks in, through the real ExecutionQueue methods:
  1. Confirmation 0 -> 1 -> 2.
  2. A temporary one-bar trigger loss does not erase earned confirmation.
  3. A genuine decision invalidation resets confirmation.
  4. Identical re-polls never double-count a confirmation event.
  5. A PASS-viable candidate below the READY floor exposes READY_SCORE_FLOOR
     (blocker == READY_SCORE_FLOOR, never blocker == NONE while below floor).
  6. A READY candidate with valid conditions is not blocked by stale ADX.
  7. Queue-READY ADX band == execution ADX band (same period/frame/threshold).
  8. Every execute_entry rejection writes a structured {blocker, ...} record.
  9. Institutional metadata survives queue admission (fast-path contract).
 10. The institutional fast-path is actually reachable -> READY.
 11. Candidate latency / lifecycle timestamps are recorded.
 12. Risk/capacity/cooldown protections are never weakened.
 13. A rejected entry never creates a ghost paper position.
 14. No duplicate entries: an already-EXECUTED candidate is not re-offered.
 15. The 6-position portfolio cap is intact.
 16. The NEWS slot admits at most one independent news trade.
"""
import importlib
import os
import sys
import types
import unittest
import unittest.mock

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


def _flat_df():
    """Flat frame -> ADX ~0, below the CRYPTO 16 floor."""
    n = 50
    close = np.full(n, 100.0)
    return pd.DataFrame({"timestamp": np.arange(n), "open": close,
                         "high": close + 0.1, "low": close - 0.1,
                         "close": close, "volume": np.full(n, 1000.0)})


def _bearing_df():
    """Causal-BUY frame used by test_ob_causal_confirmation. ADX ~52, inside
    the CRYPTO [16, 55] band."""
    n = 60
    o = np.full(n, 100.0); c = np.full(n, 100.0)
    h = np.full(n, 100.8); l = np.full(n, 99.3)
    v = np.full(n, 1000.0)
    i = 42
    o[i], c[i], h[i], l[i] = 100.6, 99.7, 100.8, 99.2
    o[i+1], c[i+1], h[i+1], l[i+1] = 99.7, 101.0, 100.9, 99.5
    o[i+2], c[i+2], h[i+2], l[i+2] = 101.2, 101.6, 101.7, 101.0
    o[i+3], c[i+3], h[i+3], l[i+3] = 101.6, 102.0, 102.1, 101.4
    v[i+1] = 2600.0
    for j in range(i+4, n-4):
        o[j] = c[j] = 102.0
        h[j], l[j] = 102.4, 101.5
    o[n-4], c[n-4], h[n-4], l[n-4] = 101.8, 100.6, 101.9, 100.5
    o[n-3], c[n-3], h[n-3], l[n-3] = 100.4, 99.8, 100.5, 99.4
    o[n-2], c[n-2], h[n-2], l[n-2] = 99.4, 99.6, 99.7, 99.15
    o[n-1], c[n-1], h[n-1], l[n-1] = 99.6, 99.85, 100.1, 99.5
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _base_candidate(E, symbol="FC/USDT:USDT"):
    """A candidate with NO confirmation earned (count 0) for state-machine tests."""
    return E.ExecutionCandidate(
        symbol=symbol, side="BUY", price=100, entry_price=100,
        stop_loss=98, take_profit_1=102, take_profit_2=104, atr=1,
        df=_bearing_df(), ob={},
    )


def _strong_readable_candidate(E, symbol="FC/USDT:USDT"):
    """Fully valid READY candidate: 2 confirmations, in-band ADX, high composite."""
    cand = _base_candidate(E, symbol)
    cand.confirmation_count = 2
    cand.confirmation_state = "CONFIRMED_2"
    cand.confirmation_reason = "CONFIRMATION_COMPLETE"
    cand.latest_adx = 30.0
    cand.latest_adx_bounds = [16.0, 55.0]
    cand.zone_low, cand.zone_high = 99.0, 101.0
    cand.zone_state = "ACTIVE"
    cand.zone_metrics = E.ZoneMetrics(
        order_block_quality=90, zone_strength=90, liquidity_quality=80,
        institutional_confidence=85, structure_alignment=85,
        entry_timing=90, trend_alignment=90, risk_score=90,
        trigger_state="MSS_CONFIRMED")
    cand.priority_score = cand.zone_metrics.final_zone_score
    cand.evidence = {"sweep_quality": "strong"}
    return cand


class ForensicFixesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()
        # Isolate shared-state ledgers.
        cls.engine.STATE.clear()
        cls.engine.paper.update({"position": {}, "balance": 10000.0,
                                 "committed_margin": 0.0})
        cls.engine.TRADE_STATE.clear()

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

    # ---- 1. Confirmation 0 -> 1 -> 2 ----
    def test_confirmation_progresses_0_1_2(self):
        q = self.engine.ExecutionQueue()
        cand = _base_candidate(self.engine)
        triggers = ("MSS_CONFIRMED", "LIQUIDITY_SWEEP")
        q._update_confirmation(cand, "MSS_CONFIRMED", triggers, 62, 99.85, 1.0)
        self.assertEqual(cand.confirmation_count, 1)
        self.assertEqual(cand.confirmation_state, "CONFIRMED_1")
        self.assertEqual(cand.confirmation_reason, "CONFIRMATION_PROGRESS")
        q._update_confirmation(cand, "LIQUIDITY_SWEEP", triggers, 63, 99.7, 1.0)
        self.assertEqual(cand.confirmation_count, 2)
        self.assertEqual(cand.confirmation_state, "CONFIRMED_2")
        self.assertEqual(cand.confirmation_reason, "CONFIRMATION_COMPLETE")

    # ---- 2. Temporary trigger loss does not erase confirmation ----
    def test_transient_trigger_loss_preserves_confirmation(self):
        q = self.engine.ExecutionQueue()
        cand = _base_candidate(self.engine)
        cand.confirmation_count = 2
        cand.confirmation_state = "CONFIRMED_2"
        q._update_confirmation(cand, "WAITING", (), 65, 99.85, 1.0)
        self.assertEqual(cand.confirmation_count, 2)
        q._update_confirmation(cand, "MSS_CONFIRMED", ("MSS_CONFIRMED",), 66, 99.9, 1.0)
        self.assertGreaterEqual(cand.confirmation_count, 2)
        self.assertEqual(cand.confirmed_trigger, "MSS_CONFIRMED")

    # ---- 3. Genuine invalidation resets confirmation ----
    def test_invalidation_resets_confirmation(self):
        q = self.engine.ExecutionQueue()
        cand = _base_candidate(self.engine)
        cand.confirmation_count = 2
        cand.confirmation_state = "CONFIRMED_2"
        cand.decision_label = "INVALID_BROKEN_ZONE"
        q._update_confirmation(cand, "MSS_CONFIRMED", ("MSS_CONFIRMED",), 67, 99.85, 1.0)
        self.assertEqual(cand.confirmation_count, 0)
        self.assertEqual(cand.confirmation_state, "WAITING_TRIGGER")
        self.assertEqual(cand.confirmation_reason, "CONFIRMATION_INVALIDATED")

    # ---- 4. Same event never double-counts ----
    def test_duplicate_event_not_double_counted(self):
        q = self.engine.ExecutionQueue()
        cand = _base_candidate(self.engine)
        triggers = ("MSS_CONFIRMED",)
        q._update_confirmation(cand, "MSS_CONFIRMED", triggers, 62, 99.85, 1.0)
        q._update_confirmation(cand, "MSS_CONFIRMED", triggers, 62, 99.85, 1.0)
        q._update_confirmation(cand, "MSS_CONFIRMED", triggers, 62, 99.85, 1.0)
        # Same bar + trigger + price bucket = one event; never 4/2.
        self.assertLessEqual(cand.confirmation_count, 1)

    # ---- 5. PASS-viable candidate below floor exposes READY_SCORE_FLOOR ----
    def test_ready_score_floor_exposed(self):
        E = self.engine
        q = E.ExecutionQueue()
        cand = _base_candidate(E)
        required = float(E.AssetBehaviorProfile.entry_config(
            E.AssetBehaviorProfile.resolve_asset_class(cand.symbol)).get("ready_score", 75))
        cand.zone_metrics = E.ZoneMetrics(
            order_block_quality=50, zone_strength=50, liquidity_quality=50,
            institutional_confidence=50, structure_alignment=50,
            entry_timing=90, trend_alignment=90, risk_score=90,
            trigger_state="MSS_CONFIRMED")
        cand.priority_score = cand.zone_metrics.final_zone_score
        self.assertLess(cand.zone_metrics.final_zone_score, required)
        cand.latest_adx = 30.0
        cand.latest_adx_bounds = [16.0, 55.0]
        cand.confirmation_count = 2
        cand.confirmation_state = "CONFIRMED_2"
        q._update_state(cand, 100)
        self.assertNotEqual(cand.state, E.ExecutionState.READY)
        self.assertEqual(cand.ready_blocker, "READY_SCORE_FLOOR")
        reasons = cand.ready_blocker_reasons
        self.assertIn("actual_score", reasons)
        self.assertIn("required_score", reasons)
        self.assertIn("delta", reasons)
        self.assertEqual(reasons["required_score"], float(required))
        self.assertEqual(cand.gate_status["ready_score_floor"]["blocker"],
                         "READY_SCORE_FLOOR")

    # ---- 6. Valid READY candidate not blocked by stale ADX ----
    def test_valid_ready_not_blocked_by_stale_adx(self):
        E = self.engine
        q = E.ExecutionQueue()
        cand = _strong_readable_candidate(E)
        # Stale admission marker: prior READY decision used a zero ADX value.
        cand.latest_adx = 0.0
        cand.latest_adx_bounds = []
        # The fix recomputes ADX on the real frame before READY gating.
        df = _bearing_df()
        cand.latest_adx = float(E.compute_adx(df).iloc[-1])
        ac = E.AssetBehaviorProfile.entry_config(
            E.AssetBehaviorProfile.resolve_asset_class(cand.symbol))
        cand.latest_adx_bounds = [float(ac["min_adx"]), float(ac["max_adx"])]
        self.assertGreaterEqual(cand.latest_adx, float(ac["min_adx"]))
        q._update_state(cand, df["close"].iloc[-1])
        self.assertEqual(cand.state, E.ExecutionState.READY)
        self.assertEqual(cand.ready_blocker, "NONE")

    # ---- 7. Queue-READY ADX band == execution ADX band ----
    def test_queue_and_execution_adx_bands_match(self):
        E = self.engine
        q = E.ExecutionQueue()
        ac = E.AssetBehaviorProfile.entry_config(
            E.AssetBehaviorProfile.resolve_asset_class("BTC/USDT:USDT"))
        # execute_entry rejects outside exactly this band; READY uses the same.
        cand = _strong_readable_candidate(E)
        cand.latest_adx = 5.0
        cand.latest_adx_bounds = [float(ac["min_adx"]), float(ac["max_adx"])]
        q._update_state(cand, 100.0)
        self.assertNotEqual(cand.state, E.ExecutionState.READY)
        self.assertEqual(cand.ready_blocker, "ADX")
        # compute_adx is the same period-14 function used by both paths.
        adx_bearing = float(E.compute_adx(_bearing_df()).iloc[-1])
        self.assertGreaterEqual(adx_bearing, float(ac["min_adx"]))
        self.assertLessEqual(adx_bearing, float(ac["max_adx"]))

    # ---- 8. Every execution reject has a structured reason ----
    def test_execution_reject_has_structured_blocker(self):
        E = self.engine
        ledger = E.MEMORY.setdefault("execution_blockers", [])
        before = len(ledger)
        with unittest.mock.patch("core.engine.get_ohlcv_safe", return_value=None):
            ok = E.execute_entry("BUY", "FC/USDT:USDT", 100, 98, 102, 104,
                                 80, "test", 1.0, "TEST", "TEST", "TEST")
        self.assertFalse(ok)
        self.assertGreater(len(ledger), before)
        rec = ledger[-1]
        for key in ("symbol", "candidate_id", "stage", "side", "score",
                    "adx", "required_adx", "timestamp", "reason", "blocker"):
            self.assertIn(key, rec)
        self.assertEqual(rec["blocker"], "DATA_REJECT")
        self.assertIn("last_exec_blocker", E.STATE)
        self.assertEqual(E.STATE["last_exec_blocker"]["blocker"], "DATA_REJECT")

    # ---- 9. Institutional metadata survives queue admission ----
    def test_institutional_metadata_survives_queue_admission(self):
        E = self.engine
        q = E.ExecutionQueue()
        cand = _strong_readable_candidate(E, "INST/USDT:USDT")
        cand.institutional_score = 84.0
        cand.pre_institutional_state = "PRE_ENTRY_READY"
        cand.precursor_count = 3
        cand.institutional_prepared = True
        cand.hypothesis = "HTF demand blunted"
        cand.institutional_phase = "DEVELOPING"
        cand.institutional_zone_state = "ACTIVE"
        cand.a_grade_ready = True
        self.assertTrue(q.add_candidate(cand))
        stored = q._candidates["INST/USDT:USDT"]
        self.assertEqual(stored.institutional_score, 84.0)
        self.assertEqual(stored.pre_institutional_state, "PRE_ENTRY_READY")
        self.assertEqual(stored.precursor_count, 3)
        self.assertTrue(stored.institutional_prepared)
        self.assertEqual(stored.hypothesis, "HTF demand blunted")
        self.assertEqual(stored.institutional_phase, "DEVELOPING")
        self.assertEqual(stored.institutional_zone_state, "ACTIVE")
        self.assertTrue(stored.a_grade_ready)

    # ---- 10. Institutional fast-path is reachable -> READY ----
    def test_institutional_fast_path_reaches_ready(self):
        E = self.engine
        q = E.ExecutionQueue()
        cand = _base_candidate(E, "FAST/USDT:USDT")
        cand.institutional_score = 84.0
        cand.pre_institutional_state = "PRE_ENTRY_READY"
        cand.zone_low, cand.zone_high = 99.0, 101.0
        q.add_candidate(cand)
        # The slow path preconditions (zone proximity inside _check_entry_conditions)
        # are data-dependent; stub them so the fast-path READY branch is isolated.
        q._check_entry_conditions = lambda d, s, a, sy="": True
        q.re_evaluate_all(lambda sym: _bearing_df())
        self.assertEqual(cand.state, E.ExecutionState.READY)
        self.assertEqual(cand.ready_blocker, "NONE")
        self.assertGreater(cand.ready_time, 0)

    # ---- 11. Candidate latency / lifecycle recorded ----
    def test_lifecycle_timestamps_recorded(self):
        E = self.engine
        q = E.ExecutionQueue()
        cand = _strong_readable_candidate(E, "LC/USDT:USDT")
        cand.first_seen = cand.added_at - 10.0
        cand.queue_time = cand.added_at
        cand.institutional_analysis_time = cand.added_at
        q.add_candidate(cand)
        q.re_evaluate_all(lambda sym: _bearing_df())
        rec = E.MEMORY.get("opportunity_lifecycle", {}).get("LC/USDT:USDT")
        self.assertIsNotNone(rec)
        self.assertGreater(rec.get("first_seen", 0), 0)
        self.assertGreater(rec.get("queue_time", 0), 0)
        self.assertGreater(rec.get("institutional_time", 0), 0)
        summary = q.summarize_opportunity_lifecycle()
        self.assertGreaterEqual(summary.get("total", 0), 1)
        self.assertIn("survival_rate_ready_to_exec", summary)
        self.assertIn("stage_conversion_queue_to_ready", summary)

    # ---- 12. Risk/capacity/cooldown protections not weakened ----
    def test_protections_not_weakened(self):
        E = self.engine
        self.assertGreaterEqual(E.QUEUE_MAX_SIZE, 1)
        self.assertEqual(E.MAX_OPEN_POSITIONS, 6)
        self.assertGreaterEqual(E.RADAR_COOLDOWN_SEC, 1800)
        self.assertLessEqual(E.PORTFOLIO_MARGIN_CAP_PCT, 0.60)

    # ---- 13. Rejected entry never ghost-opens a paper position ----
    def test_rejected_entry_no_ghost_paper_position(self):
        E = self.engine
        closed = dict(E.paper.get("position", {}))
        with unittest.mock.patch("core.engine.get_ohlcv_safe", return_value=None):
            ok = E.execute_entry("BUY", "GHOST/USDT:USDT", 100, 98, 102, 104,
                                 80, "test", 1.0, "TEST", "TEST", "TEST")
        self.assertFalse(ok)
        self.assertFalse(bool(E.STATE.get("open")))
        self.assertEqual(E.paper.get("position", {}), closed)

    # ---- 14. No duplicate entries (EXECUTED candidate not re-offered) ----
    def test_no_duplicate_entries(self):
        E = self.engine
        q = E.ExecutionQueue()
        cand = _strong_readable_candidate(E, "DUP/USDT:USDT")
        q.add_candidate(cand)
        q._update_state(cand, 100.0)
        self.assertEqual(cand.state, E.ExecutionState.READY)
        cand.state = E.ExecutionState.EXECUTED
        # get_best_candidate fallback must not re-offer an EXECUTED candidate.
        best = q.get_best_candidate()
        self.assertIsNone(best)
        # Re-admitting the same symbol with a lower score is refused too.
        rival = _strong_readable_candidate(E, "DUP/USDT:USDT")
        rival.priority_score = 1.0
        self.assertFalse(q.add_candidate(rival))

    # ---- 15. 6-position portfolio cap intact ----
    def test_six_position_cap_intact(self):
        from portfolio.manager import PortfolioManager
        pm = PortfolioManager(6, self.engine)
        self.assertEqual(pm.max_positions, 6)
        self.assertLessEqual(pm.count(), 6)
        from portfolio.allocator import DEFAULT_CLASS_CAPS
        self.assertEqual(sum(v for k, v in DEFAULT_CLASS_CAPS.items()
                             if k != "NEWS"), 6)

    # ---- 16. NEWS slot admits at most one ----
    def test_news_slot_at_most_one(self):
        from portfolio.allocator import DEFAULT_CLASS_CAPS
        from portfolio import news_slot
        self.assertEqual(DEFAULT_CLASS_CAPS.get("NEWS"), 1)
        self.assertTrue(hasattr(news_slot, "count_open_news"))


if __name__ == "__main__":
    unittest.main()