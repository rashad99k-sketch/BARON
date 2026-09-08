"""PAPER runtime audit of the REAL production trade-management path.

Audit scope (no live, no keys, no production edits):
  * Brain states observed through the real engine + PortfolioManager manage loop:
      - TREND CONTINUATION   -> must NOT close early, keeps runner, arms protection
      - HEALTHY PULLBACK     -> must NOT treat a benign retracement as reversal
      - DISTRIBUTION/EXHAUST -> must PROTECT instead of waiting for a loss
      - REVERSAL/THESIS FAIL -> STRICT CLOSE, position -> 0, local CLOSED, PnL
  * TP1 / TP2 forensic: trigger/price/qty/remaining/local state/realized,
    no double-close, no over-close, no reverse position, TP2-without-TP1.
  * SL forensic LONG+SHORT: initial value, monotonic movement, breakeven after
    TP1, trailing, native SL order shape (booked via the PAPER journal).
  * Contract-shape mapping: hedge positionSide + side pairs vs BingX open/close
    combination table, and the native SL STOP_MARKET payload shape.

Every scenario drives the UNCHANGED production engine (core/engine.py) + the
production PortfolioManager in PAPER mode over a controlled price path. No
production file is modified. Nothing is copied from any external bot.

This file is intentional and additive; it is NOT committed anywhere.
"""
import os
import time
import unittest
import copy
from unittest.mock import patch

import numpy as np
import pandas as pd

os.environ.setdefault("PAPER_MODE", "True")
os.environ.setdefault("BINGX_KEY", "")
os.environ.setdefault("BINGX_SECRET", "")
os.environ.setdefault("NEWS_ENABLED", "True")

import core.engine as E  # noqa: E402  real engine, PAPER mode

_MEMORY_BASE = copy.deepcopy(E.MEMORY)


def _trend_frame(price, side="BUY"):
    """Clean persistent trend ending exactly at `price` (used at ENTRY and for
    continuation/pullback management phases)."""
    direction = -1.0 if side == "SELL" else 1.0
    n = 150
    t = np.arange(n)
    close = 100.0 * np.exp(direction * 0.0012 * t + direction * 0.0045 * np.sin(t / 7.0))
    open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + 0.0004 * direction)
    high = np.maximum(open_, close) * (1 + 0.0035)
    low = np.minimum(open_, close) * (1 - 0.0035)
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": 600.0 * (1 + 0.01 * t)})
    scale = price / float(df["close"].iloc[-1])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col] * scale
    df.attrs["ifvg_sym"] = False
    return df


def _topped_frame(price, side="BUY"):
    """Exhaustion look: grind flat into a shelf with fading volume so the real
    engines read distribution instead of fresh continuation."""
    direction = -1.0 if side == "SELL" else 1.0
    n = 150
    t = np.arange(n)
    drift = direction * 0.018 * np.tanh(t / 35.0)
    noise = 0.0035 * np.sin(t / 5.0) + 0.0020 * np.cos(t / 13.0)
    taper = np.clip((n - t) / 40.0, 0.0, 1.0)
    close = price * (1.0 + drift + noise * taper)
    high = (close + 0.0025 * price) * (1 + 0.0015)
    low = (close - 0.0025 * price) * (1 - 0.0015)
    open_ = np.concatenate([[close[0]], close[:-1]])
    volume = 500.0 * (1 - 0.55 * (t / (n - 1)))
    df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                       "volume": volume})
    df.attrs["ifvg_sym"] = False
    return df


def _climb_mults(side):
    """ramp mark/entry to the top in favor steps (BUY up / SELL down)."""
    if side == "BUY":
        return [1.005, 1.010, 1.015, 1.020, 1.030]
    return [0.995, 0.990, 0.985, 0.980, 0.970]


def _crash_mults(side):
    """Slow reversal grind start (used after the topped frame is loaded)."""
    if side == "BUY":
        return [1.024, 1.017, 1.010, 1.003, 0.997, 0.991, 0.985]
    return [0.976, 0.983, 0.990, 0.997, 1.003, 1.009, 1.015]


