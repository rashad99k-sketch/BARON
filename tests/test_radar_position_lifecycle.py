"""Radar discovery -> six-slot positions -> REAL profit taking -> management.

End-to-end contract lock for the Early Institutional Radar (ATOM v5.2): its
staggered discovery cycle is the FEEDER of the real PortfolioManager. The radar
tier runs against its established deterministic provider/model boundary (the
same stubs as test_early_institutional_radar); everything downstream of the
discovered watchlist runs on the REAL production path against the shared
core.engine instance:

  Part 1 - radar cycle seeds a 5-symbol near-OB watchlist carrying the exact
           fields the execution layer consumes (price, asset_class, side).
  Part 2 - those RADAR candidates (2 CRYPTO / 2 INDEX / 1 GOLD) plus the
           independent NEWS slot open six positions on the real entry path.
  Part 3 - REAL profit taking: apply_profit_engine books TP1 (30% partial,
           SL to breakeven, trail armed) then TP2 (second partial), and
           finalize_trade_with_reality releases margin and books the win.
  Part 4 - manage_all() runs the real position-management loop over the six
           slots (sync + live management + council exits) without corrupting
           the portfolio; freed slots leave room for the next radar rotation.
"""
import importlib
import os
import sys
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

# Re-import DeepScanner on top of the shared E the portfolio layer already uses,
# so one provider boundary feeds both discovery and execution.
sys.modules.pop("scanner.deep_scanner", None)
importlib.invalidate_caches()
from scanner.deep_scanner import DeepScanner  # noqa: E402

PRICES = {
    "BTC/USDT:USDT": 60000.0,
    "ETH/USDT:USDT": 3000.0,
    "US500/USDT:USDT": 5000.0,
    "USTECH/USDT:USDT": 17000.0,
    "XAUUSD": 2300.0,
    "NCSKNVDA2USD/USDT:USDT": 130.0,
    "NOISE000/USDT:USDT": 100.0,
    "NOISE001/USDT:USDT": 100.0,
    "NOISE002/USDT:USDT": 100.0,
    "NOISE003/USDT:USDT": 100.0,
    "NOISE004/USDT:USDT": 100.0,
}

# The five technical symbols the radar must surface (near-OB, strong movement).
# US500/USTECH classify as INDEX via the real scanner.universe.classify, giving
# the slot model a genuine 2 CRYPTO / 2 INDEX / 1 GOLD mix. The independent
# NEWS symbol is NOT radar-qualified: it is fed by the news slot.
QUALIFIED = {"BTC/USDT:USDT", "ETH/USDT:USDT",
             "US500/USDT:USDT", "USTECH/USDT:USDT", "XAUUSD"}

# Frames that must pass the real entry gates (trending family). The news symbol
# needs one too, or the real sell-side-liquidity-sweep gate rejects it.
FRAME_STRONG = QUALIFIED | {"NCSKNVDA2USD/USDT:USDT"}

RADAR_MARKETS = {
    "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "type": "swap", "active": True},
    "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "type": "swap", "active": True},
    "US500/USDT:USDT": {"base": "US500", "quote": "USDT", "type": "swap", "active": True},
    "USTECH/USDT:USDT": {"base": "USTECH", "quote": "USDT", "type": "swap", "active": True},
    "XAUUSD": {"base": "XAUUSD", "quote": "USD", "type": "swap", "active": True},
    "NOISE000/USDT:USDT": {"base": "NOISE000", "quote": "USDT", "type": "swap", "active": True},
    "NOISE001/USDT:USDT": {"base": "NOISE001", "quote": "USDT", "type": "swap", "active": True},
    "NOISE002/USDT:USDT": {"base": "NOISE002", "quote": "USDT", "type": "swap", "active": True},
    "NOISE003/USDT:USDT": {"base": "NOISE003", "quote": "USDT", "type": "swap", "active": True},
    "NOISE004/USDT:USDT": {"base": "NOISE004", "quote": "USDT", "type": "swap", "active": True},
}


def _price(symbol):
    return float(PRICES.get(str(symbol), 100.0))


def _frame(base: float, n: int = 250) -> pd.DataFrame:
    """Same trending frame family the T4/T6 tests use: passes all entry gates."""
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


def _quiet_frame(base: float, n: int = 250) -> pd.DataFrame:
    t = np.arange(n)
    c = np.full(n, base) + np.sin(t / 5.0) * 0.02
    return pd.DataFrame({"timestamp": t, "open": c - 0.1, "high": c + 0.2,
                         "low": c - 0.2, "close": c, "volume": np.full(n, 1000.0)})


class FakeExchange:
    def __init__(self):
        self.markets = RADAR_MARKETS

    def load_markets(self):
        return self.markets

    def fetch_tickers(self):
        return {}


