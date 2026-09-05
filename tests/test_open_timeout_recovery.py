"""Timeout-reconciliation regression suite for OPEN orders.

Reproduces the production bug where a market order is FILLED on BingX but the
confirm-order status fetch times out, causing the bot to log
"[OPEN] Order failed: TIMEOUT", never register the REAL exchange position,
never start Trade Management, and never publish management/Dashboard logs.

The fix treats a confirm TIMEOUT as ORDER_STATUS_UNKNOWN (never REJECTED),
reconciles the real exchange position (hedge side LONG for BUY / SHORT for
SELL — NEVER BOTH or set_position_mode(False)), adopts it, and converges into
the EXACT same post-fill lifecycle a normally confirmed order uses.

Scenarios covered (A-I from the production request):
  A  create_order ok + confirm TIMEOUT -> adopt real position, local trade
     state active, management starts, NO duplicate open.
  B  create_order ok + confirm TIMEOUT + no exchange position -> unresolved,
     no phantom state, retry NOT submitted.
  C  TIMEOUT + LONG position exists -> BUY/LONG adopted.
  D  TIMEOUT + SHORT position exists -> SELL/SHORT adopted.
  E  recovered LONG with protection already present -> no duplicate TP/SL.
  F  recovered LONG without protection -> existing TP/SL init runs once.
  G  recovered position reaches the management loop + Dashboard logs.
  H  partial/full close after a recovered position preserves PositionSide
     and reduceOnly.
  I  duplicate reconciliation cycle -> same position not re-registered and
     no second order is created.
"""
import os
import re
import time
import unittest
from contextlib import contextmanager
from unittest import mock

import numpy as np
import pandas as pd

os.environ.setdefault("ENTRY_QUALITY_AUTHORITY", "false")

import core.engine as engine  # noqa: E402
from core.engine import OrderManager  # noqa: E402


def _entry_df(n=250):
    """Trending BUY frame that passes the REAL execute_entry gates (ADX in
    [25,38] and a tail candle sweeping the prior low -> sell_side_taken)."""
    t = np.arange(n)
    x = 100.0 + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    o[n - 2] = prior_low - 0.2
    c[n - 2] = prior_low + 0.3
    h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
    l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1
    c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3
    l[n - 1] = prior_low - 0.1
    return pd.DataFrame({
        "timestamp": t, "open": o, "high": h, "low": l, "close": c,
        "volume": np.full(n, 1000.0),
    })


class FakeVenue:
    """Deterministic fake BingX venue:

    - create_order records the exact Hedge-Mode params (idempotency guard).
    - fetch_order RAISES (network timeout) so the REAL OrderManager.confirm_order
      converges to status=TIMEOUT, reproducing the production failure.
    - fetch_positions returns the venue truth used by reconciliation.
    """

    SAFE_CID = re.compile(r"^[A-Za-z0-9_]+$")

    def __init__(self, price=100.0, balance=10000.0, min_amount=0.001):
        self.created = []
        self.price = price
        self.free_usdt = balance
        self.min_amount = min_amount
        self.fail_fetch_order = True   # confirm-order fetch raises -> TIMEOUT
        self.positions = []            # venue truth returned by fetch_positions
        self.markets = {
            "BTC/USDT:USDT": {"id": "BTC-USDT", "symbol": "BTC/USDT:USDT",
                              "limits": {"amount": {"min": min_amount}},
                              "precision": {"amount": min_amount}},
        }

    def reset(self):
        self.created = []
        self.positions = []

    @staticmethod
    def position(side="long", contracts=0.1, entry=100.0,
                 symbol="BTC/USDT:USDT", leverage=10, pid="pos-1"):
        return {"symbol": symbol, "side": side, "contracts": contracts,
                "entryPrice": entry, "markPrice": entry, "leverage": leverage,
                "positionId": pid, "unrealizedPnl": 0.0,
                "initialMargin": entry * contracts / leverage}

    def create_order(self, sym, order_type, side, amount, params=None):
        params = params or {}
        self.created.append({"sym": sym, "type": order_type, "side": side,
                             "amount": amount, "params": params})
        return {"id": f"order-{len(self.created)}", "status": "closed",
                "filled": amount}

    def fetch_order(self, order_id, sym=None, params=None):
        if self.fail_fetch_order:
            raise TimeoutError("network timeout while fetching order status")
        return {"id": order_id, "status": "closed", "filled": 0.0}

    def fetch_positions(self, symbols=None, params=None):
        return list(self.positions)

    def fetch_open_positions(self, symbols=None, params=None):
        return list(self.positions)

    def fetch_balance(self, params=None):
        return {"free": {"USDT": self.free_usdt}}

    def fetch_ticker(self, sym, params=None):
        return {"last": self.price}

    def amount_to_precision(self, sym, amount):
        return round(float(amount), 6)

    def market(self, sym):
        return self.markets.get(sym) or {
            "symbol": sym, "limits": {"amount": {"min": self.min_amount}},
            "precision": {"amount": self.min_amount}}

    def set_leverage(self, leverage, sym, params=None):
        return {"leverage": leverage, "symbol": sym}

    def fetch_my_trades(self, sym, params=None):
        return []


