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


class _MockExchange:
    """Records create_order params so tests can assert the exact BingX payload."""

    def __init__(self):
        self.created = []
        self.fetch_order = lambda *a, **k: {"status": "closed", "filled": 1.0}
        self.fetch_positions = lambda *a, **k: []
        self.amount_to_precision = lambda sym, qty: qty

    def create_order(self, sym, order_type, side, amount, params=None):
        params = params or {}
        self.created.append(
            {"sym": sym, "type": order_type, "side": side, "amount": amount, "params": params}
        )
        return {"id": "mock-order-id", "status": "closed"}


def _patch_market_registry():
    engine.ex.markets = {
        "BTC/USDT:USDT": {},
        "BTC/USDT": {},
        "BTC:-USDT": {},
    }


_ORIG_EX = engine.ex
_ORIG_PAPER = engine.PAPER_MODE
_ORIG_STATE = dict(engine.STATE)
_ORIG_ACTIVE_TRADE = getattr(engine, "_ACTIVE_TRADE", None)
_ORIG_CLOSING = getattr(engine, "_closing_in_progress", None)
_ORIG_RECON = getattr(engine, "_reconciliation_pending", None)


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


class HedgePositionSideHelperTest(unittest.TestCase):
    def test_maps_position_direction_to_hedge_mode(self):
        self.assertEqual(engine._hedge_position_side("BUY"), "LONG")
        self.assertEqual(engine._hedge_position_side("SELL"), "SHORT")
        self.assertEqual(engine._hedge_position_side("buy"), "LONG")
        self.assertEqual(engine._hedge_position_side("sell"), "SHORT")
        self.assertEqual(engine._hedge_position_side("LONG"), "LONG")
        self.assertEqual(engine._hedge_position_side("SHORT"), "SHORT")

    def test_rejects_unknown_direction(self):
        with self.assertRaises(ValueError):
            engine._hedge_position_side("neither")


class PositionSideOpenOrders(unittest.TestCase):
    BINGX_SAFE_CHARS = re.compile(r"^[A-Za-z0-9_]+$")

    def setUp(self):
        _patch_market_registry()
        self.ex = _MockExchange()
        self.mgr = OrderManager(self.ex)

    # TEST 1 — OPEN LONG
    def test_open_long_sends_buy_and_long(self):
        self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.01, 10)
        self.assertEqual(len(self.ex.created), 1)
        rec = self.ex.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertEqual(rec["params"]["leverage"], 10)

    # TEST 2 — OPEN SHORT
    def test_open_short_sends_sell_and_short(self):
        self.mgr.submit_order("BTC/USDT:USDT", "sell", 0.01, 10)
        self.assertEqual(len(self.ex.created), 1)
        rec = self.ex.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertEqual(rec["params"]["leverage"], 10)

    # TEST 9/11 — never BOTH; OPEN with uppercase BUY/SELL also maps correctly
    def test_open_never_sends_both(self):
        self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.01, 10)
        self.mgr.submit_order("BTC/USDT:USDT", "sell", 0.01, 10)
        for rec in self.ex.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")

    # TEST 10 — CLIENTORDERID <= 40 and safe
    def _assert_safe_cid(self, cid):
        self.assertLessEqual(len(cid), 40)
        self.assertRegex(cid, self.BINGX_SAFE_CHARS)

    def test_open_client_order_id_under_limit(self):
        self.mgr.submit_order("BTC/USDT:USDT", "buy", 0.01, 10)
        self.mgr.submit_order("BTC/USDT:USDT", "sell", 0.01, 10)
        for rec in self.ex.created:
            cid = rec["params"].get("clientOrderId", "")
            self._assert_safe_cid(cid)


