"""Focused tier regressions for the VPA volume-intelligence layer:

* TradeCouncil: VPA banked partial + strict institutional-reversal close;
  healthy trends ride untouched.
* Engine: an OB under attack can never be graded A/A+.
"""

import unittest

import numpy as np
import pandas as pd

from core.engine import ExecutionQueue
from core.trade import (ExitReason, ProfitStage, ProtectionState, Trade,
                        TradeStatus, TradeStyle)
from portfolio.trade_council import MarketSnapshot, TradeCouncil


def _df_from(opens, highs, lows, closes, volumes):
    return pd.DataFrame({"open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": volumes})


def _weak_result_df(base=100.0):
    """Effort without result: drifting closes fight the thesis while volume
    ramps, so the BUY-side reading is WEAK_RESULT (net <= -0.3 ATR)."""
    n = 60
    closes = np.linspace(base, base - 1.5, n)
    opens = closes + 0.5
    highs = np.maximum(opens, closes) + 1.5
    lows = np.minimum(opens, closes) - 1.5
    volumes = np.linspace(1000.0, 9000.0, n)
    return _df_from(opens, highs, lows, closes, volumes)


def _confirmed_down_df(base=100.0):
    """Strong, moderately-efficient SELL displacement (net >= 0.8 ATR, vol
    ~1.6x) — efficient enough for CONFIRMED but below the blowoff/CLIMAX
    threshold so the opposing-side check fires."""
    n = 60
    o = np.full(n, base)
    c = np.full(n, base)
    o[-3], c[-3] = base, base - 0.6
    o[-2], c[-2] = base - 0.6, base - 1.2
    o[-1], c[-1] = base - 1.1, base - 1.8
    h = np.maximum(o, c) + 0.1
    l = np.minimum(o, c) - 0.1
    v = np.full(n, 1000.0)
    v[-3:] = 1600.0
    return _df_from(o, h, l, c, v)


def _make_trade(price=100.0, qty=1.0, style=TradeStyle.TREND):
    return Trade(
        symbol="TEST/USDT:USDT", side="BUY", asset_class="CRYPTO",
        entry_price=price, original_qty=qty, remaining_qty=qty,
        synthetic_sl=price - price * 0.02, status=TradeStatus.FILLED)


class CouncilVpaDefenseTest(unittest.TestCase):
    def _market(self, price, df, trend=0.3, adx=10.0, ef=0.0, es=0.0):
        m = MarketSnapshot(price=price, atr=1.0, adx=adx,
                           trend_strength=trend, ema_fast=ef, ema_slow=es)
        m.df = df
        return m

    def test_bank_profit_when_effort_fails_at_high_roe(self):
        trade = _make_trade(price=100.0)
        trade.roe_pct = 19.0
        market = self._market(price=119.0, df=_weak_result_df())
        council = TradeCouncil(trade)
        decision = council._check_vpa_profit_defense(market)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "PARTIAL_CLOSE")
        self.assertEqual(round(decision.close_ratio, 2), 0.4)
        self.assertEqual(decision.exit_reason, ExitReason.PROFIT_LOCK)
        self.assertEqual(trade.protection_state, ProtectionState.PROFIT_LOCK)
        self.assertEqual(trade.profit_stage, ProfitStage.TRAILING_ACTIVE)
        # NOT an instant full close: the runner must survive.
        self.assertNotEqual(decision.action, "FULL_CLOSE")

    def test_strict_institutional_reversal_on_confirmed_opposing_effort(self):
        trade = _make_trade(price=100.0)
        trade.roe_pct = 0.2
        market = self._market(price=100.2, df=_confirmed_down_df(),
                              ef=10.0, es=12.0)
        council = TradeCouncil(trade)
        decision = council._check_vpa_profit_defense(market)
        self.assertIsNotNone(decision)
        self.assertEqual(decision.action, "FULL_CLOSE")
        self.assertEqual(decision.exit_reason, ExitReason.INSTITUTIONAL_REVERSAL)
        self.assertTrue(decision.board_notes.get("strict_close"))

    def test_healthy_strong_trend_rides_untouched(self):
        trade = _make_trade(price=100.0)
        market = self._market(price=101.0, df=_weak_result_df(),
                              trend=0.85, adx=35.0)
        council = TradeCouncil(trade)
        self.assertIsNone(council._check_vpa_profit_defense(market))

    def test_scalp_passes_through(self):
        trade = _make_trade(price=100.0, style=TradeStyle.SCALP)
        market = self._market(price=119.0, df=_weak_result_df())
        council = TradeCouncil(trade)
        self.assertIsNone(council._check_vpa_profit_defense(market))


class EngineUnderAttackGradeTest(unittest.TestCase):
    def test_under_attack_ob_never_graded_a(self):
        q = ExecutionQueue.__new__(ExecutionQueue)

        class Cand:
            pass

        cand = Cand()
        cand.roro_signal = True
        cand.strong_ob_present = True
        cand.side = "BUY"
        cand.vpa = {"under_attack": True}
        cand.evidence = {"rejection_or_displacement": True}
        cand.decision_label = "VALID"
        cand.zone_state = "ENTRY_WINDOW"
        cand.zone_metrics = type("M", (), {
            "final_zone_score": 90,
            "trigger_state": "MSS_CONFIRMED",
            "liquidity_quality": 80,
        })()
        self.assertFalse(q._is_a_grade(cand))

        cand.vpa = {"under_attack": False}
        self.assertTrue(q._is_a_grade(cand))


if __name__ == "__main__":
    unittest.main()