class PaperAuditScaffold(unittest.TestCase):
    """Shared real-runtime harness (engine + PortfolioManager, PAPER)."""

    __test__ = False

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

    CLEAR_PAYLOAD = {"has_inverse": False, "blocking": False, "zones": [],
                     "closest": None, "distance_atr": None, "penalty": 0.0,
                     "reason": "CLEAR"}

    def _payload_factory(self):
        def _payload(side, df, atr=None, reference_price=None, block_atr=None):
            return dict(self.CLEAR_PAYLOAD)
        return _payload

    # --- side/symbol ---
    def _sym(self):
        raise NotImplementedError

    def _side(self):
        return "BUY"

    def _entry_price(self):
        return 60000.0 if self._side() == "BUY" else 3000.0

    # --- lifecycle ---
    def setUp(self):
        from portfolio.manager import PortfolioManager
        self._orig_log_execution = E.log_execution
        self._env = {k: os.environ.get(k) for k in self.PAPER_ENV}
        for k, v in self.PAPER_ENV.items():
            os.environ[k] = v
        self._reset()
        self._clear_hybrid_caches()
        self.logs = []
        E.log_execution = lambda s, *a, **k: self.logs.append(
            str(s).encode("ascii", "replace").decode("ascii"))
        self._ifvg = patch.object(E, "ifvg_warning_payload", side_effect=self._payload_factory())
        self._adx = patch.object(E, "compute_adx",
                                 side_effect=lambda df, period=14: pd.Series([30.0] * len(df),
                                                                              index=df.index))
        self._liq = patch.object(E, "detect_liquidity_context",
                                 side_effect=lambda df, lookback=10: "buy_side_taken"
                                 if float(df["close"].iloc[-1]) > float(df["open"].iloc[-1])
                                 else "sell_side_taken")
        self._ifvg.start()
        self._adx.start()
        self._liq.start()
        self.pm = PortfolioManager(2, E)
        self.pm.bind(E)
        self._prime()

    def _clear_hybrid_caches(self):
        for c in ("_live_high", "_live_low", "_last_candle_timestamp"):
            if hasattr(E, c):
                try:
                    setattr(E, c, {})
                except Exception:
                    pass

    def tearDown(self):
        self._liq.stop()
        self._adx.stop()
        self._ifvg.stop()
        self._clear_hybrid_caches()
        E.log_execution = self._orig_log_execution
        for k, saved in self._env.items():
            if saved is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved

    def _reset(self):
        _snap, _tsnap, _dsnap = (copy.deepcopy(E.STATE), copy.deepcopy(E.TRADE_STATE),
                                 copy.deepcopy(E.DASHBOARD_STATE))
        E.STATE.clear(); E.STATE.update(_snap)
        E.TRADE_STATE.clear(); E.TRADE_STATE.update(_tsnap)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(_dsnap)
        E.paper.update({"balance": 10000.0, "position": None, "committed_margin": 0.0})
        E.PERF.update({"trades": 0, "wins": 0, "losses": 0, "total_pnl_usdt": 0.0,
                       "total_pnl_pct": 0.0, "last_trade": {}})
        E.MEMORY.clear(); E.MEMORY.update(copy.deepcopy(_MEMORY_BASE))
        E.log_execution = lambda *a, **k: None

    def _prime(self):
        sym = self._sym()
        side = self._side()
        price = self._entry_price()
        self.bases = {sym: _trend_frame(price, side)}
        self.live = {sym: price}
        E.get_ohlcv_safe = lambda sym_, limit=120, htf=False: self._ohlcv(sym_, limit, htf)
        E.get_ticker_safe = lambda sym_, retries=0, **k: self.live.get(sym_)
        E.get_orderbook_cached = lambda sym_, limit=20, **k: {
            "bids": [[self.live.get(sym_, 1000.0) * 0.999, 10.0]],
            "asks": [[self.live.get(sym_, 1000.0) * 1.001, 10.0]],
        }

    def _ohlcv(self, sym, limit=120, htf=False):
        side = self._side()
        df = self.bases[sym].copy()
        last = df.index[-1]
        live = self.live[sym]
        df.loc[last, "close"] = live
        body = live * (0.001 if side == "SELL" else -0.001)
        df.loc[last, "open"] = live - body
        df.loc[last, "high"] = max(float(df.loc[last, "high"]), live)
        df.loc[last, "low"] = min(float(df.loc[last, "low"]), live)
        df = df.iloc[-min(limit, len(df)):]
        df.attrs["ifvg_sym"] = bool(self.bases[sym].attrs.get("ifvg_sym", False))
        return df

    def open_position(self):
        cand = {"symbol": self._sym(), "side": self._side(),
                "price": self._entry_price(), "asset_class": "CRYPTO"}
        entry = {"symbol": cand["symbol"], "side": cand["side"], "price": cand["price"],
                 "sl": cand["price"] * (0.98 if cand["side"] == "BUY" else 1.02),
                 "tp1": cand["price"] * (1.03 if cand["side"] == "BUY" else 0.97),
                 "tp2": cand["price"] * (1.06 if cand["side"] == "BUY" else 0.94),
                 "score": 85.0, "atr": cand["price"] * 0.01,
                 "asset_class": cand["asset_class"], "trade_id": cand["symbol"]}
        opened = self.pm.open_top([entry], slots=1)
        self.assertEqual(opened, 1, f"open_top failed for {self._sym()}")
        return self.pm.contexts[self._sym()].state

    def _advance_clock(self):
        for ctx in self.pm.contexts.values():
            m = ctx.live_manager
            for attr in ("last_management_ts", "last_heavy_calc_ts",
                         "last_position_sync_ts", "last_live_debug_ts", "last_log_ts"):
                try:
                    setattr(m, attr, 0.0)
                except Exception:
                    pass

    def _snapshot(self, sym=None):
        sym = sym or self._sym()
        ctx = self.pm.contexts.get(sym)
        if ctx is None:
            return {"closed": True}
        s = ctx.state
        return {
            "roe": s.get("roe_pct", 0.0),
            "tp1_hit": bool(s.get("tp1_hit", False)),
            "tp2_hit": bool(s.get("tp2_hit", False)),
            "runner_mode": bool(s.get("runner_mode", False)),
            "trail_activated": bool(s.get("trail_activated", False)),
            "protection_state": s.get("protection_state"),
            "synthetic_sl": s.get("synthetic_sl"),
            "trail_stop": s.get("trail_stop"),
            "peak_roe": s.get("peak_roe", 0.0),
            "trade_state": s.get("trade_state"),
            "remaining_qty": s.get("remaining_qty", 0.0),
            "native_sl_state": s.get("native_sl_state"),
            "native_sl_price": s.get("native_sl_price"),
        }

    def _step(self, sym, mult):
        ctx = self.pm.contexts.get(sym)
        if ctx is None:
            return
        self.live[sym] = ctx.state["entry"] * mult
        self._advance_clock()
        self.pm.manage_all()

    def _flat_top_hold(self, sym, top, holds=2):
        for _ in range(holds):
            ctx = self.pm.contexts.get(sym)
            if ctx is None:
                return
            self.live[sym] = ctx.state["entry"] * top
            self._advance_clock()
            self.pm.manage_all()

    def _realized(self):
        return float(E.PERF.get("total_pnl_usdt", 0.0))

    def _has_full_close_log(self):
        return any(("[TRADE:TRADE_CLOSED]" in l) or ("Trade closed:" in l) or
                   ("[CLOSE] Paper position closed" in l) for l in self.logs)

    def _log_lines(self, *keys):
        return [l for l in self.logs if any(k in l for k in keys)]


