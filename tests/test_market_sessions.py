import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from core.market_sessions import detect_fx_pair, session_context, session_allows_entry


class MarketSessionTest(unittest.TestCase):
    def test_bingx_usdcad_alias_is_detected(self):
        self.assertEqual(detect_fx_pair("NCFXUSD2CAD/USDT:USDT"), ("USD", "CAD"))

    def test_usdcad_ny_window_is_preferred(self):
        # 14:00 ET is inside the supplied indicator's NY AM/PM family and NY
        # centre is open; use UTC to make the test DST-independent.
        dt = datetime(2026, 1, 15, 14, 0, tzinfo=ZoneInfo("America/New_York")).astimezone(timezone.utc)
        ctx = session_context("NCFXUSD2CAD/USDT:USDT", dt)
        self.assertEqual(ctx["pair"], "USD/CAD")
        self.assertEqual(ctx["state"], "PREFERRED")
        self.assertIn("NY_PM", ctx["indicator_sessions"])

    def test_fx_hard_gate_is_opt_in(self):
        dt = datetime(2026, 1, 15, 3, 0, tzinfo=ZoneInfo("America/New_York"))
        ok, ctx = session_allows_entry("NCFXUSD2CAD/USDT:USDT", dt.astimezone(timezone.utc))
        self.assertTrue(ok)
        self.assertFalse(ctx["hard_blocked"])
