"""Unified 50/50 profit-taking regression suite (Part 3) — scenarios A-J.

Locks the 2-phase TP model through the REAL production code paths:

  A  data duplicate TP1 partial blocked after the verified TP1 fill
  B  runner partial (any ratio) blocked after TP1; diagnostic recorded
  C  strict full close closes the ENTIRE runner (real finalize math)
  D  gap through both phases: full close on a full position never over-closes
     (win and loss runs; side never reverses, no phantom remainder)
  E  LONG / SHORT symmetry for the exact halving and the gate
  F  partial-fill reconcile: confirm TIMEOUT proved on the venue books the
     ACTUAL fill, marks TP1 once, blocks the next fractional close pre-venue
  G  restart: initial size + TP1 reconstructed from remaining + partial legs
  H  coordinator partial mirrors the engine ACTUAL remaining (single authority)
  I  paper ledger tracks the true post-close size (no phantom runner growth)
     and a loss-mark partial never over-closes
  J  full pipeline runs unwind cleanly and aggregates exactly once

Conventions: engine is executed fresh into the shared identity per class
(setUpClass) exactly like the T6 full-cycle suite; paper/live branches are
driven through close_partial / close_position_full / finalize_trade_with_reality.
"""
import copy
import os
import types
import unittest

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")

import core.engine as E  # noqa: E402  real production engine
from core.trade import ExitReason, Trade  # noqa: E402
from portfolio.coordinator import TradeExecutionCoordinator  # noqa: E402


def _freshen_engine():
    """Rebuild the canonical engine in place before the test so engine state is
    deterministic regardless of which files ran earlier in the pytest process."""
    try:
        exec(compile(E.__loader__.get_source("core.engine"), E.__file__, "exec"),
             vars(E))
    except Exception:  # pragma: no cover - defensive
        pass