def _ohlcv(symbol, limit=120, htf=False):
    df = _frame(base=_price(symbol)) if symbol in FRAME_STRONG \
        else _quiet_frame(base=_price(symbol))
    df.symbol = symbol
    return df


def _ticker(symbol, retries=3):
    return _price(symbol)


def _orderbook(symbol, limit=10):
    return {"bids": [[_price(symbol) - 1.0, 10.0]], "asks": [[_price(symbol) + 1.0, 5.0]]}


def _balance(retries=3):
    return E.paper["balance"]


class _RF:
    def compute(self, df):
        sym = getattr(df, "symbol", "")
        if sym in QUALIFIED:
            return {"triggered": True, "distance": 0.001, "signal": "BUY"}
        return {"triggered": False, "distance": 0.01, "signal": "NEUTRAL"}


def _fake_rf(period=20, multiplier=3.5):
    return _RF()


def _fake_atr(df):
    return pd.Series(np.full(len(df), 1.0), index=df.index)


def _fake_adx(df):
    sym = getattr(df, "symbol", "")
    v = 26.0 if sym in QUALIFIED else 8.0
    return pd.Series(np.full(len(df), v), index=df.index)


def _fake_momentum(df):
    sym = getattr(df, "symbol", "")
    return {"trend_expansion": sym in QUALIFIED,
            "flow_bias": "BUY" if sym in QUALIFIED else "NEUTRAL"}


def _fake_smart(df):
    sym = getattr(df, "symbol", "")
    return {"smart_money_dominant": sym in QUALIFIED,
            "institutional_bias": "BUY" if sym in QUALIFIED else "NEUTRAL",
            "distribution_risk": 10}


def _fake_zones(sym, df, ob=None):
    price = float(df["close"].iloc[-1])
    if sym in QUALIFIED:
        return {"buy_zones": [{"price": price * 0.999, "strength": 80}],
                "sell_zones": [{"price": price * 1.02, "strength": 20}]}
    return {"buy_zones": [{"price": price * 1.25, "strength": 5}],
            "sell_zones": [{"price": price * 1.30, "strength": 5}]}


def _fake_regime(df):
    return {"regime": "TRENDING" if getattr(df, "symbol", "") in QUALIFIED else "CHOPPY"}


def _radar_cand(entry):
    """Bridge a radar watchlist row into the execution candidate contract."""
    sym = entry["symbol"]
    price = _price(sym)
    atr = price * 0.01
    side = entry.get("candidate_side") or "BUY"
    if side == "BUY":
        sl, tp1, tp2 = price - atr * 1.6, price + atr * 1.5, price + atr * 2.5
    else:
        sl, tp1, tp2 = price + atr * 1.6, price - atr * 1.5, price - atr * 2.5
    return {"symbol": sym, "side": side, "price": price, "sl": sl,
            "tp1": tp1, "tp2": tp2,
            # Execution score is produced downstream by the strategy layer,
            # never by discovery; keep the canonical suite value here.
            "score": 88.0, "atr": atr, "asset_class": entry["asset_class"],
            "trade_id": sym, "near_ob": entry.get("near_ob"),
            "radar_rank": entry.get("radar_rank"),
            "radar_score": entry.get("radar_score")}


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


