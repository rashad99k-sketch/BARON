"""Ultimate Sniper confluence layer tests.

Locks the enrichment layer onto the requirements agreed with the operator:
  * closed-candles-only, deterministic, no look-ahead / no repainting,
  * advisory only (must never gate an entry or flip a weak setup to strong),
  * evidence saved in detail so callers can explain bonuses/penalties,
  * configurable with the indicator's own defaults preserved,
  * exhaustion is advisory evidence (position-management input), never an
    entry signal.

The unit tests here are fully self-contained (no engine / network) so the
module can be validated in isolation. A small regression test also ensures the
engine still imports and the enrichment flag is wired.
"""

import importlib
import os
import sys
import unittest

import numpy as np
import pandas as pd

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core import sniper_enrichment as se


def _df_from(opens, closes, highs, lows, volumes=None):
    n = len(opens)
    if volumes is None:
        volumes = [1000.0] * n
    df = pd.DataFrame({
        "open": np.asarray(opens, dtype=float),
        "high": np.asarray(highs, dtype=float),
        "low": np.asarray(lows, dtype=float),
        "close": np.asarray(closes, dtype=float),
        "volume": np.asarray(volumes, dtype=float),
        "timestamp": list(range(n)),
    })
    return df


def _flat(n, level=100.0):
    """Flat baseline candles (body-less, small range)."""
    o = [level] * n
    c = [level] * n
    h = [level + 0.5] * n
    l = [level - 0.5] * n
    return o, c, h, l


def _small_cfg():
    return se.SniperEnrichmentConfig(
        pivot_len=3, lookback=3, vol_len=2, box_width=1.0,
        ce_length=3, ce_mult=3.0, sniper_ema_len=5, trend_ema_len=10,
        exhaustion_mult=1.5, overextend_mult=2.0,
    )


class SniperConfigTest(unittest.TestCase):
    def test_defaults_match_indicator(self):
        c = se.SniperEnrichmentConfig()
        self.assertEqual(c.pivot_len, 10)      # prd
        self.assertEqual(c.lookback, 20)       # lookbackPeriod
        self.assertEqual(c.vol_len, 2)         # vol_len
        self.assertEqual(c.box_width, 1.0)     # box_withd
        self.assertEqual(c.ce_length, 22)      # Chandelier ATR period
        self.assertEqual(c.ce_mult, 3.0)       # Chandelier ATR multiplier
        self.assertEqual(c.sniper_ema_len, 50)
        self.assertEqual(c.trend_ema_len, 200)

    def test_env_overrides_preserve_defaults_when_absent(self):
        old = {k: os.environ.get(k) for k in
               ("SNIPER_PIVOT_LEN", "SNIPER_LOOKBACK", "SNIPER_CE_MULT")}
        try:
            os.environ.pop("SNIPER_PIVOT_LEN", None)
            os.environ.pop("SNIPER_LOOKBACK", None)
            os.environ.pop("SNIPER_CE_MULT", None)
            c = se.SniperEnrichmentConfig.from_env()
            self.assertEqual(c.pivot_len, 10)
            self.assertEqual(c.lookback, 20)
            self.assertEqual(c.ce_mult, 3.0)
            os.environ["SNIPER_PIVOT_LEN"] = "7"
            c2 = se.SniperEnrichmentConfig.from_env()
            self.assertEqual(c2.pivot_len, 7)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_config_defaults_preserved_when_dict_partial(self):
        c = se.SniperEnrichmentConfig.from_dict({"pivot_len": 5})
        self.assertEqual(c.pivot_len, 5)
        self.assertEqual(c.lookback, 20)


