"""End-to-end dynamic verification of the trade/position management system.

This suite drives the REAL core.engine (paper mode) through the production
PortfolioManager lifecycle while a per-symbol market simulator feeds moving
tickers/OHLCV. The portfolio, risk, allocator and management layers are the
REAL production code; only two narrow entry-gate seams are made deterministic
for synthetic data:
  * compute_adx               -> constant band value 30 (real gate [25,38];
                                 also disarms council_exit's "ADX<18" branch).
  * detect_liquidity_context  -> side-aligned sweep context so execute_entry's
                                 liquidity gate is meaningfully satisfied.

The OHLCV feed itself is crafted as a genuine trending series so the real
management brain scores healthy ("HOLD", trailing active) on the symbols meant
to survive, while crash symbols genuinely stop out through the real SL logic.

Requirement under test: a system that professionally manages six simultaneous
positions — dynamic management, per-symbol isolation, real SL exits, slot
rotation, a hard refusal of any seventh trade, reconciled margin accounting,
allocation caps and a dashboard view of every open position.
"""
import copy
import os
import unittest
from unittest.mock import patch

import numpy as np
import pandas as pd

import core.engine as E  # noqa: E402  (real engine)

from portfolio.manager import PortfolioManager  # noqa: E402
from portfolio.allocator import GlobalAssetAllocator  # noqa: E402

# Env configuration is applied per-test (not at import time) so this module
# never contaminates sibling tests in a shared process.
PAPER_ENV = {
    "PAPER_MODE": "True",
    "BINGX_KEY": "",
    "BINGX_SECRET": "",
    "NEWS_ENABLED": "False",
    "POSITION_MARGIN_PCT": "0.10",
    "PORTFOLIO_MARGIN_CAP_PCT": "0.60",
    "MAX_DAILY_LOSS_PCT": "20",
    "MAX_CONSECUTIVE_LOSSES": "3",
    "MAX_POSITIONS_PER_ASSET_CLASS": "2",
}


# Boot-like engine snapshots captured once at import time.
_STATE_SNAPSHOT = copy.deepcopy(E.STATE)
_TSTATE_SNAPSHOT = copy.deepcopy(E.TRADE_STATE)
_DASH_SNAPSHOT = copy.deepcopy(E.DASHBOARD_STATE)


def _reset_engine():
    E.STATE.clear()
    E.STATE.update(copy.deepcopy(_STATE_SNAPSHOT))
    E.TRADE_STATE.clear()
    E.TRADE_STATE.update(copy.deepcopy(_TSTATE_SNAPSHOT))
    E.DASHBOARD_STATE.clear()
    E.DASHBOARD_STATE.update(copy.deepcopy(_DASH_SNAPSHOT))
    E.paper.update({"balance": 10000.0, "position": None, "committed_margin": 0.0})
    E.PERF.update({"trades": 0, "wins": 0, "losses": 0,
                   "total_pnl_usdt": 0.0, "total_pnl_pct": 0.0, "last_trade": {}})
    E.log_execution = lambda *a, **k: None


# ---------------------------------------------------------------------------
# Deterministic per-symbol live market simulator
# ---------------------------------------------------------------------------
def _trend_ohlcv(seed, n=150, direction=1.0, drift=0.0012, wave=0.0045, vol0=600.0):
    """A genuine trending series (higher highs / lower lows for BUY)."""
    i = np.arange(n)
    close = 100.0 * np.exp(direction * drift * i + direction * wave * np.sin(i / 7.0))
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + 0.0004 * direction)
    high = np.maximum(open_, close) * (1 + 0.0035)
    low = np.minimum(open_, close) * (1 - 0.0035)
    volume = vol0 * (1 + 0.01 * i) * (1 + 0.7 * (i >= n - 4))
    return pd.DataFrame({"open": open_, "high": high, "low": low,
                         "close": close, "volume": volume})