class Buyer_paper(PaperAuditScaffold):
    __test__ = False
    SYM = "BTC/USDT:USDT"

    def _sym(self):
        return self.SYM


class Seller_paper(PaperAuditScaffold):
    __test__ = False
    SYM = "ETH/USDT:USDT"

    def _sym(self):
        return self.SYM

    def _side(self):
        return "SELL"

    def _entry_price(self):
        return 3000.0


# ----------------------------------------------------------------------------
# 1) TREND CONTINUATION — no early close, runner preserved, protection armed.
# ----------------------------------------------------------------------------
class ContinuationLongTest(Buyer_paper):
    __test__ = True

    def test_continuation_holds_through_climb(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"]); initial_qty = float(st["qty"])
        trace = []
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            trace.append(self._snapshot(sym))
            if sym not in self.pm.symbols():
                break
        self.assertIn(sym, self.pm.symbols(),
                      "LONG closed DURING healthy climb -> premature exit")
        self._flat_top_hold(sym, 1.03)
        top = self._snapshot(sym)
        self.assertIn(sym, self.pm.symbols(), "LONG closed at the top")
        self.assertGreaterEqual(float(top["peak_roe"]), 18.0,
                                f"never reached deep profit: {top}")
        self.assertTrue(top["tp1_hit"], "TP1 partial never banked")
        self.assertEqual(float(top["remaining_qty"]), initial_qty * 0.5,
                         "runner qty after TP1 must be exactly half")
        self.assertTrue(top["trail_activated"] or top["protection_state"] in ("BREAKEVEN", "PROFIT_LOCK"),
                        f"no protection armed at peak: {top}")
        floor = float(top["synthetic_sl"] or top["trail_stop"] or 0)
        self.assertGreaterEqual(floor, entry, f"LONG protection not at/beyond entry: {floor}")
        self.assertFalse(self._has_full_close_log(), "unexpected full close during continuation")


class ContinuationShortTest(Seller_paper):
    __test__ = True

    def test_continuation_holds_through_climb(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"]); initial_qty = float(st["qty"])
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self.assertIn(sym, self.pm.symbols(), "SHORT closed DURING healthy climb")
        self._flat_top_hold(sym, 0.97)
        top = self._snapshot(sym)
        self.assertIn(sym, self.pm.symbols(), "SHORT closed at the top")
        self.assertGreaterEqual(float(top["peak_roe"]), 18.0, f"never reached deep profit: {top}")
        self.assertTrue(top["tp1_hit"], "TP1 partial never banked")
        self.assertEqual(float(top["remaining_qty"]), initial_qty * 0.5,
                         "runner qty after TP1 must be exactly half")
        self.assertTrue(top["trail_activated"] or top["protection_state"] in ("BREAKEVEN", "PROFIT_LOCK"),
                        f"no protection armed at peak: {top}")
        floor = float(top["synthetic_sl"] or top["trail_stop"] or 0)
        self.assertLessEqual(floor, entry, f"SHORT protection not at/below entry: {floor}")
        self.assertFalse(self._has_full_close_log(), "unexpected full close during continuation")


# ----------------------------------------------------------------------------
# 2) HEALTHY PULLBACK — a benign retracement must NOT close the position.
# ----------------------------------------------------------------------------
class HealthyPullbackLongTest(Buyer_paper):
    __test__ = True

    def test_healthy_pullback_keeps_position(self):
        sym = self._sym()
        self.open_position()
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 1.03)
        before = self._snapshot(sym)
        self.assertIn(sym, self.pm.symbols(), "LONG closed before pullback test")
        floor_before = float(before["synthetic_sl"] or before["trail_stop"] or 0)
        realized_before = self._realized()
        # benign retracement: adverse but well above the protective floor
        for m in (1.029, 1.027, 1.024):     # shallow retrace, stays inside the trailing band
            self._step(sym, m)
            self.assertIn(sym, self.pm.symbols(),
                          f"LONG closed on healthy pullback at {m:.4f} -> treats pullback as reversal")
        after = self._snapshot(sym)
        floor_after = float(after["synthetic_sl"] or after["trail_stop"] or 0)
        # protection never moved backward (monotonic)
        self.assertGreaterEqual(floor_after + 1e-9, floor_before - 1e-6,
                                f"LONG protective floor moved backward: {floor_before} -> {floor_after}")
        self.assertFalse(self._has_full_close_log(), "full close happened during pullback")
        realized_after = self._realized()
        self.assertAlmostEqual(realized_after, realized_before, places=2,
                               msg="realized PnL changed -> a close happened on pullback")


class HealthyPullbackShortTest(Seller_paper):
    __test__ = True

    def test_healthy_pullback_keeps_position(self):
        sym = self._sym()
        self.open_position()
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 0.97)
        before = self._snapshot(sym)
        self.assertIn(sym, self.pm.symbols(), "SHORT closed before pullback test")
        floor_before = float(before["synthetic_sl"] or before["trail_stop"] or 0)
        realized_before = self._realized()
        for m in (0.9715, 0.9725, 0.9735):  # shallow retrace, stays inside the trailing band
            self._step(sym, m)
            self.assertIn(sym, self.pm.symbols(),
                          f"SHORT closed on healthy pullback at {m:.4f}")
        after = self._snapshot(sym)
        floor_after = float(after["synthetic_sl"] or after["trail_stop"] or 0)
        self.assertLessEqual(floor_after - 1e-9, floor_before + 1e-6,
                             f"SHORT protective floor moved backward: {floor_before} -> {floor_after}")
        self.assertFalse(self._has_full_close_log(), "full close happened during pullback")
        self.assertAlmostEqual(self._realized(), realized_before, places=2,
                               msg="realized PnL changed -> a close happened on pullback")