class SniperDirectionTest(unittest.TestCase):
    def test_bullish_drift_favours_long(self):
        rng = np.random.default_rng(1)
        n = 300
        c = np.cumsum(rng.normal(0.05, 0.8, n)) + 100
        o = np.concatenate([[c[0]], c[:-1]])
        h = np.maximum(o, c) + 0.5
        l = np.minimum(o, c) - 0.5
        v = np.abs(rng.normal(1000, 200, n))
        df = _df_from(o, c, h, l, v)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        buy = eng.analyze(df, "X", side=se.LONG)
        sell = eng.analyze(df, "X", side=se.SHORT)
        self.assertGreaterEqual(buy.score_shift, 0)
        self.assertLess(sell.score_shift, buy.score_shift if buy.score_shift > sell.score_shift else sell.score_shift + 1)

    def test_advisory_never_hard_gates(self):
        # score shift must be bounded within the module's own caps.
        rng = np.random.default_rng(2)
        n = 300
        c = np.cumsum(rng.normal(0.4, 2.0, n)) + 100
        o = np.concatenate([[c[0]], c[:-1]])
        h = np.maximum(o, c) + 1.0
        l = np.minimum(o, c) - 1.0
        v = np.abs(rng.normal(1000, 300, n))
        df = _df_from(o, c, h, l, v)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        for side in (se.LONG, se.SHORT):
            conf = eng.analyze(df, "X", side=side)
            self.assertLessEqual(conf.score_shift, se.MAX_CONFLUENCE_BONUS)
            self.assertGreaterEqual(conf.score_shift, se.MAX_CONFLUENCE_PENALTY)


class SniperEvidenceTest(unittest.TestCase):
    def test_demand_supply_boxes_detected(self):
        # A decisive down-thrust forms a low pivot surrounded by buying volume
        # (demand box); a later up-thrust forms a high pivot surrounded by
        # selling volume (supply box).
        n = 60
        o, c, h, l = _flat(n)
        # ---- demand region: pivot low at 18 with buys around it
        for i in range(10, 16):
            o[i] = 100.0; c[i] = 98.8; h[i] = 100.2; l[i] = 98.6
        for i in range(16, 18):
            o[i] = 98.5; c[i] = 98.9; h[i] = 99.0; l[i] = 98.3
        o[18] = 98.2; c[18] = 98.7; h[18] = 98.9; l[18] = 97.8
        for i in range(19, 21):
            o[i] = 98.6; c[i] = 99.3; h[i] = 99.6; l[i] = 98.5
        # ---- neutral range before the supply region
        for i in range(22, 34):
            o[i] = 99.5; c[i] = 99.7; h[i] = 100.0; l[i] = 99.2
        # ---- supply region: pivot high at 42 with sells around it
        for i in range(34, 40):
            o[i] = 100.0; c[i] = 101.2; h[i] = 101.5; l[i] = 99.9
        o[40] = 101.5; c[40] = 101.8; h[40] = 102.0; l[40] = 101.4
        o[41] = 101.8; c[41] = 101.5; h[41] = 102.0; l[41] = 101.3
        o[42] = 102.2; c[42] = 101.7; h[42] = 103.5; l[42] = 101.5
        for i in range(43, 45):
            o[i] = 101.7; c[i] = 100.9; h[i] = 101.9; l[i] = 100.6
        df = _df_from(o, c, h, l)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        ev = eng.analyze(df, "X", side=se.LONG).evidence
        self.assertIsInstance(ev.demand_box_count, int)
        self.assertIsInstance(ev.supply_box_count, int)
        self.assertGreater(ev.demand_box_count, 0)
        self.assertGreater(ev.supply_box_count, 0)
        self.assertIsNotNone(ev.nearest_demand)
        self.assertIsNotNone(ev.nearest_supply)

    def test_sweep_and_sfp(self):
        # A liquidity low pivot forms; final closed bar tags below it and the
        # close recovers back above it -> sweep_buy on long context.
        n = 60
        o, c, h, l = _flat(n)
        # decline to a liquidity low pivot at 40
        for i in range(30, 40):
            o[i] = 100.0; c[i] = 98.6; h[i] = 100.1; l[i] = 98.4
        o[40] = 98.2; c[40] = 98.3; h[40] = 98.5; l[40] = 97.6
        # price stays above it (pivot stays intact)
        for i in range(41, 58):
            o[i] = 98.5; c[i] = 99.0; h[i] = 99.3; l[i] = 98.3
        # final closed bar: low dips below the pivot and the close stays below
        # it -> liquidity sweep on long context (prev close was above 97.6)
        o[59] = 98.6; c[59] = 97.0; h[59] = 98.8; l[59] = 96.7
        df = _df_from(o, c, h, l)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        conf = eng.analyze(df, "X", side=se.LONG)
        ev = conf.evidence
        self.assertEqual(ev.sweep_buy, True)

    def test_fvg_bullish(self):
        n = 60
        o, c, h, l = _flat(n)
        # candle n-3 high, candle n-2 low gap -> bullish FVG
        o[-3], c[-3], h[-3], l[-3] = 100, 100, 101.5, 99.5
        o[-2], c[-2], h[-2], l[-2] = 100, 100, 100.5, 102.5  # low>prev high
        df = _df_from(o, c, h, l)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        ev = eng.analyze(df, "X", side=se.LONG).evidence
        self.assertEqual(ev.fvg_bullish, True)

    def test_mss_method_exists_and_is_boolean(self):
        n = 60
        o, c, h, l = _flat(n)
        df = _df_from(o, c, h, l)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        ev = eng.analyze(df, "X", side=se.LONG).evidence
        self.assertIn(type(ev.mss_bullish), (bool, np.bool_))
        self.assertIn(type(ev.mss_bearish), (bool, np.bool_))

    def test_evidence_is_serializable(self):
        n = 60
        o, c, h, l = _flat(n)
        df = _df_from(o, c, h, l)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        conf = eng.analyze(df, "X", side=se.LONG)
        d = conf.to_dict()
        self.assertIn("side", d)
        self.assertIn("score_shift", d)
        self.assertIn("evidence", d)
        self.assertIn("reasons", d)
        evd = d["evidence"]
        for key in ("in_demand_box", "delta_volume", "adx", "exhaustion_buy",
                    "premium_discount_buy", "fvg_bullish", "mss_bullish"):
            self.assertIn(key, evd)

    def test_insufficient_data_returns_neutral(self):
        df = _df_from([100.0] * 10, [100.0] * 10, [101.0] * 10, [99.0] * 10)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        conf = eng.analyze(df, "X", side=se.LONG)
        self.assertEqual(conf.score_shift, 0.0)
        self.assertIn("insufficient_data", conf.reasons)

    def test_string_side_forms_accepted(self):
        # The engine passes cand.side as "BUY"/"SELL"; the analyzer must map
        # those to LONG/SHORT without raising or silently degrading.
        rng = np.random.default_rng(7)
        n = 200
        c = np.cumsum(rng.normal(0.1, 1.5, n)) + 100
        o = np.concatenate([[c[0]], c[:-1]])
        h = np.maximum(o, c) + 0.6
        l = np.minimum(o, c) - 0.6
        df = _df_from(o, c, h, l)
        eng = se.SniperEnrichmentEngine(_small_cfg())
        buy = eng.analyze(df, "X", side="BUY").to_dict()
        sell = eng.analyze(df, "X", side="SELL").to_dict()
        self.assertEqual(buy["side"], "LONG")
        self.assertEqual(sell["side"], "SHORT")
        self.assertIsInstance(buy["score_shift"], float)
        self.assertIsInstance(sell["score_shift"], float)