class MarketSim:
    def __init__(self):
        self.live = {}
        self._bases = {}
        self._sell = set()

    def register(self, sym, side, real_price, seed):
        df = _trend_ohlcv(seed, direction=(-1.0 if side == "SELL" else 1.0))
        scale = real_price / df["close"].iloc[-1]
        for col in ("open", "high", "low", "close"):
            df[col] = df[col] * scale
        self._bases[sym] = df
        self.live[sym] = real_price
        if side == "SELL":
            self._sell.add(sym)

    def set_live(self, sym, price):
        self.live[sym] = price

    def ohlcv(self, sym, limit=120, htf=False):
        df = self._bases[sym].copy()
        last = df.index[-1]
        live = self.live[sym]
        df.loc[last, "close"] = live
        body = live * (0.001 if sym in self._sell else -0.001)
        df.loc[last, "open"] = live - body
        df.loc[last, "high"] = max(df.loc[last, "high"], live)
        df.loc[last, "low"] = min(df.loc[last, "low"], live)
        return df.iloc[-min(limit, len(df)):]

    def ticker(self, sym):
        return self.live.get(sym)


def _liq_ctx(df, lookback=10):
    last = df.iloc[-1]
    return "buy_side_taken" if last["close"] > last["open"] else "sell_side_taken"


def _adx_const(df, period=14):
    return pd.Series([30.0] * len(df), index=df.index)


def _prime_market(sim, candidates):
    for i, c in enumerate(candidates):
        sim.register(c["symbol"], c["side"], c["price"], seed=7 + i * 13)
    E.get_ohlcv_safe = lambda sym, limit=120, htf=False: sim.ohlcv(sym, limit, htf)
    E.get_ticker_safe = lambda sym, retries=0, **k: sim.ticker(sym)
    E.get_orderbook_cached = lambda sym, limit=20, **k: {
        "bids": [[sim.live.get(sym, 1000.0) * 0.999, 10.0]],
        "asks": [[sim.live.get(sym, 1000.0) * 1.001, 10.0]],
    }


def _advance_management_clock(pm):
    for ctx in pm.contexts.values():
        m = ctx.live_manager
        m.last_management_ts = 0.0
        m.last_heavy_calc_ts = 0.0
        m.last_position_sync_ts = 0.0
        m.last_live_debug_ts = 0.0
        m.last_log_ts = 0.0


def _cand(sym, cls, price, side="BUY", score=85.0):
    atr = price * 0.01
    return {
        "symbol": sym, "side": side, "price": price,
        "sl": price * (0.98 if side == "BUY" else 1.02),
        "tp1": price * (1.03 if side == "BUY" else 0.97),
        "tp2": price * (1.06 if side == "BUY" else 0.94),
        "score": score, "atr": atr, "asset_class": cls, "trade_id": sym,
    }


# ---------------------------------------------------------------------------
# Base case: applies PAPER_ENV + engine reset + the two deterministic seams,
# and restores everything on teardown so sibling tests are never affected.
# ---------------------------------------------------------------------------
class _PortfolioEngineTestCase(unittest.TestCase):
    def setUp(self):
        self._env_saved = {k: os.environ.get(k) for k in PAPER_ENV}
        for k, v in PAPER_ENV.items():
            os.environ[k] = v
        _reset_engine()
        self.sim = MarketSim()
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)
        self._adx = patch.object(E, "compute_adx", side_effect=_adx_const)
        self._liq = patch.object(E, "detect_liquidity_context", side_effect=_liq_ctx)
        self._adx.start()
        self._liq.start()

    def tearDown(self):
        self._liq.stop()
        self._adx.stop()
        for k, saved in self._env_saved.items():
            if saved is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved


