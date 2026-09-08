import os
import unittest

from portfolio.manager import PortfolioManager, PositionContext


class FakeManager:
    def __init__(self):
        self.STATE = {"open": False, "current_symbol": None}
        self.TRADE_STATE = {"in_position": False, "symbol": None}
        self.paper = {"position": None}
        self._live_manager = "default"
        self.LiveTradeManager = lambda *args: "new-manager"
        self._event_bus = None
        self._exchange_sync = None
        self._recovery_guard = None


class PortfolioIsolationTest(unittest.TestCase):
    def test_symbol_contexts_are_isolated(self):
        e = FakeManager()
        p = PortfolioManager(2, e)
        p.bind(e)
        p.contexts["BTC"] = PositionContext(
            "BTC",
            {"open": True, "current_symbol": "BTC", "side": "BUY"},
            {"in_position": True, "symbol": "BTC"},
            "BTC-MGR",
        )
        p.contexts["ETH"] = PositionContext(
            "ETH",
            {"open": True, "current_symbol": "ETH", "side": "SELL"},
            {"in_position": True, "symbol": "ETH"},
            "ETH-MGR",
        )

        p.activate("BTC")
        self.assertEqual(e.STATE["current_symbol"], "BTC")
        self.assertEqual(e._live_manager, "BTC-MGR")

        p.activate("ETH")
        self.assertEqual(e.STATE["current_symbol"], "ETH")
        self.assertEqual(e._live_manager, "ETH-MGR")

        p.deactivate()
        self.assertFalse(e.STATE["open"])


if __name__ == "__main__":
    unittest.main()

class PortfolioCapacityPolicyTest(unittest.TestCase):
    def test_asset_class_cap_is_enforced(self):
        # The shipped manager defaults per-class capacity to the 6-market model
        # (DEFAULT_CLASS_CAPS: CRYPTO 2 / INDEX 2 / GOLD 1 / OIL 1 / NEWS 1);
        # MAX_POSITIONS_PER_ASSET_CLASS is a master override for EVERY class
        # when explicitly set only (no 999 fake default).
        os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = "2"
        try:
            e = FakeManager()
            p = PortfolioManager(6, e)
            p.bind(e)
            p.contexts["BTC"] = PositionContext("BTC", {}, {}, "mgr")
            p.contexts["ETH"] = PositionContext("ETH", {}, {}, "mgr")
            self.assertFalse(p.can_open("SOL", "CRYPTO"))
            self.assertTrue(p.can_open("AAPL", "STOCK"))
        finally:
            os.environ.pop("MAX_POSITIONS_PER_ASSET_CLASS", None)


class PaperMarginEngine:
    """Fake engine with realistic 6-position margin accounting (mirrors edits
    applied to core/engine.py): each open commits 10% of free balance into
    committed_margin, restored (with PnL) on close."""

    def __init__(self):
        self.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        self.STATE = {"open": False, "current_symbol": None, "side": None, "entry": 0.0, "qty": 0.0}
        self.TRADE_STATE = {"in_position": False, "symbol": None}
        self.PERF = {"trades": 0, "last_trade": {}}
        self.MEMORY = {}
        self._live_manager = None
        self._event_bus = None
        self._exchange_sync = None
        self._recovery_guard = None

    def get_balance_safe(self, *a, **k):
        return self.paper["balance"]

    def allocate(self, price, side):
        # Compute investment of notional needed for qty (mirrors engine sizing)
        free = self.paper["balance"]
        margin = free * 0.10
        return margin

    def execute_entry(self, side, symbol, price, sl, tp1, tp2, score, reason,
                      atr_val, trade_type, entry_type, classification):
        free = self.paper["balance"]
        margin = free * 0.10
        notional = margin * 10
        qty = notional / price
        self.STATE.update({
            "open": True, "side": side, "entry": price, "qty": qty,
            "remaining_qty": qty, "current_symbol": symbol, "mark_price": price,
            "synthetic_sl": sl, "synthetic_tp1": tp1, "tp2_price": tp2,
            "margin": margin,
        })
        self.paper["balance"] -= margin
        self.paper["committed_margin"] += margin
        self.paper["position"] = {"side": side, "entry": price, "qty": qty, "remaining_qty": qty}
        self._live_manager = "mgr-" + symbol
        return True

    def log_execution(self, *a, **k):
        pass

    def LiveTradeManager(self, *a, **k):
        return None

    def close_position_full(self):
        # Mirror engine Edit 3: restore committed margin (with PnL)
        margin = self.STATE.get("margin", 0.0)
        self.paper["balance"] += margin
        self.paper["committed_margin"] -= margin
        self.STATE["open"] = False
        self.STATE["current_symbol"] = None
        self.TRADE_STATE["in_position"] = False
        return True


