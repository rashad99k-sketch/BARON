"""T4: REAL six-market + independent NEWS slot execution.

Exercises the production open path end to end:
PortfolioManager.can_open (real per-class caps + risk guard + portfolio margin)
-> engine.execute_entry (real ADX / liquidity-sweep gate, real 10%-of-free
margin commit, real portfolio margin-cap ledger) -> context stored.

Only the network/provider boundary (OHLCV / ticker / orderbook) is replaced;
the scanner->queue->execute machinery, sizing, margin accounting and class
classification are the real production code.
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

import core.engine as E  # noqa: E402  (real engine)
from portfolio.manager import PortfolioManager  # noqa: E402
from portfolio.news_slot import count_open_news, scan_for_news_candidate  # noqa: E402


PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "SOL/USDT:USDT": 150.0,
    "US500/USDT:USDT": 5000.0,
    "USTECH/USDT:USDT": 17000.0,
    "XAUUSD": 2300.0,
    "OILWTI": 75.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
}


def _price(symbol):
    return float(PRICES.get(str(symbol), 100.0))


def _frame(n=250, side="BUY", base=100.0, reaction=None):
    """Trending frame shaped so the REAL execute_entry gates approve:
    ADX inside [25,38] and the tail candle sweeps the relevant liquidity
    (low-side sweep for BUY, high-side sweep for SELL) so
    detect_liquidity_context returns sell_side_taken / buy_side_taken."""
    t = np.arange(n)
    x = base + 3.0 * (1 - np.exp(-t / 900.0)) + 1.5 * np.sin(t / 6.0)
    o = x - 0.2
    c = x
    h = np.maximum(o, c) + 0.4
    l = np.minimum(o, c) - 0.4
    prior_low = l[n - 3]
    prior_hi = h[n - 3]
    if str(side).upper() == "BUY":
        o[n - 2] = prior_low - 0.2
        c[n - 2] = prior_low + 0.3
        h[n - 2] = max(prior_hi - 0.1, prior_low + 0.5)
        l[n - 2] = prior_low - 1.2
        o[n - 1] = prior_low + 0.1
        c[n - 1] = prior_low + 0.9
        h[n - 1] = prior_low + 1.3
        l[n - 1] = prior_low - 0.1
    else:
        o[n - 2] = prior_hi - 0.3
        c[n - 2] = prior_hi - 0.2
        l[n - 2] = max(prior_low + 0.1, prior_hi - 0.5)
        h[n - 2] = prior_hi + 1.2
        o[n - 1] = prior_hi - 0.1
        c[n - 1] = prior_hi - 0.9
        h[n - 1] = prior_hi + 0.1
        l[n - 1] = prior_hi - 1.3
# Reaction overlay (news harness): ONLY when a news reaction is requested, end
    # the last two bars with a CLEAR directional post-news move so the REAL
    # reaction-based news scanner measures it deterministically. Technical
    # frames keep their original shape (baseline management behavior).
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
    return pd.DataFrame({
        "timestamp": t, "open": o, "high": h, "low": l, "close": c,
        "volume": volume,
    })


def _cand(sym, cls, side, score=88.0):
    price = _price(sym)
    atr = price * 0.01
    if side == "SELL":
        sl, tp1, tp2 = price + atr * 1.6, price - atr * 1.5, price - atr * 2.5
    else:
        sl, tp1, tp2 = price - atr * 1.6, price + atr * 1.5, price + atr * 2.5
    return {"symbol": sym, "side": side, "price": price, "sl": sl, "tp1": tp1, "tp2": tp2,
            "score": score, "atr": atr, "asset_class": cls, "trade_id": sym}


def _news_assessment(symbol, bias="BULLISH", risk=20.0, strong=True):
    return types.SimpleNamespace(
        risk=risk,
        bias=bias,
        headlines=[{"impact_strength": "STRONG" if strong else "LOW",
                    "scope": "DIRECT", "headline": f"{symbol} impact"}],
        as_dict=lambda: {"bias": bias, "risk": risk},
    )


class SixMarketSlotExecutionTest(unittest.TestCase):
    """The 6 technical slots (2 CRYPTO / 2 INDEX / 1 GOLD / 1 OIL) open on the
    REAL portfolio path, the 7th cannot open, and margin accounting is real."""

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self.sides = {}

        def frame_provider(symbol, limit=120, htf=False):
            _sym = str(symbol)
            _nw = E.MEMORY.get("watchlist", {}).get(_sym)
            _react = "BUY" if (_nw is not None and "news" in _nw) else None
            return _frame(side=self.sides.get(_sym, "BUY"),
                          base=_price(symbol), reaction=_react)
        E.get_ohlcv_safe = frame_provider
        E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]

        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()

    def _open_one(self, sym, cls, side):
        self.sides[sym] = side
        return self.pm.open_candidate(_cand(sym, cls, side))

    def test_six_technical_slots_open_with_exact_class_mix(self):
        cands = [
            _cand("BTC/USDT:USDT", "CRYPTO", "BUY"),
            _cand("ETH/USDT:USDT", "CRYPTO", "BUY"),
            _cand("US500/USDT:USDT", "INDEX", "BUY"),
            _cand("USTECH/USDT:USDT", "INDEX", "BUY"),
            _cand("XAUUSD", "GOLD", "BUY"),
            _cand("OILWTI", "OIL", "SELL"),
        ]
        for c in cands:
            self.sides[c["symbol"]] = c["side"]
        opened = self.pm.open_top(cands, slots=6)
        self.assertEqual(opened, 6)
        self.assertEqual(self.pm.count(), 6)
        classes = sorted(PortfolioManager._asset_class(s) for s in self.pm.symbols())
        self.assertEqual(classes.count("CRYPTO"), 2)
        self.assertEqual(classes.count("INDEX"), 2)
        self.assertEqual(classes.count("GOLD"), 1)
        self.assertEqual(classes.count("OIL"), 1)
        for sym in self.pm.symbols():
            ctx = self.pm.contexts[sym]
            self.assertTrue(ctx.state.get("open"))
            self.assertIsNotNone(ctx.live_manager)

    def test_third_crypto_rejected_while_other_slots_remain(self):
        self.assertTrue(self._open_one("BTC/USDT:USDT", "CRYPTO", "BUY"))
        self.assertTrue(self._open_one("ETH/USDT:USDT", "CRYPTO", "BUY"))
        self.sides["SOL/USDT:USDT"] = "BUY"
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        self.assertFalse(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO", "BUY")))
        self.assertTrue(self.pm.can_open("XAUUSD", "GOLD"))
        self.assertEqual(self.pm.count(), 2)

    def test_six_total_positions_can_include_one_news_slot(self):
        # Production portfolio contract: MAX_OPEN_POSITIONS=6 is the total
        # capacity. The NEWS class is independent of technical class caps,
        # but it still consumes one of the six global positions.
        technical = [
            _cand("BTC/USDT:USDT", "CRYPTO", "BUY"),
            _cand("ETH/USDT:USDT", "CRYPTO", "BUY"),
            _cand("US500/USDT:USDT", "INDEX", "BUY"),
            _cand("USTECH/USDT:USDT", "INDEX", "BUY"),
            _cand("XAUUSD", "GOLD", "BUY"),
        ]
        for c in technical:
            self.sides[c["symbol"]] = c["side"]
        self.assertEqual(self.pm.open_top(technical, slots=5), 5)

        E.MEMORY["watchlist"] = {
            "NCSKNVDA2USD/USDT:USDT": {
                "price": _price("NCSKNVDA2USD/USDT:USDT"),
                "atr": _price("NCSKNVDA2USD/USDT:USDT") * 0.01,
                "news_risk": 20.0,
                "news": _news_assessment("NCSKNVDA2USD/USDT:USDT", "BULLISH", 20.0, True),
            }
        }
        news = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(news)
        self.sides[news["symbol"]] = news["side"]
        self.assertTrue(self.pm.open_candidate(news))

        self.assertEqual(self.pm.count(), 6)
        self.assertEqual(count_open_news(self.pm), 1)
        self.assertEqual(sum(1 for c in self.pm.contexts.values() if c.asset_class == "NEWS"), 1)
        self.assertTrue(all(c.live_manager is not None for c in self.pm.contexts.values()))
        self.assertFalse(self.pm.can_open("OILWTI", "OIL"))

    def test_seventh_position_rejected_by_global_and_margin_policy(self):
        cands = [
            _cand("BTC/USDT:USDT", "CRYPTO", "BUY"),
            _cand("ETH/USDT:USDT", "CRYPTO", "BUY"),
            _cand("US500/USDT:USDT", "INDEX", "BUY"),
            _cand("USTECH/USDT:USDT", "INDEX", "BUY"),
            _cand("XAUUSD", "GOLD", "BUY"),
            _cand("OILWTI", "OIL", "SELL"),
        ]
        for c in cands:
            self.sides[c["symbol"]] = c["side"]
        self.assertEqual(self.pm.open_top(cands, slots=6), 6)
        self.sides["SOL/USDT:USDT"] = "BUY"
        self.assertFalse(self.pm.can_open("SOL/USDT:USDT", "CRYPTO"))
        status = self.pm.risk_guard.status("SOL/USDT:USDT", self.pm.count())
        self.assertEqual(status.reason, "PORTFOLIO_MARGIN_CAP")
        self.assertFalse(self.pm.open_candidate(_cand("SOL/USDT:USDT", "CRYPTO", "BUY")))
        self.assertEqual(self.pm.count(), 6)

    def test_margin_accounting_is_real_and_under_cap(self):
        for sym, cls, side in [
            ("BTC/USDT:USDT", "CRYPTO", "BUY"),
            ("ETH/USDT:USDT", "CRYPTO", "BUY"),
            ("US500/USDT:USDT", "INDEX", "BUY"),
            ("USTECH/USDT:USDT", "INDEX", "BUY"),
            ("XAUUSD", "GOLD", "BUY"),
            ("OILWTI", "OIL", "SELL"),
        ]:
            self.assertTrue(self._open_one(sym, cls, side))
            bal = E.paper["balance"]
            committed = E.paper["committed_margin"]
            equity = bal + committed
            self.assertAlmostEqual(equity, 10000.0, places=6)
            self.assertLessEqual(committed, E.PORTFOLIO_MARGIN_CAP_PCT * equity + 1e-9)
        self.assertAlmostEqual(E.paper["committed_margin"], 4685.59, places=2)
        self.assertGreater(E.paper["balance"], 0)

    def test_direct_entry_blocked_by_ledger_margin_cap(self):
        # Simulate a portfolio already near the real 60% cap: a direct entry
        # (no manager in the call path) must be blocked by the actual ledger.
        E.paper["balance"] = 4200.0
        E.paper["committed_margin"] = 5800.0
        self.sides["BTC/USDT:USDT"] = "BUY"
        ok = E.execute_entry(
            "BUY", "BTC/USDT:USDT", 60000.0, 59000.0, 63000.0, 66000.0,
            88, "TEST_MARGIN_CAP", 600.0, "INSTITUTIONAL", "DEEP_SCANNER", "SNIPER",
        )
        self.assertFalse(ok)
        self.assertFalse(E.STATE.get("open"))

    def test_can_open_uses_six_market_model_not_999(self):
        saved_env = os.environ.pop("MAX_POSITIONS_PER_ASSET_CLASS", None)
        try:
            self.assertEqual(PortfolioManager._class_cap("CRYPTO"), 2)
            self.assertEqual(PortfolioManager._class_cap("INDEX"), 2)
            self.assertEqual(PortfolioManager._class_cap("GOLD"), 1)
            self.assertEqual(PortfolioManager._class_cap("OIL"), 1)
            self.assertEqual(PortfolioManager._class_cap("NEWS"), 1)
            self.assertEqual(PortfolioManager._class_cap("STOCK"), 0)
            self.assertFalse(self.pm.can_open("NCSKNVDA2USD/USDT:USDT", "STOCK"))
        finally:
            if saved_env is not None:
                os.environ["MAX_POSITIONS_PER_ASSET_CLASS"] = saved_env


class NewsSlotExecutionTest(unittest.TestCase):
    """The independent NEWS slot opens on the REAL path without consuming a
    technical class cap, and is capped at exactly one open news position."""

    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self.sides = {}

        def frame_provider(symbol, limit=120, htf=False):
            _sym = str(symbol)
            _nw = E.MEMORY.get("watchlist", {}).get(_sym)
            _react = "BUY" if (_nw is not None and "news" in _nw) else None
            return _frame(side=self.sides.get(_sym, "BUY"),
                          base=_price(symbol), reaction=_react)
        E.get_ohlcv_safe = frame_provider
        E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
        E.get_orderbook_cached = lambda *a, **k: {
            "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
        E.get_balance_safe = lambda retries=3: E.paper["balance"]

        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0

    def tearDown(self):
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()

    def _watch_with_news(self, symbol, **kw):
        E.MEMORY["watchlist"][symbol] = {
            "price": _price(symbol),
            "atr": _price(symbol) * 0.01,
            "news_risk": kw.get("risk", 20.0),
            "news": _news_assessment(symbol, kw.get("bias", "BULLISH"),
                                     kw.get("risk", 20.0), kw.get("strong", True)),
        }

    def test_news_candidate_opens_without_consuming_class_cap(self):
        # Strong BULLISH news on a STOCK symbol (no stock slot exists): the
        # NEWS slot qualifies it independently, opening as its own class.
        self._watch_with_news("NCSKNVDA2USD/USDT:USDT", bias="BULLISH", risk=20.0)
        cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(cand)
        self.assertEqual(cand["asset_class"], "NEWS")
        self.assertEqual(cand["side"], "BUY")
        self.sides[cand["symbol"]] = cand["side"]
        self.assertTrue(self.pm.open_candidate(cand))
        self.assertEqual(count_open_news(self.pm), 1)
        classes = sorted(ctx.asset_class for ctx in self.pm.contexts.values())
        self.assertEqual(classes.count("NEWS"), 1)
        # A technical GOLD slot is still available: news did NOT consume it.
        self.assertTrue(self._open_one_gold())
        self.assertEqual(self.pm.count(), 2)
        self.assertIn("NEWS", {ctx.asset_class for ctx in self.pm.contexts.values()})
        self.assertIn("GOLD", {ctx.asset_class for ctx in self.pm.contexts.values()})

    def _open_one_gold(self):
        self.sides["XAUUSD"] = "BUY"
        return self.pm.open_candidate(_cand("XAUUSD", "GOLD", "BUY"))

    def test_no_strong_news_means_no_news_candidate(self):
        self._watch_with_news("BTC/USDT:USDT", bias="NEUTRAL", risk=90.0, strong=False)
        cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNone(cand)

    def test_second_news_position_rejected(self):
        self._watch_with_news("NCSKNVDA2USD/USDT:USDT", bias="BULLISH", risk=20.0)
        first = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.sides[first["symbol"]] = first["side"]
        self.assertTrue(self.pm.open_candidate(first))
        self.assertEqual(count_open_news(self.pm), 1)
        # A second, different strong-news symbol still hits the NEWS slot cap.
        self._watch_with_news("BTC/USDT:USDT", bias="BEARISH", risk=15.0)
        second_wl = {k: v for k, v in E.MEMORY["watchlist"].items() if k != first["symbol"]}
        second = scan_for_news_candidate(second_wl)
        self.assertIsNotNone(second)
        self.assertNotEqual(second["symbol"], first["symbol"])
        self.assertFalse(self.pm.can_open(second["symbol"], "NEWS"))
        self.assertFalse(self.pm.open_candidate(second))
        self.assertEqual(count_open_news(self.pm), 1)


if __name__ == "__main__":
    unittest.main()