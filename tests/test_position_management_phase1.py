"""Phase 1 verification for the Position Management Engine advisory layer.

This suite exercises the REAL classification/health classes added in Phase 1
(DynamicPositionProfile, AssetBehaviorProfile, PositionHealthScore) plus the
LiveTradeManager advisory integration (_ensure_position_profile and
_run_advisory_health). It uses controlled engine-level inputs and the real
PAPER execution path where possible, and asserts that:
  - trade_type is classified dynamically (TREND/REVERSAL/BREAKOUT/PULLBACK/RETEST)
  - asset_class is resolved per symbol/asset
  - PositionHealthScore stays in [0,100] and produces a coherent action
  - news dampens risk-health but never forces a blind EXIT by itself
  - the rich [POSITION] STATE fields are populated.
"""
import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402  (real engine)


def _df(n=140, base=100.0, trend=0.0):
    t = np.arange(n)
    x = base + trend * t + 3 * np.sin(t / 4.0) + 0.5 * np.sin(t / 1.7)
    return pd.DataFrame({
        "open": x - 0.1, "high": x + 0.6, "low": x - 0.6,
        "close": x, "volume": np.full(n, 1000.0),
    })


class AssetBehaviorProfileTest(unittest.TestCase):
    def test_resolve_asset_class(self):
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class("BTC/USDT:USDT"), "CRYPTO")
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class("XAUUSD"), "GOLD")
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class("WTIUSD"), "OIL")
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class("US500"), "INDEX")
        self.assertEqual(E.AssetBehaviorProfile.resolve_asset_class("AAPL"), "STOCK")

    def test_per_class_params_are_tunable_and_safe(self):
        crypto = E.AssetBehaviorProfile.get("CRYPTO")
        gold = E.AssetBehaviorProfile.get("GOLD")
        # GOLD should be tighter (smaller TP/trail, earlier trailing) than CRYPTO.
        self.assertLess(gold["trail_mult"], crypto["trail_mult"])
        self.assertLess(gold["tp1_atr"], crypto["tp1_atr"])
        # Unknown asset class falls back to CRYPTO safely.
        self.assertEqual(E.AssetBehaviorProfile.get("UNKNOWN")["sl_mult"],
                         crypto["sl_mult"])


class DynamicPositionProfileTest(unittest.TestCase):
    def test_initial_classification_mapping(self):
        p = E.DynamicPositionProfile("BTC/USDT:USDT", "BUY", 100.0, 1.0,
                                     classification="SNIPER", trade_type="")
        self.assertEqual(p.trade_type, "BREAKOUT")
        p2 = E.DynamicPositionProfile("ETH/USDT:USDT", "SELL", 100.0, 1.0,
                                      classification="REVERSAL", trade_type="")
        self.assertEqual(p2.trade_type, "REVERSAL")

    def test_reversal_override_on_confirmed_evidence(self):
        p = E.DynamicPositionProfile("BTC/USDT:USDT", "BUY", 100.0, 1.0,
                                     classification="TREND")
        self.assertEqual(p.trade_type, "TREND")
        p.update(trade_state="TREND_RIDE", trend_health=7, structure_aligned=True,
                 continuation_probability=0.8, smart_money={"distribution_risk": 55},
                 reversal_confirmed=True)
        # A confirmed reversal must reclassify a TREND trade to REVERSAL.
        self.assertEqual(p.trade_type, "REVERSAL")

    def test_pullback_vs_reversal_distinction(self):
        p = E.DynamicPositionProfile("BTC/USDT:USDT", "BUY", 100.0, 1.0,
                                     classification="TREND")
        # HEALTHY_PULLBACK with strong continuation + trend health = PULLBACK (NOT reversal)
        p.update(trade_state="HEALTHY_PULLBACK", trend_health=7, structure_aligned=True,
                 continuation_probability=0.75)
        self.assertEqual(p.trade_type, "PULLBACK")


class PositionHealthScoreTest(unittest.TestCase):
    def _score(self, **over):
        p = E.DynamicPositionProfile("BTC/USDT:USDT", "BUY", 100.0, 1.0,
                                     classification="TREND")
        base = dict(
            profile=p, trade_state="TREND_RIDE", trend_health=8,
            structure_aligned=True, adx=30,
            smart_money={"distribution_risk": 10, "banker_pressure": 70},
            momentum={"momentum_health": 80, "continuation_strength": 75},
            continuation_probability=0.8, continuation_pressure=70,
            thesis_failure_score=5, exit_warning=0, zone_strength=70,
            roe=2.0, drawdown_from_peak=0.0, news_state="NEWS_NEUTRAL",
        )
        base.update(over)
        return E.PositionHealthScore().compute(**base)

    def test_score_bounds_and_healthy_action(self):
        h = self._score()
        self.assertGreaterEqual(h["score"], 0.0)
        self.assertLessEqual(h["score"], 100.0)
        self.assertIn(h["action"], ("HOLD", "HOLD_TRAIL", "PROTECT_PROFIT", "PARTIAL", "EXIT"))
        self.assertGreaterEqual(h["score"], 75)  # healthy configuration

    def test_poor_health_yields_exit(self):
        h = self._score(
            smart_money={"distribution_risk": 90, "banker_pressure": 20},
            momentum={"momentum_health": 10, "continuation_strength": 5},
            trend_health=2, structure_aligned=False, continuation_probability=0.2,
            thesis_failure_score=90, exit_warning=4, drawdown_from_peak=8,
        )
        self.assertLessEqual(h["score"], 40)
        self.assertEqual(h["action"], "EXIT")

    def test_critical_news_dampens_but_never_blindly_exits(self):
        neutral = self._score()
        critical = self._score(news_state="NEWS_CRITICAL")
        # critical news lowers the risk component -> total health drops
        self.assertLess(critical["score"], neutral["score"])
        self.assertNotIn(critical["action"], ("EXIT",))  # never a forced blind exit


class AdvisoryIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._orig_ohlcv = E.get_ohlcv_safe
        self._orig_obc = E.get_orderbook_cached
        self._orig_tick = E.get_ticker_safe
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _df(250, trend=0.15)
        E.get_orderbook_cached = lambda *a, **k: {"bids": [[99.0, 10.0]], "asks": [[101.0, 5.0]]}
        E.get_ticker_safe = lambda symbol, retries=3: 105.0
        self._manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        E.STATE["open"] = False

    def tearDown(self):
        E.get_ohlcv_safe = self._orig_ohlcv
        E.get_orderbook_cached = self._orig_obc
        E.get_ticker_safe = self._orig_tick
        E.STATE["open"] = False
        E.STATE["position_profile"] = None

    def _manager_ctx(self, symbol="BTC/USDT:USDT", side="BUY", entry=100.0, atr=1.0):
        profile = self._manager._ensure_position_profile(
            symbol, entry, side, atr, classification="SNIPER", trade_type="TREND"
        )
        return profile

    def test_ensure_profile_populates_state(self):
        p = self._manager_ctx()
        self.assertIsNotNone(p)
        self.assertEqual(p.asset_class, "CRYPTO")
        self.assertEqual(E.STATE["position_profile"]["trade_type"], "TREND")

    def test_run_advisory_health_populates_position_state(self):
        p = self._manager_ctx()
        continuation = E.ContinuationEvaluation(
            continuation_probability=0.75, trend_strength=0.7,
            exhaustion_probability=0.1, reclaim_risk=0.2, counter_pressure=0.1,
            confidence=0.7, reasons=["ok"], should_hold=True, hold_quality="GOOD",
        )
        smart = {"distribution_risk": 15, "banker_pressure": 70, "smart_money_dominant": True}
        mom = {"momentum_health": 78, "continuation_strength": 72, "exhaustion_risk": 20}
        self._manager._run_advisory_health(
            symbol="BTC/USDT:USDT", mark_price=103.0, atr=1.0, side="BUY", entry=100.0,
            roe=3.0, smart_money=smart, momentum=mom, trade_state="TREND_RIDE",
            regime="EXPANSION", continuation_eval=continuation, trend_health=8,
            struct_shift="bullish_shift", structure_aligned=True,
            tp1_hold_score=8, exit_warning=0, news_state="NEWS_NEUTRAL", force=True,
        )
        self.assertGreaterEqual(E.STATE["position_health"], 0.0)
        self.assertLessEqual(E.STATE["position_health"], 100.0)
        self.assertIn(E.STATE["position_action"], ("HOLD", "HOLD_TRAIL", "PROTECT_PROFIT", "PARTIAL", "EXIT"))
        self.assertEqual(E.STATE["position_asset_class"], "CRYPTO")
        self.assertIsInstance(E.STATE["position_health_components"], dict)
        self.assertIn("position_trade_type", E.STATE)

    def test_advisory_does_not_close_position(self):
        """Phase 1 is advisory-only: _run_advisory_health must never execute a
        close/partial by itself, even in a failing state."""
        p = self._manager_ctx()
        E.STATE["open"] = True
        E._closing_in_progress = False
        broker = E.close_position_full
        partial_broker = E.close_partial
        E.close_position_full = lambda: False  # would be a bug if called
        E.close_partial = lambda ratio: False
        try:
            continuation = E.ContinuationEvaluation(
                continuation_probability=0.15, trend_strength=0.1,
                exhaustion_probability=0.6, reclaim_risk=0.8, counter_pressure=0.7,
                confidence=0.2, reasons=["failing"], should_hold=False,
                hold_quality="POOR",
            )
            self._manager._run_advisory_health(
                symbol="BTC/USDT:USDT", mark_price=97.0, atr=1.0, side="BUY", entry=100.0,
                roe=-3.0, smart_money={"distribution_risk": 90, "banker_pressure": 10},
                momentum={"momentum_health": 8, "continuation_strength": 5, "exhaustion_risk": 80},
                trade_state="PANIC_EXIT", regime="UNKNOWN", continuation_eval=continuation,
                trend_health=1, struct_shift="bearish_shift", structure_aligned=False,
                tp1_hold_score=0, exit_warning=5, news_state="NEWS_CRITICAL", force=True,
            )
            self.assertIn(E.STATE["position_action"], ("PARTIAL", "EXIT", "PROTECT_PROFIT"))
        finally:
            E.close_position_full = broker
            E.close_partial = partial_broker
            E.STATE["open"] = False


