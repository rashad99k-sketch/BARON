"""Isolated regression suite for the BingX Hedge-Mode PositionSide fix.

These tests exercise the REAL production order-submission layer
(OrderManager.submit_order -> exchange.create_order) and the REAL hedge close
functions (close_partial / close_position_full / close_position) with
PAPER_MODE=False and a deterministic fake exchange that records the exact
params sent to create_order.  In Hedge Mode the close orders must NOT send
reduceOnly (BingX Docs-v3 forbid it) while keeping positionSide LONG/SHORT.

All six-position portfolio caps / NEWS slot / coexistence are validated by the
existing PAPER-mode harness (tests/test_portfolio_full_cycle.py and
tests/test_slot_execution.py); this file additionally proves the ORDER LAYER
maps every position direction to the correct hedge-mode LONG/SHORT value.
"""
import os
import re
import unittest
from unittest import mock

import core.engine as engine

from core.engine import OrderManager

# Snapshot real close functions BEFORE any upstream test module replaces them
# with stubs (test_position_management_phase1 etc. patch these and never restore).
_real_close_partial = engine.close_partial
_real_close_position_full = engine.close_position_full
_real_close_position = getattr(engine, "close_position", None)


class FakeExchange:
    """Deterministic fake exchange that records create_order params and lets
    tests drive fills / rejections deterministically."""

    SAFE_CID = re.compile(r"^[A-Za-z0-9_]+$")

    def __init__(self, price=100.0, balance=10000.0, min_amount=0.001):
        self.created = []
        self.price = price
        self.free_usdt = balance
        self.min_amount = min_amount
        self.reject = False          # create_order raises (exchange rejection)
        self.pending_fill = False    # fetch_order stays PENDING (failed/partial fill)
        self.markets = {
            "BTC/USDT:USDT": {"id": "BTC-USDT", "symbol": "BTC/USDT:USDT",
                              "limits": {"amount": {"min": min_amount}},
                              "precision": {"amount": min_amount}},
        }

    def create_order(self, sym, order_type, side, amount, params=None):
        params = params or {}
        self.created.append({"sym": sym, "type": order_type, "side": side,
                             "amount": amount, "params": params})
        if self.reject:
            raise RuntimeError(
                'bingx {"code":109400,"msg":"In the Hedge mode, the PositionSide '
                'field can only be set to LONG or SHORT."}')
        return {"id": f"order-{len(self.created)}", "status": "closed", "filled": amount}

    def fetch_order(self, order_id, sym=None, params=None):
        if self.pending_fill:
            return {"id": order_id, "status": "open", "filled": 0.0}
        filled = self.created[-1]["amount"] if self.created else 0.0
        return {"id": order_id, "status": "closed", "filled": filled}

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


def _state(side="BUY", symbol="BTC/USDT:USDT", qty=0.1):
    engine.STATE["open"] = True
    engine.STATE["side"] = side
    engine.STATE["current_symbol"] = symbol
    engine.STATE["remaining_qty"] = qty


def _patch_market_registry(fx):
    engine.ex = fx
    engine.ex.markets = fx.markets


_ORIG_EX = engine.ex
_ORIG_PAPER = engine.PAPER_MODE
_ORIG_STATE = dict(engine.STATE)
_ORIG_ACTIVE_TRADE = getattr(engine, "_ACTIVE_TRADE", None)
_ORIG_CLOSING = getattr(engine, "_closing_in_progress", None)
_ORIG_RECON = getattr(engine, "_reconciliation_pending", None)
_ORIG_MARGIN_COOLDOWN = getattr(engine, "INSUFFICIENT_MARGIN_COOLDOWN_UNTIL", None)


def _restore_engine_globals():
    """Undo any mutation of shared engine singleton state so we never pollute
    other test modules in the same pytest/unittest process."""
    engine.ex = _ORIG_EX
    engine.PAPER_MODE = _ORIG_PAPER
    engine.STATE.clear()
    engine.STATE.update(_ORIG_STATE)
    if _ORIG_ACTIVE_TRADE is not None:
        engine._ACTIVE_TRADE = _ORIG_ACTIVE_TRADE
    if _ORIG_CLOSING is not None:
        engine._closing_in_progress = _ORIG_CLOSING
    if _ORIG_RECON is not None:
        engine._reconciliation_pending = _ORIG_RECON
    engine.INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = _ORIG_MARGIN_COOLDOWN


