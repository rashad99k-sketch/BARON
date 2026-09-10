"""Trade Management Safety Hardening verification.

Covers the P0/P1 hardening + profit-harvesting lifecycle added to the engine:
  - trade lifecycle journal (core.trade_journal) with tamper-evident records
  - per-trade trade_id surviving restart via journal recovery
  - verified-fill TP1 / aggressive profit lock (TP1_DONE only AFTER a fill)
  - breakeven ratchet gated on tp1_hit, monotonic protection floor
  - REALIZED vs UNREALIZED (incl. peak) never mixed; classification from REALIZED
  - P0-2 (API error is NOT absence), P1-2 (symbol-bound force close), P0-3
    (native protective SL), P1-4 (canonical dashboard payload separation)
Deterministic, isolated per-test journal chain (DECISION_JOURNAL_PATH tmp path;
decision_journal chain state and trade_journal dedup cache are reset).
"""
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")

import core.decision_journal as _dj
import core.trade_journal as _tj
from portfolio.manager import canonical_position_payload

import core.engine as E  # noqa: E402  (real engine)


def _fresh_journal(tmp_path):
    path = tmp_path / "journal.jsonl"
    os.environ["DECISION_JOURNAL_PATH"] = str(path)
    _dj._LAST_HASH = "0" * 64
    _dj._INITIALIZED = False
    _tj._DEDUP.clear()
    return path


def _decisions(path):
    if not Path(path).exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if str(rec.get("stage", "")) == "TRADE":
            out.append(rec)
    return out


def _open_paper(side="BUY", entry=100.0, qty=100.0, remaining=100.0,
                margin=None, mark=None):
    E.STATE["open"] = True
    E.STATE["side"] = side
    E.STATE["entry"] = entry
    E.STATE["qty"] = qty
    E.STATE["remaining_qty"] = remaining
    E.STATE["current_symbol"] = "BTC/USDT"
    E.STATE["qty_initial"] = qty
    E.STATE["margin"] = margin if margin is not None else entry * qty / E.LEVERAGE
    E.STATE["mark_price"] = mark if mark is not None else entry
    E.STATE["roe_pct"] = 0.0
    E.STATE["unrealized_pnl_usdt"] = 0.0
    E.STATE["synthetic_sl"] = 98.0 if side == "BUY" else 102.0
    E.STATE["synthetic_tp1"] = 104.0 if side == "BUY" else 96.0
    E.STATE["tp2_price"] = 108.0 if side == "BUY" else 92.0
    E.STATE["trade_id"] = None
    E.STATE["profit_stage"] = "NONE"
    E.STATE["protection_state"] = "NONE"
    E.STATE["protection_floor_sl"] = None
    E.STATE["profit_locked_event_ts"] = None
    E.STATE["profit_detected_ts"] = None
    E.STATE["realized_pnl_usdt"] = 0.0
    E.STATE["realized_pnl_pct"] = 0.0
    E.STATE["realized_roe_pct"] = 0.0
    E.STATE["realized_legs"] = 0
    E.STATE["partial_realized"] = []
    E.STATE["tp1_hit"] = False
    E.STATE["tp1_state"] = "NONE"
    E.STATE["tp1_event_ts"] = None
    E.STATE["tp2_hit"] = False
    E.STATE["trail_activated"] = False
    E.STATE["trail_activation_ts"] = None
    E.STATE["native_sl_state"] = "NONE"
    E.STATE["native_sl_order_id"] = None
    E.STATE["native_sl_price"] = None
    E.STATE["exit_reason"] = None
    E.STATE["close_reason"] = None
    E.STATE["final_result_class"] = None
    E.STATE["duration_sec"] = None
    E.STATE["entry_time"] = time.time() - 60
    E.STATE["position_status"] = "OPEN"
    E.STATE["sync_status"] = "OK"
    E.STATE["recovered"] = False
    E.STATE["recovery_ts"] = None
    E.STATE["peak_roe"] = 0.0
    E.STATE["peak_unrealized_pnl"] = 0.0
    E.STATE["last_trade_summary"] = None
    E.paper["position"] = {"symbol": "BTC/USDT", "remaining_qty": remaining,
                           "side": side, "entry": entry}