def _perf_reset():
    return {"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "last_trade": {}, "symbols": {}}


class _Ticker:
    """Deterministic venue mark for the paper ledger."""

    def __init__(self, base=100.0):
        self.base = float(base)

    def __call__(self, symbol, retries=3, **kwargs):
        return self.base


class ProfitTaker5050Test(unittest.TestCase):
    """Seeded paper-venue tests driving close_partial / close_position_full."""

    @classmethod
    def setUpClass(cls):
        _freshen_engine()

    def setUp(self):
        E.PAPER_MODE = True
        E._closing_in_progress = False
        E._reconciliation_pending = False
        self.tick = _Ticker(104.0)
        self._seed("BUY", mark=104.0)

    def tearDown(self):
        E.PAPER_MODE = True
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.paper = {"balance": 1000.0, "position": None, "committed_margin": 0.0}
        E.PERF = _perf_reset()
        E.DASHBOARD_STATE.clear()
        E.DASHBOARD_STATE.update({"logs": [], "errors": [],
                                  "live_trade_mode": False, "position": None})
        E._closing_in_progress = False
        E._reconciliation_pending = False

    def _seed(self, side="BUY", qty=100.0, entry=100.0, mark=104.0,
              margin=10.0, balance=1000.0, symbol="BTC/USDT"):
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.STATE.update({
            "open": True, "side": side, "entry": entry, "qty": qty,
            "qty_initial": qty, "remaining_qty": qty, "margin": margin,
            "fill_request_price": entry, "partial_realized": [],
            "sl": entry - 2.0, "synthetic_tp1": entry * 1.08,
            "tp1_price": entry * 1.08, "tp2_price": entry * 1.20,
            "current_symbol": symbol, "mark_price": mark,
            "position_asset_class": "CRYPTO", "trade_type": "REVERSAL",
            "entry_time": 1000000.0,
            "realized_pnl_usdt": 0.0, "realized_pnl_pct": 0.0,
            "realized_legs": 0, "realized_roe_pct": 0.0,
            "diagnostics": [],
        })
        E.paper = {"balance": balance,
                   "position": {"side": side, "entry": entry, "qty": qty,
                                "remaining_qty": qty, "symbol": symbol},
                   "committed_margin": margin}
        E.PERF = _perf_reset()
        E.TRADE_STATE.update({"in_position": True, "qty": qty,
                              "symbol": symbol})
        E.DASHBOARD_STATE.clear()
        E.DASHBOARD_STATE.update({"logs": [], "errors": [],
                                  "live_trade_mode": True, "position": None})
        E.get_ticker_safe = self.tick
        E.get_balance_safe = lambda retries=3: E.paper["balance"]

    # A ----------------------------------------------------------------------
    def test_duplicate_tp1_partial_blocked_after_verified_fill(self):
        ok1 = E.close_partial(0.5)
        self.assertTrue(ok1)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertEqual(str(E.STATE.get("tp1_state")), "EXECUTED")
        self.assertAlmostEqual(float(E.STATE["tp1_fill_qty"]), 50.0, places=6)
        self.assertEqual(len(E.STATE["partial_realized"]), 1)
        # the duplicate TP1 close must be rejected, not silently re-scaled
        ok2 = E.close_partial(0.5)
        self.assertFalse(ok2)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertEqual(len(E.STATE["partial_realized"]), 1)
        self.assertEqual(str(E.STATE.get("tp1_state")), "EXECUTED")
        self.assertEqual(E.STATE["runner_mode"], True)

    # B ----------------------------------------------------------------------
    def test_runner_any_ratio_partial_blocked_after_tp1(self):
        self.assertTrue(E.close_partial(0.5))
        ok = E.close_partial(0.25)  # runner de-risk attempt
        self.assertFalse(ok, "runner partial must be blocked after TP1")
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertEqual(str(E.STATE.get("tp1_state")), "EXECUTED")
        diag = [d for d in E.STATE.get("diagnostics", [])
                if d.get("event") == "runner_partial_blocked"]
        self.assertEqual(len(diag), 1)
        self.assertAlmostEqual(float(diag[0]["ratio"]), 0.25, places=6)

    # C ----------------------------------------------------------------------
    def test_strict_full_close_closes_entire_runner_with_real_finalize(self):
        # TP1 banks 50% at 104.0 -> 200 USDT realized on the leg.
        self.assertTrue(E.close_partial(0.5))
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertAlmostEqual(float(E.STATE["realized_pnl_usdt"]), 200.0, places=6)
        # strict close = the ENTIRE runner at +8% (not a second partial).
        self.tick.base = 108.0
        E.STATE["mark_price"] = 108.0
        self.assertTrue(E.close_position_full())
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.0, places=6)
        self.assertFalse(bool(E.STATE.get("open")))
        self.assertIsNone(E.paper["position"])
        self.assertAlmostEqual(float(E.paper["committed_margin"]), 0.0, places=6)
        self.assertAlmostEqual(float(E.paper["balance"]), 1610.0, places=6)
        self.assertEqual(E.PERF["trades"], 1)
        self.assertEqual(E.PERF["wins"], 1)
        self.assertEqual(E.PERF["losses"], 0)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], 600.0, places=6)
        self.assertAlmostEqual(E.PERF["total_pnl_pct"], 6.0, places=6)

    # D ----------------------------------------------------------------------
    def test_gap_full_close_never_over_closes_nor_reverses(self):
        # Straight to full close with the price already past TP2: no partials.
        self.assertAlmostEqual(float(E.STATE["qty_initial"]), 100.0, places=6)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 100.0, places=6)
        self.tick.base = 120.0
        E.STATE["mark_price"] = 120.0
        self.assertTrue(E.close_position_full())
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.0, places=6,
                               msg="exactly the position size closed, never more")
        self.assertEqual(len(E.STATE["partial_realized"]), 0)
        self.assertFalse(bool(E.STATE.get("open")))
        self.assertIsNone(E.paper["position"])
        self.assertEqual(E.PERF["trades"], 1)
        self.assertEqual(E.PERF["wins"], 1)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], 2000.0, places=6)
        # LOSS run: the close side is derived from the open side (no reversal);
        # the position closes to exactly zero and is booked once.
        self._seed("BUY", mark=90.0)
        self.assertEqual(E.STATE["side"], "BUY")
        self.tick.base = 90.0
        self.assertTrue(E.close_position_full())
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 0.0, places=6)
        self.assertFalse(bool(E.STATE.get("open")))
        self.assertEqual(E.PERF["losses"], 1)
        self.assertEqual(E.PERF["wins"], 0)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], -1000.0, places=6)

    # E ----------------------------------------------------------------------
    def test_long_short_symmetry_for_halving_and_gate(self):
        self.assertTrue(E.close_partial(0.5))
        qty_long = float(E.STATE["remaining_qty"])
        self.assertAlmostEqual(qty_long, 50.0, places=6)
        self.assertFalse(E.close_partial(0.5))
        self._seed("SELL", mark=96.0)
        self.tick.base = 96.0
        self.assertTrue(E.close_partial(0.5))
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertEqual(str(E.STATE.get("tp1_state")), "EXECUTED")
        self.assertFalse(E.close_partial(0.5))
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        short_leg = E.STATE["partial_realized"][0]
        self.assertEqual(short_leg["side"], "SELL")
        self.assertEqual(short_leg["mode"], "PAPER")

    # F ----------------------------------------------------------------------
    def test_timeout_reconcile_books_actual_fill_and_marks_tp1_once(self):
        E.PAPER_MODE = False
        venue_calls = []

        class _Venue:
            markets = {}

            def amount_to_precision(self, symbol, amount):
                return float(amount)

            def price_to_precision(self, symbol, price):
                return float(price)

            def create_order(self, symbol, order_type, side, amount,
                             price=None, params=None):
                venue_calls.append((symbol, side, amount, params))
                return {"id": "ord-1", "average": 104.0, "price": 104.0}

        _saved = {
            "ex": E.ex,
            "verify_order_filled": E.verify_order_filled,
            "_reconcile_close_timeout": E._reconcile_close_timeout,
            "fetch_position": E.fetch_position,
            "finalize_trade_with_reality": E.finalize_trade_with_reality,
            "_exchange_sync": E._exchange_sync,
        }
        try:
            E.ex = _Venue()
            # confirm TIMEOUT: the order may have executed, we cannot distinguish
            E.verify_order_filled = (lambda symbol, order_id, side, qty, timeout=10:
                                     (False, qty))
            # reconcile PROVES the fill: remaining = initial - proven closed qty
            E._reconcile_close_timeout = (
                lambda symbol, side, cid, qty, expected=0.0:
                E.STATE.__setitem__(
                    "remaining_qty",
                    float(max(0.0, float(E.STATE["remaining_qty"]) - float(qty)))) or True)
            E.fetch_position = lambda symbol: {"contracts":
                                               float(E.STATE["remaining_qty"])}
            E.finalize_trade_with_reality = lambda *a, **k: None
            E._exchange_sync = types.SimpleNamespace(reconcile=lambda *a, **k: None)

            self._seed("BUY", mark=104.0)
            E.STATE["mark_price"] = 104.0
            ok = E.close_partial(0.5)
            self.assertTrue(ok, "proven reconcile must adopt the recovered TP1 fill")
            self.assertEqual(len(venue_calls), 1)
            self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
            self.assertEqual(str(E.STATE.get("tp1_state")), "EXECUTED")
            self.assertAlmostEqual(float(E.STATE["tp1_fill_qty"]), 50.0, places=6)
            self.assertEqual(str(E.STATE.get("profit_stage")), "TP1_EXECUTED")
            leg = E.STATE["partial_realized"][0]
            self.assertEqual(leg["mode"], "LIVE_RECOVERED")
            self.assertAlmostEqual(float(leg["qty"]), 50.0, places=6)
            self.assertAlmostEqual(float(leg["price"]), 104.0, places=6)
            self.assertAlmostEqual(float(E.STATE["realized_pnl_usdt"]), 200.0, places=6)
            # the next fractional close is blocked BEFORE any venue activity
            E.PAPER_MODE = True
            self.assertFalse(E.close_partial(0.5))
            self.assertEqual(len(venue_calls), 1)
        finally:
            # Restore every stub this test installed so later files in the
            # same process keep the real engine (no cross-file pollution).
            for _k, _v in _saved.items():
                setattr(E, _k, _v)

    # G ----------------------------------------------------------------------
    def test_restart_reconstructs_initial_size_and_tp1_from_legs(self):
        class _FakeJournal:
            def recover_trade_id(self, symbol):
                return "T-RECOVERY-1"

            def reconstruct_trade_history(self, symbol, trade_id):
                return [
                    {"decision": "PARTIAL_CLOSE", "ts": 2.0,
                     "metadata": {"partial_close_leg": {
                         "qty": 50.0, "price": 104.0,
                         "realized_pnl_usdt": 200.0,
                         "realized_pnl_pct": 4.0,
                         "reason": "TP1", "timestamp": 2.0}}},
                    {"decision": "TP1_EXECUTED", "ts": 3.0,
                     "metadata": {"tp1_exec_price": 104.0,
                                  "tp1_fill_qty": 50.0}},
                ]

        coord = TradeExecutionCoordinator()  # no engine -> no live recompute
        trades = coord.recover_from_exchange(
            positions=[{"symbol": "BTC/USDT", "side": "LONG",
                        "contracts": 50.0, "entryPrice": 100.0,
                        "markPrice": 104.0}],
            trade_journal=_FakeJournal(),
        )
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertAlmostEqual(float(t.remaining_qty), 50.0, places=6)
        self.assertAlmostEqual(float(t.original_qty), 100.0, places=6,
                               msg="initial = venue remaining + partial legs")
        self.assertEqual(t.tp1_state, "EXECUTED")
        self.assertAlmostEqual(float(t.tp1_fill_qty), 50.0, places=6)
        self.assertEqual(len(t.partial_legs), 1)
        self.assertAlmostEqual(float(t.tp1_target_qty), 50.0, places=6)
        self.assertAlmostEqual(float(t.runner_qty), 50.0, places=6)
        self.assertTrue(t.runner_active)

    # H ----------------------------------------------------------------------
    def test_coordinator_partial_mirrors_engine_single_authority(self):
        coord = TradeExecutionCoordinator(E)
        candidate = {
            "symbol": "BTC/USDT", "side": "BUY", "price": 100.0,
            "sl": 98.0, "tp1": 108.0, "tp2": 120.0,
            "score": 88.0, "atr": 1.0, "asset_class": "CRYPTO",
            "trade_type": "SCALP",
        }

        def fake_execute(side, symbol, *args, **kwargs):
            return True  # engine STATE already seeded

        trade = coord.open_trade(candidate, lambda s, c: True, fake_execute)
        self.assertIsNotNone(trade)
        self.assertAlmostEqual(float(trade.original_qty), 100.0, places=6)
        self.assertAlmostEqual(float(trade.remaining_qty), 100.0, places=6)
        # first partial: engine IS the authority, the mirror follows it
        self.assertTrue(coord.partial_close_trade(
            trade.trade_id, 0.5, ExitReason.TP1, engine_partial=E.close_partial))
        self.assertAlmostEqual(float(trade.remaining_qty), 50.0, places=6)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertEqual(trade.tp1_state, "EXECUTED")
        self.assertAlmostEqual(float(trade.tp1_fill_qty), 50.0, places=6)
        self.assertEqual(len(trade.partial_legs), 1)
        # second partial is blocked by the same single authority
        self.assertFalse(coord.partial_close_trade(
            trade.trade_id, 0.5, ExitReason.TP1, engine_partial=E.close_partial))
        self.assertAlmostEqual(float(trade.remaining_qty), 50.0, places=6)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertEqual(len(trade.partial_legs), 1)

    # I ----------------------------------------------------------------------
    def test_paper_ledger_tracks_post_close_size_and_loss_never_over_closes(self):
        # after TP1 the PAPER venue must carry the SHRUNK size (no phantom
        # runner that "grows back" after a restart double-exit).
        self.assertTrue(E.close_partial(0.5))
        self.assertAlmostEqual(float(E.paper["position"]["qty"]), 50.0, places=6)
        self.assertAlmostEqual(float(E.paper["position"]["remaining_qty"]), 50.0,
                               places=6)
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        # loss-mark partial still books exactly min(initial*0.5, remaining)
        self._seed("BUY", mark=90.0)
        self.tick.base = 90.0
        self.assertTrue(E.close_partial(0.5))
        self.assertAlmostEqual(float(E.STATE["remaining_qty"]), 50.0, places=6)
        self.assertGreaterEqual(float(E.STATE["remaining_qty"]), 0.0)
        # full close clears the paper venue entirely
        self.assertTrue(E.close_position_full())
        self.assertIsNone(E.paper["position"])

    # J ----------------------------------------------------------------------
    def test_full_pipeline_aggregates_exactly_once(self):
        # Trade 1: WIN — TP1 50@104 then runner 50@108.
        self.assertTrue(E.close_partial(0.5))
        self.tick.base = 108.0
        E.STATE["mark_price"] = 108.0
        self.assertTrue(E.close_position_full())
        self.assertEqual(E.PERF["trades"], 1)
        self.assertAlmostEqual(float(E.paper["balance"]), 1610.0, places=6)
        # Trade 2: LOSS — full close at 90 on the SAME symbol. Keep the
        # accumulated PERF ledger from trade 1 (the pipeline aggregates).
        held = E.PERF
        self._seed("BUY", mark=90.0, balance=1610.0)
        E.PERF = held
        self.tick.base = 90.0
        self.assertTrue(E.close_position_full())
        self.assertEqual(E.PERF["trades"], 2)
        self.assertEqual(E.PERF["wins"], 1)
        self.assertEqual(E.PERF["losses"], 1)
        self.assertAlmostEqual(E.PERF["total_pnl_usdt"], -400.0, places=6)
        self.assertAlmostEqual(E.PERF["total_pnl_pct"], -4.0, places=6)
        self.assertAlmostEqual(float(E.paper["balance"]), 620.0, places=6)
        self.assertAlmostEqual(float(E.paper["committed_margin"]), 0.0, places=6)
        ledger = E.PERF["symbols"]["BTC/USDT"]
        self.assertAlmostEqual(float(ledger["realized_usdt"]), -400.0, places=6)
        self.assertAlmostEqual(float(ledger["realized_pct"]), -4.0, places=6)
        self.assertEqual(ledger["trades"], 2)


if __name__ == "__main__":
    unittest.main()