_ORIG_EX = engine.ex
_ORIG_PAPER = engine.PAPER_MODE
_ORIG_STATE = dict(engine.STATE)
_ORIG_TRADE_STATE = dict(engine.TRADE_STATE)
_ORIG_ACTIVE_TRADE = getattr(engine, "_ACTIVE_TRADE", None)
_ORIG_CLOSING = getattr(engine, "_closing_in_progress", None)
_ORIG_RECON = getattr(engine, "_reconciliation_pending", None)
_ORIG_ORDER_MANAGER = engine._order_manager


class TimeoutRecoveryBase(unittest.TestCase):
    SPEED_KEY = "open_timeout_recovery"

    def setUp(self):
        self.fx = FakeVenue()
        engine.ex = self.fx
        engine.ex.markets = self.fx.markets
        engine.PAPER_MODE = False
        engine._ACTIVE_TRADE = False
        engine._closing_in_progress = False
        engine._reconciliation_pending = False
        engine.STATE["open"] = False
        engine.STATE["symbol"] = None
        engine.TRADE_STATE["in_position"] = False
        engine.DASHBOARD_STATE["position"] = None
        # DASHBOARD logs are a shared global: isolate within this suite.
        self._orig_logs = list(engine.DASHBOARD_STATE.get("logs", []))
        self._orig_errors = list(engine.DASHBOARD_STATE.get("errors", []))
        engine.DASHBOARD_STATE["logs"] = []
        engine.DASHBOARD_STATE["errors"] = []
        # Fast real confirm_order: fetch_order raises -> single loop iteration.
        self.mgr = OrderManager(self.fx, confirm_timeout=0.05)
        engine._order_manager = self.mgr
        self.addCleanup(self._restore)

    def _restore(self):
        engine.ex = _ORIG_EX
        engine.PAPER_MODE = _ORIG_PAPER
        engine.STATE.clear()
        engine.STATE.update(_ORIG_STATE)
        engine.TRADE_STATE.clear()
        engine.TRADE_STATE.update(_ORIG_TRADE_STATE)
        engine._ACTIVE_TRADE = _ORIG_ACTIVE_TRADE
        engine._closing_in_progress = _ORIG_CLOSING
        engine._reconciliation_pending = _ORIG_RECON
        engine._order_manager = _ORIG_ORDER_MANAGER
        engine.DASHBOARD_STATE["logs"] = self._orig_logs
        engine.DASHBOARD_STATE["errors"] = self._orig_errors

    @contextmanager
    def _patch_open_gates(self):
        """Patch the network/venue boundaries open_position depends on."""
        with mock.patch.object(engine, "get_spread_bps", return_value=0.0), \
             mock.patch.object(engine, "dynamic_spread_tolerance", return_value=99.0):
            yield

    @contextmanager
    def _patch_entry_network(self):
        """Patch the entry-time data sources so REAL execute_entry can run."""
        with mock.patch.object(engine, "get_ohlcv_safe", return_value=_entry_df()), \
             mock.patch.object(engine, "get_orderbook_cached",
                               return_value={"bids": [[99.0, 10.0]],
                                             "asks": [[101.0, 5.0]]}), \
             mock.patch.object(engine, "get_free_balance_safe", return_value=20000.0):
            yield

    @staticmethod
    def _logs():
        return "\n".join(engine.DASHBOARD_STATE.get("logs", []))


