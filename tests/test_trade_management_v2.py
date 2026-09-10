"""Tests for the new trade management system.

Tests cover:
  1. Trade entity (core/trade.py)
  2. TradeCouncil decisions (portfolio/trade_council.py)
  3. TradeExecutionCoordinator (portfolio/coordinator.py)
  4. PortfolioRiskGuard with closure log (portfolio/risk.py)
"""
import os
import time
import unittest

from core.trade import (
    Trade, TradeStatus, ProfitStage, ProtectionState,
    TradeStyle, ExitReason, PartialCloseLeg,
)
from portfolio.trade_board import (
    render_open_board, render_position_board, render_close_board,
    render_risk_alert, TradeBoardLogger,
)
from portfolio.trade_council import TradeCouncil, MarketSnapshot, TradeDecision
from portfolio.coordinator import TradeExecutionCoordinator, _generate_client_order_id
from portfolio.risk import PortfolioRiskGuard


# ──────────────────────────────────────────────────────────────────────────
# Trade Entity Tests
# ──────────────────────────────────────────────────────────────────────────

class TestTradeEntity(unittest.TestCase):
    def test_create_basic_trade(self):
        trade = Trade(
            symbol="BTC/USDT:USDT",
            side="BUY",
            asset_class="CRYPTO",
            entry_price=50000.0,
            original_qty=0.1,
        )
        self.assertEqual(trade.symbol, "BTC/USDT:USDT")
        self.assertEqual(trade.side, "BUY")
        self.assertTrue(trade.is_open)
        self.assertEqual(trade.remaining_ratio, 1.0)

    def test_trade_id_unique(self):
        t1 = Trade(symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO")
        t2 = Trade(symbol="ETH/USDT:USDT", side="SELL", asset_class="CRYPTO")
        self.assertNotEqual(t1.trade_id, t2.trade_id)

    def test_partial_close(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
        )
        leg = trade.add_partial_leg(0.05, 51000.0, "TP1")
        self.assertEqual(leg.leg_id, 1)
        self.assertEqual(trade.remaining_qty, 0.05)
        self.assertEqual(trade.status, TradeStatus.PARTIAL_CLOSE)
        self.assertTrue(trade.realized_pnl_pct > 0)

    def test_finalize_trade(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
        )
        trade.finalize(ExitReason.TP2, "WIN", "tp2 hit")
        self.assertEqual(trade.status, TradeStatus.CLOSED)
        self.assertEqual(trade.exit_reason, ExitReason.TP2)
        self.assertEqual(trade.final_result_class, "WIN")
        self.assertGreater(trade.close_time, 0)

    def test_profit_stage_monotonic(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
        )
        self.assertTrue(trade.advance_stage(ProfitStage.PROFIT_DETECTED))
        self.assertEqual(trade.profit_stage, ProfitStage.PROFIT_DETECTED)
        # Can't go backward
        self.assertFalse(trade.advance_stage(ProfitStage.OPENED))
        self.assertEqual(trade.profit_stage, ProfitStage.PROFIT_DETECTED)
        # Can advance forward
        self.assertTrue(trade.advance_stage(ProfitStage.TP1_ELIGIBLE))

    def test_ratchet_sl_buy(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
            synthetic_sl=49000.0, protection_floor_sl=49000.0,
        )
        # Can move SL up
        self.assertTrue(trade.ratchet_sl(49500.0))
        self.assertEqual(trade.protection_floor_sl, 49500.0)
        # Can't move SL down
        self.assertFalse(trade.ratchet_sl(49200.0))
        self.assertEqual(trade.protection_floor_sl, 49500.0)

    def test_ratchet_sl_sell(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="SELL", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
            synthetic_sl=51000.0, protection_floor_sl=51000.0,
        )
        # Can move SL down
        self.assertTrue(trade.ratchet_sl(50500.0))
        self.assertEqual(trade.protection_floor_sl, 50500.0)
        # Can't move SL up
        self.assertFalse(trade.ratchet_sl(50800.0))

    def test_to_state_dict(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
        )
        state = trade.to_state_dict()
        self.assertTrue(state["open"])
        self.assertEqual(state["side"], "BUY")
        self.assertEqual(state["entry"], 50000.0)
        self.assertEqual(state["qty"], 0.1)
        self.assertEqual(state["trade_id"], trade.trade_id)

    def test_from_exchange_position(self):
        pos = {
            "symbol": "BTC/USDT:USDT",
            "side": "long",
            "entryPrice": 50000.0,
            "contracts": 0.1,
            "markPrice": 51000.0,
            "unrealizedPnl": 100.0,
        }
        trade = Trade.from_exchange_position(pos, trade_id="T-test-123")
        self.assertEqual(trade.symbol, "BTC/USDT:USDT")
        self.assertEqual(trade.side, "BUY")
        self.assertEqual(trade.entry_price, 50000.0)
        self.assertTrue(trade.recovered)

    def test_serialization_roundtrip(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
            synthetic_sl=49000.0, tp1_price=51000.0, tp2_price=52000.0,
        )
        trade.add_partial_leg(0.05, 51000.0, "TP1")
        data = trade.to_dict()
        restored = Trade.from_dict(data)
        self.assertEqual(restored.symbol, trade.symbol)
        self.assertEqual(restored.side, trade.side)
        self.assertEqual(restored.entry_price, trade.entry_price)
        self.assertEqual(len(restored.partial_legs), 1)
        self.assertEqual(restored.remaining_qty, 0.05)


# ──────────────────────────────────────────────────────────────────────────
# TradeCouncil Tests
# ──────────────────────────────────────────────────────────────────────────

class TestTradeCouncil(unittest.TestCase):
    def setUp(self):
        os.environ["SCALP_ROI_THRESHOLD"] = "0.3"
        os.environ["SCALP_TIME_LIMIT_SEC"] = "300"
        os.environ["TREND_ADX_MIN"] = "25"
        os.environ["BREAKEVEN_ROI_PCT"] = "0.2"
        os.environ["PROFIT_LOCK_ROI_PCT"] = "0.5"
        os.environ["HARD_STOP_ROE_PCT"] = "-2.0"
        os.environ["FORCE_CLOSE_AFTER_SEC"] = "172800"

    def tearDown(self):
        for k in ["SCALP_ROI_THRESHOLD", "SCALP_TIME_LIMIT_SEC", "TREND_ADX_MIN",
                   "BREAKEVEN_ROI_PCT", "PROFIT_LOCK_ROI_PCT", "HARD_STOP_ROE_PCT",
                   "FORCE_CLOSE_AFTER_SEC"]:
            os.environ.pop(k, None)

    def test_hard_stop_loss(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=48000.0)  # -4% = -400% ROE with leverage
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "FULL_CLOSE")
        self.assertEqual(decision.exit_reason, ExitReason.STOP_LOSS)

    def test_tp1_partial_close(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            tp1_price=51000.0, tp2_price=52000.0,
            status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=51000.0)  # TP1 hit
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "PARTIAL_CLOSE")
        self.assertEqual(decision.exit_reason, ExitReason.TP1)
        self.assertGreater(decision.close_ratio, 0)

    def test_tp2_full_close(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            tp1_price=51000.0, tp1_state="EXECUTED",
            tp2_price=52000.0,
            status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=52000.0)  # TP2 hit
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "FULL_CLOSE")
        self.assertEqual(decision.exit_reason, ExitReason.TP2)

    def test_scalp_roi_exit(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            trade_style=TradeStyle.SCALP,
            entry_time=time.time() - 60,  # 1 minute ago
            status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=50150.0)  # 0.3% ROI
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "FULL_CLOSE")
        self.assertTrue(decision.is_scalp_exit)

    def test_breakeven_ratchet(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            synthetic_sl=49000.0, protection_floor_sl=49000.0,
            status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=50100.0)  # 0.2% ROI
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "ADJUST_SL")
        self.assertIn("breakeven", decision.reason)

    def test_hold_when_no_signal(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=50050.0)  # Small profit, no signals
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "HOLD")

    def test_inactive_trade_hold(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1,
            status=TradeStatus.CLOSED,
        )
        market = MarketSnapshot(price=51000.0)
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        self.assertEqual(decision.action, "HOLD")

    def test_classify_trade_style_trend(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0,
        )
        market = MarketSnapshot(price=50000.0, trend_strength=0.8, adx=30)
        style = TradeCouncil.classify_trade_style(trade, market)
        self.assertEqual(style, TradeStyle.TREND)

    def test_council_votes_five_named_members(self):
        # Every evaluate() run records exactly the five named advisory members
        # in trade.board_decisions["council_votes"], each with a verdict.
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            synthetic_sl=47000.0, status=TradeStatus.FILLED,
        )
        market = MarketSnapshot(price=50100.0, adx=30, trend_strength=0.7,
                                rsi=55, atr=400)
        council = TradeCouncil(trade)
        decision = council.evaluate(market)
        votes = trade.board_decisions.get("council_votes", [])
        self.assertEqual(
            [v["name"] for v in votes],
            ["TrendRider", "ProfitGuardian", "RiskOfficer",
             "ThesisOfficer", "ScalpDesk"],
        )
        for v in votes:
            self.assertIn(v["vote"], ("HOLD", "PARTIAL_CLOSE",
                                      "FULL_CLOSE", "ADJUST_SL"))
            self.assertGreaterEqual(v["score"], 0.0)
            self.assertLessEqual(v["score"], 100.0)
            self.assertTrue(v["rationale"])
        self.assertTrue(decision.board_notes.get("council_votes"))
        # Same verdicts ride the decision board_notes for the close board.
        self.assertEqual(len(decision.board_notes["council_votes"]), 5)

    def test_council_votes_read_only(self):
        # The advisory layer must never mutate the trade beyond the normal
        # market snapshot bookkeeping done by the cascade.
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            synthetic_sl=47000.0, status=TradeStatus.FILLED,
        )
        before = {
            "synth_sl": trade.synthetic_sl,
            "protection": trade.protection_state,
            "stage": trade.profit_stage,
            "remaining": trade.remaining_qty,
        }
        market = MarketSnapshot(price=49100.0, adx=10, trend_strength=0.1,
                                rsi=50, atr=400)
        TradeCouncil(trade).evaluate(market)
        self.assertEqual(before["synth_sl"], trade.synthetic_sl)
        self.assertEqual(before["protection"], trade.protection_state)
        self.assertEqual(before["stage"], trade.profit_stage)
        self.assertEqual(before["remaining"], trade.remaining_qty)
        self.assertEqual(trade.status, TradeStatus.FILLED)

    def test_board_renders_aligned_boxes(self):
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            tp1_price=51000.0, tp2_price=52000.0, synthetic_sl=49000.0,
            mark_price=50500.0, status=TradeStatus.FILLED,
            client_order_id="BARON_sample_ENTRY",
        )
        for board in (
            render_open_board(trade),
            render_position_board(trade),
            render_close_board(trade),
            render_risk_alert(trade, "sl_distance_warning", "WARNING"),
        ):
            lines = board.splitlines()
            width = len(lines[0])
            # Boards are rendered with the professional unicode double-line box
            # (╔ ═ ╗ ║ ╠ ╣ ╚ ╝); accept both the unicode and the ASCII fallback.
            self.assertTrue(lines[0].startswith(("╔", "+")))
            self.assertTrue(lines[0].endswith(("╗", "+")))
            for line in lines:
                self.assertEqual(len(line), width,
                                 f"misaligned box line: {line!r}")
                self.assertTrue(line.startswith(("║", "╚", "╔", "╠", "|", "+")) and
                                line.endswith(("║", "╝", "╗", "╣", "|", "+")))

    def test_open_long_deterministic_cid(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        candidate = {
            "symbol": "BTC/USDT:USDT", "side": "BUY", "price": 50000.0,
            "sl": 49000.0, "tp1": 51000.0, "tp2": 52000.0,
            "score": 80, "atr": 500, "asset_class": "CRYPTO",
        }

        def fake_execute(*args, **kwargs):
            engine.STATE["open"] = True
            engine.STATE["entry"] = 50000.0
            engine.STATE["entry_time"] = time.time()
            engine.STATE["qty"] = 0.1
            engine.STATE["remaining_qty"] = 0.1
            return True

        trade = coord.open_trade(candidate, lambda s, c: True, fake_execute)
        self.assertIsNotNone(trade)
        # clientOrderId is deterministic from the trade_id and registered.
        expected = _generate_client_order_id(
            "BTC/USDT:USDT", "BUY", trade.trade_id, "ENTRY")
        self.assertEqual(trade.client_order_id, expected)
        self.assertLessEqual(len(expected), 40)
        self.assertRegex(expected, r"^[A-Za-z0-9_]+$")
        # Board votes were attached on open.
        self.assertEqual(len(trade.board_decisions.get("entry_votes", [])), 5)
        # Opening the same symbol again is blocked (idempotency).
        self.assertIsNone(coord.open_trade(candidate, lambda s, c: True,
                                           fake_execute))

    def test_open_short_hedge_direction(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        candidate = {
            "symbol": "ETH/USDT:USDT", "side": "SELL", "price": 3000.0,
            "sl": 3150.0, "tp1": 2900.0, "tp2": 2850.0,
            "score": 70, "atr": 30, "asset_class": "CRYPTO",
        }
        seen = {}

        def fake_execute(side, symbol, *args, **kwargs):
            seen["side"] = side
            engine.STATE["open"] = True
            engine.STATE["entry"] = 3000.0
            engine.STATE["entry_time"] = time.time()
            engine.STATE["qty"] = 2.0
            engine.STATE["remaining_qty"] = 2.0
            return True

        trade = coord.open_trade(candidate, lambda s, c: True, fake_execute)
        self.assertIsNotNone(trade)
        # SHORT direction is carried end-to-end; the exchange order is routed
        # with positionSide=SHORT by the engine close/SL paths (no reduceOnly).
        self.assertEqual(seen["side"], "SELL")
        self.assertEqual(trade.side, "SELL")
        self.assertTrue(trade.is_active)
        board = coord.snapshot()
        self.assertEqual(board[0]["side"], "SELL")
        self.assertEqual(trade.client_order_id, _generate_client_order_id(
            "ETH/USDT:USDT", "SELL", trade.trade_id, "ENTRY"))

    def test_board_logger_emits_close_board(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            mark_price=51000.0, status=TradeStatus.FILLED,
        )
        coord._active_trades[trade.trade_id] = trade
        success = coord.close_trade(
            trade.trade_id, ExitReason.MANUAL, "test", lambda: True,
        )
        self.assertTrue(success)
        # A STRICT CLOSE board (with the box header) was pushed to the
        # dashboard log stream by the coordinator.
        joined = "\n".join(str(x.get("text", "")) for x in engine.dashboard["logs"])
        self.assertIn("STRICT CLOSE", joined)
        self.assertIn("RESULT BREAKEVEN", joined)


# ──────────────────────────────────────────────────────────────────────────
# TradeExecutionCoordinator Tests
# ──────────────────────────────────────────────────────────────────────────

class TestTradeExecutionCoordinator(unittest.TestCase):
    def test_client_order_id_deterministic(self):
        # Deterministic contract: same trade_id + purpose => same clientOrderId
        # (idempotency key that survives timeouts/retries), different trade_id
        # => different id. Under 40 chars and SAFE_CID only.
        tid = "BTC-USDT-T-1789000000000-abc12345"
        id1 = _generate_client_order_id("BTC/USDT:USDT", "BUY", tid, "ENTRY")
        id2 = _generate_client_order_id("BTC/USDT:USDT", "BUY", tid, "ENTRY")
        self.assertEqual(id1, id2)
        id3 = _generate_client_order_id("BTC/USDT:USDT", "BUY", tid + "X", "ENTRY")
        self.assertNotEqual(id1, id3)
        id4 = _generate_client_order_id("BTC/USDT:USDT", "BUY", tid, "CLOSE")
        self.assertNotEqual(id1, id4)
        for cid in (id1, id2, id3, id4):
            self.assertLessEqual(len(cid), 40)
            self.assertRegex(cid, r"^[A-Za-z0-9_]+$")

    def test_duplicate_open_blocked(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        # Manually register an active trade
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            status=TradeStatus.FILLED,
        )
        coord._active_trades[trade.trade_id] = trade

        # Try to open another trade for the same symbol
        candidate = {
            "symbol": "BTC/USDT:USDT", "side": "BUY", "price": 50000.0,
            "sl": 49000.0, "tp1": 51000.0, "tp2": 52000.0,
            "score": 80, "atr": 500, "asset_class": "CRYPTO",
        }
        result = coord.open_trade(candidate, lambda s, c: True, lambda *a, **k: True)
        self.assertIsNone(result)  # Blocked

    def test_successful_open(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        candidate = {
            "symbol": "BTC/USDT:USDT", "side": "BUY", "price": 50000.0,
            "sl": 49000.0, "tp1": 51000.0, "tp2": 52000.0,
            "score": 80, "atr": 500, "asset_class": "CRYPTO",
        }

        def fake_execute(*args, **kwargs):
            engine.STATE["open"] = True
            engine.STATE["entry"] = 50000.0
            engine.STATE["entry_time"] = time.time()
            engine.STATE["qty"] = 0.1
            engine.STATE["remaining_qty"] = 0.1
            return True

        trade = coord.open_trade(candidate, lambda s, c: True, fake_execute)
        self.assertIsNotNone(trade)
        self.assertTrue(trade.is_active)
        self.assertEqual(coord.count_active(), 1)

    def test_close_trade(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        # Create and register a trade manually
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            status=TradeStatus.FILLED,
        )
        coord._active_trades[trade.trade_id] = trade

        success = coord.close_trade(
            trade.trade_id, ExitReason.MANUAL, "test",
            lambda: True,
        )
        self.assertTrue(success)
        self.assertEqual(coord.count_active(), 0)
        self.assertEqual(len(coord.closure_log), 1)
        self.assertEqual(coord.closure_log[0]["result"], "BREAKEVEN")

    def test_partial_close(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            mark_price=51000.0, status=TradeStatus.FILLED,
        )
        coord._active_trades[trade.trade_id] = trade

        success = coord.partial_close_trade(
            trade.trade_id, 0.5, ExitReason.TP1, "tp1 hit",
            lambda ratio: True,
        )
        self.assertTrue(success)
        self.assertEqual(len(trade.partial_legs), 1)
        self.assertEqual(trade.remaining_qty, 0.05)
        self.assertEqual(trade.status, TradeStatus.PARTIAL_CLOSE)

    def test_snapshot(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        trade = Trade(
            symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
            entry_price=50000.0, original_qty=0.1, remaining_qty=0.1,
            mark_price=51000.0, status=TradeStatus.FILLED,
        )
        coord._active_trades[trade.trade_id] = trade

        snapshot = coord.snapshot()
        self.assertEqual(len(snapshot), 1)
        self.assertEqual(snapshot[0]["symbol"], "BTC/USDT:USDT")

    def test_risk_snapshot(self):
        engine = _FakeEngine()
        coord = TradeExecutionCoordinator(engine)
        risk = coord.risk_snapshot()
        self.assertEqual(risk["active_trades"], 0)
        self.assertEqual(risk["closure_count"], 0)


# ──────────────────────────────────────────────────────────────────────────
# PortfolioRiskGuard v2 Tests (closure log)
# ──────────────────────────────────────────────────────────────────────────

class TestPortfolioRiskGuardV2(unittest.TestCase):
    def setUp(self):
        self._env = dict(os.environ)
        os.environ["POSITION_MARGIN_PCT"] = "0.10"
        os.environ["PORTFOLIO_MARGIN_CAP_PCT"] = "0.60"
        os.environ["MAX_DAILY_LOSS_PCT"] = "5"
        os.environ["MAX_CONSECUTIVE_LOSSES"] = "3"
        os.environ["COOLDOWN_MINUTES_LOSS"] = "1"
        os.environ["COOLDOWN_MINUTES_DRAWDOWN"] = "2"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_closure_log_processes_multiple_losses(self):
        """P0-2 fix: multiple closures between sync are all processed."""
        engine = _FakeRiskEngine()
        coord = TradeExecutionCoordinator(engine)

        # Simulate 3 consecutive losses in the closure log
        for i in range(3):
            coord._closure_log.append({
                "trade_id": f"T-{i}",
                "symbol": f"SYM{i}",
                "result": "LOSS",
                "realized_pnl_pct": -1.0,
            })

        guard = PortfolioRiskGuard(engine, coord)
        guard.sync_closed_trades()

        # All 3 losses should be processed
        self.assertEqual(guard._consecutive_losses, 3)
        status = guard.status(current_positions=0)
        self.assertFalse(status.allowed)
        self.assertEqual(status.reason, "GLOBAL_LOSS_COOLDOWN")

    def test_closure_log_win_resets_losses(self):
        engine = _FakeRiskEngine()
        coord = TradeExecutionCoordinator(engine)

        # 2 losses then a win
        coord._closure_log.extend([
            {"trade_id": "T-0", "symbol": "SYM0", "result": "LOSS", "realized_pnl_pct": -1.0},
            {"trade_id": "T-1", "symbol": "SYM1", "result": "LOSS", "realized_pnl_pct": -1.0},
            {"trade_id": "T-2", "symbol": "SYM2", "result": "WIN", "realized_pnl_pct": 2.0},
        ])

        guard = PortfolioRiskGuard(engine, coord)
        guard.sync_closed_trades()

        self.assertEqual(guard._consecutive_losses, 0)
        status = guard.status(current_positions=0)
        self.assertTrue(status.allowed)

    def test_per_symbol_cooldown_from_closure_log(self):
        engine = _FakeRiskEngine()
        coord = TradeExecutionCoordinator(engine)

        coord._closure_log.append({
            "trade_id": "T-0",
            "symbol": "BTC/USDT:USDT",
            "result": "LOSS",
            "realized_pnl_pct": -1.0,
        })

        guard = PortfolioRiskGuard(engine, coord)
        guard.sync_closed_trades()

        # Check that per-symbol cooldown was set
        self.assertIn("BTC/USDT:USDT", guard._symbol_cooldown_until)
        self.assertGreater(guard._symbol_cooldown_until["BTC/USDT:USDT"], time.time())
        # Also verify the symbol-specific status would block
        status = guard.status("BTC/USDT:USDT", current_positions=0)
        self.assertFalse(status.allowed)


# ──────────────────────────────────────────────────────────────────────────
# Close clientOrderId uniqueness (BingX permanently burns used IDs,
# error 101400 "clientOrderID unique check failed" on any reuse)
# ──────────────────────────────────────────────────────────────────────────

class TestCloseClientOrderId(unittest.TestCase):
    def setUp(self):
        import core.engine as engine
        self._engine = engine
        self._saved = dict(engine.STATE)
        engine.STATE["trade_id"] = "TRADE-9fb2c81e4a7d6c3f0a1b2c3d4e5f6789"
        engine.STATE["entry_time"] = time.time()
        engine.STATE["current_symbol"] = "BTC/USDT:USDT"

    def tearDown(self):
        self._engine.STATE.clear()
        self._engine.STATE.update(self._saved)

    def test_length_and_shape(self):
        from core.engine import _close_client_order_id
        for purpose, tag in (("C", "_C_"), ("P", "_P_"), ("R", "_R_"), ("E", "_E_")):
            cid = _close_client_order_id("BTC/USDT:USDT", purpose=purpose, nonce=1)
            self.assertLessEqual(len(cid), 40, cid)
            self.assertTrue(cid.startswith("BARON_"), cid)
            self.assertIn(tag, cid, cid)

    def test_unique_across_purposes_attempts(self):
        from core.engine import _close_client_order_id
        ids = set()
        for purpose in ("C", "P", "R", "E"):
            for nonce in range(1, 6):
                ids.add(_close_client_order_id(
                    "BTC/USDT:USDT", purpose=purpose, nonce=nonce))
        self.assertEqual(len(ids), 20, ids)

    def test_unique_across_invocations(self):
        # The regression: repeated close_position_full calls for the SAME trade
        # must never reuse one burned id (deterministic ids caused the 101400
        # loop in production). Auto-nonce must already guarantee freshness.
        from core.engine import _close_client_order_id
        ids = {_close_client_order_id("BTC/USDT:USDT") for _ in range(6)}
        self.assertEqual(len(ids), 6, ids)

    def test_burned_id_detection(self):
        from core.engine import _close_cid_burned
        msg = ('bingx {"code":101400,"msg":"clientOrderID unique check '
               'failed","data":{}}')
        self.assertTrue(_close_cid_burned(msg))
        self.assertTrue(_close_cid_burned("unique check failed"))
        self.assertFalse(_close_cid_burned("code 109415: contract paused"))


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

class _FakeEngine:
    def __init__(self):
        self.STATE = {
            "open": False, "side": None, "entry": 0.0,
            "qty": 0.0, "remaining_qty": 0.0,
            "entry_time": 0.0, "mark_price": 0.0,
        }
        self.PERF = {"trades": 0, "last_trade": None}
        self.MEMORY = {}
        self.paper = {"balance": 10000.0, "committed_margin": 0.0}
        self._TRADE_LOCK = __import__("threading").RLock()
        self._live_manager = None
        self.dashboard = {"logs": []}

    def log_execution(self, msg, level="INFO"):
        self.dashboard["logs"].append({"text": msg, "level": level})

    def get_ticker_safe(self, symbol):
        return 50000.0

    def get_ohlcv_safe(self, symbol, limit):
        return None


class _FakeRiskEngine:
    def __init__(self):
        self.PERF = {"trades": 0, "last_trade": None}
        self.balance = 1000.0
        self.paper = {"balance": 1000.0, "committed_margin": 0.0}

    def get_balance_safe(self):
        return self.balance

    def get_equity_safe(self):
        return self.balance + self.paper.get("committed_margin", 0.0)


if __name__ == "__main__":
    unittest.main()