class RadarPositionLifecycleTest(unittest.TestCase):
    """Radar discovery feeds the REAL six-slot position + profit-taking path."""

    _model_names = ("RFEngine", "MomentumFlowEngine", "SmartMoneyEngine",
                    "compute_atr", "compute_adx", "get_smart_zones",
                    "detect_market_regime")

    def setUp(self):
        self._saved = {n: getattr(E, n) for n in self._model_names}
        self._saved_prov = (E.get_ohlcv_safe, E.get_ticker_safe,
                            E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        E.get_ohlcv_safe = _ohlcv
        E.get_ticker_safe = _ticker
        E.get_orderbook_cached = _orderbook
        E.get_balance_safe = _balance
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        E.MEMORY.setdefault("pipeline", {})
        E.queue._candidates.clear()
        E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
                       "wins": 0, "losses": 0})
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self.pm.risk_guard._day = None
        self.pm.risk_guard._consecutive_losses = 0
        self.pm.risk_guard._cooldown_until = 0.0
        self._prev_wl = os.environ.get("DEEP_WATCHLIST_SIZE")
        os.environ["DEEP_WATCHLIST_SIZE"] = "5"

    def tearDown(self):
        for name, original in self._saved.items():
            try:
                setattr(E, name, original)
            except Exception:
                pass
        (E.get_ohlcv_safe, E.get_ticker_safe,
         E.get_orderbook_cached, E.get_balance_safe) = self._saved_prov
        perf, dash = self._saved_perf
        E.PERF.clear(); E.PERF.update(perf)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(dash)
        E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}
        E.STATE.clear()
        E.TRADE_STATE.clear()
        E.MEMORY.setdefault("watchlist", {}).clear()
        if self._prev_wl is None:
            os.environ.pop("DEEP_WATCHLIST_SIZE", None)
        else:
            os.environ["DEEP_WATCHLIST_SIZE"] = self._prev_wl

    # --- radar model boundary: patch only for the discovery phase ----------
    def _patch_radar_models(self):
        E.RFEngine = _fake_rf
        E.MomentumFlowEngine = types.SimpleNamespace(analyze_momentum_flow=_fake_momentum)
        E.SmartMoneyEngine = types.SimpleNamespace(analyze_smart_money=_fake_smart)
        E.compute_atr = _fake_atr
        E.compute_adx = _fake_adx
        E.get_smart_zones = _fake_zones
        E.detect_market_regime = _fake_regime

    def _restore_radar_models(self):
        for name, original in self._saved.items():
            try:
                setattr(E, name, original)
            except Exception:
                pass

    def _radar_watchlist(self):
        """Run the REAL staggered radar cycle over the 10-symbol universe and
        seed the 5-target near-OB watchlist. Returns (watchlist, scanner)."""
        self._patch_radar_models()
        scanner = DeepScanner(max_symbols=5)
        scanner.radar_symbols = 0  # cover the whole injected universe
        scanner.exchange = FakeExchange()
        scanner._market_loader = lambda: RADAR_MARKETS
        try:
            watch = scanner.scan(force=True)
        finally:
            self._restore_radar_models()
        self.assertEqual(scanner.status["radar_cycle"], "COMPLETE")
        self.assertEqual(scanner.status["watchlist"], "HEALTHY")
        return watch, scanner

    # --- Part 1: radar deliverable + execution bridge contract -------------
    def test_radar_seeds_five_executable_near_ob_symbols(self):
        watch, scanner = self._radar_watchlist()
        self.assertEqual(len(watch), 5)
        self.assertEqual(scanner.stats["radar_scanned"], 10)
        for w in watch:
            self.assertIn(w["symbol"], QUALIFIED)
            self.assertTrue(w["near_ob"])
            self.assertEqual(w["prox_side"], "BUY")
            self.assertGreater(w["radar_rank"], 0)
            # Execution-layer contract the candidate bridge consumes:
            self.assertIsNotNone(w.get("price"))
            self.assertIsNotNone(w.get("asset_class"))
            self.assertIn(w.get("candidate_side"), ("BUY", "SELL"))
        classes = sorted({w["asset_class"] for w in watch})
        self.assertEqual(classes, ["CRYPTO", "GOLD", "INDEX"])
        pipe = E.MEMORY.get("pipeline", {}).get("radar", {})
        self.assertEqual(pipe["scanned"], 10)
        self.assertEqual(sum(1 for w in watch if w["near_ob"]), 5)
        # Radar filter proof: the 5 noise symbols never made the watchlist.
        self.assertEqual({w["symbol"] for w in watch} & {"NOISE000/USDT:USDT"}, set())

    # --- Part 2: six simultaneous slots fed by radar candidates -------------
    def test_six_open_from_radar_candidates_including_news(self):
        watch, _scanner = self._radar_watchlist()
        cands = [_radar_cand(w) for w in watch]
        # Every radar symbol carries the class/side/price the six-slot model maps.
        for c in cands:
            self.assertIn(c["asset_class"], ("CRYPTO", "INDEX", "GOLD"))
            self.assertTrue(c["near_ob"])
        _news_watch("NCSKNVDA2USD/USDT:USDT")
        news_cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(news_cand, "Strong-news candidate must be found")
        news_cand["side"] = "BUY"
        cands.append(news_cand)
        opened = self.pm.open_top(cands, slots=6)
        self.assertEqual(opened, 6)
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
        equity = E.paper["balance"] + E.paper["committed_margin"]
        self.assertAlmostEqual(equity, 10000.0, places=6)
        self.assertEqual(len(self.pm.snapshot()), 6)

    # --- Part 3: REAL profit taking on a radar-derived position -------------
    def test_profit_taking_books_tp1_tp2_and_releases_margin(self):
        watch, _scanner = self._radar_watchlist()
        entry_row = next(w for w in watch if w["symbol"] == "BTC/USDT:USDT")
        self.assertTrue(self.pm.open_candidate(_radar_cand(entry_row)))
        self.assertTrue(self.pm.contexts["BTC/USDT:USDT"].state.get("open"))
        self.pm.activate("BTC/USDT:USDT")
        try:
            entry = float(self.pm.contexts["BTC/USDT:USDT"].state["entry"])
            qty0 = E.STATE["remaining_qty"]
            df = _frame(base=entry)
            # TP1: +0.6% > the 0.5% profit-engine bar -> partial + breakeven.
            self.assertEqual(E.apply_profit_engine("BTC/USDT:USDT", entry * 1.006,
                                                   df, len(df) - 1, E.STATE), "TP1")
            self.assertTrue(E.STATE["tp1_hit"])
            self.assertLess(E.STATE["remaining_qty"], qty0)
            self.assertAlmostEqual(E.STATE["sl"], entry, places=3)  # breakeven
            self.assertTrue(E.STATE["trail_activated"])
            qty1 = E.STATE["remaining_qty"]
            self.assertAlmostEqual(qty1, qty0 * 0.7, places=6)
            # TP2: second 30% partial.
            self.assertEqual(E.apply_profit_engine("BTC/USDT:USDT", entry * 1.015,
                                                   df, len(df) - 1, E.STATE), "TP2")
            self.assertTrue(E.STATE["tp2_hit"])
            self.assertLess(E.STATE["remaining_qty"], qty1)
            # Real finalize: mark in profit, then book margin release + win.
            margin_committed = E.paper["committed_margin"]
            balance0 = E.paper["balance"]
            E.STATE["mark_price"] = entry * 1.015
            E.get_ticker_safe = lambda symbol, retries=3: (
                entry * 1.015 if str(symbol).startswith("BTC") else _ticker(symbol))
            pnl_usdt, pnl_pct = E.finalize_trade_with_reality("BTC/USDT:USDT")
            self.assertGreater(pnl_pct, 1.0)
            self.assertGreater(pnl_usdt, 0.0)
            self.assertFalse(E.STATE.get("open"))
            self.assertLess(E.paper["committed_margin"], margin_committed)
            self.assertGreater(E.paper["balance"], balance0)
            self.assertEqual(E.PERF["trades"], 1)
            self.assertEqual(E.PERF["wins"], 1)
            self.assertEqual(E.PERF["losses"], 0)
        finally:
            self.pm.deactivate()
        # The management loop reaps the closed context; the slot frees.
        self.pm.manage_all()
        self.assertNotIn("BTC/USDT:USDT", self.pm.contexts)
        self.assertEqual(self.pm.count(), 0)

    # --- Part 4: REAL position management across six radar-fed slots --------
    def test_manage_all_manages_six_radar_positions_and_rotates_slots(self):
        watch, _scanner = self._radar_watchlist()
        cands = [_radar_cand(w) for w in watch]
        _news_watch("NCSKNVDA2USD/USDT:USDT")
        news_cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(news_cand)
        news_cand["side"] = "BUY"
        cands.append(news_cand)
        opened = self.pm.open_top(cands, slots=6)
        self.assertEqual(opened, 6)
        self.assertEqual(E.PERF["trades"], 0)
        committed0 = E.paper["committed_margin"]
        self.pm.manage_all()
        # The REAL management loop ran (sync_position_state + live management +
        # council exits) and booked at least one exit; surviving contexts are
        # structurally intact and the ledger stayed real. We assert the loop's
        # guarantees, not which specific class the trading brain exited (its
        # react-to-frame behavior belongs to the dedicated brain tests).
        self.assertGreaterEqual(E.PERF["trades"], 1)
        self.assertGreaterEqual(E.PERF["wins"], 1)
        self.assertGreaterEqual(E.PERF["losses"], 0)
        freed = 6 - self.pm.count()
        self.assertGreater(freed, 0)
        for sym in self.pm.symbols():
            ctx = self.pm.contexts[sym]
            self.assertTrue(ctx.state.get("open"))
            self.assertIsNotNone(ctx.live_manager)
            self.assertGreater(ctx.state.get("qty", 0), 0)
        self.assertLess(E.paper["committed_margin"], committed0)
        self.assertAlmostEqual(E.paper["balance"] + E.paper["committed_margin"],
                               10000.0, places=6)
        snap = self.pm.snapshot()
        self.assertEqual(len(snap), self.pm.count())
        for row in snap:
            self.assertGreaterEqual(row["roe_pct"], -100)
            self.assertTrue(bool(row["side"]))
        self.assertEqual(count_open_news(self.pm), 1)
        # Rotation: the freed slots accept fresh radar candidates and the
        # portfolio returns to full six-slot capacity.
        rotated = self.pm.open_top([_radar_cand(w) for w in watch], slots=freed)
        self.assertEqual(rotated, freed)
        self.assertEqual(self.pm.count(), 6)


if __name__ == "__main__":
    unittest.main()