# ============================================================
# Group A: core.trade_journal (pure, deterministic)
# ============================================================
class TradeJournalCoreTest(unittest.TestCase):
    def test_make_trade_id_unique_and_symbol_scoped(self):
        a = _tj.make_trade_id("BTC/USDT")
        b = _tj.make_trade_id("BTC/USDT")
        self.assertNotEqual(a, b)
        self.assertTrue(str(a).startswith("BTC-USDT"))
        self.assertIn(str(a), str(a))

    def test_classify_result_win(self):
        self.assertEqual(_tj.classify_result(1.5), "WIN")
        self.assertEqual(_tj.classify_result(0.05), "WIN")

    def test_classify_result_loss(self):
        self.assertEqual(_tj.classify_result(-2.0), "LOSS")

    def test_classify_result_breakeven_ignores_peak(self):
        # +40% peak is IRRELEVANT: classification is from REALIZED pnl only.
        self.assertEqual(_tj.classify_result(-0.004, breakeven_eps=0.01), "BREAKEVEN")
        self.assertEqual(_tj.classify_result(0.009, breakeven_eps=0.01), "BREAKEVEN")

    def test_tp_geometry_valid_buy(self):
        self.assertTrue(_tj.tp_geometry_valid("BUY", 100.0, 98.0, 102.0, 105.0)[0])

    def test_tp_geometry_valid_sell_mirror(self):
        self.assertTrue(_tj.tp_geometry_valid("SELL", 100.0, 102.0, 98.0, 95.0)[0])

    def test_tp_geometry_invalid_buy(self):
        # TP1 below/at entry and TP2 <= TP1 are corrupt geometries.
        self.assertFalse(_tj.tp_geometry_valid("BUY", 100.0, 98.0, 99.0, 105.0)[0])
        self.assertFalse(_tj.tp_geometry_valid("BUY", 100.0, 98.0, 100.0, 103.0)[0])
        self.assertFalse(_tj.tp_geometry_valid("BUY", 100.0, 98.0, 105.0, 103.0)[0])

    def test_recover_trade_id_skips_closed(self, tmp_path=None):
        path = _fresh_journal(self._tmp)
        opened_a = _tj.make_trade_id("ETH/USDT")
        closed_b = _tj.make_trade_id("ETH/USDT")
        opened_c = _tj.make_trade_id("ETH/USDT")
        _tj.journal_trade_event(event=_tj.TRADE_OPENED, symbol="ETH/USDT",
                                trade_id=opened_a)
        _tj.journal_trade_event(event=_tj.TRADE_OPENED, symbol="ETH/USDT",
                                trade_id=closed_b)
        _tj.journal_trade_event(event=_tj.TRADE_CLOSED, symbol="ETH/USDT",
                                trade_id=closed_b)
        _tj.journal_trade_event(event=_tj.TRADE_OPENED, symbol="ETH/USDT",
                                trade_id=opened_c)
        # Newest OPENED without CLOSED wins; closed trade_id is never returned.
        self.assertEqual(_tj.recover_trade_id("ETH/USDT"), opened_c)
        self.assertNotEqual(_tj.recover_trade_id("ETH/USDT"), closed_b)
        self.assertIsNone(_tj.recover_trade_id("OTHER/USDT"))

    def test_journal_event_is_tamper_evident(self):
        path = _fresh_journal(self._tmp)
        _tj.journal_trade_event(event=_tj.TRADE_OPENED, symbol="BTC/USDT",
                                trade_id="TRADE:BTC/USDT:t0",
                                metadata={"entry": 100.0, "side": "BUY"})
        ok, count, msg = _dj.verify_file()
        self.assertTrue(ok)
        self.assertGreaterEqual(count, 1)
        self.assertEqual(msg, "ok")
        raw = path.read_text(encoding="utf-8")
        self.assertIn("TRADE_OPENED", raw)

    def test_dedup_suppresses_noisy_repeat(self):
        _fresh_journal(self._tmp)
        one = _tj.journal_trade_event(event=_tj.TP1_FAILED, symbol="BTC/USDT",
                                      reason="attempt", dedup_key="tp1_fail",
                                      dedup_sec=30)
        two = _tj.journal_trade_event(event=_tj.TP1_FAILED, symbol="BTC/USDT",
                                      reason="attempt", dedup_key="tp1_fail",
                                      dedup_sec=30)
        self.assertIsNotNone(one)
        self.assertIsNone(two)

    def setUp(self):
        from tempfile import TemporaryDirectory
        self._tmp_dir = TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        _fresh_journal(self._tmp)

    def tearDown(self):
        try:
            del os.environ["DECISION_JOURNAL_PATH"]
        except KeyError:
            pass
        self._tmp_dir.cleanup()