class OrderTimeoutRecoveryTest(TimeoutRecoveryBase):
    """Scenarios A-D: adoption path for TIMEOUT after actual fill."""

    def test_a_timeout_after_fill_recovers_and_starts_management(self):
        self.fx.positions = [FakeVenue.position(side="long", contracts=0.1,
                                                entry=100.0, pid="pos-long")]
        with self._patch_entry_network(), self._patch_open_gates():
            ok = engine.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER")
        self.assertTrue(ok)
        # The REAL exchange position was adopted and local trade state is active.
        self.assertTrue(engine.STATE.get("open"))
        self.assertTrue(engine.TRADE_STATE.get("in_position"))
        pos = engine.DASHBOARD_STATE.get("position")
        self.assertIsNotNone(pos)
        self.assertEqual(pos["symbol"], "BTC/USDT:USDT")
        # Only ONE open order was ever sent: NO duplicate after timeout.
        self.assertEqual(len(self.fx.created), 1)
        rec = self.fx.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")
        # Dashboard evidence requested by the recovery spec.
        logs = self._logs()
        self.assertIn("[OPEN_RECOVERY]", logs)
        self.assertIn("status=FILLED_AFTER_TIMEOUT", logs)
        self.assertIn("[POSITION_ADOPTED]", logs)
        self.assertIn("[TRADE_MANAGEMENT]", logs)
        self.assertIn("management_initialized=true", logs)
        # The local order book now knows the recovered order is FILLED.
        cid = rec["params"]["clientOrderId"]
        self.assertEqual(self.mgr.get_order_status(cid), "FILLED")

    def test_b_timeout_without_position_no_phantom_no_retry(self):
        self.fx.positions = []   # reconciliation proves NO fill
        with self._patch_entry_network(), self._patch_open_gates():
            ok = engine.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER")
        self.assertFalse(ok)
        # No phantom local/trade state and no dashboard position.
        self.assertFalse(engine.STATE.get("open"))
        self.assertFalse(engine.TRADE_STATE.get("in_position"))
        self.assertIsNone(engine.DASHBOARD_STATE.get("position"))
        # Exactly ONE open attempt: the timeout path must NOT blindly retry.
        self.assertEqual(len(self.fx.created), 1)
        logs = self._logs()
        self.assertIn("status=NOT_FILLED", logs)
        self.assertNotIn("[POSITION_ADOPTED]", logs)
        self.assertNotIn("[TRADE_MANAGEMENT]", logs)
        # The order stays UNKNOWN/unresolved locally (never FILLED, never REJECTED).
        cid = self.fx.created[0]["params"]["clientOrderId"]
        self.assertEqual(self.mgr.get_order_status(cid), "TIMEOUT")

    def test_c_timeout_short_adopted(self):
        self.fx.positions = [FakeVenue.position(side="short", contracts=0.2,
                                                entry=99.0, pid="pos-short")]
        with self._patch_open_gates():
            recovered = engine.open_position("SELL", 0.2, "BTC/USDT:USDT")
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["positionSide"], "SHORT")
        self.assertEqual(recovered["side"], "sell")
        self.assertEqual(recovered["filled"], 0.2)
        self.assertEqual(recovered["average"], 99.0)
        self.assertEqual(recovered["id"], "pos-short")
        # The one order that went out is SELL + SHORT (never BOTH).
        self.assertEqual(len(self.fx.created), 1)
        rec = self.fx.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")

    def test_d_timeout_long_adopted(self):
        self.fx.positions = [FakeVenue.position(side="long", contracts=0.1,
                                                entry=100.0, pid="pos-long")]
        with self._patch_open_gates():
            recovered = engine.open_position("BUY", 0.1, "BTC/USDT:USDT")
        self.assertIsNotNone(recovered)
        self.assertTrue(recovered["recovered"])
        self.assertEqual(recovered["positionSide"], "LONG")
        self.assertEqual(recovered["side"], "buy")
        self.assertEqual(recovered["filled"], 0.1)
        self.assertEqual(recovered["average"], 100.0)
        self.assertEqual(recovered["id"], "pos-long")
        self.assertEqual(len(self.fx.created), 1)
        rec = self.fx.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")