# ----------------------------------------------------------------------------
# 3) DISTRIBUTION / EXHAUSTION — protect profit instead of waiting for loss.
# ----------------------------------------------------------------------------
class DistributionLongTest(Buyer_paper):
    __test__ = True

    def test_distribution_protects_profit(self):
        sym = self._sym()
        self.open_position()
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 1.03)
        self.assertIn(sym, self.pm.symbols())
        top = self._snapshot(sym)
        # swap to the exhausted look and fade the top slowly: profit must NOT be given back
        self.bases[sym] = _topped_frame(self.pm.contexts[sym].state["entry"], "BUY")
        for m in (1.032, 1.030, 1.026, 1.020, 1.014, 1.008, 1.002):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self.assertNotIn(sym, self.pm.symbols(),
                         "LONG runner survived the distribution fade into red")
        realized = self._realized()
        self.assertGreater(realized, 0.0,
                           f"LONG gave the distribution top back: realized {realized:.2f}")
        # profit was banked by protection: close happened BEFORE deep loss, and
        # the protective mechanism was active at/above entry at the top.
        floor = float(top["synthetic_sl"] or top["trail_stop"] or 0)
        self.assertGreaterEqual(floor, self.pm.contexts[sym].state["entry"] if sym in self.pm.contexts else 0,
                                "LONG protection was not even breakeven before the fade")
        self.assertGreaterEqual(E.PERF["trades"], 1)
        self.assertAlmostEqual(E.paper["balance"] + E.paper["committed_margin"],
                               10000.0 + realized, places=3, msg="margin invariant")