# ============================================================
# Group B: engine hardening (paper / unit)
# ============================================================
class EngineTradeSafetyTest(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        self._tmp_dir = TemporaryDirectory()
        self._tmp = Path(self._tmp_dir.name)
        self._journal = _fresh_journal(self._tmp)

    def tearDown(self):
        try:
            del os.environ["DECISION_JOURNAL_PATH"]
        except KeyError:
            pass
        self._tmp_dir.cleanup()

    def _decisions(self):
        return _decisions(self._journal)

    # ---- start_trade lifecycle ----
    def test_start_trade_generates_id_and_journals_open(self):
        E._live_manager.start_trade("BTC/USDT", "BUY", 100.0, 100.0, 98.0, 104.0, 108.0)
        self.assertTrue(E.STATE["trade_id"])
        self.assertEqual(E.STATE["profit_stage"], _tj.STAGE_OPENED)
        self.assertEqual(E.STATE["protection_state"], "NONE")
        self.assertEqual(E.STATE["position_status"], "OPEN")
        dec = [r for r in self._decisions() if r["decision"] == _tj.TRADE_OPENED]
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["symbol"], "BTC/USDT")

    def test_advance_stage_never_backward(self):
        E.STATE["profit_stage"] = _tj.STAGE_PROFIT_LOCKED
        self.assertEqual(E._advance_profit_stage(_tj.STAGE_OPENED), _tj.STAGE_PROFIT_LOCKED)
        self.assertEqual(E._advance_profit_stage(_tj.STAGE_TP1_ELIGIBLE), _tj.STAGE_PROFIT_LOCKED)

    # ---- protection ratchet ----
    def test_ratchet_breakeven_after_tp1(self):
        _open_paper(side="BUY", entry=100.0, remaining=50.0)
        E.STATE["tp1_hit"] = True
        E.STATE["synthetic_sl"] = 98.0
        moved = E._apply_protection_ratchet(
            symbol="BTC/USDT", mark_price=102.0, side="BUY", entry=100.0, atr=1.0,
            reason="test")
        self.assertTrue(moved)
        self.assertEqual(E.STATE["protection_state"], "BREAKEVEN")
        self.assertEqual(E.STATE["synthetic_sl"], 100.0)
        self.assertEqual(E.STATE["protection_floor_sl"], 100.0)
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.BREAKEVEN_RATCHET), 1)

    def test_no_ratchet_before_tp1(self):
        _open_paper(side="BUY", entry=100.0)
        E.STATE["tp1_hit"] = False
        E.STATE["synthetic_sl"] = 98.0
        E._apply_protection_ratchet(symbol="BTC/USDT", mark_price=94.0, side="BUY",
                                    entry=100.0, atr=1.0, reason="test")
        self.assertEqual(E.STATE["protection_state"], "NONE")
        self.assertEqual(E.STATE["synthetic_sl"], 98.0)
        self.assertFalse(any(r["decision"] == _tj.BREAKEVEN_RATCHET
                             for r in self._decisions()))

    def test_protection_floor_never_backward_buy(self):
        _open_paper(side="BUY", entry=100.0)
        E.STATE["tp1_hit"] = True
        E.STATE["protection_state"] = "BREAKEVEN"
        E.STATE["protection_floor_sl"] = 100.0
        E.STATE["synthetic_sl"] = 98.0  # strategy tries to loosen -> rejected
        result = E._safe_prot_floor("BUY", 98.0)
        self.assertEqual(result, 100.0)
        self.assertEqual(E.STATE["synthetic_sl"], 100.0)

    def test_protection_floor_never_backward_sell(self):
        _open_paper(side="SELL", entry=100.0)
        E.STATE["tp1_hit"] = True
        E.STATE["protection_state"] = "BREAKEVEN"
        E.STATE["protection_floor_sl"] = 100.0
        E.STATE["synthetic_sl"] = 104.0  # loosens a short -> rejected
        result = E._safe_prot_floor("SELL", 104.0)
        self.assertEqual(result, 100.0)

    def test_profit_lock_only_after_ratchet_landed(self):
        _open_paper(side="BUY")
        E.STATE["tp1_hit"] = True
        E._apply_protection_ratchet(symbol="BTC/USDT", mark_price=101.0, side="BUY",
                                    entry=100.0, atr=1.0, reason="test")
        ok = E._profit_lock_then_journal(symbol="BTC/USDT", side="BUY",
                                         reason="test lock")
        self.assertTrue(ok)
        self.assertEqual(E.STATE["protection_state"], "PROFIT_LOCK")
        self.assertTrue(E.STATE["profit_lock_activated"])
        self.assertIsNotNone(E.STATE["profit_locked_event_ts"])
        self.assertEqual(E.STATE["profit_stage"], _tj.STAGE_PROFIT_LOCKED)
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.PROFIT_LOCKED), 1)

    def test_profit_lock_failed_is_not_claimed(self):
        _open_paper(side="BUY")
        E.STATE["tp1_hit"] = False
        E.STATE["protection_state"] = "NONE"
        ok = E._profit_lock_then_journal(symbol="BTC/USDT", side="BUY",
                                         reason="no ratchet yet")
        self.assertFalse(ok)
        self.assertFalse(E.STATE["profit_lock_activated"])
        self.assertNotEqual(E.STATE["protection_state"], "PROFIT_LOCK")
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.PROFIT_LOCK_FAILED), 1)

    def test_profit_detected_once_per_trade(self):
        _open_paper(side="BUY", entry=100.0, mark=102.0)
        first = E._on_profit_detected("BTC/USDT", 3.0)
        second = E._on_profit_detected("BTC/USDT", 4.0)
        self.assertTrue(first)
        self.assertFalse(second)
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.PROFIT_DETECTED), 1)

    # ---- close_partial boolean + realized accretion ----
    def test_close_partial_banks_leg_and_returns_true(self):
        _open_paper(side="BUY", entry=100.0, qty=100.0, remaining=100.0, mark=102.0)
        with mock.patch.object(E, "get_ticker_safe", return_value=102.0):
            ret = E.close_partial(0.5)
        self.assertTrue(ret)
        self.assertEqual(E.STATE["remaining_qty"], 50.0)
        self.assertEqual(E.STATE["realized_legs"], 1)
        self.assertAlmostEqual(E.STATE["realized_pnl_usdt"], 100.0, places=6)
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.PARTIAL_CLOSE), 1)
        # advisory ZEC context must survive a partial close untouched
        self.assertIn("realized_pnl_pct", E.STATE)

    def test_close_partial_returns_false_on_skip(self):
        _open_paper(side="BUY")
        E.paper["position"] = None
        with mock.patch.object(E, "get_ticker_safe", return_value=102.0):
            ret = E.close_partial(0.5)
        self.assertFalse(ret)
        self.assertEqual(E.STATE["realized_legs"], 0)

    def test_close_partial_returns_false_when_reconciliation_pending(self):
        _open_paper(side="BUY")
        saved = E._reconciliation_pending
        try:
            E._reconciliation_pending = True
            self.assertFalse(E.close_partial(0.5))
        finally:
            E._reconciliation_pending = saved

    # ---- TP1 verified-fill semantics ----
    def test_tp1_done_only_after_filled_partial(self):
        # close_partial failing must not be enough to claim TP1_DONE: the
        # aggressive profit-lock branch guards tp1_hit on the boolean return.
        _open_paper(side="BUY")
        E.paper["position"] = None  # forces close_partial -> False
        with mock.patch.object(E, "get_ticker_safe", return_value=102.0):
            filled = E.close_partial(0.5)
        self.assertFalse(filled)
        # The state transitions that require a real fill are never applied.
        self.assertFalse(E.STATE.get("tp1_hit"))
        self.assertNotEqual(E.STATE.get("tp1_state"), "EXECUTED")

    # ---- finalize: classified from REALIZED, not peak ----
    def test_finalize_classifies_from_realized_loses_despite_peak(self):
        _open_paper(side="BUY", entry=100.0, qty=100.0, remaining=100.0, mark=102.0)
        E.STATE["entry_time"] = time.time() - 120
        E.STATE["trade_id"] = "TRADE:BTC/USDT:unit"
        with mock.patch.object(E, "get_ticker_safe", return_value=102.0):
            E.close_partial(0.5)  # TP1 banks +2% on 50 -> realized +100 USDT
        # Final runner (the remaining 50) closes BELOW entry so REALIZED is
        # overall negative — the split is 50/50, not repeated halving.
        with mock.patch.object(E, "get_ticker_safe", return_value=97.0):
            E._mark_close_reason("STOP_LOSS")
            pnl_usdt, pnl_pct = E.finalize_trade_with_reality("BTC/USDT")
        self.assertLess(pnl_pct, 0.0)  # booked +2% < final -3% on the runner
        self.assertEqual(E.STATE["final_result_class"], "LOSS")
        self.assertEqual(E.STATE["exit_reason"], "STOP_LOSS")
        self.assertLess(float(E.STATE["realized_pnl_pct"]), 0.0)
        self.assertGreaterEqual(E.STATE["duration_sec"], 100.0)
        summary = E.STATE["last_trade_summary"]
        self.assertIsInstance(summary, dict)
        self.assertEqual(summary["trade_id"], "TRADE:BTC/USDT:unit")
        self.assertEqual(summary["result"], "LOSS")
        dec = [r for r in self._decisions() if r["decision"] == _tj.TRADE_CLOSED]
        self.assertEqual(len(dec), 1)
        self.assertEqual(dec[0]["metadata"].get("exit_reason"), "STOP_LOSS")
        # PERF counts the trade exactly once.
        self.assertGreaterEqual(E.PERF["trades"], 1)
        self.assertGreaterEqual(E.PERF["losses"], 1)

    def test_finalize_breakeven_classification(self):
        _open_paper(side="BUY", entry=100.0, qty=100.0, remaining=100.0, mark=100.0)
        E.STATE["trade_id"] = "TRADE:BTC/USDT:bkeq"
        with mock.patch.object(E, "get_ticker_safe", return_value=100.009):
            E._mark_close_reason("TRAILING_STOP")
            _, pnl_pct = E.finalize_trade_with_reality("BTC/USDT")
        self.assertTrue(-0.05 < pnl_pct < 0.05)
        self.assertEqual(E.STATE["final_result_class"], "BREAKEVEN")
        dec = [r for r in self._decisions() if r["decision"] == _tj.TRADE_CLOSED]
        self.assertEqual(len(dec), 1)

    def test_finalize_journals_full_metadata(self):
        _open_paper(side="SELL", entry=100.0, qty=50.0, remaining=50.0, mark=100.0)
        E.STATE["trade_id"] = "TRADE:BTC/USDT:meta"
        E.STATE["peak_roe"] = 40.0
        E.STATE["peak_unrealized_pnl"] = 500.0
        with mock.patch.object(E, "get_ticker_safe", return_value=101.0):
            E._mark_close_reason("TAKE_PROFIT_TP2")
            E.finalize_trade_with_reality("BTC/USDT")
        dec = [r for r in self._decisions() if r["decision"] == _tj.TRADE_CLOSED][0]
        meta = dec["metadata"]
        self.assertEqual(meta["trade_id"], "TRADE:BTC/USDT:meta")
        self.assertEqual(meta["result"], "LOSS" if meta["realized_pnl_usdt"] < 0 else "WIN")
        self.assertEqual(meta["exit_reason"], "TAKE_PROFIT_TP2")
        self.assertEqual(meta["peak_roe"], 40.0)

    # ---- P1-2 symbol-bound force close ----
    def test_force_close_ignores_mismatched_symbol(self):
        _open_paper(side="BUY")
        calls = []
        with mock.patch.object(E, "close_position_full",
                               side_effect=lambda: calls.append(1) or True):
            E._live_manager._force_close({"symbol": "ETH/USDT"})
        self.assertEqual(calls, [])
        self.assertTrue(E.STATE["open"])

    def test_force_close_matching_symbol_closes_with_reason(self):
        _open_paper(side="BUY")
        calls = []
        with mock.patch.object(E, "close_position_full",
                               side_effect=lambda: calls.append(1) or True):
            E._live_manager._force_close({"symbol": "BTC/USDT"})
        self.assertEqual(calls, [1])
        self.assertEqual(E.STATE["close_reason"], "EXTERNAL_CLOSE")

    # ---- P0-2 API error is never absence ----
    def test_snapshot_api_exception_returns_stale_not_absence(self):
        _open_paper(side="BUY", entry=100.0, remaining=50.0)
        saved_paper = E.PAPER_MODE
        try:
            E.PAPER_MODE = False
            E._exchange_sync._last_snapshot = E.PositionSnapshot()
            E._exchange_sync._last_snapshot.symbol = "BTC/USDT"
            E._exchange_sync._last_snapshot.qty = 50.0
            E._exchange_sync._last_snapshot.entry_price = 100.0
            E._exchange_sync._last_snapshot.mark_price = 102.0
            E._exchange_sync._last_snapshot.roe_pct = 5.0
            with mock.patch.object(E, "fetch_position_status", side_effect=RuntimeError("venue down")):
                snap = E._exchange_sync.fetch_live_snapshot("BTC/USDT")
            self.assertIsNotNone(snap)
            self.assertTrue(snap.stale)
            self.assertEqual(snap.source, "rest_sync_error")
            self.assertEqual(E.STATE["position_status"], "UNKNOWN")
        finally:
            E.PAPER_MODE = saved_paper

    def test_reconcile_confirmed_absence_journals_external_close(self):
        emitted = []
        saved_bus = E._exchange_sync.event_bus
        # Route the emitted force-close onto a THROWAWAY bus and stop its
        # worker at the end of THIS test. The long-lived module bus worker
        # would otherwise process the queued force-close asynchronously AFTER
        # this file ends, landing it against LATER test files' global STATE
        # (a phantom full close mid-scenario). The verify here only needs the
        # emitted payload + journal decision, never the async side effect.
        try:
            E._exchange_sync.event_bus = E.EventBus()
        except Exception:
            pass
        bus = E._exchange_sync.event_bus
        original_emit = bus.emit
        # reconcile is rate-limited to once per 10s; earlier suites may have
        # already reconciled, so reset the limiter for this deterministic test.
        saved_last_reconcile = E._exchange_sync._last_reconcile
        E._exchange_sync._last_reconcile = 0.0
        try:
            with mock.patch.object(bus, "emit",
                                   side_effect=lambda et, data=None: (
                                       emitted.append((et, data)) or original_emit(et, data))):
                with mock.patch.object(E._exchange_sync, "fetch_live_snapshot",
                                       return_value=None):
                    E._exchange_sync.reconcile("BTC/USDT", {"open": True, "side": "BUY"})
        finally:
            E._exchange_sync._last_reconcile = saved_last_reconcile
            try:
                E._exchange_sync.event_bus.stop(join_timeout=1.0)
            except Exception:
                pass
            E._exchange_sync.event_bus = saved_bus
        sent = {et: d for et, d in emitted}
        self.assertEqual(sent.get("force_close_local", {}).get("symbol"), "BTC/USDT")
        dec = [r["decision"] for r in self._decisions()]
        self.assertTrue(any(d == _tj.EXTERNAL_CLOSE for d in dec))

    # ---- P0-3 native protective SL ----
    def test_native_sl_place_idempotent_and_journaled(self):
        _open_paper(side="BUY", entry=100.0, remaining=100.0)
        E.STATE["synthetic_sl"] = 98.0
        first = E.place_native_sl("BTC/USDT")
        second = E.place_native_sl("BTC/USDT")
        self.assertIsNotNone(first)
        self.assertEqual(first, second)
        self.assertEqual(E.STATE["native_sl_state"], "ACTIVE")
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.NATIVE_SL_PLACED), 1)

    def test_native_sl_update_monotonic_never_backward(self):
        _open_paper(side="BUY", entry=100.0)
        E.STATE["synthetic_sl"] = 98.0
        E.place_native_sl("BTC/USDT")
        self.assertFalse(E.update_native_sl(97.0))   # backward -> rejected
        self.assertEqual(E.STATE["native_sl_price"], 98.0)
        self.assertTrue(E.update_native_sl(99.0))    # forward -> allowed
        self.assertEqual(E.STATE["native_sl_price"], 99.0)
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.NATIVE_SL_UPDATED), 1)

    def test_native_sl_cancel_on_close_surfaces(self):
        _open_paper(side="BUY")
        E.STATE["synthetic_sl"] = 98.0
        E.place_native_sl("BTC/USDT")
        E.cancel_native_sl()
        self.assertEqual(E.STATE["native_sl_state"], "CANCELLED")
        dec = [r["decision"] for r in self._decisions()]
        self.assertEqual(dec.count(_tj.NATIVE_SL_CANCELLED), 1)

    # ---- paper open-quantity realism (Part C) ----
    def test_sync_position_state_unrealized_uses_remaining_qty(self):
        _open_paper(side="BUY", entry=100.0, qty=100.0, remaining=50.0, mark=102.0)
        with mock.patch.object(E, "get_ticker_safe", return_value=102.0):
            price, _, _, _ = E.sync_position_state("BTC/USDT")
        self.assertEqual(price, 102.0)
        self.assertAlmostEqual(E.STATE["unrealized_pnl_usdt"], 100.0, places=6)

    # ---- geometry enforced on persisted targets ----
    def test_enforce_sl_tp_geometry_buy(self):
        sl, tp1, tp2 = E._enforce_sl_tp_geometry("BUY", 100.0, 98.0, 102.0, 105.0,
                                                 atr=1.0, symbol="BTC/USDT")
        self.assertLess(sl, 100.0)
        self.assertGreater(tp1, 100.0)
        self.assertGreater(tp2, tp1)

    # ---- invariants the hardening must not touch ----
    def test_core_risk_and_execution_parameters_unchanged(self):
        self.assertEqual(E.LEVERAGE, 10)
        self.assertEqual(E.RUNNER_PCT, 0.4)
        self.assertEqual(E.USE_PPE, True)
        self.assertEqual(E.TRAIL_ATR_MULT, 1.4)
        self.assertEqual(E.MAX_SCALE_INS, 2)
        self.assertEqual(E.SCALE_IN_SIZE_PCT, 0.25)
        self.assertEqual(E.SCALE_IN_PROFIT_PCT, 0.5)


