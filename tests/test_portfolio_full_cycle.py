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
  Part 3 - REAL profit taking: apply_profit_engine drives the unified 50/50
           phase model — TP1 banks exactly 50% of the INITIAL size (SL to
           breakeven, trail armed), TP2 closes the ENTIRE runner through the
           strict-close pipeline, releasing margin and booking realized PnL +
           win in PAPER_MODE; the closed context is reaped by manage_all.
           Runner partials are forbidden after TP1.
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


def _frame(n=250, base=100.0, reaction=None):
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
    # Reaction overlay (news harness): only for the NEWS symbol, and only when
    # explicitly requested, end the frame with a CLEAR directional post-news
    # move so the REAL reaction-based news scanner measures it deterministically.
    if reaction is not None:
        _d = 1.0 if str(reaction).upper() in ("BULLISH", "BUY") else -1.0
        c[n - 2] = c[n - 2] + _d * 0.4
        o[n - 1] = c[n - 2]
        c[n - 1] = c[n - 2] + _d * 0.9
        h[n - 1] = max(h[n - 1], c[n - 1])
        l[n - 1] = min(l[n - 1], c[n - 1])
        volume = np.full(n, 1000.0)
        volume[n - 1] += 400.0
    else:
        volume = np.full(n, 1000.0)
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": volume})


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


def _freshen_engine():
    """Self-isolate: rebuild the canonical engine in place before the test so
    engine state is deterministic regardless of which files ran earlier in the
    pytest process (single-position engine + professional live book)."""
    try:
        exec(compile(E.__loader__.get_source("core.engine"), E.__file__, "exec"), vars(E))
    except Exception:  # pragma: no cover - defensive
        pass


class SixSlotFullCycleTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _freshen_engine()

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _frame(
            base=_price(symbol),
            reaction=("BUY" if "NCSKNVDA2" in str(symbol) else None))
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
        # The reaction overlay models a real post-news BUY jump: the market must
        # actually trade above entry afterwards (a static flat ticker == entry
        # would make the engine's synthetic breakeven SL hit on the next tick).
        _news_sym = news_cand["symbol"]
        _react_close = float(E.get_ohlcv_safe(_news_sym, 120)["close"].iloc[-1])
        E.get_ticker_safe = lambda symbol, retries=3: (
            _react_close if str(symbol) == _news_sym else _price(symbol))
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

    @classmethod
    def setUpClass(cls):
        _freshen_engine()

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _frame(
            base=_price(symbol),
            reaction=("BUY" if "NCSKNVDA2" in str(symbol) else None))
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

    def test_tp2_closes_runner_and_releases_and_reaps(self):
        entry = self._open_buy("BTC/USDT:USDT", "CRYPTO")
        self.pm.activate("BTC/USDT:USDT")
        try:
            qty0 = E.STATE["remaining_qty"]
            df = _frame(base=entry)
            # Live mark for the paper ledger: mutable so TP1 and TP2 each book
            # their true realized PnL (TP1 banks at +0.6%, runner closes at +1.5%).
            tick = {"v": entry}
            E.get_ticker_safe = lambda symbol, retries=3: (
                tick["v"] if str(symbol).startswith("BTC")
                else float(_price(symbol)))
            tick["v"] = entry * 1.006
            E.STATE["mark_price"] = tick["v"]
            self.assertEqual(E.apply_profit_engine("BTC/USDT:USDT", tick["v"],
                                                   df, len(df) - 1, E.STATE), "TP1")
            qty1 = E.STATE["remaining_qty"]
            # Unified 50/50 model: TP1 banks exactly 50% of the INITIAL size.
            self.assertAlmostEqual(qty1, qty0 * 0.5, places=6)
            # Mark the runner deep in profit BEFORE TP2 so the runner's
            # strict-close finalize books the trade with the real exit price.
            tick["v"] = entry * 1.015
            E.STATE["mark_price"] = tick["v"]
            # TP2 = the ENTIRE runner exits through the strict-close pipeline
            # (full close, finalize included — never a second partial).
            self.assertEqual(E.apply_profit_engine("BTC/USDT:USDT", tick["v"],
                                                   df, len(df) - 1, E.STATE), "TP2")
            self.assertTrue(E.STATE["tp2_hit"])
            self.assertFalse(E.STATE.get("open"))
            self.assertAlmostEqual(E.STATE["remaining_qty"], 0.0, places=6)
            self.assertLess(E.paper["committed_margin"], 1000.0)
            self.assertGreater(E.paper["balance"], 10000.0)
            self.assertEqual(E.PERF["trades"], 1)
            self.assertEqual(E.PERF["wins"], 1)
            self.assertEqual(E.PERF["losses"], 0)
            self.assertGreater(E.PERF["total_pnl_pct"], 1.0)
        finally:
            self.pm.deactivate()
        # The management loop reaps the closed context and the slot frees.
        self.pm.manage_all()
        self.assertNotIn("BTC/USDT:USDT", self.pm.contexts)
        self.assertEqual(self.pm.count(), 0)


if __name__ == "__main__":
    unittest.main()