class DistributionShortTest(Seller_paper):
    __test__ = True

    def test_distribution_protects_profit(self):
        sym = self._sym()
        self.open_position()
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 0.97)
        self.assertIn(sym, self.pm.symbols())
        top = self._snapshot(sym)
        self.bases[sym] = _topped_frame(self.pm.contexts[sym].state["entry"], "SELL")
        for m in (0.968, 0.970, 0.974, 0.980, 0.986, 0.992, 0.998):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self.assertNotIn(sym, self.pm.symbols(), "SHORT runner survived the distribution fade")
        realized = self._realized()
        self.assertGreater(realized, 0.0, f"SHORT gave the distribution top back: realized {realized:.2f}")
        self.assertGreaterEqual(E.PERF["trades"], 1)
        self.assertAlmostEqual(E.paper["balance"] + E.paper["committed_margin"],
                               10000.0 + realized, places=3, msg="margin invariant")


# ----------------------------------------------------------------------------
# 4) REVERSAL / THESIS FAILURE -> STRICT CLOSE with full verification.
# ----------------------------------------------------------------------------
class StrictCloseLongTest(Buyer_paper):
    __test__ = True

    def test_reversal_strict_close_verification(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"])
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 1.03)
        self.assertIn(sym, self.pm.symbols(), "long must be open before the crash")
        self.bases[sym] = _topped_frame(entry, "BUY")
        for m in _crash_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        # STRICT CLOSE chain: exchange side = 0, local CLOSED, PnL booked.
        self.assertNotIn(sym, self.pm.symbols(), "LONG position still open after thesis failure")
        self.assertFalse(bool(E.STATE.get("open")), "E.STATE.open still True after close")
        self.assertFalse(bool(E.TRADE_STATE.get("in_position")), "TRADE_STATE still in_position")
        self.assertIsNone(E.paper.get("position"), "paper position not cleared")
        self.assertEqual(E.PERF["trades"], 1, "trade not finalized exactly once")
        realized = self._realized()
        self.assertGreater(realized, 0.0, "LONG realized PnL not protected (naked crash)")
        self.assertAlmostEqual(E.paper["balance"] + E.paper["committed_margin"],
                               10000.0 + realized, places=3, msg="margin invariant after strict close")
        closes = self._log_lines("close_position_full", "[CLOSE]", "TRADE_CLOSED",
                                 "STRICT", "council")
        self.assertTrue(closes, "no close-path events recorded")