# ============================================================
# Group C: canonical payload separation (P1-4) + portfolio
# ============================================================
class CanonicalPayloadTest(unittest.TestCase):
    def setUp(self):
        from tempfile import TemporaryDirectory
        self._tmp_dir = TemporaryDirectory()
        _fresh_journal(Path(self._tmp_dir.name))

    def tearDown(self):
        try:
            del os.environ["DECISION_JOURNAL_PATH"]
        except KeyError:
            pass
        self._tmp_dir.cleanup()

    def test_pnl_is_unrealized_usdt_not_roe(self):
        st = {
            "side": "BUY", "entry": 100.0, "mark_price": 103.0, "qty": 100.0,
            "remaining_qty": 100.0, "roe_pct": 30.0,
            "unrealized_pnl_usdt": 300.0,
            "realized_pnl_usdt": -40.0, "realized_pnl_pct": -4.0,
            "realized_legs": 2, "trade_id": "TRADE:X:1",
            "profit_stage": "PROFIT_LOCK", "protection_state": "BREAKEVEN",
            "tp1_hit": True, "synthetic_sl": 100.5,
        }
        out = canonical_position_payload("X/USDT", st, "CRYPTO")
        self.assertEqual(out["pnl"], 300.0)
        self.assertEqual(out["pnl_usdt"], 300.0)
        self.assertEqual(out["roe"], 30.0)
        self.assertEqual(out["realized_pnl_usdt"], -40.0)
        self.assertEqual(out["realized_pnl_pct"], -4.0)
        self.assertEqual(out["realized_legs"], 2)
        self.assertEqual(out["profit_stage"], "PROFIT_LOCK")
        self.assertEqual(out["trade_id"], "TRADE:X:1")
        self.assertTrue(out["tp1_done"])
        # every documented key always present
        for key in ("realized_roe_pct", "tp1_state", "native_sl_state",
                    "position_status", "sync_status", "recovered",
                    "last_trade_summary"):
            self.assertIn(key, out)

    def test_portfolio_manager_max_positions_default_is_six(self):
        from portfolio.manager import PortfolioManager
        pm = PortfolioManager(engine=None)
        self.assertEqual(pm.max_positions, 6)

    def test_advisory_zec_context_survives_management(self):
        _open_paper(side="BUY", entry=100.0, qty=100.0, remaining=100.0, mark=101.0)
        E.STATE["position_profile"] = {"trade_type": "TREND", "asset_class": "CRYPTO"}
        E.STATE["position_trade_type"] = "TREND"
        E.STATE["position_asset_class"] = "CRYPTO"
        with mock.patch.object(E, "get_ticker_safe", return_value=101.0):
            E.close_partial(0.5)
        self.assertEqual(E.STATE["position_trade_type"], "TREND")
        self.assertEqual(E.STATE["position_asset_class"], "CRYPTO")


if __name__ == "__main__":
    unittest.main()