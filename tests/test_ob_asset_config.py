"""Per-asset-class Order-Block tuning (implementation plan T8-T12).

OB_ASSET_TUNING=off (default) must reproduce the legacy unified behaviour
exactly (OB_UNIFIED for every class); with tuning on, per-class overrides
apply: ob_min_disp_atr, ob_search_bars, ob_fresh_grade, A+ sweep/PD
requirements, round-number liquidity pools (GOLD), per-class news events and
spread caps. Signatures gain an optional cfg param so legacy paths are
untouched when no config is supplied.
"""
import importlib
import os
import sys
import unittest
from unittest import mock

import numpy as np
import pandas as pd


def _load_engine():
    saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine")}
    old_paper = os.environ.pop("PAPER_MODE", None)

    class _FakeFlask:
        def __init__(self, *args, **kwargs):
            pass
        def route(self, *args, **kwargs):
            return lambda fn: fn
        def add_url_rule(self, *args, **kwargs):
            return None

    fake_ccxt = importlib.util.find_spec("ccxt") is not None
    if not fake_ccxt:
        import types
        ccxt_mod = types.ModuleType("ccxt")
        class FakeBingX:
            def __init__(self, *args, **kwargs):
                self.markets = {}
        ccxt_mod.bingx = FakeBingX
        sys.modules["ccxt"] = ccxt_mod
    sys.modules["flask"] = _fake_flask_with(_FakeFlask)
    sys.modules.pop("core.engine", None)
    engine = importlib.import_module("core.engine")
    return engine, saved, old_paper


def _fake_flask_with(_FakeFlask):
    import types
    f = types.ModuleType("flask")
    f.Flask = _FakeFlask
    f.jsonify = lambda *a, **k: None
    f.request = types.SimpleNamespace()
    return f


def _base(n=60, level=100.0):
    o = np.full(n, level); c = np.full(n, level)
    h = np.full(n, level + 0.5); l = np.full(n, level - 0.5)
    v = np.full(n, 1000.0)
    return o, c, h, l, v


def _buy_displacement_frame(n=30, base=22, disp_atr=0.8, level=100.0):
    """Red base candle at `base`, 3 green displacement legs reaching
    `base_high + disp_atr` (atr = 1.0 by construction), price resting high
    so the zone is NOT broken; nothing else red in the window."""
    o, c, h, l, v = _base(n, level)
    o[base], c[base], h[base], l[base] = level + 0.4, level - 0.4, level + 0.8, level - 0.6
    base_high = level + 0.8
    closers = (base_high + disp_atr - 0.10, base_high + disp_atr, base_high + disp_atr - 0.05)
    o[base + 1] = level - 0.3
    c[base + 1] = closers[0]
    h[base + 1] = closers[0] + 0.2
    l[base + 1] = level - 0.35
    for k, cl in enumerate(closers[1:], start=2):
        o[base + k] = closers[k - 1]
        c[base + k] = cl
        h[base + k] = cl + 0.25
        l[base + k] = closers[k - 1] - 0.1
    v[base + 1] = 2600.0
    for j in range(base + 4, n):
        o[j] = c[j] = closers[-1] - 0.4
        h[j], l[j] = o[j] + 0.3, o[j] - 0.3
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


def _aplus_seed_frame():
    """Single red base bar at 54, strong displacement close ~103.1, price
    resting in PREMIUM (pd_aligned False), touches == 1, freshness 6:
    legacy-only scoring reaches A+."""
    n = 60
    o, c, h, l, v = _base(n)
    o[54], c[54], h[54], l[54] = 100.4, 99.6, 100.8, 99.4
    o[55], c[55], h[55], l[55] = 99.6, 102.6, 102.8, 99.5
    o[56], c[56], h[56], l[56] = 102.4, 102.9, 103.0, 102.3
    o[57], c[57], h[57], l[57] = 102.9, 103.1, 103.2, 102.8
    o[58], c[58], h[58], l[58] = 103.1, 103.0, 103.3, 102.9
    o[59], c[59], h[59], l[59] = 103.0, 103.15, 103.3, 102.95
    v[55] = 2600.0
    return pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                         "low": l, "close": c, "volume": v})


class _EngineTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine, cls.saved, cls.old_paper = _load_engine()
        cls.engine.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        cls.engine.STATE.clear()
        cls.engine.TRADE_STATE.clear()
        cls.engine.TRADE_STATE.update({"in_position": False})

    @classmethod
    def tearDownClass(cls):
        sys.modules.pop("core.engine", None)
        for name, module in cls.saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module
        if cls.old_paper is not None:
            os.environ["PAPER_MODE"] = cls.old_paper