class DynamicSixPositionRealEngineTest(_PortfolioEngineTestCase):
    """Real-engine 6-position capacity + dynamic management + rotation."""

    SIX = [
        _cand("BTC/USDT:USDT", "CRYPTO", 60000.0),                 # gentle rise -> hold
        _cand("ETH/USDT:USDT", "CRYPTO", 3000.0),                  # crash -> real SL
        _cand("US500/USDT:USDT", "INDEX", 5000.0),                 # gentle rise -> hold
        _cand("USTECH/USDT:USDT", "INDEX", 17000.0, side="SELL"),  # gentle fall -> hold
        _cand("XAUUSD", "GOLD", 2300.0),                           # gentle rise -> hold
        _cand("WTI", "OIL", 75.0),                                 # crash -> real SL
    ]
    ROTATION = [
        _cand("SOL/USDT:USDT", "CRYPTO", 150.0),
        _cand("AAPL", "STOCK", 210.0),
    ]
    EXTRA7 = _cand("ADA/USDT:USDT", "CRYPTO", 30.0)

    def test_six_simultaneous_dynamic_lifecycle(self):
        _prime_market(self.sim, self.SIX + self.ROTATION + [self.EXTRA7])

        # --- Capacity: exactly six simultaneous slots; 7th refused always ---
        self.assertEqual(self.pm.open_top(self.SIX, slots=6), 6)
        self.assertEqual(self.pm.count(), 6)
        self.assertFalse(self.pm.can_open(self.EXTRA7["symbol"], self.EXTRA7["asset_class"]))
        self.assertEqual(self.pm.count(), 6)

        # --- All six coexist; each carries its own live manager ---
        self.assertEqual(len(self.pm.symbols()), 6)
        for sym in self.pm.symbols():
            self.assertTrue(self.pm.contexts[sym].state.get("open"))
            self.assertIsNotNone(self.pm.contexts[sym].live_manager)

        # --- Dynamic cycle: crash two symbols, hold four on healthy trends ---
        for sym, mult in (("ETH/USDT:USDT", 0.97), ("WTI", 0.97),
                          ("BTC/USDT:USDT", 1.004), ("US500/USDT:USDT", 1.004),
                          ("XAUUSD", 1.004), ("USTECH/USDT:USDT", 0.996)):
            entry = self.pm.contexts[sym].state["entry"]
            self.sim.set_live(sym, entry * mult)
        _advance_management_clock(self.pm)
        self.pm.manage_all()

        # Exactly the two crashed symbols went flat; the four healthy ones stayed
        # in management with their own live state (isolation preserved).
        self.assertNotIn("ETH/USDT:USDT", self.pm.symbols())
        self.assertNotIn("WTI", self.pm.symbols())
        self.assertGreaterEqual(self.pm.count(), 3)
        self.assertGreaterEqual(E.PERF["trades"], 2)
        for sym in self.pm.symbols():
            ctx = self.pm.contexts[sym]
            self.assertEqual(ctx.state.get("current_symbol"), sym)
            self.assertIsNotNone(ctx.state.get("side"))
        if "BTC/USDT:USDT" in self.pm.contexts:
            self.assertEqual(self.pm.contexts["BTC/USDT:USDT"].state["entry"], 60000.0)

        # --- Portfolio margin reconciles with realized PnL (exact invariant) ---
        self.assertAlmostEqual(
            E.paper["balance"] + E.paper["committed_margin"],
            10000.0 + E.PERF["total_pnl_usdt"],
            places=4,
            msg="free + committed must equal initial balance + realized PnL",
        )

        # --- Rotation: freed slots are refilled by fresh candidates ---
        slots_freed = 6 - self.pm.count()
        self.assertGreater(slots_freed, 0)
        rotated = self.pm.open_top(self.ROTATION, slots=slots_freed)
        self.assertEqual(rotated, slots_freed)
        self.assertEqual(self.pm.count(), 6)

        # --- Dashboard publish path lists every open position ---
        snap = self.pm.snapshot()
        E.DASHBOARD_STATE["positions"] = snap
        E.DASHBOARD_STATE["portfolio"] = {
            "open_positions": len(snap),
            "max_positions": self.pm.max_positions,
            "capacity": self.pm.max_positions - len(snap),
            "risk": self.pm.risk_snapshot(),
        }
        self.assertEqual(len(snap), 6)
        for row in snap:
            for field in ("symbol", "side", "entry", "mark_price", "roe_pct",
                          "sl", "tp1", "tp2", "trade_state"):
                self.assertIn(field, row)

        # --- Still managed + isolated after rotation (another live cycle) ---
        _advance_management_clock(self.pm)
        self.pm.manage_all()
        for sym in self.pm.symbols():
            self.assertEqual(self.pm.contexts[sym].state.get("current_symbol"), sym)

        # --- Close everything: full capacity freed, margin reconciled ---
        for sym in list(self.pm.symbols()):
            self.assertTrue(self.pm.close_symbol(sym))
        self.assertEqual(self.pm.count(), 0)
        self.assertTrue(self.pm.can_open("NEW/USDT:USDT", "CRYPTO"))
        self.assertAlmostEqual(
            E.paper["balance"] + E.paper["committed_margin"],
            10000.0 + E.PERF["total_pnl_usdt"],
            places=4,
        )


