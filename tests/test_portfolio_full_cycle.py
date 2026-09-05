"""T6: six positions open SIMULTANEOUSLY (incl. the independent NEWS slot),
trade management loop, and REAL profit taking end to end.

The entry/exit/sizing/margin path is the production code; only the provider
boundary (OHLCV / ticker / orderbook / balance) is replaced, matching the
established T4 test convention.

  Part 1 - open six slots at once incl. NEWS (2 CRYPTO / 2 INDEX / 1 GOLD /
           1 NEWS) on the real open_candidate -> execute_entry path.
  Part 2 - the real portfolio management loop (manage_all: sync_position_state
           + live management + council exit) runs without corrupting the
           portfolio; contexts keep their live managers and snapshot() is real.
  Part 3 - REAL profit taking: apply_profit_engine books TP1 (30% close, SL to
           breakeven, trail armed) and TP2 (second 30% close), then
           finalize_trade_with_reality releases margin, books realized PnL +
           win in PAPER_MODE and the closed context is reaped by manage_all.
"""
import os
import types
import unittest

import numpy as np
import pandas as pd

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402
from portfolio.manager import PortfolioManager  # noqa: E402
from portfolio.news_slot import count_open_news, scan_for_news_candidate  # noqa: E402


PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "US500/USDT:USDT": 5000.0,
    "USTECH/USDT:USDT": 17000.0,
    "XAUUSD": 2300.0,
    "SOL/USDT:USDT": 150.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}


def _price(symbol):
    return float(PRICES.get(str(symbol), 100.0))


def _frame(n=250, base=100.0):
    """Same trending frame family the T4 tests use: passes the REAL entry
    gates (ADX in [25,38] + sell-side liquidity sweep + strong reclaim)."""
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    o[n - 2] = prior_low - 0.2
    c[n - 2] = prior_low + 0.3
    h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
    l[n - 2] = prior_low - 1.2
    o[n - 1] = prior_low + 0.1
    c[n - 1] = prior_low + 0.9
    h[n - 1] = prior_low + 1.3
    l[n - 1] = prior_low - 0.1
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


def _cand(sym, cls, side="BUY", score=88.0):
    price = _price(sym)
    atr = price * 0.01
    sl, tp1, tp2 = price - atr * 1.6, price + atr * 1.5, price + atr * 2.5
    return {"symbol": sym, "side": side, "price": price, "sl": sl, "tp1": tp1,
            "tp2": tp2, "score": score, "atr": atr, "asset_class": cls,
            "trade_id": sym}


def _news_watch(symbol, bias="BULLISH", risk=20.0):
    E.MEMORY["watchlist"][symbol] = {
        "price": _price(symbol),
        "atr": _price(symbol) * 0.01,
        "news_risk": risk,
        "news": types.SimpleNamespace(
            risk=risk, bias=bias,
            headlines=[{"impact_strength": "STRONG", "scope": "DIRECT",
                        "headline": f"{symbol} impact"}],
            as_dict=lambda: {"bias": bias, "risk": risk},
        ),
    }


