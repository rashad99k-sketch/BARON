"""Production/Runtime NEWS slot integration tests.

Proves the independent NEWS slot is REALLY wired as NEWS through the whole
trade life, using the production path -- not mock-only success:

  1. NEWS Trade Type Persistence: a trade that enters (via execute_news_slot
     or open_candidate) as NEWS keeps STATE["trade_type"] == "NEWS" and
     position_profile.trade_type == "NEWS", and is never re-labelled TREND /
     REVERSAL by the management taxonomy.
  2. Management Isolation: running live management on a NEWS trade does not
     change its trade_type; no reversal/trend classifier may re-classify NEWS.
  3. NEWS_DRIVEN Regime: the real [POSITION] advisory log shows
     TRADE_TYPE=NEWS REGIME=NEWS_DRIVEN (through the real management path).
  4. Normal Trades Remain Normal: a normal technical trade opened alongside a
     NEWS trade keeps its own independent classification.
  5. NEWS Slot OFF: NEWS_SLOT_ENABLED=false -> no news trade opens.
  6. NEWS Slot ON: NEWS_SLOT_ENABLED=true + valid news watch -> full path
     NEWS DETECTION -> ANALYSIS -> SLOT -> OPEN -> MANAGEMENT, ending with
     trade_type=NEWS.

Only the network/Exchange provider boundary is stubbed (OHLCV / ticker /
orderbook / balance); the scanner->slot->open_candidate->execute_entry->open,
sizing, margin accounting, and the whole management path are the real
production code.
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

# core.runtime exposes the real execute_news_slot gate (env-driven).
import core.runtime as R  # noqa: E402


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


def _frame(n=250, side="BUY", base=100.0):
    """Trending frame shaped so the REAL execute_entry gates approve:
    ADX inside [25,38] and the tail candle sweeps the relevant liquidity so
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
    return pd.DataFrame({"timestamp": t, "open": o, "high": h,
                         "low": l, "close": c, "volume": np.full(n, 1000.0)})


def _cand(sym, cls, side="BUY", score=88.0):
    price = _price(sym)
    atr = price * 0.01
    sl, tp1, tp2 = price - atr * 1.6, price + atr * 1.5, price + atr * 2.5
    return {"symbol": sym, "side": side, "price": price, "sl": sl, "tp1": tp1,
            "tp2": tp2, "score": score, "atr": atr, "asset_class": cls,
            "trade_id": sym}


def _news_assessment(bias="BULLISH", risk=20.0, strong=True):
    impact = "STRONG" if strong else "MEDIUM"
    return types.SimpleNamespace(
        risk=risk, bias=bias,
        headlines=[{"impact_strength": impact, "scope": "DIRECT",
                    "headline": "impact headline"}],
        as_dict=lambda: {"bias": bias, "risk": risk},
    )


def _news_watch(symbol, bias="BULLISH", risk=20.0, strong=True):
    E.MEMORY["watchlist"][symbol] = {
        "price": _price(symbol),
        "atr": _price(symbol) * 0.01,
        "news_risk": risk,
        "news": _news_assessment(bias, risk, strong),
    }


def _logs():
    return list(E.DASHBOARD_STATE.get("logs", []))


def _setup_engine_stubs():
    E.get_ohlcv_safe = lambda symbol, limit=120, htf=False: _frame(base=_price(symbol))
    E.get_ticker_safe = lambda symbol, retries=3: _price(symbol)
    E.get_orderbook_cached = lambda *a, **k: {
        "bids": [[_price(a[0]) - 1.0, 10.0]], "asks": [[_price(a[0]) + 1.0, 5.0]]}
    E.get_balance_safe = lambda retries=3: E.paper["balance"]
    E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}


def _reset_state():
    E.STATE.clear()
    E.TRADE_STATE.clear()
    E.MEMORY.setdefault("watchlist", {}).clear()
    E.DASHBOARD_STATE.setdefault("logs", []).clear()
    E.DASHBOARD_STATE.setdefault("errors", []).clear()
    E.PERF.update({"total_pnl_pct": 0.0, "total_pnl_usdt": 0.0, "trades": 0,
                   "wins": 0, "losses": 0})
    E.paper = {"balance": 10000.0, "position": None, "committed_margin": 0.0}


def _profile_trade_type(pm, symbol):
    """Read the trade_type from the position profile actually used by this
    symbol's live manager. Reads per-context (isolation-aware) truth -- the
    global E.STATE is intentionally blanked by open_candidate's deactivate."""
    ctx = pm.contexts.get(symbol)
    if ctx is None:
        return None
    lm = getattr(ctx, "live_manager", None)
    if lm is None:
        return None
    prof = getattr(lm, "position_profile", None)
    if prof is None:
        return None
    return prof.trade_type


