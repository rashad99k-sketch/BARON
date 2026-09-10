"""Portfolio tests adapted for v2 Trade-based architecture.

The old tests tested the activate/deactivate state-swapping pattern.
These are updated to test the new Trade entity + coordinator architecture.
"""
import os
import unittest

from portfolio.manager import PortfolioManager
from core.trade import Trade, TradeStatus


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
        self._TRADE_LOCK = __import__("threading").RLock()
        self.MEMORY = {}
        self.PERF = {"trades": 0, "last_trade": None}

    def log_execution(self, *a, **k):
        pass

    def get_balance_safe(self):
        return 10000.0

    def get_equity_safe(self):
        return 10000.0


class PortfolioIsolationTest(unittest.TestCase):
    def test_symbol_trades_are_isolated(self):
        """v2: Each trade is independent via Trade entities."""
        e = FakeManager()
        p = PortfolioManager(2, e)
        p.bind(e)

        # Register trades directly
        t1 = Trade(symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
                   entry_price=50000.0, status=TradeStatus.FILLED)
        t2 = Trade(symbol="ETH/USDT:USDT", side="SELL", asset_class="CRYPTO",
                   entry_price=3000.0, status=TradeStatus.FILLED)
        p._trades[t1.trade_id] = t1
        p._trades[t2.trade_id] = t2
        p._sync_legacy_contexts()

        self.assertEqual(p.count(), 2)
        self.assertIn("BTC/USDT:USDT", p.symbols())
        self.assertIn("ETH/USDT:USDT", p.symbols())

    def test_activate_deactivate_are_noops(self):
        """v2: activate/deactivate are no-ops (backward compat)."""
        e = FakeManager()
        p = PortfolioManager(2, e)
        p.bind(e)
        # These should not raise
        p.activate("BTC")
        p.deactivate()


class PortfolioCapacityPolicyTest(unittest.TestCase):
    def test_asset_class_cap_is_enforced(self):
        os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = "2"
        try:
            e = FakeManager()
            p = PortfolioManager(6, e)
            p.bind(e)
            # Simulate 2 CRYPTO positions
            t1 = Trade(symbol="BTC/USDT:USDT", side="BUY", asset_class="CRYPTO",
                       status=TradeStatus.FILLED)
            t2 = Trade(symbol="ETH/USDT:USDT", side="BUY", asset_class="CRYPTO",
                       status=TradeStatus.FILLED)
            p._trades[t1.trade_id] = t1
            p._trades[t2.trade_id] = t2
            self.assertFalse(p.can_open("SOL/USDT:USDT", "CRYPTO"))
            self.assertTrue(p.can_open("AAPL", "STOCK"))
        finally:
            os.environ.pop("MAX_POSITIONS_PER_ASSET_CLASS", None)


class PaperMarginEngine:
    """Fake engine with realistic 6-position margin accounting."""

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
        self._TRADE_LOCK = __import__("threading").RLock()

    def get_balance_safe(self, *a, **k):
        return self.paper["balance"]

    def get_equity_safe(self):
        return self.paper["balance"] + self.paper["committed_margin"]

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
            "margin": margin, "entry_time": __import__("time").time(),
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

    def get_ticker_safe(self, symbol):
        return 50000.0

    def get_ohlcv_safe(self, symbol, limit):
        return None

    def close_position_full(self):
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

    def test_opens_six_positions(self):
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
            self._cand("SOL/USDT:USDT", "CRYPTO", 150.0),
        ]

        opened = p.open_top(candidates, slots=6)
        self.assertEqual(opened, 6)
        self.assertEqual(p.count(), 6)

        # 7th cannot open
        self.assertFalse(p.can_open("SOL/USDT:USDT", "CRYPTO"))

        # Margin accounting
        self.assertAlmostEqual(e.paper["balance"] + e.paper["committed_margin"], 10000.0, places=6)
        self.assertGreater(e.paper["committed_margin"], 0)

        # Class distribution
        classes = sorted(PortfolioManager._asset_class(s) for s in p.symbols())
        self.assertEqual(classes.count("CRYPTO"), 2)
        self.assertEqual(classes.count("INDEX"), 2)
        self.assertEqual(classes.count("GOLD"), 1)
        self.assertEqual(classes.count("OIL"), 1)

        # Snapshot
        self.assertEqual(len(p.snapshot()), p.count())

    def test_close_frees_slot(self):
        e = PaperMarginEngine()
        p = PortfolioManager(6, e)
        p.bind(e)

        candidates = [
            self._cand("BTC/USDT:USDT", "CRYPTO", 60000.0),
            self._cand("ETH/USDT:USDT", "CRYPTO", 3000.0),
        ]
        p.open_top(candidates, slots=2)
        self.assertEqual(p.count(), 2)

        first = p.symbols()[0]
        self.assertTrue(p.close_symbol(first))
        self.assertEqual(p.count(), 1)
        self.assertTrue(p.can_open("SOL/USDT:USDT", "CRYPTO"))

    def test_max_crypto_positions_wins_over_master_override(self):
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

        # The 6th CRYPTO slot comes from the per-class override
        self.assertTrue(p.can_open("WLD/USDT:USDT", "CRYPTO"))

        from portfolio.allocator import GlobalAssetAllocator, class_cap_from_env
        self.assertEqual(class_cap_from_env("CRYPTO"), 6)
        self.assertEqual(class_cap_from_env("INDEX"), 1)
        self.assertEqual(class_cap_from_env("GOLD"), 1)


if __name__ == "__main__":
    unittest.main()