class ObConfigValues(_EngineTestCase):

    def test_tuning_off_returns_unified_for_every_class(self):
        for ac in ("CRYPTO", "INDEX", "STOCK", "GOLD", "OIL", "NEWS"):
            with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "false"}, clear=False):
                cfg = self.engine.AssetBehaviorProfile.ob_config(ac)
            self.assertEqual(cfg["ob_min_disp_atr"], 0.6, ac)
            self.assertEqual(cfg["ob_search_bars"], 35)
            self.assertEqual(cfg["ob_fresh_grade"], (10, 20, 40))
            self.assertFalse(cfg["ob_require_sweep_aplus"])
            self.assertFalse(cfg["ob_require_pd_aplus"])
            self.assertIsNone(cfg["ob_spread_cap_pct"])

    def test_tuning_on_applies_per_class(self):
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            cfg_c = self.engine.AssetBehaviorProfile.ob_config("CRYPTO")
            cfg_g = self.engine.AssetBehaviorProfile.ob_config("GOLD")
            cfg_oi = self.engine.AssetBehaviorProfile.ob_config("OIL")
            cfg_x = self.engine.AssetBehaviorProfile.ob_config("INDEX")
            cfg_s = self.engine.AssetBehaviorProfile.ob_config("STOCK")
        self.assertEqual(cfg_c["ob_min_disp_atr"], 1.1)
        self.assertTrue(cfg_c["ob_require_pd_aplus"])
        self.assertFalse(cfg_c["ob_require_sweep_aplus"])
        self.assertEqual(cfg_g["ob_min_disp_atr"], 2.5)
        self.assertTrue(cfg_g["ob_round_number_liq"])
        self.assertTrue(cfg_g["ob_require_sweep_aplus"])
        self.assertEqual(cfg_oi["ob_min_disp_atr"], 2.0)
        self.assertIn("EIA", cfg_oi["ob_news_events"])
        self.assertEqual(cfg_x["ob_min_disp_atr"], 1.2)
        self.assertEqual(cfg_x["ob_spread_cap_pct"], 0.05)
        self.assertEqual(cfg_s["ob_min_disp_atr"], 1.0)
        self.assertIn("EARNINGS", cfg_s["ob_news_events"])

    def test_env_override_crypto_disp(self):
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true",
                                          "OB_CRYPTO_DISP_ATR": "1.4"}, clear=False):
            self.assertEqual(self.engine.AssetBehaviorProfile.ob_config("CRYPTO")["ob_min_disp_atr"], 1.4)
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true",
                                          "OB_CRYPTO_DISP_ATR": "junk"}, clear=False):
            self.assertEqual(self.engine.AssetBehaviorProfile.ob_config("CRYPTO")["ob_min_disp_atr"], 1.1)


class MinDisplacementPerClass(_EngineTestCase):

    def test_crypto_rejects_0_8_accepts_1_3(self):
        q = self.engine.ExecutionQueue()
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            cfg_c = self.engine.AssetBehaviorProfile.ob_config("CRYPTO")
        zl_lo, _, _, _ = q._find_causal_ob_zone(_buy_displacement_frame(disp_atr=0.8), "BUY", 1.0, cfg_c)
        self.assertEqual(zl_lo, 0.0)
        zl_hi, _, _, _ = q._find_causal_ob_zone(_buy_displacement_frame(disp_atr=1.3), "BUY", 1.0, cfg_c)
        self.assertGreater(zl_hi, 0.0)

    def test_gold_tighter_than_crypto(self):
        q = self.engine.ExecutionQueue()
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            cfg_c = self.engine.AssetBehaviorProfile.ob_config("CRYPTO")
            cfg_g = self.engine.AssetBehaviorProfile.ob_config("GOLD")
        df_13 = _buy_displacement_frame(disp_atr=1.3)
        self.assertGreater(q._find_causal_ob_zone(df_13, "BUY", 1.0, cfg_c)[0], 0.0)
        self.assertEqual(q._find_causal_ob_zone(df_13, "BUY", 1.0, cfg_g)[0], 0.0)
        df_26 = _buy_displacement_frame(disp_atr=2.6)
        self.assertGreater(q._find_causal_ob_zone(df_26, "BUY", 1.0, cfg_g)[0], 0.0)

    def test_legacy_unified_still_finds_0_8(self):
        q = self.engine.ExecutionQueue()
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "false"}, clear=False):
            cfg = self.engine.AssetBehaviorProfile.ob_config("CRYPTO")
        self.assertGreater(q._find_causal_ob_zone(_buy_displacement_frame(disp_atr=0.8), "BUY", 1.0, cfg)[0], 0.0)


class SearchBarsPerClass(_EngineTestCase):

    def test_gold_wider_window_finds_zone_legacy_misses(self):
        q = self.engine.ExecutionQueue()
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            cfg_g = self.engine.AssetBehaviorProfile.ob_config("GOLD")
            cfg_legacy = self.engine.AssetBehaviorProfile.ob_config("GOLD") if False else dict(
                self.engine.AssetBehaviorProfile.OB_UNIFIED)
        df = _buy_displacement_frame(n=60, base=20, disp_atr=2.6)
        zl_legacy, _, _, _ = q._find_causal_ob_zone(df, "BUY", 1.0, cfg_legacy)
        self.assertEqual(zl_legacy, 0.0)
        zl_gold, _, _, _ = q._find_causal_ob_zone(df, "BUY", 1.0, cfg_g)
        self.assertGreater(zl_gold, 0.0)