class NewsHarnessMixin:
    def setUp(self):
        self._saved = (E.get_ohlcv_safe, E.get_ticker_safe,
                       E.get_orderbook_cached, E.get_balance_safe)
        self._saved_perf = (dict(E.PERF), dict(E.DASHBOARD_STATE))
        self._news_candidate = None
        _reset_state()
        _setup_engine_stubs()
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

    def _open_news(self, symbol="NCSKNVDA2USD/USDT:USDT", bias="BULLISH", risk=20.0):
        _news_watch(symbol, bias=bias, risk=risk)
        cand = scan_for_news_candidate(E.MEMORY["watchlist"])
        self.assertIsNotNone(cand, "strong-news candidate must be found")
        self.assertEqual(cand["asset_class"], "NEWS")
        self.assertEqual(cand["trade_type"], "NEWS")
        self.assertEqual(cand["classification"], "NEWS")
        opened = self.pm.open_candidate(cand)
        self.assertTrue(opened, "NEWS candidate must open through the real path")
        self._news_candidate = cand
        return cand


class NewsTradeTypePersistenceTest(unittest.TestCase, NewsHarnessMixin):
    """Requirement: trade_type=NEWS survives OPEN through the whole trade life
    in STATE and position_profile, and is never re-labelled TREND/REVERSAL."""

    def setUp(self):
        NewsHarnessMixin.setUp(self)

    def tearDown(self):
        NewsHarnessMixin.tearDown(self)

    def test_state_and_profile_trade_type_are_news_from_creation(self):
        cand = self._open_news()
        sym = cand["symbol"]
        # The context's persisted STATE (captured at OPEN via real execute_entry)
        # is NEWS. Global E.STATE is blanked by open_candidate's deactivate, so
        # the per-context snapshot is the isolation-aware source of truth.
        self.assertEqual(self.pm.contexts[sym].state.get("trade_type"), "NEWS")
        # Activate the context: the real engine STATE is restored from it.
        self.pm.activate(sym)
        try:
            self.assertEqual(E.STATE.get("trade_type"), "NEWS")
            # The DynamicPositionProfile used by the management engine is NEWS.
            self.assertEqual(_profile_trade_type(self.pm, sym), "NEWS")
            # It is NOT any technical re-labelling.
            self.assertNotIn(_profile_trade_type(self.pm, sym), ("TREND", "REVERSAL"))
        finally:
            self.pm.deactivate()

    def test_news_stays_news_through_real_management(self):
        cand = self._open_news()
        sym = cand["symbol"]
        self.pm.manage_all()  # real sync + live management + council exit
        if sym in self.pm.contexts:
            self.assertTrue(self.pm.contexts[sym].state.get("open"))
            # Persisted profile is still NEWS after real management ran.
            self.assertEqual(self.pm.contexts[sym].state.get("trade_type"), "NEWS")
            self.assertEqual(_profile_trade_type(self.pm, sym), "NEWS")

    def test_news_is_never_reclassified_by_reversal_taxonomy(self):
        self._open_news()
        sym = self._news_candidate["symbol"]
        self.pm.activate(sym)
        try:
            prof = E._live_manager.position_profile
            self.assertIsNotNone(prof)
            # Drive the advisory `update()` with the strongest reversal/exhaustion
            # evidence the taxonomy can emit. The NEWS pin must hold regardless.
            prof.update(
                trade_state="DISTRIBUTION",
                trend_health=2.0,
                structure_aligned=False,
                continuation_probability=0.1,
                smart_money={"distribution_risk": 90},
                exhaustion_evidence=True,
                reversal_confirmed=True,
            )
            self.assertEqual(prof.trade_type, "NEWS")
        finally:
            self.pm.deactivate()