class ProtectionAfterRecoveryTest(TimeoutRecoveryBase):
    """Scenarios E-F: TP/SL protection after adopting a timeout position."""

    def _setup_long_recovery(self):
        self.fx.positions = [FakeVenue.position(side="long", contracts=0.1,
                                                entry=100.0, pid="pos-long")]
        real_set_atr = engine._live_manager.set_entry_atr
        with mock.patch.object(engine._live_manager, "set_entry_atr",
                               wraps=real_set_atr) as set_atr_mock, \
             self._patch_entry_network(), self._patch_open_gates():
            ok = engine.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER")
            return ok, set_atr_mock

    def test_e_recovered_long_with_existing_protection_single_initialize(self):
        # Simulate protection already established on the venue/local ledger.
        engine.STATE["synthetic_sl"] = 98.0
        engine.STATE["synthetic_tp1"] = 105.0
        ok, set_atr_mock = self._setup_long_recovery()
        self.assertTrue(ok)
        # Existing TP/SL initialization path runs EXACTLY once (no duplicate).
        self.assertEqual(set_atr_mock.call_count, 1)
        self.assertIn("synthetic_sl", engine.STATE)
        # No TP/SL protection order was duplicated on the venue.
        self.assertEqual(len(self.fx.created), 1)

    def test_f_recovered_long_without_protection_initializes_once(self):
        engine.STATE.pop("synthetic_sl", None)
        engine.STATE.pop("synthetic_tp1", None)
        ok, set_atr_mock = self._setup_long_recovery()
        self.assertTrue(ok)
        # Existing TP/SL initialization ran exactly once (set_entry_atr is the
        # production synthetic-SL init used by a normal fill).
        self.assertEqual(set_atr_mock.call_count, 1)
        self.assertGreater(float(engine.STATE.get("synthetic_sl", 0.0)), 0.0)
        self.assertEqual(len(self.fx.created), 1)


class ManagementLoopAfterRecoveryTest(TimeoutRecoveryBase):
    """Scenario G: recovered position reaches the management loop."""

    def test_g_recovered_position_reaches_management_loop(self):
        self.fx.positions = [FakeVenue.position(side="long", contracts=0.1,
                                                entry=100.0, pid="pos-long")]
        with self._patch_entry_network(), self._patch_open_gates():
            ok = engine.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER")
        self.assertTrue(ok)
        self.assertEqual(engine._live_manager.lifecycle_state,
                         engine.TradeLifecycleState.OPEN_PENDING_CONFIRMATION)
        seen = []
        with mock.patch.object(engine._live_manager, "_apply_management",
                               side_effect=lambda symbol, now: seen.append(symbol)):
            engine._live_manager.manage_live_trade()
        # Management loop transitioned to LIVE and reached _apply_management.
        self.assertEqual(engine._live_manager.lifecycle_state,
                         engine.TradeLifecycleState.LIVE)
        self.assertEqual(seen, ["BTC/USDT:USDT"])
        self.assertIn("[TRADE_MANAGEMENT]", self._logs())
        logs = self._logs()
        self.assertIn("status=ACTIVE", logs)