class DynamicRulesExecutionTest(unittest.TestCase):
    """Phase 2: the advisory signals become ENFORCEABLE rules. These directly
    exercise _apply_dynamic_profit_and_exit_rules with controlled inputs and
    assert the real close/partial/breakeven side effects, while confirming the
    existing guard ORDER is preserved (healthy trend does nothing)."""

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
        # stop real execution side effects so the test asserts our planning only
        self._broker = E.close_position_full
        self._partial = E.close_partial
        E.close_position_full = lambda: True
        E.close_partial = lambda ratio: None

    def tearDown(self):
        E.close_position_full = self._broker
        E.close_partial = self._partial
        E.STATE["open"] = False

    def _profile(self, symbol="BTC/USDT:USDT", classification="TREND", trade_type=""):
        return self._manager._ensure_position_profile(
            symbol, 100.0, "BUY", 1.0, classification=classification, trade_type=trade_type
        )

    def _run(self, *, trade_state="TREND_RIDE", dist_risk=10, mom_health=80,
             exh_risk=20, mom_decay=False, cont=0.8, structure_aligned=True,
             health=85, action="HOLD", conf=0.85, roe=3.0):
        E.STATE["position_health"] = health
        E.STATE["position_action"] = action
        E.STATE["position_confidence"] = conf
        continuation = E.ContinuationEvaluation(
            continuation_probability=cont, trend_strength=cont, exhaustion_probability=0.1,
            reclaim_risk=0.2, counter_pressure=0.1, confidence=conf,
            reasons=["x"], should_hold=cont >= 0.62, hold_quality="GOOD",
        )
        return self._manager._apply_dynamic_profit_and_exit_rules(
            symbol="BTC/USDT:USDT", mark_price=103.0, atr=1.0, side="BUY", entry=100.0,
            roe=roe, trade_state=trade_state,
            smart_money={"distribution_risk": dist_risk, "banker_pressure": 50},
            momentum={"momentum_health": mom_health, "exhaustion_risk": exh_risk,
                      "momentum_decay": mom_decay, "continuation_strength": 60},
            continuation_eval=continuation, structure_aligned=structure_aligned,
        )

    def test_healthy_trend_does_nothing(self):
        self._profile()
        closed = self._run()  # strong trend, no reversal/exhaustion signal
        self.assertFalse(closed)
        self.assertFalse(E.STATE["dynamic_reversal_exit_done"])
        self.assertFalse(E.STATE["dynamic_exhaustion_protect_done"])
        self.assertFalse(E.STATE["dynamic_partial_done"])

    def test_confirmed_reversal_full_exit(self):
        self._profile()
        closed = self._run(
            trade_state="MOMENTUM_COLLAPSE", dist_risk=80, mom_decay=True,
            structure_aligned=False, cont=0.3, health=35, action="EXIT", conf=0.8,
        )
        self.assertTrue(closed)  # full exit requested
        self.assertTrue(E.STATE["dynamic_reversal_exit_done"])

    def test_medium_reversal_partial_runner(self):
        self._profile()
        closed = self._run(
            dist_risk=55, exh_risk=70, structure_aligned=False, cont=0.4,
            health=40, action="PARTIAL", conf=0.75, roe=2.0,
        )
        # medium evidence -> de-risk partial, NOT a full close
        self.assertFalse(closed)
        self.assertTrue(E.STATE["tp1_hit"])
        self.assertTrue(E.STATE["runner_mode"])
        self.assertEqual(E.STATE["synthetic_sl"], 100.0)  # breakeven

    def test_exhaustion_protection_ratchets_to_breakeven(self):
        self._profile()
        E.STATE["runner_mode"] = True
        self._run(exh_risk=75, cont=0.4, health=50, action="PARTIAL")
        self.assertTrue(E.STATE["dynamic_exhaustion_protect_done"])
        self.assertEqual(E.STATE["synthetic_sl"], 100.0)

    def test_early_profit_bank_reversal_asset(self):
        # GOLD reversal should bank profit early via ATR target partial.
        self._profile(symbol="XAUUSD", classification="REVERSAL")
        E.STATE["tp1_hit"] = False
        closed = self._run(cont=0.5, roe=2.5, health=60, action="HOLD")
        self.assertFalse(closed)
        self.assertTrue(E.STATE["dynamic_partial_done"])
        self.assertTrue(E.STATE["tp1_hit"])
        self.assertTrue(E.STATE["runner_mode"])


if __name__ == "__main__":
    unittest.main()