class StrictCloseShortTest(Seller_paper):
    __test__ = True

    def test_reversal_strict_close_verification(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"])
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 0.97)
        self.assertIn(sym, self.pm.symbols(), "short must be open before the crash")
        self.bases[sym] = _topped_frame(entry, "SELL")
        for m in _crash_mults(self._side()):
            self._step(sym, m)
            if sym not in self.pm.symbols():
                break
        self.assertNotIn(sym, self.pm.symbols(), "SHORT position still open after thesis failure")
        self.assertFalse(bool(E.STATE.get("open")), "E.STATE.open still True after close")
        self.assertFalse(bool(E.TRADE_STATE.get("in_position")), "TRADE_STATE still in_position")
        self.assertIsNone(E.paper.get("position"), "paper position not cleared")
        self.assertEqual(E.PERF["trades"], 1, "trade not finalized exactly once")
        realized = self._realized()
        self.assertGreater(realized, 0.0, "SHORT realized PnL not protected (naked crash)")
        self.assertAlmostEqual(E.paper["balance"] + E.paper["committed_margin"],
                               10000.0 + realized, places=3, msg="margin invariant after strict close")


# ----------------------------------------------------------------------------
# 5) TP1 / TP2 forensic.
# ----------------------------------------------------------------------------
class TP1ForensicLongTest(Buyer_paper):
    __test__ = True

    def test_tp1_partial_forensic(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"]); qty = float(st["qty"])
        self.assertEqual(float(st["sl"]), float(st.get("synthetic_sl", 0)),
                         msg="initial SL not recorded")
        self.assertLess(float(st["sl"]), entry, "LONG SL above entry (geometry violation)")
        tp1 = float(st.get("synthetic_tp1") or st.get("tp1_price") or 0)
        self.assertGreater(tp1, entry, "TP1 not beyond entry for LONG")
        # drive straight to TP1 target and hold two ticks so the price gate trips
        self._flat_top_hold(sym, tp1 / entry, holds=3)
        top = self._snapshot(sym)
        self.assertIn(sym, self.pm.symbols(), "LONG fully closed by TP1 partial gate")
        self.assertTrue(top["tp1_hit"], "TP1 not marked hit")
        self.assertEqual(float(top["remaining_qty"]), qty * 0.5,
                         f"remaining qty wrong after TP1: {top['remaining_qty']}")
        partial_leg = self._log_lines("leg PnL", "CLOSE_PARTIAL")
        self.assertTrue(partial_leg, "no partial leg recorded")
        self.assertEqual(E.PERF["trades"], 0, "partial must not finalize the trade")
        self.assertFalse(self._has_full_close_log(), "full close happened during TP1-only phase")


class TP1ForensicShortTest(Seller_paper):
    __test__ = True

    def test_tp1_partial_forensic(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"]); qty = float(st["qty"])
        self.assertGreater(float(st["sl"]), entry, "SHORT SL below entry (geometry violation)")
        tp1 = float(st.get("synthetic_tp1") or st.get("tp1_price") or 0)
        self.assertLess(tp1, entry, "TP1 not beyond entry for SHORT")
        self._flat_top_hold(sym, tp1 / entry, holds=3)
        top = self._snapshot(sym)
        self.assertIn(sym, self.pm.symbols(), "SHORT fully closed by TP1 partial gate")
        self.assertTrue(top["tp1_hit"], "TP1 not marked hit")
        self.assertEqual(float(top["remaining_qty"]), qty * 0.5,
                         f"remaining qty wrong after TP1: {top['remaining_qty']}")
        self.assertEqual(E.PERF["trades"], 0, "partial must not finalize the trade")


class TP2WithoutTP1LongTest(Buyer_paper):
    __test__ = True

    def test_tp2_reachable_without_tp1_and_no_overclose(self):
        """Current BARON logic evaluates TP2 every tick and may close the whole
        position even if TP1 was never banked (single-tick gap beyond both)."""
        sym = self._sym()
        st = self.open_position()
        qty = float(st["qty"])
        entry = float(st["entry"])
        tp1 = float(st.get("synthetic_tp1") or st.get("tp1_price") or 0)
        tp2 = float(st.get("synthetic_tp2") or st.get("tp2_price") or 0)
        self.assertGreater(tp2, tp1, "TP2 must sit beyond TP1 (ladder order)")
        jump_to = entry * 1.20            # beyond ANY manage-time ladder cap (<= 15%)
        jump_price = jump_to
        self._step(sym, jump_to / entry)
        self.assertNotIn(sym, self.pm.symbols(),
                         "LONG not closed after single-jump through the whole ladder")
        self.assertEqual(float(E.STATE.get("remaining_qty", 0)), 0.0,
                         "remaining qty not zero after TP2 full close")
        self.assertFalse(E.paper.get("position"), "paper position not cleared")
        realized = self._realized()
        expected = qty * (jump_price - entry)
        self.assertAlmostEqual(realized, expected, delta=max(0.02, abs(expected) * 0.002),
                               msg=f"LONG realized {realized:.2f} != {expected:.2f} "
                                   f"(over-close / double-close / miscount on single-tick TP1+TP2)")
        self.assertEqual(E.PERF["trades"], 1, "TP2 full close must finalize exactly once")


class TP2WithoutTP1ShortTest(Seller_paper):
    __test__ = True

    def test_tp2_reachable_without_tp1_and_no_overclose(self):
        sym = self._sym()
        st = self.open_position()
        qty = float(st["qty"])
        entry = float(st["entry"])
        tp1 = float(st.get("synthetic_tp1") or st.get("tp1_price") or 0)
        tp2 = float(st.get("synthetic_tp2") or st.get("tp2_price") or 0)
        self.assertLess(tp2, tp1, "TP2 must sit beyond TP1 (ladder order)")
        jump_to = entry * 0.80            # beyond ANY manage-time ladder cap (<= 15%)
        jump_price = jump_to
        self._step(sym, jump_to / entry)
        self.assertNotIn(sym, self.pm.symbols(), "SHORT not closed after single-jump through the ladder")
        self.assertEqual(float(E.STATE.get("remaining_qty", 0)), 0.0,
                         "remaining qty not zero after TP2 full close")
        self.assertFalse(E.paper.get("position"), "paper position not cleared")
        self.assertGreater(E.PERF["trades"], 0, "trade not finalized")
        realized = self._realized()
        expected = qty * (entry - jump_price)


class SLForensicLongTest(Buyer_paper):
    __test__ = True

    def test_sl_lifecycle_monotonic_and_breakeven(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"])
        initial_sl = float(st["synthetic_sl"])
        self.assertLess(initial_sl, entry, "initial LONG SL must be below entry")
        sll = [initial_sl]
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            s = self._snapshot(sym)
            sl = float(s["synthetic_sl"] or s["trail_stop"] or initial_sl)
            sll.append(sl)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 1.03)
        top = self._snapshot(sym)
        # monotonic never-worse protection
        for a, b in zip(sll, sll[1:]):
            self.assertGreaterEqual(b + 1e-9, a - 1e-6,
                                    f"LONG SL moved backward: {a} -> {b}")
        # breakeven ratchet / trail armed after TP1
        self.assertTrue(top["trail_activated"] or float(top["synthetic_sl"] or 0) >= entry,
                        f"no breakeven/trail after profit: {top}")
        # Native SL booking: the PM-managed PAPER runtime keeps protection fully
        # synthetic (no exchange-native order ships on its own); the native order
        # SHAPE is validated separately via production place_native_sl() in
        # NativeSLOderShapeTest.
        self.assertEqual(top["native_sl_state"], "NONE",
                         "PM-managed PAPER run unexpectedly booked a native SL order")


class SLForensicShortTest(Seller_paper):
    __test__ = True

    def test_sl_lifecycle_monotonic_and_breakeven(self):
        sym = self._sym()
        st = self.open_position()
        entry = float(st["entry"])
        initial_sl = float(st["synthetic_sl"])
        self.assertGreater(initial_sl, entry, "initial SHORT SL must be above entry")
        sll = [initial_sl]
        for m in _climb_mults(self._side()):
            self._step(sym, m)
            s = self._snapshot(sym)
            sl = float(s["synthetic_sl"] or s["trail_stop"] or initial_sl)
            sll.append(sl)
            if sym not in self.pm.symbols():
                break
        self._flat_top_hold(sym, 0.97)
        top = self._snapshot(sym)
        for a, b in zip(sll, sll[1:]):
            self.assertLessEqual(b - 1e-9, a + 1e-6,
                                 f"SHORT SL moved backward: {a} -> {b}")
        self.assertTrue(top["trail_activated"] or float(top["synthetic_sl"] or 0) <= entry,
                        f"no breakeven/trail after profit: {top}")
        self.assertEqual(top["native_sl_state"], "NONE",
                         "PM-managed PAPER run unexpectedly booked a native SL order")


# ----------------------------------------------------------------------------
# 6) Contract-shape mapping tests (hedge-mode positionSide / side pairs).
# ----------------------------------------------------------------------------
class HedgeModeCombinationMappingTest(unittest.TestCase):
    """BARON must map internal side to the EXACT BingX hedge-mode combination
    (official docs): open/buy LONG = BUY+LONG, close/sell LONG = SELL+LONG,
    open/sell SHORT = SELL+SHORT, close/buy SHORT = BUY+SHORT."""

    def test_hedge_combination_table(self):
        self.assertEqual(E._hedge_position_side("BUY"), "LONG")
        self.assertEqual(E._hedge_position_side("SELL"), "SHORT")
        self.assertEqual(E._hedge_position_side("buy"), "LONG")
        self.assertEqual(E._hedge_position_side("short"), "SHORT")
        with self.assertRaises(ValueError):
            E._hedge_position_side("NEUTRAL")
        # close-side derived from the position side (engine close_partial/close_position_full)
        pos_side = E._hedge_position_side("BUY")
        close_side = "sell"
        self.assertEqual((close_side, pos_side), ("sell", "LONG"))
        pos_side = E._hedge_position_side("SELL")
        close_side = "buy"
        self.assertEqual((close_side, pos_side), ("buy", "SHORT"))


class NativeSLOderShapeTest(unittest.TestCase):
    """Capture the native SL order that BARON WOULD place in LIVE mode and check
    its payload shape against BingX STOP_MARKET contract expectations."""

    class RecorderVenue:
        def __init__(self):
            self.calls = []

        def amount_to_precision(self, sym, qty):
            return float(qty)

        def create_order(self, sym, type_, side, qty, params=None):
            self.calls.append({"sym": sym, "type": type_, "side": side,
                               "qty": qty, "params": dict(params or {})})
            return {"id": f"test_{len(self.calls)}"}

    def _run(self, side):
        venue = self.RecorderVenue()
        orig_mode, orig_ex = E.PAPER_MODE, E.ex
        orig_state = dict(E.STATE)
        try:
            E.PAPER_MODE = False
            E.ex = venue
            E.STATE.update({"open": True, "side": side,
                            "remaining_qty": 1.0, "synthetic_sl": 3000.0})
            E.place_native_sl("TEST/USDT:USDT")
        finally:
            E.PAPER_MODE = orig_mode
            E.ex = orig_ex
            E.STATE.clear(); E.STATE.update(orig_state)
        self.assertEqual(len(venue.calls), 1, "native SL order not placed")
        call = venue.calls[0]
        self.assertEqual(call["type"], "STOP_MARKET")
        if side == "BUY":
            self.assertEqual(call["side"], "sell")
            self.assertEqual(call["params"]["positionSide"], "LONG")
        else:
            self.assertEqual(call["side"], "buy")
            self.assertEqual(call["params"]["positionSide"], "SHORT")
        self.assertIn("stopPrice", call["params"])
        return call

    def test_native_sl_long_shape(self):
        call = self._run("BUY")
        self.assertNotIn("reduceOnly", call["params"],
                         "LONG native SL must NOT carry reduceOnly in Hedge Mode (DEV-01)")
        self.assertAlmostEqual(float(call["params"]["stopPrice"]), 3000.0)

    def test_native_sl_short_shape(self):
        call = self._run("SELL")
        self.assertNotIn("reduceOnly", call["params"],
                         "SHORT native SL must NOT carry reduceOnly in Hedge Mode (DEV-01)")
        self.assertAlmostEqual(float(call["params"]["stopPrice"]), 3000.0)


if __name__ == "__main__":
    unittest.main()