class SixSlotFullCycleTest(unittest.TestCase):

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _frame(base=_price(symbol))
        E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
                       "wins": 0, "losses": 0})
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved
        perf, dash = self._saved_perf
        E.PERF.clear(); E.PERF.update(perf)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(dash)
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()

    def _open_six_including_news(self):
        _news_watch("NCSKNVDA2USD/USDT:USDT")
        news_cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(news_cand, "Strong-news candidate must be found")
        cands = [
            _cand("BTC/USDT:USDT", "CRYPTO"),
            _cand("ETH/USDT:USDT", "CRYPTO"),
            _cand("US500/USDT:USDT", "INDEX"),
            _cand("USTECH/USDT:USDT", "INDEX"),
            _cand("XAUUSD", "GOLD"),
        ]
        news_cand["side"] = "BUY"
        cands.append(news_cand)
        opened = self.pm.open_top(cands, slots=6)
        self.assertEqual(opened, 6)
        return news_cand

    def test_six_positions_open_simultaneously_including_news(self):
        self._open_six_including_news()
        self.assertEqual(self.pm.count(), 6)
        self.assertEqual(count_open_news(self.pm), 1)
        classes = {}
        for sym in self.pm.symbols():
            ctx = self.pm.contexts[sym]
            classes[ctx.asset_class] = classes.get(ctx.asset_class, 0) + 1
            self.assertTrue(ctx.state.get("open"))
            self.assertIsNotNone(ctx.live_manager)
            self.assertGreater(ctx.state.get("qty", 0), 0)
        self.assertEqual(classes, {"CRYPTO": 2, "INDEX": 2, "GOLD": 1, "NEWS": 1})
        # margin ledger stayed real through all six commits
        equity = E.paper["balance"] + E.paper["committed_margin"]
        self.assertAlmostEqual(equity, 10000.0, places=6)
        self.assertGreaterEqual(E.paper["committed_margin"], 0)
        self.assertEqual(len(self.pm.snapshot()), 6)

    def test_manage_all_runs_real_management_and_books_exits(self):
        self._open_six_including_news()
        self.assertEqual(E.PERF["trades"], 0)
        committed0 = E.paper["committed_margin"]
        self.pm.manage_all()
        # The REAL loop executed (sync_position_state + live management + exit
        # checks). The trading brain reacts to the synthetic CRYPTO frames with
        # its deterministic "aggressive profit lock" (DISTRIBUTION state) ->
        # partial + breakeven SL + instant full close for BTC/ETH. That is real
        # trade management: PERF booking, margin release and context reaping.
        before = {
            "BTC/USDT:USDT", "ETH/USDT:USDT", "US500/USDT:USDT",
            "USTECH/USDT:USDT", "XAUUSD", "NCSKNVDA2USD/USDT:USDT",
        }
        remaining = set(self.pm.symbols())
        closed = before - remaining
        # Management must actually finalize at least one position. The exact
        # symbol is intentionally not fixed because indicator implementations
        # may place a boundary regime differently across environments.
        self.assertGreaterEqual(len(closed), 1)
        self.assertEqual(self.pm.count(), 6 - len(closed))
        self.assertEqual(E.PERF["trades"], len(closed))
        self.assertEqual(E.PERF["wins"] + E.PERF["losses"], E.PERF["trades"])
        remaining_classes = {}
        for sym in self.pm.symbols():
            ctx = self.pm.contexts[sym]
            remaining_classes[ctx.asset_class] = remaining_classes.get(ctx.asset_class, 0) + 1
            self.assertTrue(ctx.state.get("open"))
            self.assertIsNotNone(ctx.live_manager)
        self.assertEqual(count_open_news(self.pm), 1)
        for sym in closed:
            self.assertNotIn(sym, self.pm.contexts)
        self.assertLess(E.paper["committed_margin"], committed0)
        self.assertAlmostEqual(E.paper["balance"] + E.paper["committed_margin"],
                               10000.0, places=6)
        snap = self.pm.snapshot()
        self.assertEqual(len(snap), self.pm.count())
        for row in snap:
            self.assertGreaterEqual(row["roe_pct"], -100)
            self.assertTrue(bool(row["side"]))

    def test_closed_position_slot_reopens_for_same_class(self):
        # Close one position for real, then its class slot MUST reopen.
        cands = [_cand("BTC/USDT:USDT", "CRYPTO"), _cand("ETH/USDT:USDT", "CRYPTO")]
        self.assertEqual(self.pm.open_top(cands, slots=2), 2)
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO")))
        self.assertTrue(self.pm.close_symbol("BTC/USDT:USDT"))
        self.assertEqual(self.pm.count(), 1)
        # Real close released the CRYPTO ledger slot -> a 3rd CRYPTO now fits.
        self.assertTrue(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
        self.assertTrue(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO")))
        self.assertEqual(self.pm.count(), 2)
        classes = sorted(ctx.asset_class for ctx in self.pm.contexts.values())
        self.assertEqual(classes.count("CRYPTO"), 2)


class ProfitTakingRealPathTest(unittest.TestCase):
    """Real profit-taking functions used by the runtime book the lifecycle:
    apply_profit_engine (TP1/TP2 partial closes) then
    finalize_trade_with_reality (margin release + realized PnL + reaping)."""

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _frame(base=_price(symbol))
        E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
                       "wins": 0, "losses": 0})
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved
        perf, dash = self._saved_perf
        E.PERF.clear(); E.PERF.update(perf)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(dash)
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()

    def _open_buy(self, sym, cls):
        self.assertTrue(self.pm.open_candidate(_cand(sym, cls, "BUY")))
        self.assertTrue(self.pm.contexts[sym].state.get("open"))
        return float(self.pm.contexts[sym].state["entry"])

    def test_tp1_books_partial_close_and_breakeven_sl(self):
        entry = self._open_buy("BTC/USDT:USDT", "CRYPTO")
        self.pm.activate("BTC/USDT:USDT")
        try:
            qty_before = E.STATE["remaining_qty"]
            df = _frame(base=entry)
            tp1_price = entry * 1.006  # +0.6% > the 0.5% profit-engine TP1 bar
            result = E.apply_profit_engine("BTC/USDT:USDT", tp1_price, df,
                                           len(df) - 1, E.STATE)
            self.assertEqual(result, "TP1")
            self.assertTrue(E.STATE["tp1_hit"])
            self.assertLess(E.STATE["remaining_qty"], qty_before)
            self.assertAlmostEqual(E.STATE["sl"], entry, places=3)  # to breakeven
            self.assertTrue(E.STATE["trail_activated"])
        finally:
            self.pm.deactivate()

    def test_tp2_books_second_partial_then_finalize_releases_and_reaps(self):
        entry = self._open_buy("BTC/USDT:USDT", "CRYPTO")
        self.pm.activate("BTC/USDT:USDT")
        try:
            qty0 = E.STATE["remaining_qty"]
            df = _frame(base=entry)
            self.assertEqual(E.apply_profit_engine("BTC/USDT:USDT", entry * 1.006,
                                                   df, len(df) - 1, E.STATE), "TP1")
            qty1 = E.STATE["remaining_qty"]
            self.assertAlmostEqual(qty1, qty0 * 0.7, places=6)
            self.assertEqual(E.apply_profit_engine("BTC/USDT:USDT", entry * 1.015,
                                                   df, len(df) - 1, E.STATE), "TP2")
            self.assertTrue(E.STATE["tp2_hit"])
            self.assertLess(E.STATE["remaining_qty"], qty1)
            margin_committed = E.paper["committed_margin"]
            balance0 = E.paper["balance"]
            # mark the price in profit, then the REAL finalize books the closed
            # trade with reality: margin released + realized win in PERF.
            E.STATE["mark_price"] = entry * 1.015
            target_tick = entry * 1.015
            E.get_ticker_safe = lambda symbol, retries=3: (
                target_tick if str(symbol).startswith("BTC")
                else float(_price(symbol)))
            pnl_usdt, pnl_pct = E.finalize_trade_with_reality("BTC/USDT:USDT")
            self.assertGreater(pnl_pct, 1.0)
            self.assertGreater(pnl_usdt, 0)
            self.assertFalse(E.STATE.get("open"))
            self.assertLess(E.paper["committed_margin"], margin_committed)
            self.assertGreater(E.paper["balance"], balance0)
            self.assertEqual(E.PERF["trades"], 1)
            self.assertEqual(E.PERF["wins"], 1)
            self.assertEqual(E.PERF["losses"], 0)
            self.assertGreaterEqual(E.PERF["total_pnl_usdt"], 0)
        finally:
            self.pm.deactivate()
        # The management loop reaps the closed context and the slot frees.
        self.pm.manage_all()
        self.assertNotIn("BTC/USDT:USDT", self.pm.contexts)
        self.assertEqual(self.pm.count(), 0)


if __name__ == "__main__":
    unittest.main()