class CloseAfterRecoveryTest(TimeoutRecoveryBase):
    """Scenario H: close path preserves Hedge PositionSide + reduceOnly."""

    @contextmanager
    def _patch_close(self, pos_return):
        with mock.patch.object(engine, "fetch_position",
                               side_effect=lambda *a, **k: pos_return), \
             mock.patch.object(engine, "verify_order_filled",
                               return_value=(True, 0.05)), \
             mock.patch.object(engine, "finalize_trade_with_reality",
                               return_value=None), \
             mock.patch.object(engine, "_exchange_sync"):
            yield

    def test_h_partial_and_full_close_after_recovered_long(self):
        self.fx.positions = [FakeVenue.position(side="long", contracts=0.1,
                                                entry=100.0, pid="pos-long")]
        with self._patch_entry_network(), self._patch_open_gates():
            ok = engine.execute_entry(
                "BUY", "BTC/USDT:USDT", 104.0, 100.0, 105.0, 106.0,
                90, "TEST", 1.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER")
        self.assertTrue(ok)
        engine.STATE["remaining_qty"] = 0.1
        remaining = FakeVenue.position(side="long", contracts=0.05, entry=100.0,
                                       pid="pos-long")
        with self._patch_close(remaining):
            engine.close_partial(0.5)               # SELL + reduceOnly + LONG
        engine.STATE["remaining_qty"] = 0.05
        with self._patch_close(None):
            engine.close_position_full()            # SELL + reduceOnly + LONG

        self.assertEqual(len(self.fx.created), 3)
        open_rec, partial_rec, final_rec = self.fx.created
        # OPEN LONG recovered
        self.assertEqual(open_rec["side"], "buy")
        self.assertEqual(open_rec["params"]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", open_rec["params"])
        # PARTIAL CLOSE LONG
        self.assertEqual(partial_rec["side"], "sell")
        self.assertEqual(partial_rec["params"]["positionSide"], "LONG")
        self.assertEqual(partial_rec["params"]["reduceOnly"], True)
        # FINAL CLOSE LONG
        self.assertEqual(final_rec["side"], "sell")
        self.assertEqual(final_rec["params"]["positionSide"], "LONG")
        self.assertEqual(final_rec["params"]["reduceOnly"], True)
        for rec in self.fx.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")

    def test_h_partial_and_full_close_after_recovered_short(self):
        self.fx.positions = [FakeVenue.position(side="short", contracts=0.2,
                                                entry=99.0, pid="pos-short")]
        with self._patch_open_gates():
            recovered = engine.open_position("SELL", 0.2, "BTC/USDT:USDT")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["positionSide"], "SHORT")
        # Adopt the SHORT position into local state (as execute_entry would).
        engine.STATE.update({"open": True, "side": "SELL", "qty": 0.2,
                             "remaining_qty": 0.2, "current_symbol": "BTC/USDT:USDT"})
        remaining = FakeVenue.position(side="short", contracts=0.1, entry=99.0,
                                       pid="pos-short")
        with self._patch_close(remaining):
            engine.close_partial(0.5)               # BUY + reduceOnly + SHORT
        engine.STATE["remaining_qty"] = 0.1
        with self._patch_close(None):
            engine.close_position_full()            # BUY + reduceOnly + SHORT

        self.assertEqual(len(self.fx.created), 3)
        open_rec, partial_rec, final_rec = self.fx.created
        self.assertEqual(open_rec["side"], "sell")
        self.assertEqual(open_rec["params"]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", open_rec["params"])
        self.assertEqual(partial_rec["side"], "buy")
        self.assertEqual(partial_rec["params"]["positionSide"], "SHORT")
        self.assertEqual(partial_rec["params"]["reduceOnly"], True)
        self.assertEqual(final_rec["side"], "buy")
        self.assertEqual(final_rec["params"]["positionSide"], "SHORT")
        self.assertEqual(final_rec["params"]["reduceOnly"], True)
        for rec in self.fx.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")


class DuplicateReconciliationTest(TimeoutRecoveryBase):
    """Scenario I: repeated reconciliation never double-registers or re-opens."""

    def test_i_duplicate_reconciliation_no_duplicate_order(self):
        self.fx.positions = [FakeVenue.position(side="long", contracts=0.1,
                                                entry=100.0, pid="pos-long")]
        with self._patch_open_gates():
            recovered = engine.open_position("BUY", 0.1, "BTC/USDT:USDT")
        self.assertIsNotNone(recovered)
        self.assertEqual(len(self.fx.created), 1)
        cid = recovered["clientOrderId"]
        # Reconciliation is a pure READ: it never creates an order.
        again = engine.reconcile_open_after_timeout("BTC/USDT:USDT", "BUY", cid)
        self.assertIsNotNone(again)
        self.assertEqual(again["id"], recovered["id"])
        self.assertEqual(again["clientOrderId"], cid)
        self.assertEqual(len(self.fx.created), 1)
        # mark_recovered is idempotent: the same order is not re-registered.
        self.mgr.mark_recovered(cid, recovered)
        self.mgr.mark_recovered(cid, recovered)
        data = self.mgr._pending_orders[cid]
        self.assertEqual(data["status"], "FILLED")
        self.assertEqual(data["filled"], 0.1)
        self.assertEqual(len(self.fx.created), 1)
        self.assertEqual(len(self.fx.created), 1)


if __name__ == "__main__":
    unittest.main()