class PortfolioSixPositionTest(unittest.TestCase):
    """The core 6-simultaneous-positions requirement with real margin commitment."""

    def setUp(self):
        os.environ["POSITION_MARGIN_PCT"] = "0.10"
        os.environ["PORTFOLIO_MARGIN_CAP_PCT"] = "0.60"
        os.environ["MAX_DAILY_LOSS_PCT"] = "5"
        os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = "2"

    def tearDown(self):
        os.environ.clear()

    def _cand(self, sym, cls, price, side="BUY"):
        return {"symbol": sym, "side": side, "price": price, "sl": price * 0.98,
                "tp1": price * 1.03, "tp2": price * 1.06, "score": 85, "atr": price * 0.01,
                "asset_class": cls, "trade_id": sym}

    def test_opens_and_manages_six_positions(self):
        e = PaperMarginEngine()
        p = PortfolioManager(6, e)
        p.bind(e)

        candidates = [
            self._cand("BTC/USDT:USDT", "CRYPTO", 60000.0),
            self._cand("ETH/USDT:USDT", "CRYPTO", 3000.0),
            self._cand("US500/USDT:USDT", "INDEX", 5000.0),
            self._cand("USTECH/USDT:USDT", "INDEX", 17000.0),
            self._cand("XAUUSD", "GOLD", 2300.0),
            self._cand("WTI", "OIL", 75.0),
            self._cand("SOL/USDT:USDT", "CRYPTO", 150.0),  # 7th -> must be rejected
        ]

        opened = p.open_top(candidates, slots=6)
        self.assertEqual(opened, 6)
        self.assertEqual(p.count(), 6)

        # 7th cannot open (global max + margin cap)
        self.assertFalse(p.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertEqual(p.count(), 6)

        # Margin accounting: free + committed == initial balance
        self.assertAlmostEqual(e.paper["balance"] + e.paper["committed_margin"], 10000.0, places=6)
        self.assertGreater(e.paper["committed_margin"], 0)

        # Class distribution: 2 crypto, 2 index, 1 gold, 1 oil
        classes = sorted(PortfolioManager._asset_class(s) for s in p.symbols())
        self.assertEqual(classes.count("CRYPTO"), 2)
        self.assertEqual(classes.count("INDEX"), 2)
        self.assertEqual(classes.count("GOLD"), 1)
        self.assertEqual(classes.count("OIL"), 1)

        # Each context is isolated and carries its own state
        for sym in p.symbols():
            self.assertTrue(p.contexts[sym].state.get("open"))
            self.assertIsNotNone(p.contexts[sym].live_manager)

        # Manage all without crash; snapshot reflects open positions
        p.manage_all()
        self.assertEqual(len(p.snapshot()), p.count())

    def test_max_crypto_positions_wins_over_master_override(self):
        # A per-class env cap must take precedence over the EVERY-class master
        # override, and the allocator (dashboard authority) must agree with the
        # open authority (PortfolioManager._class_cap) on the SAME rule.
        os.environ["MAX_CRYPTO_POSITIONS"] = "6"
        os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = "1"
        e = PaperMarginEngine()
        p = PortfolioManager(6, e)
        p.bind(e)

        five = [
            self._cand("BTC/USDT:USDT", "CRYPTO", 60000.0),
            self._cand("ETH/USDT:USDT", "CRYPTO", 3000.0, side="SELL"),
            self._cand("SOL/USDT:USDT", "CRYPTO", 150.0),
            self._cand("BNB/USDT:USDT", "CRYPTO", 600.0, side="SELL"),
            self._cand("XRP/USDT:USDT", "CRYPTO", 2.0),
        ]
        self.assertEqual(p.open_top(five, slots=5), 5)

        # The 6th CRYPTO slot comes from the per-class override (5 < 6), even
        # though the master override only allows 1 position per class.
        self.assertTrue(p.can_open("WLD/USDT:USDT", "CRYPTO"))
        # Different classes are still governed by the master override -> cap 1.
        self.assertTrue(p.can_open("US500/USDT:USDT", "INDEX"))
        self.assertTrue(p.can_open("XAUUSD", "GOLD"))

        self.assertEqual(
            p.open_top([self._cand("WLD/USDT:USDT", "CRYPTO", 15.0, side="SELL")],
                       slots=1), 1)
        self.assertEqual(p.count(), 6)

        from portfolio.allocator import GlobalAssetAllocator, class_cap_from_env
        self.assertEqual(class_cap_from_env("CRYPTO"), 6)
        self.assertEqual(class_cap_from_env("INDEX"), 1)
        self.assertEqual(class_cap_from_env("GOLD"), 1)

        # A 7th CRYPTO candidate is denied by the GLOBAL cap (limit 6), NOT by
        # the class cap — proving the per-class override is what granted the
        # sixth seat.
        alloc = GlobalAssetAllocator(p, e)
        report = alloc.allocate(
            [{"symbol": "ADA/USDT:USDT", "side": "SELL",
              "asset_class": "CRYPTO", "priority_score": 85.0}],
            limit=6,
        )
        self.assertFalse(report.decisions[0].allowed)
        self.assertIn(report.decisions[0].reason, ("SLOT_CAP", "CRYPTO_CAP"))

        # Closing frees a slot
        first = p.symbols()[0]
        before = p.count()
        self.assertTrue(p.close_symbol(first))
        self.assertEqual(p.count(), before - 1)
        self.assertTrue(p.can_open("NEW/USDT:USDT", "CRYPTO"))

        # Close everything -> free balance restored to initial (margin released)
        for sym in list(p.symbols()):
            p.close_symbol(sym)
        self.assertEqual(p.count(), 0)
        self.assertAlmostEqual(e.paper["balance"], 10000.0, places=6)
        self.assertAlmostEqual(e.paper["committed_margin"], 0.0, places=6)