class PositionSideCloseOrders(unittest.TestCase):
    def setUp(self):
        _patch_market_registry()
        engine.PAPER_MODE = False
        engine._closing_in_progress = False
        engine._reconciliation_pending = False
        engine.STATE["open"] = True
        engine.STATE["current_symbol"] = "BTC/USDT:USDT"
        engine.STATE["remaining_qty"] = 0.1
        engine.ex = _MockExchange()
        self.addCleanup(_restore_engine_globals)

        self.patches = [
            mock.patch.object(engine, "close_partial", _real_close_partial),
            mock.patch.object(engine, "close_position_full", _real_close_position_full),
            mock.patch.object(engine, "fetch_position", return_value=None),
            mock.patch.object(engine, "verify_order_filled", return_value=(True, 0.05)),
            mock.patch.object(engine, "finalize_trade_with_reality", return_value=None),
            mock.patch.object(engine, "_exchange_sync"),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    # TEST 3 — CLOSE LONG (reduceOnly preserved)
    def test_close_partial_long(self):
        engine.STATE["side"] = "BUY"
        engine.close_partial(0.5)
        rec = engine.ex.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertEqual(rec["params"]["reduceOnly"], True)

    # TEST 4 — CLOSE SHORT (reduceOnly preserved)
    def test_close_partial_short(self):
        engine.STATE["side"] = "SELL"
        engine.close_partial(0.5)
        rec = engine.ex.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertEqual(rec["params"]["reduceOnly"], True)

    # TEST 7 — FULL CLOSE LONG
    def test_close_full_long(self):
        engine.STATE["side"] = "BUY"
        engine.close_position_full()
        rec = engine.ex.created[0]
        self.assertEqual(rec["side"], "sell")
        self.assertEqual(rec["params"]["positionSide"], "LONG")
        self.assertEqual(rec["params"]["reduceOnly"], True)

    # TEST 8 — FULL CLOSE SHORT
    def test_close_full_short(self):
        engine.STATE["side"] = "SELL"
        engine.close_position_full()
        rec = engine.ex.created[0]
        self.assertEqual(rec["side"], "buy")
        self.assertEqual(rec["params"]["positionSide"], "SHORT")
        self.assertEqual(rec["params"]["reduceOnly"], True)

    # TEST 5 — PARTIAL CLOSE LONG keeps LONG
    def test_partial_close_long_position_side_stays_long(self):
        engine.STATE["side"] = "BUY"
        engine.close_partial(0.5)
        self.assertEqual(engine.ex.created[0]["params"]["positionSide"], "LONG")

    # TEST 6 — PARTIAL CLOSE SHORT keeps SHORT
    def test_partial_close_short_position_side_stays_short(self):
        engine.STATE["side"] = "SELL"
        engine.close_partial(0.5)
        self.assertEqual(engine.ex.created[0]["params"]["positionSide"], "SHORT")

    # TEST 9 — no close path sends BOTH
    def test_closes_never_send_both(self):
        engine.STATE["side"] = "BUY"
        engine.close_partial(0.5)
        engine.close_position_full()
        engine.STATE["side"] = "SELL"
        engine.close_partial(0.5)
        engine.close_position_full()
        for rec in engine.ex.created:
            self.assertNotEqual(rec["params"].get("positionSide"), "BOTH")

    # TEST 10 — force exact clientOrderId under limit through OrderManager open path
    def test_explicit_client_order_id_under_limit(self):
        mgr = OrderManager(engine.ex)
        mgr.submit_order("BTC/USDT:USDT", "buy", 0.01, 10, client_order_id="short_cid")
        rec = engine.ex.created[0]
        self.assertEqual(rec["params"]["clientOrderId"], "short_cid")
        self.assertEqual(rec["params"]["positionSide"], "LONG")


def _scan_for_both_and_one_way():
    """Assert no active production order path sends BOTH or forces one-way."""
    import pathlib
    active = [pathlib.Path("core/engine.py"), pathlib.Path("app/bootstrap.py"),
              pathlib.Path("execution/execution_service.py")]
    for p in active:
        text = p.read_text(encoding="utf-8")
        if "positionSide\": \"BOTH\"" in text or "positionSide': 'BOTH'" in text:
            return f"{p} still contains positionSide BOTH"
        if "positionSide=\"BOTH\"" in text:
            return f"{p} still contains positionSide BOTH"
        if "set_position_mode(False)" in text:
            return f"{p} still forces one-way mode"
    return None


class NoBothAndNoOneWayScanTest(unittest.TestCase):
    def test_active_code_has_no_both_or_one_way_force(self):
        problem = _scan_for_both_and_one_way()
        self.assertIsNone(problem, problem)


class OriginalErrorRegressionTest(unittest.TestCase):
    """Reproduce the original 109400 condition: hedge mode rejects BOTH.

    The corrected implementation must only emit LONG/SHORT from the actual
    position direction, so the hedge-mode Path of the original error cannot
    recur through the same code paths.
    """

    def test_open_long_emits_long_not_both(self):
        _patch_market_registry()
        ex = _MockExchange()
        mgr = OrderManager(ex)
        mgr.submit_order("BTC/USDT:USDT", "buy", 0.01, 10)
        self.assertEqual(ex.created[0]["params"]["positionSide"], "LONG")
        self.assertNotEqual(ex.created[0]["params"]["positionSide"], "BOTH")

    def test_open_short_emits_short_not_both(self):
        _patch_market_registry()
        ex = _MockExchange()
        mgr = OrderManager(ex)
        mgr.submit_order("BTC/USDT:USDT", "sell", 0.01, 10)
        self.assertEqual(ex.created[0]["params"]["positionSide"], "SHORT")
        self.assertNotEqual(ex.created[0]["params"]["positionSide"], "BOTH")


if __name__ == "__main__":
    unittest.main()
