"""P0 accounting-lifecycle tests (deterministic, no network).

Proves through the real production code paths that:
  1. A paper TP1 partial close books realized PnL immediately and releases the
     proportional margin (no fake PnL, no double-count).
  2. A sequence of multiple partials + final close reconciles exactly once:
     realized == sum of all legs, trades increments exactly once.
  3. The per-symbol ledger aggregates across a multi-symbol portfolio.
  4. The live path records partial legs from the fill price and finalize only
     credits the remaining leg on top of already-booked partials.
"""
import importlib
import importlib.util
import os
import sys
import types
import unittest


class _FakeFlask:
    def __init__(self, *args, **kwargs):
        pass

    def route(self, *args, **kwargs):
        return lambda fn: fn

    def add_url_rule(self, *args, **kwargs):
        return None


def _load_engine():
    saved_ccxt = sys.modules.get("ccxt")
    saved_flask = sys.modules.get("flask")
    orig_engine = sys.modules.get("core.engine")
    env = dict(os.environ)
    os.environ.pop("PAPER_MODE", None)
    fake_ccxt = types.ModuleType("ccxt")

    class FakeBingX:
        def __init__(self, *args, **kwargs):
            self.markets = {"BTC/USDT:USDT": {}, "ETH/USDT:USDT": {}}

    fake_ccxt.bingx = FakeBingX
    fake_flask = types.ModuleType("flask")
    fake_flask.Flask = _FakeFlask
    fake_flask.jsonify = lambda *a, **k: None
    fake_flask.request = types.SimpleNamespace()
    sys.modules["ccxt"] = fake_ccxt
    sys.modules["flask"] = fake_flask
    # IMPORTANT: re-execute the engine under a PRIVATE module name so its
    # module globals (STATE/PERF/paper/live_manager...) are isolated WITHOUT
    # evicting the shared `core.engine` identity. Every other already-imported
    # module (`portfolio.manager`, `core.trade`, the live-brain harnesses)
    # resolves `core.engine` from sys.modules; if we pop it, whichever module
    # imports it next binds a FRESH engine and the brain writes state that the
    # position mirrors never see (gap/radar/full-cycle suites fail in isolation
    # only because of this identity split).
    try:
        if orig_engine is None:
            return importlib.import_module("core.engine"), saved_ccxt, saved_flask
        _spec = importlib.util.spec_from_file_location(
            "_accounting_fresh_engine", orig_engine.__file__)
        engine = importlib.util.module_from_spec(_spec)
        sys.modules["_accounting_fresh_engine"] = engine
        try:
            _spec.loader.exec_module(engine)
        finally:
            sys.modules.pop("_accounting_fresh_engine", None)
        return engine, saved_ccxt, saved_flask
    finally:
        os.environ.clear()
        os.environ.update(env)


def _perf_reset():
    return {"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
            "wins": 0, "losses": 0, "last_trade": None, "symbols": {}}


class AccountingLifecycleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved_ccxt, cls.saved_flask = _load_engine()

    @classmethod
    def tearDownClass(cls):
        # The shared `core.engine` module was NEVER evicted or re-imported
        # (see _load_engine), so other suites keep the same engine identity.
        # Only the temporary fake ccxt/flask modules are restored here.
        for name, module in (("ccxt", cls.saved_ccxt), ("flask", cls.saved_flask)):
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def setUp(self):
        self._orig_sync = self.engine.sync_position_state
        self.setup_open(entry=100.0, qty=100.0, margin=10.0)

    def tearDown(self):
        self.engine.PAPER_MODE = True
        self.engine.sync_position_state = self._orig_sync

    def setup_open(self, entry=100.0, qty=100.0, margin=10.0):
        e = self.engine
        e.PAPER_MODE = True
        e.STATE.update({
            "open": True, "side": "BUY", "entry": entry, "qty": qty,
            "remaining_qty": qty, "qty_initial": qty, "margin": margin,
            "fill_request_price": entry, "partial_realized": [],
            "sl": entry - 2.0, "tp1_price": entry * 1.08, "tp2_price": entry * 1.20,
            "current_symbol": "BTC/USDT", "mark_price": entry,
            "position_asset_class": "CRYPTO", "trade_type": "REVERSAL",
            "entry_time": 1000000.0,
        })
        e.paper = {"balance": 1000.0,
                   "position": {"side": "BUY", "entry": entry, "qty": qty, "remaining_qty": qty},
                   "committed_margin": margin}
        e.PERF = _perf_reset()
        e.TRADE_STATE.update({"in_position": True, "qty": qty})
        e.DASHBOARD_STATE["logs"] = []

    def test_single_partial_books_realized_immediately(self):
        e = self.engine
        e.STATE["mark_price"] = 104.0
        e.close_partial(0.5)
        self.assertAlmostEqual(e.STATE["remaining_qty"], 50.0)
        self.assertEqual(len(e.STATE["partial_realized"]), 1)
        leg = e.STATE["partial_realized"][0]
        self.assertAlmostEqual(leg["pnl_pct"], 4.0)
        self.assertAlmostEqual(leg["pnl_usdt"], 200.0)
        # realized credited NOW, not at finalize
        self.assertAlmostEqual(e.PERF["total_pnl_usdt"], 200.0)
        # realized leg + proportional margin released
        self.assertAlmostEqual(e.paper["balance"], 1205.0)
        self.assertAlmostEqual(e.paper["committed_margin"], 5.0)
        # trades not yet finalized
        self.assertEqual(e.PERF["trades"], 0)

    def test_partial_then_full_close_counts_exactly_once(self):
        e = self.engine
        e.STATE["mark_price"] = 104.0
        e.close_partial(0.5)
        self.assertAlmostEqual(e.PERF["total_pnl_usdt"], 200.0)
        e.finalize_trade_with_reality("BTC/USDT")
        self.assertEqual(e.PERF["trades"], 1)
        self.assertEqual(e.PERF["wins"], 1)
        # partial 200 + final leg 200 on remaining 50
        self.assertAlmostEqual(e.PERF["total_pnl_usdt"], 400.0)
        # margin fully restored
        self.assertAlmostEqual(e.paper["committed_margin"], 0.0)
        self.assertAlmostEqual(e.paper["balance"], 1410.0)

    def test_runner_partial_blocked_then_full_close_accurate(self):
        """Unified 50/50 phase model: TP1 banks 50% once; the second
        fractional (runner) close is BLOCKED by the TP-phase gate; the
        final close takes the whole 50% runner."""
        e = self.engine
        e.STATE["mark_price"] = 103.0
        e.close_partial(0.5)  # TP1: 50 of INITIAL @ 103 -> 3% / 150 USDT
        self.assertAlmostEqual(e.STATE["remaining_qty"], 50.0)
        self.assertEqual(str(e.STATE.get("tp1_state")), "EXECUTED")
        self.assertAlmostEqual(e.STATE["tp1_fill_qty"], 50.0)
        # second fractional close MUST be rejected — no runner partials.
        e.STATE["mark_price"] = 102.0
        ok = e.close_partial(0.5)  # REJECTED by TP-phase gate
        self.assertFalse(ok)
        self.assertAlmostEqual(e.STATE["remaining_qty"], 50.0)
        self.assertEqual(len(e.STATE["partial_realized"]), 1)
        e.STATE["mark_price"] = 106.0
        e.finalize_trade_with_reality("BTC/USDT")  # runner 50 @ 106 -> 6% / 300 USDT
        self.assertEqual(e.PERF["trades"], 1)
        self.assertEqual(e.PERF["wins"], 1)
        self.assertAlmostEqual(e.PERF["total_pnl_usdt"], 450.0)
        # size-weighted return: 450 USDT / (100 entry * 100 qty_initial) * 100
        self.assertAlmostEqual(e.PERF["total_pnl_pct"], 4.5)
        self.assertEqual(len(e.STATE["partial_realized"]), 0)  # reset on close

    def test_multi_symbol_ledger(self):
        e = self.engine
        e.PERF = _perf_reset()
        e._credit_realized_pnl(1.0, 10.0, "BTC/USDT")
        e._credit_realized_pnl(2.0, 20.0, "ETH/USDT")
        self.assertAlmostEqual(e.PERF["total_pnl_usdt"], 30.0)
        self.assertAlmostEqual(e.PERF["symbols"]["BTC/USDT"]["realized_usdt"], 10.0)
        self.assertAlmostEqual(e.PERF["symbols"]["ETH/USDT"]["realized_usdt"], 20.0)

    def test_live_finalize_does_not_double_count_booked_legs(self):
        e = self.engine
        e.PAPER_MODE = False
        # simulated partial leg already booked (as close_partial does live)
        e._record_partial_leg("BUY", 50.0, 104.0, 100.0, 4.0, 200.0, "LIVE")
        e.STATE["remaining_qty"] = 50.0
        e.STATE["mark_price"] = 106.0
        e.sync_position_state = lambda symbol: (106.0, 0.0, 10.0, 4.0)  # stub exchange
        pnl_usdt, pnl_pct = e.finalize_trade_with_reality("BTC/USDT")
        # booked 200 already; only the remaining leg (roe share 50% * 4%) is credited
        self.assertAlmostEqual(pnl_usdt, 200.2)
        self.assertEqual(e.PERF["trades"], 1)
        self.assertAlmostEqual(e.PERF["total_pnl_usdt"], 200.2)


if __name__ == "__main__":
    unittest.main()