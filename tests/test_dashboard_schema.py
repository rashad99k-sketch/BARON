"""P1 dashboard canonical-schema tests (deterministic, no network).

Proves through real production code paths that:
  1. The canonical position payload always carries every documented key and
     never emits the literal sentinels 'undefined'/'N/A'.
  2. publish_position_state publishes exactly that canonical payload.
  3. PortfolioManager.canonical_position_payload / snapshot() produce the same
     contract for multi-position contexts.
  4. The dashboard /data normalization deep-cleans sentinel strings and NaN so
     the browser never receives undefined literals.
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
    saved_engine = sys.modules.get("core.engine")
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
    try:
        # Re-execute the engine under a private name so `core.engine` keeps its
        # canonical identity (bound by portfolio.manager and the brain harnesses)
        # for the rest of the suite.
        if saved_engine is not None:
            spec = importlib.util.spec_from_file_location("_schema_fresh_engine", saved_engine.__file__)
            engine = importlib.util.module_from_spec(spec)
            sys.modules["_schema_fresh_engine"] = engine
            try:
                spec.loader.exec_module(engine)
            finally:
                sys.modules.pop("_schema_fresh_engine", None)
        else:
            engine = importlib.import_module("core.engine")
        return engine
    finally:
        os.environ.clear()
        os.environ.update(env)
        for name, module in (("ccxt", saved_ccxt), ("flask", saved_flask),
                             ("core.engine", saved_engine)):
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


CANONICAL_KEYS = {
    "symbol", "side", "entry", "current_price", "pnl", "roe", "sl", "tp1", "tp2",
    "tp1_done", "trailing_active", "trail_stop", "location", "zone", "zone_behaviour",
    "narrative", "narrative_confidence", "confidence", "confidence_level", "regime",
    "market_session", "session_label", "trade_state", "board", "state",
    "trail_multiplier", "delay_tp1", "trade_type", "entry_type", "classification",
    "score", "current_confidence", "continuation_pressure", "market_phase",
    "entry_timing", "narrative_classification", "qty", "remaining_qty", "entry_atr",
    "dynamic_tp1", "dynamic_tp2", "last_update_ts",
}


def _assert_no_undefined_sentinels(testcase, value, path=""):
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_no_undefined_sentinels(testcase, v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _assert_no_undefined_sentinels(testcase, v, f"{path}[{i}]")
    elif isinstance(value, str):
        testcase.assertNotIn(value, ("undefined", "N/A", "n/a"), f"sentinel at {path}")


class EngineCanonicalPayloadTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _load_engine()

    def _open_state(self):
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
            "mark_price": 100.5, "roe_pct": 0.5, "unrealized_pnl_usdt": 2.5,
            "trade_score": 80, "market_regime": "TREND",
            "trade_state": "HOLD", "tp1_hit": True, "trail_activated": True,
            "smart_trail_mult": 2.0, "continuation_pressure": 65,
            "last_update_ts": 1234567890.0,
        })

    def test_payload_always_has_full_canonical_key_set(self):
        e = self.engine
        self._open_state()
        payload = e._build_canonical_position_payload()
        self.assertTrue(CANONICAL_KEYS.issubset(set(payload.keys())),
                        CANONICAL_KEYS - set(payload.keys()))
        _assert_no_undefined_sentinels(self, payload)
        self.assertEqual(payload["symbol"], "BTC/USDT")
        self.assertEqual(payload["side"], "BUY")
        self.assertEqual(payload["entry"], 100.0)
        self.assertEqual(payload["sl"], 99.0)
        self.assertEqual(payload["tp1"], 100.8)
        self.assertEqual(payload["tp2"], 102.0)
        self.assertTrue(payload["tp1_done"])
        self.assertTrue(payload["trailing_active"])
        self.assertEqual(payload["trail_multiplier"], 2.0)

    def test_unknown_fields_are_none_never_sentinels(self):
        e = self.engine
        self._open_state()
        for key in ("location", "zone", "zone_behaviour", "session_label",
                    "confidence_level", "market_phase", "entry_timing",
                    "narrative_classification", "trade_type"):
            e.STATE.pop(key, None)
        payload = e._build_canonical_position_payload()
        _assert_no_undefined_sentinels(self, payload)
        for key in ("location", "zone", "session_label", "confidence_level",
                    "market_phase", "entry_timing"):
            self.assertIsNone(payload[key], f"{key} should be None when unknown")
        self.assertIsInstance(payload["score"], int)

    def test_publish_position_state_uses_canonical_payload(self):
        e = self.engine
        self._open_state()
        e.publish_position_state("BTC/USDT", "BUY", 100.0, 100.0, 0.5)
        pos = e.DASHBOARD_STATE["position"]
        self.assertIsInstance(pos, dict)
        self.assertTrue(CANONICAL_KEYS.issubset(set(pos.keys())))
        self.assertEqual(pos["symbol"], "BTC/USDT")
        _assert_no_undefined_sentinels(self, pos)
        self.assertEqual(pos["pnl"], 2.5)


class PortfolioManagerSchemaTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = _load_engine()

    def _state(self):
        return {
            "open": True, "side": "SELL", "entry": 5000.0, "qty": 10.0,
            "remaining_qty": 10.0, "mark_price": 5050.0,
            "unrealized_pnl_usdt": -25.0, "roe_pct": -1.0,
            "synthetic_sl": 5100.0, "synthetic_tp1": 4800.0, "tp2_price": 4500.0,
            "tp1_hit": False, "trail_activated": False, "market_regime": "RANGE",
            "trade_state": "HOLD", "classification": "SNIPER",
            "trade_type": "NEWS", "current_confidence": 72.0,
            "trade_score": 76, "session_label": "LONDON",
            "trade_board": {"stage": "ENTRY", "verdict": "MONITOR"},
        }

    def test_manager_canonical_payload_has_full_key_set(self):
        from portfolio.manager import canonical_position_payload
        payload = canonical_position_payload("US500/USDT", self._state(), "INDEX")
        self.assertTrue(CANONICAL_KEYS.issubset(set(payload.keys())))
        _assert_no_undefined_sentinels(self, payload)
        self.assertEqual(payload["asset_class"], "INDEX")
        self.assertEqual(payload["trade_type"], "NEWS")
        self.assertEqual(payload["session_label"], "LONDON")
        self.assertEqual(payload["confidence"], 72.0)

    def test_snapshot_entries_match_canonical_contract(self):
        from portfolio.manager import PortfolioManager, PositionContext
        pm = PortfolioManager(6, self.engine)
        pm.contexts["US500/USDT"] = PositionContext(
            symbol="US500/USDT", state=self._state(), trade_state={},
            live_manager=None, opened_at=1.0, asset_class="INDEX",
        )
        snap = pm.snapshot()
        self.assertEqual(len(snap), 1)
        self.assertTrue(CANONICAL_KEYS.issubset(set(snap[0].keys())))
        _assert_no_undefined_sentinels(self, snap)
        self.assertEqual(snap[0]["symbol"], "US500/USDT")
        self.assertEqual(snap[0]["side"], "SELL")

    def test_snapshot_accepts_symbols_with_asset_class_inference(self):
        from portfolio.manager import PortfolioManager, PositionContext
        pm = PortfolioManager(6, self.engine)
        pm.contexts["XAUUSD"] = PositionContext(
            symbol="XAUUSD", state=self._state(), trade_state={},
            live_manager=None, opened_at=1.0, asset_class="GOLD",
        )
        snap = pm.snapshot()
        self.assertEqual(snap[0]["asset_class"], "GOLD")
        _assert_no_undefined_sentinels(self, snap)


class DashboardNormalizeTest(unittest.TestCase):
    def test_normalize_payload_deep_cleans_sentinels(self):
        env = dict(os.environ)
        saved_mods = {name: sys.modules.get(name)
                      for name in ("ccxt", "flask", "core.engine", "dashboard.app")}
        os.environ["PAPER_MODE"] = "True"
        try:
            fake_ccxt = types.ModuleType("ccxt")

            class FakeBingX:
                def __init__(self, *args, **kwargs):
                    self.markets = {"BTC/USDT:USDT": {}}

            fake_ccxt.bingx = FakeBingX
            fake_flask = types.ModuleType("flask")
            fake_flask.Flask = _FakeFlask
            fake_flask.jsonify = lambda *a, **k: None
            fake_flask.request = types.SimpleNamespace()
            sys.modules["ccxt"] = fake_ccxt
            sys.modules["flask"] = fake_flask
            # core.engine intentionally NOT evicted here: a fresh re-import
            # mid-suite orphans every module that already holds `import
            # core.engine` (dashboard.app can import the canonical engine fine).
            sys.modules.pop("dashboard.app", None)
            dash = importlib.import_module("dashboard.app")
        finally:
            os.environ.clear()
            os.environ.update(env)
            for name, module in saved_mods.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        clean = dash._normalize_payload({
            "position": {"symbol": "BTC/USDT", "trade_type": "N/A",
                         "location": "undefined", "narrative": "N/A",
                         "score": 80},
            "positions": [{"symbol": "ETH/USDT", "entry_type": "n/a",
                           "entry": 2000.0}],
            "portfolio": {"open_positions": 1, "risk": {"ok": True}},
            "bad_float": float("nan"),
        })
        self.assertIsNone(clean["position"]["trade_type"])
        self.assertIsNone(clean["position"]["location"])
        self.assertIsNone(clean["position"]["narrative"])
        self.assertEqual(clean["position"]["score"], 80)
        self.assertIsNone(clean["positions"][0]["entry_type"])
        self.assertEqual(clean["positions"][0]["entry"], 2000.0)
        self.assertEqual(clean["position"]["symbol"], "BTC/USDT")


if __name__ == "__main__":
    unittest.main()