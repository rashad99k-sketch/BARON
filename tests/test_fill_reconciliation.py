"""P0 fill-reconciliation tests (deterministic, no network).

Proves through the real production code paths that:
  1. The directional SL/TP geometry guard holds for BUY and SELL.
  2. A live fill that diverges from the admission price re-derives SL/TP from
     the authoritative fill entry (never persists a corrupt TP).
  3. Non-divergent fills are left untouched (no noisy reconcile).
  4. Missing OHLCV/ATR falls back to the strategy default while preserving
     the geometric invariant.
  5. Adoption of a position with zero levels still yields valid defaults.
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
    # Re-execute the engine under a PRIVATE name so the shared core.engine
    # identity (bound by portfolio.manager and the live-brain harnesses) is
    # never evicted / orphaned mid-suite.
    try:
        orig_engine = sys.modules.get("core.engine")
        if orig_engine is not None:
            spec = importlib.util.spec_from_file_location(
                "_fill_recon_fresh_engine", orig_engine.__file__)
            engine = importlib.util.module_from_spec(spec)
            sys.modules["_fill_recon_fresh_engine"] = engine
            try:
                spec.loader.exec_module(engine)
            finally:
                sys.modules.pop("_fill_recon_fresh_engine", None)
        else:
            engine = importlib.import_module("core.engine")
        return engine, saved_ccxt, saved_flask
    finally:
        os.environ.clear()
        os.environ.update(env)


class FillReconciliationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved_ccxt, cls.saved_flask = _load_engine()

    @classmethod
    def tearDownClass(cls):
        for name, module in (("ccxt", cls.saved_ccxt), ("flask", cls.saved_flask)):
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _reset_state(self):
        e = self.engine
        e.STATE.update({
            "open": True, "side": "BUY", "entry": 100.0, "qty": 100.0,
            "remaining_qty": 100.0, "qty_initial": 100.0,
            "sl": 99.0, "tp1_price": 100.8, "tp2_price": 102.0,
            "dynamic_tp1": 100.8, "dynamic_tp2": 102.0,
            "synthetic_sl": 99.0, "synthetic_tp1": 100.8, "synthetic_tp2": 102.0,
            "entry_atr": 2.0, "fill_request_price": 100.0,
            "trade_type": "REVERSAL", "classification": "REVERSAL",
            "current_symbol": "BTC/USDT", "position_asset_class": "CRYPTO",
            "market_session": None,
        })
        e.DASHBOARD_STATE["logs"] = []

    def test_buy_geometry_enforced(self):
        e = self.engine
        sl, tp1, tp2 = e._enforce_sl_tp_geometry("BUY", 100.0, 101.0, 99.0, 98.0, 2.0, "BTC/USDT")
        self.assertLess(sl, 100.0)
        self.assertGreater(tp1, 100.0)
        self.assertGreater(tp2, tp1)

    def test_sell_geometry_enforced(self):
        e = self.engine
        sl, tp1, tp2 = e._enforce_sl_tp_geometry("SELL", 100.0, 99.0, 101.0, 103.0, 2.0, "BTC/USDT")
        self.assertGreater(sl, 100.0)
        self.assertLess(tp1, 100.0)
        self.assertLess(tp2, tp1)

    def test_tp2_corrected_beyond_tp1(self):
        e = self.engine
        sl, tp1, tp2 = e._enforce_sl_tp_geometry("BUY", 100.0, 99.0, 100.5, 100.0, 2.0, "BTC/USDT")
        self.assertGreater(tp1, 100.0)
        self.assertGreater(tp2, tp1)

    def test_buy_fill_above_admission_reconciles(self):
        self._reset_state()
        e = self.engine
        ret = e._reconcile_levels_after_fill(100.6, "BTC/USDT", "BUY", "REVERSAL", "REVERSAL",
                                             2.0, force_validate=True)
        self.assertTrue(ret)
        self.assertAlmostEqual(e.STATE["entry"], 100.6)
        self.assertLess(e.STATE["sl"], e.STATE["entry"])
        self.assertGreater(e.STATE["tp1_price"], e.STATE["entry"])
        self.assertGreater(e.STATE["tp2_price"], e.STATE["tp1_price"])
        logs = "\n".join(str(x) for x in e.DASHBOARD_STATE["logs"])
        self.assertIn("FILL_RECONCILE", logs)

    def test_sell_fill_below_admission_mirror(self):
        self._reset_state()
        e = self.engine
        e.STATE["side"] = "SELL"
        ret = e._reconcile_levels_after_fill(99.4, "BTC/USDT", "SELL", "REVERSAL", "REVERSAL",
                                             2.0, force_validate=True)
        self.assertTrue(ret)
        self.assertGreater(e.STATE["sl"], e.STATE["entry"])
        self.assertLess(e.STATE["tp1_price"], e.STATE["entry"])
        self.assertLess(e.STATE["tp2_price"], e.STATE["tp1_price"])

    def test_no_divergence_is_untouched(self):
        self._reset_state()
        e = self.engine
        ret = e._reconcile_levels_after_fill(100.0, "BTC/USDT", "BUY", "REVERSAL", "REVERSAL",
                                             2.0, force_validate=False)
        self.assertFalse(ret)
        self.assertEqual(e.STATE["tp1_price"], 100.8)
        self.assertEqual(e.STATE["entry"], 100.0)

    def test_fallback_no_ohlcv_still_geometric(self):
        self._reset_state()
        e = self.engine
        e.STATE["entry_atr"] = 0.0
        e._reconcile_levels_after_fill(100.6, "BTC/USDT", "BUY", "REVERSAL", "REVERSAL",
                                       0.0, force_validate=True)
        self.assertLess(e.STATE["sl"], e.STATE["entry"])
        self.assertGreater(e.STATE["tp1_price"], e.STATE["entry"])
        self.assertGreater(e.STATE["tp2_price"], e.STATE["tp1_price"])

    def test_zero_level_adoption_populates_defaults(self):
        self._reset_state()
        e = self.engine
        for k in ("sl", "tp1_price", "tp2_price", "synthetic_sl", "synthetic_tp1",
                  "synthetic_tp2", "dynamic_tp1", "dynamic_tp2"):
            e.STATE[k] = 0.0
        ret = e._reconcile_levels_after_fill(100.0, "BTC/USDT", "BUY", "REVERSAL", "REVERSAL",
                                             None, force_validate=True)
        self.assertTrue(ret)
        self.assertGreater(e.STATE["sl"], 0.0)
        self.assertGreater(e.STATE["tp1_price"], e.STATE["entry"])
        self.assertGreater(e.STATE["tp2_price"], e.STATE["tp1_price"])


if __name__ == "__main__":
    unittest.main()