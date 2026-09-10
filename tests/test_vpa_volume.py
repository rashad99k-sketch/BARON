import unittest

import numpy as np
import pandas as pd

from core.vpa_volume import (effort_result, ob_volume_dna,
                             opposing_ob_conflict, retest_health,
                             volume_validation)


def _flat_df(n=120, base=100.0, volume=1000.0):
    """Completely flat, wickless frame: zero displacement, constant volume."""
    close = np.full(n, base)
    o = np.full(n, base)
    h = np.full(n, base)
    l = np.full(n, base)
    return pd.DataFrame({"open": o, "high": h, "low": l,
                         "close": close, "volume": np.full(n, volume)})


def _racy_df(n=120, base=100.0):
    """Flat closes + expanding range + ramping volume = effort without result."""
    t = np.arange(n)
    close = np.full(n, base)
    o = close
    h = base + np.linspace(0.0, 6.0, n)
    l = base - np.linspace(0.0, 6.0, n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": close,
                         "volume": np.linspace(1000.0, 9000.0, n)})


def _bearish_ob_frame(base=100.0, price=99.6):
    """Frame with a recent small GREEN base candle + downward displacement and a
    final close near the OB zone -> a fresh opposing OB owns current price."""
    n = 30
    o = np.full(n, base)
    c = np.full(n, price)
    # small green base at i=20 (doji -> valid SELL-OB origin: close > open)
    o[20] = base - 0.5
    c[20] = base - 0.4
    # big red displacement bars following it
    for i in (21, 22, 23):
        o[i] = base - 0.6
        c[i] = base - 1.6
    # recover toward the zone at the tail
    c[24:] = base - 0.4
    o[24:] = base - 0.2
    h = np.maximum(o, c) + 0.2
    l = np.minimum(o, c) - 0.2
    vol = np.full(n, 1000.0)
    vol[21:24] = 2500.0
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                         "volume": vol})


class RetestHealthTest(unittest.TestCase):
    def test_under_attack_when_close_through_zone(self):
        n = 30
        base = 100.0
        df = _flat_df(n, base)
        df.iloc[-3] = [100.1, 100.4, 99.9, 99.8, 700.0]
        df.iloc[-2] = [99.8, 100.0, 98.9, 99.0, 1500.0]
        df.iloc[-1] = [99.0, 99.1, 98.5, 98.6, 2500.0]
        r = retest_health(df, "BUY", 1.0, 99.0, 100.0)
        self.assertEqual(r["status"], "UNDER_ATTACK")
        self.assertIn("close_side", r)

    def test_not_in_zone_when_price_far(self):
        r = retest_health(_flat_df(), "BUY", 1.0, 90.0, 91.0)
        self.assertEqual(r["status"], "NOT_IN_ZONE")

    def test_bad_zone_is_neutral(self):
        r = retest_health(_flat_df(), "BUY", 1.0, -1.0, -1.0)
        self.assertEqual(r["status"], "NEUTRAL")

    def test_insufficient_data_is_neutral(self):
        r = retest_health(_flat_df(4), "BUY", 1.0, 99.0, 101.0)
        self.assertEqual(r["status"], "NEUTRAL")


class EffortResultTest(unittest.TestCase):
    def test_flat_frame_is_stall(self):
        e = effort_result(_flat_df(), "BUY", 1.0)
        self.assertEqual(e["status"], "STALL")

    def test_big_range_no_move_is_weak_result(self):
        e = effort_result(_racy_df(), "BUY", 1.0)
        self.assertEqual(e["status"], "WEAK_RESULT")

    def test_low_effort_clean_move_confirms(self):
        n = 40
        o = np.full(n, 100.0)
        c = np.full(n, 100.0)
        c[-1] = 102.0
        h = np.maximum(o, c) + 0.1
        l = np.minimum(o, c) - 0.1
        vol = np.full(n, 1000.0)
        vol[-3:] = 300.0  # contracting effort on the move -> clean continuation
        df = pd.DataFrame({"open": o, "high": h, "low": l, "close": c,
                           "volume": vol})
        e = effort_result(df, "BUY", 1.0)
        self.assertEqual(e["status"], "CONFIRMED")

    def test_returns_dict_always(self):
        r = effort_result(_flat_df(4), "BUY", 1.0)
        self.assertIsInstance(r, dict)
        self.assertEqual(r["status"], "STALL")


class VolumeValidationTest(unittest.TestCase):
    def test_displacement_with_expanded_volume_confirms(self):
        s, d = volume_validation(_flat_df(), "BUY", 1.0,
                                 displacement_atr=1.2, ob_volume_ratio=2.1,
                                 retest={"status": "HEALTHY"})
        self.assertEqual(s, "DISPLACEMENT_VOLUME_CONFIRMED")

    def test_under_attack_retest_blocks(self):
        s, d = volume_validation(
            _flat_df(), "BUY", 1.0, displacement_atr=1.5, ob_volume_ratio=2.5,
            retest={"status": "UNDER_ATTACK"})
        self.assertEqual(s, "UNDER_ATTACK")
        self.assertIn("retest_under_attack", d["reasons"])

    def test_no_confirmation_without_evidence(self):
        s, d = volume_validation(
            _flat_df(), "BUY", 1.0, displacement_atr=0.1, ob_volume_ratio=1.0,
            retest={"status": "NOT_IN_ZONE"})
        self.assertEqual(s, "NO_CONFIRMATION")


class OpposingObConflictTest(unittest.TestCase):
    def test_no_conflict_on_benign_frame(self):
        for side in ("BUY", "SELL"):
            c = opposing_ob_conflict(_flat_df(), side, 1.0, 99.0, 101.0)
            self.assertIsInstance(c, dict)
            self.assertFalse(c.get("present"))

    def test_recent_opposing_ob_detected(self):
        c = opposing_ob_conflict(_bearish_ob_frame(), "BUY", 1.0, 99.0, 101.0)
        self.assertTrue(c.get("present"))
        self.assertEqual(c["side"], "SELL")
        self.assertGreaterEqual(c["displacement_atr"], 0.8)
        self.assertGreaterEqual(c["volume_ratio"], 1.2)


class ObVolumeDnaTest(unittest.TestCase):
    def test_dna_is_structurally_complete(self):
        dna = ob_volume_dna(_flat_df(), "BUY", 1.0,
                            displacement_start=-1, zone_low=99.0,
                            zone_high=101.0)
        self.assertIsInstance(dna, dict)
        self.assertTrue(dna.get("complete"))
        for key in ("volume_ratio", "volume_climax", "effort_result",
                    "absorption", "demand_confirmation", "retest_volume",
                    "under_attack", "opposing_conflict"):
            self.assertIn(key, dna)

    def test_dna_guards_bad_input(self):
        dna = ob_volume_dna(None, "BUY", 0.0, zone_low=99.0, zone_high=101.0)
        self.assertIsInstance(dna, dict)
        self.assertFalse(dna.get("complete"))


class AtrProxyMatchTest(unittest.TestCase):
    def test_effort_never_raises(self):
        for df in (None, _flat_df(2), ""):
            try:
                effort_result(df, "BUY", 1.0)
            except Exception:
                self.fail("effort_result must never raise")


if __name__ == "__main__":
    unittest.main()