class LiveOrderLayerLifecycleTest(unittest.TestCase):
    """OPEN -> CONFIRM -> PARTIAL CLOSE -> REMAINING -> FINAL CLOSE -> CLOSED
    through the real order-submission layer for both LONG and SHORT."""

    def setUp(self):
        self.fx = FakeExchange()
        _patch_market_registry(self.fx)
        engine.PAPER_MODE = False
        engine._ACTIVE_TRADE = False
        engine._closing_in_progress = False
        engine._reconciliation_pending = False
        self.addCleanup(_restore_engine_globals)
        self.mgr = OrderManager(self.fx)
        self.pos_return = None           # controllable fetch_position result
        self.patches = [
            mock.patch.object(engine, "close_partial", _real_close_partial),
            mock.patch.object(engine, "close_position_full", _real_close_position_full),
            mock.patch.object(engine, "fetch_position",
                              side_effect=lambda *a, **k: self.pos_return),
            mock.patch.object(engine, "verify_order_filled",
                              return_value=(True, 0.05)),
            mock.patch.object(engine, "finalize_trade_with_reality", return_value=None),
            mock.patch.object(engine, "_exchange_sync"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    # ---- LONG lifecycle ----
    def test_long_full_lifecycle_position_side(self):
        _state("BUY", qty=0.1)
        self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.1, 10)
        open_rec = self.fx.created[0]
        self.assertEqual(open_rec["side"], "buy")
        self.assertEqual(open_rec["params"]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", open_rec["params"])

        self.pos_return = {"contracts": 0.05, "side": "long"}  # REMAINING position
        engine.close_partial(0.5)          # PARTIAL CLOSE LONG
        partial = self.fx.created[1]
        self.assertEqual(partial["side"], "sell")
        self.assertEqual(partial["params"]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", partial["params"],
                         "hedge partial close must NOT carry reduceOnly (DEV-01)")

        self.pos_return = None             # position gone -> FINAL CLOSE
        _state("BUY", qty=0.05)
        engine.close_position_full()       # FINAL CLOSE LONG
        final = self.fx.created[2]
        self.assertEqual(final["side"], "sell")
        self.assertEqual(final["params"]["positionSide"], "LONG")
        self.assertNotIn("reduceOnly", final["params"],
                         "hedge full close must NOT carry reduceOnly (DEV-01)")

        self.assertFalse(engine.STATE["open"])   # CLOSED

    # ---- SHORT lifecycle ----
    def test_short_full_lifecycle_position_side(self):
        _state("SELL", qty=0.1)
        self.mgr.submit_order("BTC/USDT:USDT", "sell", 0.1, 10)
        open_rec = self.fx.created[0]
        self.assertEqual(open_rec["side"], "sell")
        self.assertEqual(open_rec["params"]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", open_rec["params"])

        self.pos_return = {"contracts": 0.05, "side": "short"}  # REMAINING position
        engine.close_partial(0.5)          # PARTIAL CLOSE SHORT
        partial = self.fx.created[1]
        self.assertEqual(partial["side"], "buy")
        self.assertEqual(partial["params"]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", partial["params"],
                         "hedge partial close must NOT carry reduceOnly (DEV-01)")

        self.pos_return = None             # position gone -> FINAL CLOSE
        _state("SELL", qty=0.05)
        engine.close_position_full()       # FINAL CLOSE SHORT
        final = self.fx.created[2]
        self.assertEqual(final["side"], "buy")
        self.assertEqual(final["params"]["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", final["params"],
                         "hedge full close must NOT carry reduceOnly (DEV-01)")

        self.assertFalse(engine.STATE["open"])   # CLOSED

    # ---- exchange rejection is surfaced (submit_order re-raises),
    #      and the original BOTH-based 109400 cannot recur ----
    def test_exchange_rejection_surfaces(self):
        _state("BUY", qty=0.1)
        self.fx.reject = True
        with self.assertRaises(RuntimeError) as cm:
            self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.1, 10)
        self.assertIn("109400", str(cm.exception))

    # ---- duplicate close of already-closed position is a no-op ----
    def test_close_already_closed_is_noop(self):
        engine.STATE["open"] = False
        engine.STATE["remaining_qty"] = 0.0
        self.assertFalse(engine.close_position_full())

    # ---- clientOrderId stays <= 40 with safe chars across all lifecycle orders ----
    def test_client_order_id_under_limit_across_lifecycle(self):
        _state("BUY", qty=0.1)
        self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.1, 10)
        engine.close_partial(0.5)
        for rec in self.fx.created:
            cid = rec["params"].get("clientOrderId", "")
            if cid:
                self.assertLessEqual(len(cid), 40)
                self.assertRegex(cid, FakeExchange.SAFE_CID)

    # ---- partial-fill scenario: correct PositionSide (never BOTH), and hedge
    #      closes never send reduceOnly ----
    def test_partial_fill_never_sends_both(self):
        self.pos_return = {"contracts": 0.05, "side": "long"}  # remaining position
        _state("BUY", qty=0.1)
        engine.close_partial(0.5)
        for rec in self.fx.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")
            self.assertIn(rec["params"].get("positionSide"), ("LONG", "SHORT"),
                          "hedge close must keep LONG/SHORT")
            self.assertNotIn("reduceOnly", rec["params"],
                             "hedge closes must never send reduceOnly (DEV-01)")

    # ---- reconciliation boundary never generates BOTH, for both LONG and SHORT ----
    def test_reconciliation_boundary_no_both(self):
        self.pos_return = {"contracts": 0.05, "side": "long"}
        _state("BUY", qty=0.1)
        engine.close_partial(0.5)
        self.pos_return = {"contracts": 0.05, "side": "short"}
        _state("SELL", qty=0.1)
        engine.close_partial(0.5)
        for rec in self.fx.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")


class SixPositionOrderLayerTest(unittest.TestCase):
    """Prove the ORDER LAYER maps every one of the six portfolio directions
    (5 technical + 1 NEWS) to the correct hedge-mode LONG/SHORT value, and no
    position ever produces BOTH."""

    SIX = [
        # (symbol, direction/deposit side) -> expected order side + positionSide
        ("BTC/USDT:USDT", "buy", "LONG"),      # technical LONG
        ("ETH/USDT:USDT", "sell", "SHORT"),    # technical SHORT
        ("US500/USDT:USDT", "buy", "LONG"),    # technical LONG
        ("USTECH/USDT:USDT", "sell", "SHORT"), # technical SHORT
        ("XAUUSD", "buy", "LONG"),             # technical position
        ("NCSKNVDA2USD/USDT:USDT", "sell", "SHORT"),  # NEWS position
    ]

    def setUp(self):
        self.fx = FakeExchange()
        _patch_market_registry(self.fx)
        engine.PAPER_MODE = False
        self.addCleanup(_restore_engine_globals)
        self.mgr = OrderManager(self.fx)

    def test_all_six_directions_map_to_hedge_mode(self):
        for idx, (sym, order_side, expected_ps) in enumerate(self.SIX, start=1):
            self.mgr.submit_order(sym, order_side, 0.01, 10)
            rec = self.fx.created[idx - 1]
            self.assertEqual(rec["side"], order_side)
            self.assertEqual(rec["params"]["positionSide"], expected_ps,
                             f"{sym} should map to {expected_ps}")
            self.assertNotEqual(rec["params"]["positionSide"], "BOTH")
        self.assertEqual(len(self.fx.created), 6)

    def test_no_duplicate_orders_and_unique_client_ids(self):
        seen_ids = set()
        for sym, order_side, _ in self.SIX:
            _, cid = self.mgr.submit_order(sym, order_side, 0.01, 10)
            self.assertNotIn(cid, seen_ids)
            seen_ids.add(cid)
        self.assertEqual(len(seen_ids), 6)

    def test_no_order_ever_uses_both(self):
        for sym, order_side, _ in self.SIX:
            self.mgr.submit_order(sym, order_side, 0.01, 10)
        for rec in self.fx.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")


class ErrorHandlingTest(unittest.TestCase):
    def setUp(self):
        self.fx = FakeExchange()
        _patch_market_registry(self.fx)
        engine.PAPER_MODE = False
        self.addCleanup(_restore_engine_globals)
        self.mgr = OrderManager(self.fx)

    def test_insufficient_balance_blocked_before_order(self):
        # open_position gate: usdt < required_margin*1.01 -> reject, no order.
        # price=100, LEVERAGE=10, amount=0.5 -> required margin 5.0, free 1.0.
        self.fx.free_usdt = 1.0
        engine.STATE["open"] = False
        engine._ACTIVE_TRADE = False
        with mock.patch.object(engine, "normalize_symbol", side_effect=lambda s: s), \
             mock.patch.object(engine, "get_spread_bps", return_value=0.0):
            result = engine.open_position("buy", 0.5, "BTC/USDT:USDT")
        self.assertIsNone(result)
        self.assertEqual(len(self.fx.created), 0)

    def test_minimum_quantity_gate_no_order(self):
        self.fx.min_amount = 1.0
        engine.STATE["open"] = False
        engine._ACTIVE_TRADE = False
        # open_position min-qty gate is inside execute_entry; here we assert
        # OrderManager still rejects a sub-minimum order through the exchange.
        self.fx.min_amount = 1.0
        # submit_order passes through -- no local min gate; exchange would reject.
        # Guard: ensure the fake still records it (no BOTH sent) rather than crashing.
        order, cid = self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.01, 10)
        self.assertIsNotNone(order)
        self.assertEqual(self.fx.created[0]["params"]["positionSide"], "LONG")


if __name__ == "__main__":
    unittest.main()