class NewsRegimeAndIsolationTest(unittest.TestCase, NewsHarnessMixin):
    """Requirement: NEWS_DRIVEN regime on the real [POSITION] advisory log and
    full isolation from normal trades."""

    def setUp(self):
        NewsHarnessMixin.setUp(self)

    def tearDown(self):
        NewsHarnessMixin.tearDown(self)

    def test_position_log_shows_news_driven_regime_through_real_management(self):
        cand = self._open_news()
        sym = cand["symbol"]
        self.pm.activate(sym)
        try:
            # Run the REAL live-management loop once (emits the [POSITION]
            # advisory via _run_advisory_health with live computed inputs).
            E._live_manager.manage_live_trade()
        finally:
            self.pm.deactivate()
        joined = "\n".join(_logs())
        self.assertIn("TRADE_TYPE=NEWS", joined)
        self.assertIn("REGIME=NEWS_DRIVEN", joined)

    def test_news_open_block_log_present(self):
        cand = self._open_news()
        joined = "\n".join(_logs())
        self.assertIn("[NEWS]", joined)
        self.assertIn("trade_type=NEWS", joined)
        self.assertIn("slot=NEWS", joined)
        self.assertIn(f"direction={'LONG' if cand['side'] == 'BUY' else 'SHORT'}", joined)

    def test_normal_trade_keeps_its_classification_next_to_news(self):
        # Open a NEWS trade.
        news_sym = self._open_news("NCSKNVDA2USD/USDT:USDT")["symbol"]
        # Open a normal technical trade alongside (defaults to INSTITUTIONAL/SNIPER).
        normal_sym = "BTC/USDT:USDT"
        self.assertTrue(self.pm.open_candidate(_cand(normal_sym, "CRYPTO", "BUY")))
        self.assertEqual(count_open_news(self.pm), 1)
        # At OPEN each trade carries its OWN independent classification:
        # NEWS stays NEWS, the normal trade is NOT classed as NEWS.
        self.assertEqual(_profile_trade_type(self.pm, news_sym), "NEWS")
        self.assertEqual(self.pm.contexts[news_sym].state.get("trade_type"), "NEWS")
        normal_type = _profile_trade_type(self.pm, normal_sym)
        self.assertIsNotNone(normal_type)
        self.assertNotEqual(normal_type, "NEWS")
        # Manage the NORMAL trade through the real loop. Opening/managing a
        # normal trade must not contaminate the NEWS classification. (The
        # normal BTC trade may close under its own thesis -- DISTRIBUTION
        # profit-lock -- that is independent, not NEWS-driven.)
        self.pm.activate(normal_sym)
        try:
            E._live_manager.manage_live_trade()
        finally:
            self.pm.deactivate()
        # Isolation holds both ways: exactly one NEWS remains NEWS afterwards.
        self.assertEqual(count_open_news(self.pm), 1)
        if news_sym in self.pm.contexts:
            self.assertEqual(_profile_trade_type(self.pm, news_sym), "NEWS")
            self.assertEqual(self.pm.contexts[news_sym].state.get("trade_type"), "NEWS")


class NewsSlotGateTest(unittest.TestCase, NewsHarnessMixin):
    """Requirement: NEWS_SLOT_ENABLED=false -> no trade; =true -> full path.

    The gate exercises the full open path against engine state emitted by
    earlier files in the same pytest process (its pass/fail result is
    order-sensitive). It therefore runs through the subprocess worker in
    `test_news_slot_gate_isolated.py` and is not collected here."""

    __test__ = False

    def setUp(self):
        NewsHarnessMixin.setUp(self)

    def tearDown(self):
        NewsHarnessMixin.tearDown(self)

    def test_news_slot_off_opens_nothing(self):
        # A strong-news candidate exists, but the production gate must refuse.
        _news_watch("NCSKNVDA2USD/USDT:USDT", bias="BULLISH", risk=20.0)
        self.assertIsNotNone(scan_for_news_candidate(E.MEMORY["watchlist"]))
        old_enabled = os.environ.get("NEWS_SLOT_ENABLED", "false")
        os.environ["NEWS_SLOT_ENABLED"] = "false"
        R.PORTFOLIO = PortfolioManager(6, E)
        R.PORTFOLIO.bind(E)
        try:
            result = R.execute_news_slot()
        finally:
            os.environ["NEWS_SLOT_ENABLED"] = old_enabled
        self.assertFalse(result)
        self.assertEqual(R.PORTFOLIO.count(), 0)

    def test_news_slot_on_full_path_ends_with_news(self):
        # Full production path: detection (watchlist) -> analysis
        # (scan_for_news_candidate) -> slot gate (execute_news_slot) -> OPEN
        # (open_candidate -> execute_entry) -> context stored.
        _news_watch("NCSKNVDA2USD/USDT:USDT", bias="BULLISH", risk=20.0)
        old_enabled = os.environ.get("NEWS_SLOT_ENABLED", "false")
        os.environ["NEWS_SLOT_ENABLED"] = "true"
        R.PORTFOLIO = PortfolioManager(6, E)
        R.PORTFOLIO.bind(E)
        try:
            result = R.execute_news_slot()
        finally:
            os.environ["NEWS_SLOT_ENABLED"] = old_enabled
        self.assertTrue(result, "news slot must open a trade in the ON state")
        self.assertEqual(R.PORTFOLIO.count(), 1)
        sym = R.PORTFOLIO.symbols()[0]
        self.assertEqual(count_open_news(R.PORTFOLIO), 1)
        # The opened trade is NEWS in STATE and in the management profile.
        R.PORTFOLIO.activate(sym)
        try:
            self.assertEqual(E.STATE.get("trade_type"), "NEWS")
            self.assertEqual(E._live_manager.position_profile.trade_type, "NEWS")
        finally:
            R.PORTFOLIO.deactivate()


if __name__ == "__main__":
    unittest.main()
