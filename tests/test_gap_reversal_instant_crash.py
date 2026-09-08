"""Gap Reversal / Instant Crash runtime test (additive, no production edits).

This is the second runtime simulation requested after the SNIPER-vs-BARON
audit. It drives the REAL engine through the REAL portfolio-manage loop, but
this time against a ORDER-LEVEL fake venue instead of the PAPER ledger, so the
live (non-PAPER) order path is exercised end-to-end:

  Phase A - CLIMB:   the position ramps (trending frame) until it reaches
                     >= +20% ROE (price +2.0% at 10x leverage); assert the
                     profit-protection ladder is ARMED above break-even.
  Phase B - GAP:     in a SINGLE tick update the mark jumps straight past the
                     profit lock / trailing floor (one instant crash bar, not a
                     multi-step grind). No TP/partial is expected before the
                     crash, so the exit must come from the protection ladder.

The test then answers the exchange-fill checklist WITHOUT touching production
code:

  - Was a close order actually sent?            (venue order log)
  - Was it reduceOnly=True + correct positionSide? (order params)
  - Was the fill verified?                      (real verify_order_filled ->
                                                 venue fetch_order 'closed')
  - Was the position re-read from the venue?    (fetch_positions after close)
  - Any quantity left open?                     (venue qty == 0)
  - Does local state match the venue?           (STATE.open == False, qty 0)
  - What is the final realized PnL?             (PERF total == venue-derived)

CRITICAL DISTINCTION (per the audit finding):
If the engine were BROKEN the assertions on order/reduceOnly/verify/re-read/
consistency would fail. A NEGATIVE realized number after an instant gap is NOT
such a failure: it is the natural slippage of a single-bar crash - the venue
fills the reduce-only close AT the gap price. The test therefore asserts the
realized value equals EXACTLY what the venue's own fills imply
(sig * (fill - entry) * qty), i.e. the loss is fully accounted for by the gap
fill, and that the outcome is never WORSE than a naked position closed at the
same gap price (protection cost nothing extra).

Nothing is copied from the audited SNIPER script; this mirrors the repo's own
harness (real engine + PortfolioManager + the order-fake pattern introduced by
tests/test_position_side_lifecycle.py).
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

import core.engine as E  # noqa: E402


def _trend_frame(price, side="BUY"):
    """Clean persistent trend ending exactly at `price` (trending market used
    for the ENTRY and the whole CLIMB since the AUDIT shows the engine must see
    genuine continuation while the protection ladder arms)."""
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


class FakeVenue:
    """Order-level fake BingX venue (same shape as tests/test_position_side_lifecycle.py).

    Stateful on the position: a plain 'market' entry order OPENS the position,
    a 'market' reduceOnly order REDUCES/CLOSES it at the current venue price.
    STOP_MARKET orders (native SL placement) are recorded but never move the
    position. Every fetch_* reflects the resulting state in real time, so the
    engine really verifies fills and re-reads the position from this venue.

    The venue price is driven by the test (set `fx.price` before each tick ==
    the live mark), which is exactly how an instant crash is modelled: one
    single update of the price -> the next manage tick sees the gap.
    """

    LEVERAGE = 10.0

    def __init__(self, symbol, price):
        self.symbol = symbol
        self.price = price
        self.orders = {}          # order_id -> order dict (all orders)
        self.orders_by_cid = {}   # clientOrderId -> order_id
        self._seq = 0
        self.fills = []           # market-fill ledger (entry + closes)
        self.position = None      # None or {"qty", "entry"} for this venue
        self.fetch_order_calls = 0
        self.fetch_positions_results = []   # snapshots after each call
        self.reduce_only_closes = []        # (side, qty, params) closes
        self.markets = {
            symbol: {
                "id": symbol,
                "symbol": symbol,
                "limits": {"amount": {"min": 0.0001}},
                "precision": {"amount": 0.000001},
                "active": True,
                "type": "swap",
                "spot": False,
                "swap": True,
            }
        }

    # ---- helpers ----
    def _next_id(self):
        self._seq += 1
        return f"fx-{self._seq}"

    def _apply_market_fill(self, side, amount, params):
        """Book an order, and for real market orders move the venue position."""
        oid = self._next_id()
        filled = float(amount)
        order = {
            "id": oid,
            "clientOrderId": params.get("clientOrderId"),
            "symbol": self.symbol,
            "type": "market",
            "side": side,
            "price": self.price,
            "amount": filled,
            "filled": filled,
            "average": self.price,
            "status": "closed",
            "reduceOnly": bool(params.get("reduceOnly", False)),
            "positionSide": params.get("positionSide"),
        }
        self.orders[oid] = order
        if params.get("clientOrderId"):
            self.orders_by_cid[params["clientOrderId"]] = oid

        if bool(params.get("reduceOnly", False)):
            cur = self.position
            if cur is not None:
                qty = cur["qty"]
                closed = min(filled, qty)
                cur["qty"] = qty - closed
                self.fills.append({"id": oid, "symbol": self.symbol, "side": side,
                                   "amount": closed, "price": self.price,
                                   "fee": {"cost": 0.0, "currency": "USDT"},
                                   "timestamp": time.time()})
                if cur["qty"] <= 0:
                    self.position = None
            self.reduce_only_closes.append((side, closed if cur is not None else filled,
                                            dict(params)))
        else:
            self.position = {"symbol": self.symbol, "qty": filled, "entry": self.price,
                             "side": "short" if side == "sell" else "long"}
            self.fills.append({"id": oid, "symbol": self.symbol, "side": side,
                               "amount": filled, "price": self.price,
                               "fee": {"cost": 0.0, "currency": "USDT"},
                               "timestamp": time.time()})
        return order

    # ---- ccxt surface ----
    def load_markets(self, *a, **k):
        return self.markets

    def market(self, sym):
        return self.markets.get(sym, self.markets.get(self.symbol))

    def set_leverage(self, leverage, symbol=None, *a, **k):
        return {"leverage": leverage}

    def amount_to_precision(self, sym, amount):
        return float(amount)

    def fetch_balance(self, *a, **k):
        return {"info": {}, "free": {"USDT": 10000.0},
                "used": {"USDT": 0.0}, "total": {"USDT": 10000.0}}

    def fetch_ticker(self, sym=None):
        return {"symbol": sym or self.symbol, "last": self.price}

    def fetch_order_book(self, sym=None, limit=None, *a, **k):
        p = self.price
        return {"bids": [[p * 0.9998, 10.0]], "asks": [[p * 1.0002, 10.0]],
                "timestamp": time.time()}

    def create_order(self, sym, order_type, side, amount, price=None, params=None):
        params = params or {}
        otype = str(order_type or "").lower()
        if otype == "market":
            return self._apply_market_fill(side, amount, params)
        # STOP_MARKET (or any non-market) - native SL placement: recorded only.
        oid = self._next_id()
        order = {
            "id": oid,
            "clientOrderId": params.get("clientOrderId"),
            "symbol": sym,
            "type": otype,
            "side": side,
            "price": price,
            "params": params,
            "status": "open",
            "filled": 0.0,
            "reduceOnly": bool(params.get("reduceOnly", False)),
            "positionSide": params.get("positionSide"),
        }
        self.orders[oid] = order
        return order

    def cancel_order(self, order_id, sym=None, *a, **k):
        if order_id in self.orders:
            self.orders[order_id]["status"] = "canceled"
        return {"id": order_id, "status": "canceled"}

    def fetch_order(self, order_id, sym=None, *a, **k):
        self.fetch_order_calls += 1
        order = self.orders.get(order_id)
        if order is None and isinstance(order_id, str):
            oid = self.orders_by_cid.get(order_id)
            if oid is not None:
                order = self.orders.get(oid)
        if order is None:
            return {"id": order_id, "status": "closed", "filled": 0.0}
        # Replicate the venue: a market fill is immediately closed & filled.
        if order["type"] == "market":
            order["status"] = "closed"
            order["filled"] = float(order.get("filled", 0.0) or order.get("amount", 0.0))
        return dict(order)

    def fetch_positions(self, *a, **k):
        if self.position is None:
            self.fetch_positions_results.append([])
            return []
        cur = self.position
        qty = cur["qty"]
        entry = cur["entry"]
        sign = 1.0 if self.position.get("side", "LONG") != "short" else -1.0
        mark = self.price
        unrealized = (mark - entry) * qty * sign
        margin = (entry * qty) / self.LEVERAGE
        pos = {
            "symbol": cur["symbol"],
            "contracts": qty,
            "side": "long" if sign > 0 else "short",
            "entryPrice": entry,
            "markPrice": mark,
            "unrealizedPnl": unrealized,
            "initialMargin": margin,
            "leverage": self.LEVERAGE,
            "liquidationPrice": entry * (0.9 if sign > 0 else 1.1),
            "positionSide": "LONG" if sign > 0 else "SHORT",
        }
        self.fetch_positions_results.append([pos])
        return [pos]

    def fetch_my_trades(self, sym=None, *a, **k):
        return list(self.fills)

    def fetch_ohlcv(self, *a, **k):
        raise RuntimeError("fetch_ohlcv should not be hit (provider replaced)")


class GapReversalInstantCrashTest(unittest.TestCase):
    """One BUY and one SELL: reach >= +20% ROE, then a SINGLE-tick gap past the
    protection floor. Verifies the live order-close checklist against the
    order-level venue. Abstract scaffold (`__test__ = False`)."""

    __test__ = False

    ENV = {
        "PAPER_MODE": "False",
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

    def setUp(self):
        from portfolio.manager import PortfolioManager
        self._orig_log_execution = E.log_execution
        self._env = {k: os.environ.get(k) for k in self.ENV}
        for k, v in self.ENV.items():
            os.environ[k] = v

        self._reset()
        # Baseline snapshot: this test runs the REAL non-PAPER engine, which
        # mutates module-level state (STATE/PERF/MEMORY etc.). Restoring the
        # exact pre-test snapshots in tearDown keeps every OTHER harness seeing
        # the same pristine import-time state they would see if this file never
        # ran (they deep-copy module STATE in their own _reset).
        self._state_baseline = copy.deepcopy(E.STATE)
        self._tsnap_baseline = copy.deepcopy(E.TRADE_STATE)
        self._dsnap_baseline = copy.deepcopy(E.DASHBOARD_STATE)
        self._perf_baseline = copy.deepcopy(E.PERF)
        self._paper_baseline = copy.deepcopy(E.paper)
        self._mem_baseline = copy.deepcopy(E.MEMORY)
        self._lifecycle_baseline = getattr(E._live_manager, "lifecycle_state", None)
        self.logs = []
        E.log_execution = lambda s, *a, **k: self.logs.append(
            str(s).encode("ascii", "replace").decode("ascii"))
        self._ifvg = patch.object(E, "ifvg_warning_payload",
                                  side_effect=lambda *a, **k: dict(self.CLEAR_PAYLOAD))
        self._adx = patch.object(E, "compute_adx",
                                 side_effect=lambda df, period=14: pd.Series(
                                     [30.0] * len(df), index=df.index))
        self._liq = patch.object(E, "detect_liquidity_context",
                                 side_effect=lambda df, lookback=10: (
                                     "buy_side_taken"
                                     if float(df["close"].iloc[-1]) > float(df["open"].iloc[-1])
                                     else "sell_side_taken"))
        self._ifvg.start()
        self._adx.start()
        self._liq.start()

        # ---- REAL (non-PAPER) runtime against the order-level venue ----
        c = self._entry_candidates()[0]
        self.venue = FakeVenue(c["symbol"], c["price"])
        self._orig_ex = E.ex
        self._orig_om = E._order_manager
        E.PAPER_MODE = False
        E.ex = self.venue
        E._order_manager = E.OrderManager(self.venue, max_retries=3,
                                          retry_delay=0.5, confirm_timeout=6.0)
        E.INSUFFICIENT_MARGIN_COOLDOWN_UNTIL = 0.0
        E._closing_in_progress = False
        E._reconciliation_pending = False
        # Venue spread seam: the fake order book is not the object of this test,
        # so neutralise the OPEN spread gate (pattern from test_position_side_lifecycle).
        self._orig_get_spread = E.get_spread_bps
        E.get_spread_bps = lambda *a, **k: 0.0

        self.pm = PortfolioManager(2, E)
        self.pm.bind(E)
        self._prime([c])
        # Isolate the engine singletons from THIS test: the real non-PAPER run
        # mutates the shared LiveTradeManager/ExchangeSyncService internals on
        # the module globals. Swap in fresh instances for the duration of this
        # test and restore the pristine module originals in tearDown, so later
        # harnesses see exactly the import-time singletons.
        self._orig_live_manager = E._live_manager
        self._orig_exchange_sync = E._exchange_sync
        E._live_manager = E.LiveTradeManager(E._event_bus, E._exchange_sync, E._recovery_guard)
        E._exchange_sync = E.ExchangeSyncService(E._event_bus)
        opened = self.pm.open_top([self._cand(c)], slots=1)
        self.assertEqual(opened, 1, f"open_top failed for {self._sym()}")

    def tearDown(self):
        self._liq.stop()
        self._adx.stop()
        self._ifvg.stop()
        E.get_spread_bps = getattr(self, "_orig_get_spread", E.get_spread_bps)
        E.PAPER_MODE = True
        E.ex = getattr(self, "_orig_ex", E.ex)
        E._order_manager = getattr(self, "_orig_om", E._order_manager)
        try:
            E._exchange_sync._last_snapshot = E.PositionSnapshot()
        except Exception:
            pass
        # get_live_hybrid_df caches the last candle's high/low per symbol keyed
        # by candle timestamp; each harness reuses the full RangeIndex (0..n),
        # so a stale cache from THIS run mis-shapes later frames (DI/ADX). Restore.
        for _cache in (E._live_high, E._live_low, E._last_candle_timestamp):
            try:
                _cache.clear()
            except Exception:
                pass
        E._live_manager = getattr(self, "_orig_live_manager", E._live_manager)
        E._exchange_sync = getattr(self, "_orig_exchange_sync", E._exchange_sync)
        try:
            if getattr(self, "_lifecycle_baseline", None) is not None:
                E._live_manager.lifecycle_state = self._lifecycle_baseline
        except Exception:
            pass
        # Full module-state restoration so later harnesses start pristine.
        try:
            E.STATE.clear(); E.STATE.update(copy.deepcopy(getattr(self, "_state_baseline", {})))
            E.TRADE_STATE.clear(); E.TRADE_STATE.update(copy.deepcopy(getattr(self, "_tsnap_baseline", {})))
            E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(copy.deepcopy(getattr(self, "_dsnap_baseline", {})))
            E.PERF.clear(); E.PERF.update(copy.deepcopy(getattr(self, "_perf_baseline", {})))
            E.paper.clear(); E.paper.update(copy.deepcopy(getattr(self, "_paper_baseline", {})))
            E.MEMORY.clear(); E.MEMORY.update(copy.deepcopy(getattr(self, "_mem_baseline", {})))
        except Exception:
            pass
        E.log_execution = self._orig_log_execution
        for k, saved in self._env.items():
            if saved is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = saved

    # ---- scenario shape ----
    def _entry_candidates(self):
        raise NotImplementedError

    def _sym(self):
        raise NotImplementedError

    def _side(self):
        return "BUY"

    def _top_mult(self):
        """Price multiplier at >= +20% ROE: +2.0% for BUY, -2.0% for SELL."""
        return 1.02 if self._side() == "BUY" else 0.98

    def _gap_mult(self):
        """The single instant-crash bar: 4% gap straight through every
        protective level (BUY drops, SELL rockets)."""
        return 0.96 if self._side() == "BUY" else 1.04

    # ---- harness plumbing ----
    def _reset(self):
        import copy
        _snap, _tsnap, _dsnap = (copy.deepcopy(E.STATE),
                                 copy.deepcopy(E.TRADE_STATE),
                                 copy.deepcopy(E.DASHBOARD_STATE))
        E.STATE.clear(); E.STATE.update(_snap)
        E.TRADE_STATE.clear(); E.TRADE_STATE.update(_tsnap)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(_dsnap)
        E.paper.update({"balance": 10000.0, "position": None, "committed_margin": 0.0})
        E.PERF.update({"trades": 0, "wins": 0, "losses": 0, "total_pnl_usdt": 0.0,
                       "total_pnl_pct": 0.0, "last_trade": {}})
        E.log_execution = lambda *a, **k: None

    def _cand(self, cand, score=85.0):
        p = cand["price"]; side = cand["side"]
        return {"symbol": cand["symbol"], "side": side, "price": p,
                "sl": p * (0.98 if side == "BUY" else 1.02),
                "tp1": p * (1.03 if side == "BUY" else 0.97),
                "tp2": p * (1.06 if side == "BUY" else 0.94),
                "score": score, "atr": p * 0.01, "asset_class": cand["asset_class"],
                "trade_id": cand["symbol"]}

    def _prime(self, candidates):
        self.live = {}
        self.bases = {}
        for c in candidates:
            self.bases[c["symbol"]] = _trend_frame(c["price"], c["side"])
            self.live[c["symbol"]] = c["price"]
        E.get_ohlcv_safe = lambda sym, limit=120, htf=False: self._ohlcv(sym, limit, htf)
        E.get_ticker_safe = lambda sym, retries=0, **k: self.live.get(sym)
        E.get_orderbook_cached = lambda sym, limit=20, **k: {
            "bids": [[self.live.get(sym, 1000.0) * 0.999, 10.0]],
            "asks": [[self.live.get(sym, 1000.0) * 1.001, 10.0]],
        }

    def _ohlcv(self, sym, limit=120, htf=False):
        df = self.bases[sym].copy()
        last = df.index[-1]
        live = self.live[sym]
        df.loc[last, "close"] = live
        body = live * (0.001 if self._side() == "SELL" else -0.001)
        df.loc[last, "open"] = live - body
        df.loc[last, "high"] = max(float(df.loc[last, "high"]), live)
        df.loc[last, "low"] = min(float(df.loc[last, "low"]), live)
        df = df.iloc[-min(limit, len(df)):]
        df.attrs["ifvg_sym"] = bool(self.bases[sym].attrs.get("ifvg_sym", False))
        return df

    def _advance_clock(self):
        for ctx in self.pm.contexts.values():
            m = ctx.live_manager
            m.last_management_ts = 0.0
            m.last_heavy_calc_ts = 0.0
            m.last_position_sync_ts = 0.0
            m.last_live_debug_ts = 0.0
            m.last_log_ts = 0.0

    def _snapshot(self, sym):
        ctx = self.pm.contexts.get(sym)
        if ctx is None:
            return None
        s = ctx.state
        return {
            "roe": s.get("roe_pct", 0.0),
            "tp1_hit": bool(s.get("tp1_hit", False)),
            "runner_mode": bool(s.get("runner_mode", False)),
            "trail_activated": bool(s.get("trail_activated", False)),
            "protection_state": s.get("protection_state"),
            "synthetic_sl": s.get("synthetic_sl"),
            "trail_stop": s.get("trail_stop"),
            "peak_roe": s.get("peak_roe", 0.0),
            "trade_state": s.get("trade_state"),
        }

    def _touch(self, sym, mult):
        """One venue price update == the market tick. The venue fill price ALWAYS
        equals this, so a single update is a single instant-crash bar."""
        price = self.pm.contexts[sym].state["entry"] * mult
        self.live[sym] = price
        self.venue.price = price

    def _manage(self, sym):
        self._advance_clock()
        self.pm.manage_all()

    def _step(self, sym, mult):
        self._touch(sym, mult)
        self._manage(sym)

    def _flat_top_hold(self, sym, mult, holds=2):
        for _ in range(holds):
            self._touch(sym, mult)
            self._manage(sym)

    # ---- the scenario ----
    def _run_gap_reversal(self):
        sym = self._sym()
        side = self._side()
        entry = self.pm.contexts[sym].state["entry"]
        top = self._top_mult()
        gap = self._gap_mult()

        # Phase A: climb on a healthy trending frame to >= +20% ROE.
        ramp = np.linspace(1.0, top, 5)
        trace = []
        for f in ramp[1:]:
            self._step(sym, f)
            trace.append(self._snapshot(sym))
            if sym not in self.pm.symbols():
                break
        self.assertIn(sym, self.pm.symbols(),
                      f"{side} closed DURING the climb - pre-exit instead of gap test")
        self._flat_top_hold(sym, top, holds=2)
        top_state = self._snapshot(sym)
        trace.append(top_state)
        self.assertIn(sym, self.pm.symbols(),
                      f"{side} closed while seated on the +20% ROE top")
        max_roe = max(float(step["roe"]) for step in trace)
        self.assertGreaterEqual(max_roe, 18.0,
                                f"{side} never reached +20% ROE (max={max_roe:.2f}%)")
        # Protection ladder armed above break-even BEFORE the gap.
        self.assertTrue(
            top_state["trail_activated"] or top_state["tp1_hit"] or
            top_state["protection_state"] in ("BREAKEVEN", "PROFIT_LOCK"),
            f"{side} no protection armed at +{max_roe:.2f}% ROE: {top_state}")
        # No TP1 is hit on this climb (peak +20% ROE < tp1 +3%), so the
        # synthetic SL is still under entry and the ARMED ratchet is the trail
        # floor. Prefer the trail floor when the trail actually sits past entry.
        trail_floor = top_state["trail_stop"]
        sl_floor = top_state["synthetic_sl"]
        if top_state["trail_activated"] and float(trail_floor or 0):
            floor = trail_floor
        else:
            floor = sl_floor
        floor = floor if float(floor or 0) else None
        if side == "BUY":
            self.assertGreaterEqual(float(floor or 0), entry,
                                    f"{side} protective SL not above entry: {floor}")
        else:
            self.assertLessEqual(float(floor or 0), entry,
                                 f"{side} protective SL not below entry: {floor}")

        # ---- Baseline before the crash ----
        closes_before = list(self.venue.reduce_only_closes)
        fetch_orders_before = self.venue.fetch_order_calls
        fetches_before = len(self.venue.fetch_positions_results)
        st_pre = self.pm.contexts[sym].state
        self.qty_before_gap = float(st_pre.get("remaining_qty",
                                               st_pre.get("qty", 0.0)) or 0.0)
        self.gap_venue_qty = (self.venue.position or {}).get("qty", 0.0)

        # Phase B: ONE single price update crossing every protective level.
        self._step(sym, gap)

        # ---- POST-CRASH ORDER CHECKLIST ----
        self.assertNotIn(sym, self.pm.symbols(),
                         f"{side} runner survived the instant crash")
        new_closes = self.venue.reduce_only_closes[len(closes_before):]
        self.assertTrue(new_closes,
                        f"{side} NO reduce-only close order was sent on the gap")
        close_side = "sell" if side == "BUY" else "buy"
        pos_side = "LONG" if side == "BUY" else "SHORT"
        for c_side, c_qty, c_params in new_closes:
            self.assertTrue(c_params.get("reduceOnly") is True,
                            f"{side} close order not reduceOnly: {c_params}")
            self.assertEqual(c_params.get("positionSide"), pos_side,
                             f"{side} close order wrong positionSide: {c_params}")
            if c_params.get("positionSide") is not None:
                self.assertEqual(c_side, close_side,
                                 f"{side} close order wrong side: {c_side}")
        total_closed = sum(float(q) for _, q, _ in new_closes)
        self.assertAlmostEqual(total_closed, self.qty_before_gap, places=4,
                               msg=f"{side} closed qty {total_closed} != remaining {self.qty_before_gap}")
        if self.qty_before_gap > 0:
            self.assertAlmostEqual(float(self.gap_venue_qty), self.qty_before_gap, places=4,
                                   msg=f"{side} venue position diverged from local before the gap")

        # Fill was verified through the venue (real verify_order_filled path).
        self.assertGreater(self.venue.fetch_order_calls, fetch_orders_before,
                           f"{side} verify_order_filled never polled the venue")
        # Position re-read from the venue after the close.
        self.assertGreater(len(self.venue.fetch_positions_results), fetches_before,
                           f"{side} position was never re-read from the venue")
        self.assertTrue(any(len(r) == 0 for r in self.venue.fetch_positions_results[-3:]),
                        f"{side} venue still reports an open position after the close")
        # No quantity left open (venue AND local).
        self.assertIsNone(self.venue.position,
                          f"{side} venue position not closed: {self.venue.position}")
        st = self.pm.contexts[sym].state if sym in self.pm.contexts else None
        if st is not None:
            self.assertEqual(float(st.get("remaining_qty", 0.0) or 0.0), 0.0,
                             f"{side} local remaining_qty != 0 after close")
        self.assertFalse(bool(E.STATE.get("open")),
                         f"{side} STATE.open still True after the close")
        self.assertFalse(bool(E.TRADE_STATE.get("in_position")),
                         f"{side} TRADE_STATE.in_position still True after the close")
        # Local == exchange: both sides agree the book is flat.
        self.assertEqual(E.PERF.get("trades"), 1,
                         f"{side} PERF trades != 1: {E.PERF}")

        # ---- REALIZED PNL: exactly the venue fills imply ----
        realized = float(E.PERF["total_pnl_usdt"])
        sign = 1.0 if side == "BUY" else -1.0
        expected = 0.0
        for f in self.venue.fills:
            f_sign = 1.0 if f["side"] == "sell" else -1.0  # + sells, - buys
            expected += f_sign * f["amount"] * f["price"]
        # For a none-la a single-position buy/sell the ledger sums to
        #   BUY:  -Q*E + Q*F = Q*(F-E);   SELL: +Q*E - Q*F = Q*(E-F)
        self.assertAlmostEqual(realized, expected, places=1,
                               msg=f"{side} realized {realized:.2f} != venue {expected:.2f}")
        # Natural-slippage disambiguation: the realized value must equal the
        # same position closed at the gap price (naked baseline), because the
        # gap absolutely jumped the whole ladder in one bar. If the engine had
        # failed, realized would diverge from the venue ledger - instead the
        # (negative) PnL is exactly the venue's gap fill.
        naked = sign * (gap - 1.0) * entry * self.venue_original_qty
        slippage = naked
        self.assertGreaterEqual(realized, naked - 0.75,
                                f"{side} realized {realized:.2f} worse than the "
                                f"naked gap fill {naked:.2f} - protection cost money")
        reason = self._exit_reason()
        self.assertTrue(reason, f"{side} no exit reason recorded")
        self.report = {
            "side": side, "entry": entry, "max_roe": max_roe,
            "top_state": top_state, "gap_mult": gap,
            "closed_orders": new_closes, "venue_order_count": len(self.venue.orders),
            "realized_usdt": realized, "expected_usdt": expected,
            "gap_slippage_usdt": slippage, "exit_reason": reason,
            "verify_order_calls": self.venue.fetch_order_calls,
            "position_fetches": len(self.venue.fetch_positions_results),
            "trace": trace, "logs": self.logs,
        }
        return self.report

    def _exit_reason(self):
        for line in reversed(self.logs):
            for token in ("PROFIT_ENGINE_EXIT", "TRAILING_STOP", "COUNCIL_EXIT",
                          "PROFIT_LOCK", "SYNTHETIC_SL", "STOP_HIT", "EXIT_REASON"):
                if token in line:
                    return token
        last = E.PERF.get("last_trade") or {}
        return last.get("exit_reason") or last.get("reason") or ""

    def _venue_original_qty(self):
        return self.entry_qty

    def test_gap_reversal_instant_crash(self):
        sym = self._sym()
        st = self.pm.contexts[sym].state
        self.entry_qty = float(st.get("qty", 0.0))
        self.venue_original_qty = self.entry_qty
        self.assertGreater(self.entry_qty, 0.0,
                           f"{self._side()} entry qty not set")
        rep = self._run_gap_reversal()
        self.assertEqual(E.PERF["trades"], 1)
        last = E.PERF.get("last_trade") or {}
        realized = float(E.PERF["total_pnl_usdt"])
        # The report must EXPLICITLY distinguish engine failure from slippage:
        # exit reason + verify + re-read + reduceOnly + qty==0 are the engine's
        # duty (all asserted above); the realized number is the venue's fill.
        self.report = rep


class BuyGapReversalInstantCrashTest(GapReversalInstantCrashTest):
    __test__ = True

    def _entry_candidates(self):
        return [{"symbol": "BTC/USDT:USDT", "side": "BUY", "price": 60000.0,
                 "asset_class": "CRYPTO"}]

    def _sym(self):
        return "BTC/USDT:USDT"

    def _side(self):
        return "BUY"


class SellGapReversalInstantCrashTest(GapReversalInstantCrashTest):
    __test__ = True

    def _entry_candidates(self):
        return [{"symbol": "ETH/USDT:USDT", "side": "SELL", "price": 3000.0,
                 "asset_class": "CRYPTO"}]

    def _sym(self):
        return "ETH/USDT:USDT"

    def _side(self):
        return "SELL"


if __name__ == "__main__":
    unittest.main()