class APlusRequirements(_EngineTestCase):

    def _grade(self, sweep, pd, cfg_overrides=None):
        q = self.engine.ExecutionQueue()
        cfg = dict(self.engine.AssetBehaviorProfile.OB_UNIFIED)
        if cfg_overrides:
            cfg.update(cfg_overrides)
        with mock.patch.object(
                q, "_ob_synergy",
                new=lambda *a, **k: {"sweep_aligned": sweep, "fvg_after_displacement": False,
                                     "pd_aligned": pd, "bos_aligned": False, "bonus": 0.0}):
            result = q._select_strong_ob(_aplus_seed_frame(), "BUY", 1.0, cfg)
        return result["grade"]

    def test_legacy_seed_reaches_aplus(self):
        self.assertEqual(self._grade(sweep=False, pd=False), "A+")

    def test_require_pd_downgrades_when_premium(self):
        self.assertEqual(self._grade(sweep=False, pd=False, cfg_overrides={"ob_require_pd_aplus": True}), "A")
        self.assertEqual(self._grade(sweep=False, pd=True, cfg_overrides={"ob_require_pd_aplus": True}), "A+")

    def test_require_sweep_downgrades_without_sweep(self):
        self.assertEqual(self._grade(sweep=False, pd=False, cfg_overrides={"ob_require_sweep_aplus": True}), "A")
        self.assertEqual(self._grade(sweep=True, pd=False, cfg_overrides={"ob_require_sweep_aplus": True}), "A+")

    def test_fresh_grade_bounds(self):
        self.assertEqual(self._grade(sweep=False, pd=False, cfg_overrides={"ob_fresh_grade": (6, 6, 6)}), "A+")
        self.assertEqual(self._grade(sweep=False, pd=False, cfg_overrides={"ob_fresh_grade": (1, 1, 1)}), "INVALID")


class RoundNumberPools(_EngineTestCase):

    def test_augment_adds_integer_level(self):
        q = self.engine.ExecutionQueue()
        n = 30
        o, c, h, l, v = _base(n)
        for i in range(n - 6, n):
            h[i] = 100.03
        df = pd.DataFrame({"timestamp": np.arange(n), "open": o, "high": h,
                           "low": l, "close": c, "volume": v})
        out = q._augment_round_pools(df, {"high_pools": [], "low_pools": []}, atr=0.5)
        self.assertIn(100.0, [float(x[1]) for x in out["high_pools"]])


class SpreadCaps(_EngineTestCase):

    def test_gold_cap_applies_when_tuning_on(self):
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            tol_gold = self.engine.dynamic_spread_tolerance("XAU/USD")
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "false"}, clear=False):
            tol_legacy = self.engine.dynamic_spread_tolerance("XAU/USD")
        self.assertAlmostEqual(tol_gold, 0.20)
        self.assertAlmostEqual(tol_legacy, 0.08)

    def test_crypto_keeps_dynamic(self):
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            tol_c = self.engine.dynamic_spread_tolerance("BTC/USDT:USDT")
            tol_x = self.engine.dynamic_spread_tolerance("US30")
        self.assertAlmostEqual(tol_c, 0.08)
        self.assertAlmostEqual(tol_x, 0.05)


class NewsPerClass(_EngineTestCase):

    def _cand(self, engine, asset_class, event_type, impact):
        return engine.ExecutionCandidate(
            symbol="X", side="BUY", price=100.0, entry_price=100.0,
            stop_loss=95.0, take_profit_1=103.0, take_profit_2=106.0, atr=1.0,
            df=None, ob=None,
            ob_cfg=engine.AssetBehaviorProfile.ob_config(asset_class),
            news_event_type=event_type, news_impact_score=impact, news_risk_level="NEUTRAL")

    def test_oil_eia_event_penalises_risk(self):
        q = self.engine.ExecutionQueue()
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            en = self._cand(self.engine, "OIL", "EIA", 80)
            plain = self._cand(self.engine, "OIL", "", 0)
            other = self._cand(self.engine, "OIL", "FOMC", 80)
            stock = self._cand(self.engine, "STOCK", "EIA", 80)
        base = q._evaluate_risk(plain, 100.0)
        self.assertGreaterEqual(base, 0)
        self.assertLess(q._evaluate_risk(en, 100.0), base)
        self.assertEqual(q._evaluate_risk(other, 100.0), base)

    def test_stock_specific_events_only(self):
        q = self.engine.ExecutionQueue()
        with mock.patch.dict(os.environ, {"OB_ASSET_TUNING": "true"}, clear=False):
            earn = self._cand(self.engine, "STOCK", "EARNINGS", 80)
            eia = self._cand(self.engine, "STOCK", "EIA", 80)
        self.assertLess(q._evaluate_risk(earn, 100.0), q._evaluate_risk(eia, 100.0))


if __name__ == "__main__":
    unittest.main()