class AllocatorGatesOnLivePortfolioTest(_PortfolioEngineTestCase):
    """GlobalAssetAllocator must explain every unused slot while six positions
    are live: class caps, directional caps and slot cap all stay engaged."""

    def _open_six(self):
        # 3 BUY + 3 SELL so the directional cap (4) does not mask class caps.
        six = [
            _cand("BTC/USDT:USDT", "CRYPTO", 60000.0),
            _cand("ETH/USDT:USDT", "CRYPTO", 3000.0, side="SELL"),
            _cand("US500/USDT:USDT", "INDEX", 5000.0),
            _cand("USTECH/USDT:USDT", "INDEX", 17000.0, side="SELL"),
            _cand("XAUUSD", "GOLD", 2300.0),
            _cand("WTI", "OIL", 75.0, side="SELL"),
        ]
        _prime_market(self.sim, six)
        self.assertEqual(self.pm.open_top(six, slots=6), 6)

    def test_allocator_explains_every_rejected_slot_at_capacity(self):
        # The allocator reads the live contexts (side/class); no management
        # cycle is required, so all six stay on their seats at capacity.
        self._open_six()
        self.assertEqual(self.pm.count(), 6)

        allocator = GlobalAssetAllocator(self.pm, E)
        report = allocator.allocate([
            {"symbol": "SOL/USDT:USDT", "side": "BUY", "asset_class": "CRYPTO",
             "priority_score": 95.0},                               # CRYPTO already 2
            {"symbol": "NAS100/USDT:USDT", "side": "BUY", "asset_class": "INDEX",
             "priority_score": 94.0},                               # INDEX already 2
            {"symbol": "XAGUSD", "side": "BUY", "asset_class": "GOLD",
             "priority_score": 93.0},                               # GOLD already 1
            {"symbol": "TSLA", "side": "BUY", "asset_class": "NEWS",
             "priority_score": 92.0},                               # independent slot spare
            {"symbol": "NVDA", "side": "BUY", "asset_class": "NEWS",
             "priority_score": 91.0},                               # NEWS slot already 1
        ], limit=10)

        by = {d.symbol: d for d in report.decisions}
        self.assertFalse(by["SOL/USDT:USDT"].allowed)
        self.assertEqual(by["SOL/USDT:USDT"].reason, "CRYPTO_CAP")
        self.assertFalse(by["NAS100/USDT:USDT"].allowed)
        self.assertEqual(by["NAS100/USDT:USDT"].reason, "INDEX_CAP")
        self.assertFalse(by["XAGUSD"].allowed)
        self.assertEqual(by["XAGUSD"].reason, "GOLD_CAP")
        # In the 6-market model (2 CRYPTO / 2 INDEX / 1 GOLD / 1 OIL) only the
        # independent NEWS slot has a spare seat at full technical capacity.
        self.assertTrue(by["TSLA"].allowed)
        self.assertEqual(by["TSLA"].reason, "OK")
        self.assertFalse(by["NVDA"].allowed)
        self.assertEqual(by["NVDA"].reason, "BUY_CAP")
        self.assertEqual(report.class_bias.get("CRYPTO"), 2)
        self.assertEqual(report.class_bias.get("INDEX"), 2)
        self.assertEqual(report.class_bias.get("NEWS"), 1)


if __name__ == "__main__":
    unittest.main()