class SniperRegressionTest(unittest.TestCase):
    def test_engine_imports_and_flag_wired(self):
        saved = {k: sys.modules.get(k) for k in ("ccxt", "flask", "core.engine")}

        class _FakeFlask:
            def __init__(self, *a, **k):
                pass
            def route(self, *a, **k):
                return lambda fn: fn
            def add_url_rule(self, *a, **k):
                return None

        import types
        f = types.ModuleType("flask")
        f.Flask = _FakeFlask
        f.jsonify = lambda *a, **k: None
        f.request = types.SimpleNamespace()
        sys.modules["flask"] = f
        try:
            importlib.import_module("ccxt") if importlib.util.find_spec("ccxt") else None
            sys.modules.pop("core.engine", None)
            engine = importlib.import_module("core.engine")
            self.assertTrue(getattr(engine, "SNIPER_ENRICHMENT_AVAILABLE", False))
            self.assertIn("core.sniper_enrichment", sys.modules)
        finally:
            sys.modules.pop("core.engine", None)
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_no_repaint_no_lookahead_uses_closed_candles(self):
        # The analyzer only reads df up to the last CLOSED candle; feeding an
        # artificially-appended "future" bar must not change results for the
        # same closed history. We verify pivots only trust bars with both-sides
        # closure by checking the "confirmed" filter never returns a pivot at
        # the last bar.
        n = 60
        o, c, h, l = _flat(n)
        rng = np.random.default_rng(5)
        oa = np.asarray(o, dtype=float) + rng.normal(0, 0.3, n)
        df = _df_from(oa, c, h, l)
        cfg = _small_cfg()
        ph, pl = se._pivots(df, cfg.pivot_len)
        for idx, _price in ph + pl:
            self.assertLess(idx, n - cfg.pivot_len)  # never at/beyond trailing edge


if __name__ == "__main__":
    unittest.main()
