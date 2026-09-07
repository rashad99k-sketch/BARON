"""Six-position runtime validation harness (PAPER mode, deterministic).

Drives the REAL PortfolioManager + LiveTradeManager + engine in PAPER mode
against a simulated six-slot market feed, one independent lifecycle per
position, plus a restart/recovery sub-run and a failure-injection chapter.

This is a *runtime validation* harness, not a unit test.  It exercises the
production code paths end to end (open_top -> manage_all -> close_symbol ->
restore_from_exchange) and collects tamper-evident journal + accounting
evidence for docs/SIX_POSITION_RUNTIME_VALIDATION_REPORT.md.

It never contacts BingX.  It stubs the ccxt/flask import boundary exactly like
tools/paper_runtime_smoke.py and patches the market-feed seams (ohlcv / ticker
/ orderbook) like tests/test_profit_engine_phase3.py.  All risk gates and
safety checks run REAL and untouched.

Usage:  python tools/six_position_runtime_validation.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import time
import types
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

JOURNAL = ROOT / "logs" / "six_position_validation.jsonl"
EVIDENCE = ROOT / "logs" / "six_position_validation_evidence.json"

os.environ.update({
    "PAPER_MODE": "True",
    "BINGX_KEY": "",
    "BINGX_SECRET": "",
    "NEWS_ENABLED": "False",
    "POSITION_MARGIN_PCT": "0.10",
    "PORTFOLIO_MARGIN_CAP_PCT": "0.60",
    "MAX_DAILY_LOSS_PCT": "20",
    "MAX_CONSECUTIVE_LOSSES": "3",
    "MAX_POSITIONS_PER_ASSET_CLASS": "2",
    "DEEP_SCAN_WATCHLIST_SIZE": "5",
    "DEEP_WATCHLIST_SIZE": "5",
    "DEEP_SCAN_RADAR_SYMBOLS": "0",
    "WATCHLIST_DEEP_BATCH_SIZE": "5",
    "WATCHLIST_DEEP_INTERVAL_SEC": "0",
    "USE_EXECUTION_QUEUE": "True",
    "DECISION_JOURNAL_PATH": str(JOURNAL),
})


class FakeExchange:
    def __init__(self, *args, **kwargs):
        self.markets = {
            "BTC/USDT:USDT": {"base": "BTC", "quote": "USDT", "type": "swap", "active": True},
            "ETH/USDT:USDT": {"base": "ETH", "quote": "USDT", "type": "swap", "active": True},
            "US500/USDT:USDT": {"base": "US500", "quote": "USDT", "type": "swap", "active": True},
            "USTECH/USDT:USDT": {"base": "USTECH", "quote": "USDT", "type": "swap", "active": True},
            "XAUUSD": {"base": "XAU", "quote": "USD", "type": "swap", "active": True},
            "WTI": {"base": "WTI", "quote": "USD", "type": "swap", "active": True},
        }

    def load_markets(self):
        return self.markets


ccxt_stub = types.ModuleType("ccxt")
ccxt_stub.bingx = FakeExchange
sys.modules["ccxt"] = ccxt_stub

flask_stub = types.ModuleType("flask")


class _SmokeFlask:
    def __init__(self, *args, **kwargs):
        self.routes = {}

    def route(self, path, methods=None, **kwargs):
        def decorator(fn):
            for method in (methods or ["GET"]):
                self.routes[(method, path)] = fn
            return fn
        return decorator

    def add_url_rule(self, path, endpoint, view_func, methods=None, **kwargs):
        for method in (methods or ["GET"]):
            self.routes[(method, path)] = view_func

    def before_request(self, fn):
        return fn


flask_stub.Flask = _SmokeFlask
flask_stub.jsonify = lambda *a, **k: a[0] if a else None
flask_stub.request = types.SimpleNamespace(headers={}, remote_addr="127.0.0.1", json=None)
sys.modules["flask"] = flask_stub

import core.engine as E  # noqa: E402
import core.trade_journal as _tj  # noqa: E402
from portfolio.manager import PortfolioManager  # noqa: E402

# ---- market feed boundary (mirror tests/test_profit_engine_phase3.py) ----
_ORIG = {
    "ohlcv": E.get_ohlcv_safe,
    "ticker": E.get_ticker_safe,
    "orderbook": E.get_orderbook_cached,
    "balance": E.get_balance_safe,
}


class SixPositionRuntimeValidation:
    """One runtime definition of record: builds the simulated six-slot market,
    drives the six scenarios + restart + failure injections, and collects the
    tamper-evident evidence used by the report."""

    SIX = [
        {"extra": "S1 trend-capture   ", "symbol": "BTC/USDT:USDT", "side": "BUY", "price": 60000.0, "asset_class": "CRYPTO"},
        {"extra": "S2 spike-protect    ", "symbol": "ETH/USDT:USDT", "side": "SELL", "price": 3000.0, "asset_class": "CRYPTO"},
        {"extra": "S3 partial-precision", "symbol": "XAUUSD", "side": "BUY", "price": 2300.0, "asset_class": "GOLD"},
        {"extra": "S4 breakeven-resil  ", "symbol": "US500/USDT:USDT", "side": "BUY", "price": 5000.0, "asset_class": "INDEX"},
        {"extra": "S5 hard-loss+cool   ", "symbol": "USTECH/USDT:USDT", "side": "SELL", "price": 17000.0, "asset_class": "INDEX"},
        {"extra": "S6-restart-placeholder", "symbol": "WTI", "side": "SELL", "price": 75.0, "asset_class": "OIL"},
    ]

    REST = {  # neutral resting positions used while another symbol is driven
        "BTC/USDT:USDT": 1.001,
        "ETH/USDT:USDT": 0.999,
        "XAUUSD": 1.001,
        "US500/USDT:USDT": 1.001,
        "USTECH/USDT:USDT": 0.999,
        "WTI": 0.999,
    }

    def __init__(self):
        self.pm = None
        self.live = {}
        self.bases = {}
        self.history = {}          # symbol -> [step snapshots]
        self.tokens = {}           # symbol -> set(decision tokens)
        self.logs = []
        self.results = {}
        self.receipts = {"start_balance": 10000.0, "net_realized": 0.0}
        self.shared = {"seen": [], "close_calls": 0}
        self._journal_line = 0     # incremental journal cursor
        self._orig_log = E.log_execution
        self._safe_log = self._make_safe_logger()

    def _make_safe_logger(self):
        holder = {"active": self}
        def _safe_log(msg, level="INFO", **kw):
            line = str(msg).encode("ascii", "replace").decode("ascii")
            holder["active"].logs.append(line)
        E.log_execution = _safe_log
        return _safe_log

    # ------------------------------------------------------------------ base
    def _reset_runtime(self):
        import copy as _c
        import random as _rnd
        # Deterministic harness: the engine samples randomness for entry
        # scoring / brain heuristics; without a fixed seed the same price path
        # can exit in different cycles run-to-run.
        _rnd.seed(7 + len(self.results))
        np.random.seed(7 + len(self.results))
        _snap, _tsnap, _dsnap = _c.deepcopy(E.STATE), _c.deepcopy(E.TRADE_STATE), _c.deepcopy(E.DASHBOARD_STATE)
        E.STATE.clear(); E.STATE.update(_snap)
        E.TRADE_STATE.clear(); E.TRADE_STATE.update(_tsnap)
        E.DASHBOARD_STATE.clear(); E.DASHBOARD_STATE.update(_dsnap)
        E.paper.update({"balance": 10000.0, "position": None, "committed_margin": 0.0})
        E.PERF.update({"trades": 0, "wins": 0, "losses": 0, "total_pnl_usdt": 0.0,
                       "total_pnl_pct": 0.0, "last_trade": {}, "symbols": {}})
        E.MEMORY.setdefault("watchlist", {}).clear()
        if hasattr(_tj, "_DEDUP"):
            _tj._DEDUP.clear()
        E._exchange_sync._last_reconcile = 0.0
        self.pm = PortfolioManager(6, E)
        self.pm.bind(E)

    def _reset_book(self):
        self.history = {s["symbol"]: [] for s in self.SIX}
        self.tokens = {s["symbol"]: set() for s in self.SIX}
        # NOTE: self._journal_line intentionally NOT reset here. The journal
        # cursor is monotonic so a sub-run never re-reads (and never re-tokens)
        # records it already ingested in an earlier phase.
        self.live = {}
        self.bases = {}
        self._quiet = {}

    def _start_patches(self):
        """Pin the simulated market regime so the REAL entry/manage pipelines
        behave deterministically (exactly like tests/test_profit_engine_phase3.py)."""
        from unittest.mock import patch
        self._patchers = [
            patch.object(E, "compute_adx",
                         side_effect=lambda df, period=14: pd.Series([30.0] * len(df), index=df.index)),
            patch.object(E, "detect_liquidity_context",
                         side_effect=lambda df, lookback=10: (
                             "buy_side_taken" if float(df["close"].iloc[-1]) > float(df["open"].iloc[-1])
                             else "sell_side_taken")),
        ]
        for _p in self._patchers:
            _p.start()

    def _stop_patches(self):
        for _p in getattr(self, "_patchers", [])[::-1]:
            try:
                _p.stop()
            except Exception:
                pass
        self._patchers = []

    def _prime(self, candidates):
        self._sell = set()
        for c in candidates:
            if c["side"] == "SELL":
                self._sell.add(c["symbol"])
            direction = -1.0 if c["side"] == "SELL" else 1.0
            n = 150
            t = np.arange(n)
            close = 100.0 * np.exp(direction * 0.0012 * t + direction * 0.0045 * np.sin(t / 7.0))
            open_ = np.concatenate([[close[0]], close[:-1]]) * (1 + 0.0004 * direction)
            high = np.maximum(open_, close) * (1 + 0.0035)
            low = np.minimum(open_, close) * (1 - 0.0035)
            df = pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                               "volume": 600.0 * (1 + 0.01 * t)})
            scale = c["price"] / float(df["close"].iloc[-1])
            for col in ("open", "high", "low", "close"):
                df[col] = df[col] * scale
            base_entry = df["close"].iloc[-1]
            self.bases[c["symbol"]] = df
            self.live[c["symbol"]] = c["price"]
        E.get_ohlcv_safe = lambda sym, limit=120, htf=False: self._ohlcv(sym, limit, htf)
        E.get_ticker_safe = lambda sym, retries=0, **k: self.live.get(sym)
        E.get_orderbook_cached = lambda sym, limit=20, **k: {
            "bids": [[self.live.get(sym, 1000.0) * 0.999, 10.0]],
            "asks": [[self.live.get(sym, 1000.0) * 1.001, 10.0]],
        }
        E.get_balance_safe = lambda retries=2, **k: (
            E.paper.get("balance", 10000.0) if isinstance(E.paper, dict) else 10000.0)

    def _ohlcv(self, sym, limit=120, htf=False):
        df = self.bases[sym].copy()
        last = df.index[-1]
        live = self.live.get(sym, float(df["close"].iloc[-1]))
        df.loc[last, "close"] = live
        body = live * (0.001 if sym in getattr(self, "_sell", set()) else -0.001)
        df.loc[last, "open"] = live - body
        df.loc[last, "high"] = max(float(df.loc[last, "high"]), live)
        df.loc[last, "low"] = min(float(df.loc[last, "low"]), live)
        df = df.iloc[-min(limit, len(df)):]
        return df

    def _advance_clock(self):
        for ctx in self.pm.contexts.values():
            m = ctx.live_manager
            m.last_management_ts = 0.0
            m.last_heavy_calc_ts = 0.0
            m.last_position_sync_ts = 0.0
            m.last_live_debug_ts = 0.0
            m.last_log_ts = 0.0
        E._exchange_sync._last_reconcile = 0.0

    def _read_journal(self, cursor=None):
        if not JOURNAL.exists():
            return []
        recs = []
        with JOURNAL.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                if i < self._journal_line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    recs.append(json.loads(line))
                except Exception:
                    continue
        self._journal_line += len(recs)
        return recs

    def _ingest(self):
        for rec in self._read_journal():
            sym = rec.get("symbol") or ""
            dec = rec.get("decision") or ""
            if dec:
                self.tokens.setdefault(sym, set()).add(dec)

    def _cand(self, c, score=85.0):
        p = c["price"]; side = c["side"]
        return {"symbol": c["symbol"], "side": side, "price": p,
                "sl": p * (0.98 if side == "BUY" else 1.02),
                "tp1": p * (1.03 if side == "BUY" else 0.97),
                "tp2": p * (1.06 if side == "BUY" else 0.94),
                "score": score, "atr": p * 0.01, "asset_class": c["asset_class"],
                "trade_id": c["symbol"]}

    # -------------------------------------------------------------- driving
    def _manage_once(self, tag):
        self._advance_clock()
        self.pm.manage_all()
        self._ingest()
        for sym in [s["symbol"] for s in self.SIX]:
            if sym not in self.pm.contexts:
                continue
            st = self.pm.contexts[sym].state
            self.history[sym].append({
                "tag": tag,
                "open": bool(st.get("open")),
                "mark": st.get("mark_price"),
                "entry": st.get("entry"),
                "remaining_qty": st.get("remaining_qty"),
                "roe_pct": st.get("roe_pct"),
                "realized_pnl_usdt": st.get("realized_pnl_usdt"),
                "synthetic_sl": st.get("synthetic_sl"),
                "synthetic_tp1": st.get("synthetic_tp1"),
                "tp2_price": st.get("tp2_price"),
                "protect": st.get("protection_state"),
                "stage": st.get("profit_stage"),
                "trail": st.get("trail_activated"),
                "tp1_hit": st.get("tp1_hit"),
                "exit_reason": st.get("exit_reason"),
                "close_reason": st.get("close_reason"),
            })

    def _step(self, price_map, tag):
        for sym, mult in price_map.items():
            if sym in self.pm.contexts:
                e = self.pm.contexts[sym].state.get("entry") or self.live.get(sym)
                self.live[sym] = float(e) * mult
        self._quiet_float(self.pm.contexts, set(price_map.keys()))
        self._manage_once(tag)

    def _quiet_float(self, contexts, active):
        """Quiet symbols hold their ENTRY mark exactly (flat). The harness only
        stresses the driven symbol; flat marks produce roe==0 so no trail/SL/
        council exit can fire on an untouched part of the book during another
        scenario (keeps isolation evidence clean and deterministic)."""
        for sym, ctx in contexts.items():
            e = ctx.state.get("entry")
            if not e or sym in active:
                continue
            self.live[sym] = float(e)

    def _ramp(self, price_map, steps, tag):
        base = {sym: self.pm.contexts[sym].state["entry"] for sym in price_map if sym in self.pm.contexts}
        for k in range(1, steps + 1):
            for sym, mult in price_map.items():
                if sym in base:
                    self.live[sym] = base[sym] * (1.0 + (mult - 1.0) * k / steps)
            self._quiet_float(self.pm.contexts, set(price_map.keys()))
            self._manage_once(f"{tag}:r{k}")

    def _rest_all(self, extras: dict | None = None):
        m = dict(self.REST)
        if extras:
            m.update(extras)
        return m

    # ------------------------------------------------------------- checks #
    def _margin_invariant(self):
        bal = float(E.paper["balance"] or 0.0)
        comm = float(E.paper["committed_margin"] or 0.0)
        perf = float(E.PERF["total_pnl_usdt"] or 0.0)
        ok = abs((bal + comm) - (10000.0 + perf)) < 1e-6
        return ok, {"balance": bal, "committed_margin": comm, "equity": bal + comm,
                    "perf_total_pnl_usdt": perf, "start": 10000.0}

    def _book_perf(self):
        return {"trades": E.PERF["trades"], "wins": E.PERF["wins"], "losses": E.PERF["losses"],
                "total_pnl_usdt": E.PERF["total_pnl_usdt"], "symbols": dict(E.PERF.get("symbols", {}))}

    # -------------------------------------------------------------- scenarios
    def _open_six(self):
        self._quiet = {}
        self.pm.manage_all()  # paranoia: sweep nothing before seeding
        cands = [self._cand(c) for c in self.SIX]
        opened = self.pm.open_top(cands, slots=6)
        self._ingest()
        assert opened == 6, f"open_top opened {opened}/6"
        self._quiet = {sym: 0.0 for sym in self.pm.symbols()}
        assert self.pm.count() == 6
        for sym in self.pm.symbols():
            assert self.pm.contexts[sym].state.get("open"), f"{sym} not open after open_top"
        ok, mi = self._margin_invariant()
        assert ok, f"margin invariant broken at open: {mi}"

    def _close_all(self, via="close_symbol"):
        survivors = list(self.pm.symbols())
        for sym in survivors:
            ok = self.pm.close_symbol(sym)
            assert ok, f"close_symbol({sym}) returned False"
        self._ingest()
        assert self.pm.count() == 0, f"ghost positions after sweep: {self.pm.symbols()}"

    def _sweep_close_map(self):
        """Per-symbol FINAL close metadata as recorded in the journal after the
        phase-A teardown flush (runners that were still open at their scenario
        snapshot finalize here)."""
        out = {}
        sixset = {s["symbol"] for s in self.SIX}
        for rec in self._journal_dump():
            if rec.get("decision") != _tj.TRADE_CLOSED:
                continue
            md = rec.get("metadata") or {}
            sym = rec.get("symbol")
            if not sym or sym not in sixset:
                continue
            out[sym] = {k: md.get(k) for k in (
                "trade_id", "result", "exit_reason", "realized_pnl_usdt",
                "realized_pnl_pct", "booked_usdt", "final_usdt", "peak_roe")}
        return out

    def run_phase_a(self):
        """Six simultaneous positions; five distinct managed lifecycles plus a
        silent runner, then a full sweep.  Real manage loop, real journal."""
        res = {"scenarios": {}, "invariants": {}}
        self._reset_runtime()
        self._reset_book()
        self._prime(self.SIX)
        self._open_six()
        res["opened"] = {"count": 6, "margin": self._margin_invariant()[1]}

        # ---- S1 BTC BUY: trend capture -> TP1/partial -> breakeven -> runner
        #      -> trailing / TP2 exit on pullback.
        self._ramp({"BTC/USDT:USDT": 1.065}, steps=9, tag="S1-ramp")
        self._step({"BTC/USDT:USDT": 1.012}, "S1-pull")
        self._step({"BTC/USDT:USDT": 1.012}, "S1-hold")
        res["scenarios"]["S1"] = self._snapshot_scenario("BTC/USDT:USDT")

        # ---- S5 USTECH SELL: hard SL loss while four other positions rest at
        #      protected small profits (isolation + cooldown evidence).
        before_crash = set(self.pm.symbols())
        self._step({"USTECH/USDT:USDT": 1.09}, "S5-crash")
        self._step({"USTECH/USDT:USDT": 1.09}, "S5-crash2")
        res["scenarios"]["S5"] = self._snapshot_scenario("USTECH/USDT:USDT")
        after_crash = set(self.pm.symbols())
        res["isolation_after_loss"] = {
            "before": sorted(before_crash),
            "after": sorted(after_crash),
            "intact": sorted(after_crash) == sorted(before_crash - {"USTECH/USDT:USDT"}),
        }
        cooldown = self.pm.risk_guard.snapshot(self.pm.count())
        res["risk_guard_after_loss"] = {k: cooldown.get(k) for k in (
            "allowed", "reason", "daily_drawdown_pct", "consecutive_losses",
            "cooldown_until", "max_daily_loss_pct", "portfolio_margin_cap_pct",
            "position_margin_pct", "projected_margin_pct")}
        res["risk_guard_after_loss"]["symbol_cooldowns"] = {
            k: round(v, 2) for k, v in (self.pm.risk_guard._symbol_cooldown_until or {}).items()}

# ---- S2 ETH SELL: fast profit spike -> protection/trail -> reversal
        #      exit without giving back all profit.
        self._step({"ETH/USDT:USDT": 0.955}, "S2-spike")
        self._step({"ETH/USDT:USDT": 0.985}, "S2-reversal")
        self._step({"ETH/USDT:USDT": 0.985}, "S2-hold")
        res["scenarios"]["S2"] = self._snapshot_scenario("ETH/USDT:USDT")

        # ---- S3 XAUUSD BUY: fast profit run (TP1 partial + breakeven), deep
        #      pullback inside breakeven, then re-ramp to exit.
        self._ramp({"XAUUSD": 1.032}, steps=4, tag="S3-ramp")
        self._step({"XAUUSD": 1.006}, "S3-pull")
        self._step({"XAUUSD": 1.006}, "S3-hold")
        self._ramp({"XAUUSD": 1.05}, steps=4, tag="S3-reramp")
        res["scenarios"]["S3"] = self._snapshot_scenario("XAUUSD")

        # ---- S4 US500 BUY: steady gain, protected drawdown, continuation exit.
        self._ramp({"US500/USDT:USDT": 1.035}, steps=4, tag="S4-ramp")
        self._step({"US500/USDT:USDT": 1.003}, "S4-dip")
        self._step({"US500/USDT:USDT": 1.003}, "S4-hold")
        self._ramp({"US500/USDT:USDT": 1.05}, steps=4, tag="S4-reramp")
        res["scenarios"]["S4"] = self._snapshot_scenario("US500/USDT:USDT")

        # ---- teardown: close everything that survived; zero ghosts.
        self._close_all()
        res["sweep"] = {"count_after": self.pm.count(), "perf": self._book_perf(),
                        "closed_at_sweep": self._sweep_close_map()}
        res["sweep"]["receipts"] = self._margin_invariant()
        ok, mi = self._margin_invariant()
        res["invariants"]["margin_tol_1e-6"] = ok
        res["invariants"]["margin_detail"] = mi
        res["journal"] = self.journal_summary()
        self.results["phase_a"] = res
        return res

    def _historical_open(self):
        opened = []
        for sym, hist in self.history.items():
            if hist and hist[-1].get("open"):
                opened.append(sym)
        return opened

    def _snapshot_scenario(self, sym):
        hist = self.history.get(sym, [])
        st = self.pm.contexts[sym].state if sym in self.pm.contexts else {}
        closed_records = [r for r in self._journal_dump()
                          if r.get("symbol") == sym and r.get("decision") == _tj.TRADE_CLOSED]
        last_close = closed_records[-1] if closed_records else None
        md = (last_close or {}).get("metadata") or {}
        return {
            "tokens": sorted(self.tokens.get(sym, set())),
            "final_open": bool(st.get("open")),
            "final_api": {
                "remaining_qty": st.get("remaining_qty"), "realized_pnl_usdt": st.get("realized_pnl_usdt"),
                "exit_reason": st.get("exit_reason"), "close_reason": st.get("close_reason"),
            } if st else None,
            "history": hist,
            "perf": dict(E.PERF.get("symbols", {}).get(sym, {})),
            "last_trade_summary": st.get("last_trade_summary"),
            "closed_record_meta": {k: md.get(k) for k in (
                "trade_id", "result", "exit_reason", "realized_pnl_usdt", "realized_pnl_pct",
                "booked_usdt", "final_usdt", "peak_roe", "legs")} if md else None,
        }

    # ------------------------------------------------------- scenario 6
    def run_scenario_six(self, partial_path=None):
        """Restart / recovery: single WTI SELL driven into a TP1 partial, then
        a simulated engine restart re-seats the runner from the venue through
        restore_from_exchange() and management continues to a clean exit."""
        res = dict(phase="restart_recovery")
        res["premature_exit_before_restart"] = False
        self._reset_runtime()
        self._reset_book()
        spec = next(s for s in self.SIX if s["symbol"] == "WTI")
        self._prime([spec])
        opened = self.pm.open_top([self._cand(spec)], slots=1)
        assert opened == 1
        # Fast profit spike into momentum condition -> TP1 partial is the intent.
        self._ramp({"WTI": 0.948}, steps=8, tag="S6-ramp")
        if "WTI" not in self.pm.contexts:
            res["premature_exit_before_restart"] = True
            self._ingest()
            res["note"] = "ramp exited the runner before a partial; no restart evidence captured"
            res["pre_restart"] = {"tokens": sorted(self.tokens.get("WTI", set()))}
            res["post_restore"] = {}
            res["post_exit"] = {}
            res["margin_ok"] = None
            self.results["scenario_6"] = res
            return res
        pre = self.pm.contexts["WTI"].state
        # Reseat the PAPER venue mirror exactly as a crash MID-active-cycle
        # would leave it: paper["position"] holds the WTI run incl. the
        # partial-leg quantities the engine wrote during close_partial().
        self.pm.activate("WTI")
        pp_venue = E.paper.get("position") if isinstance(E.paper, dict) else None
        pre_snapshot = {
            "trade_id": pre.get("trade_id"),
            "symbol": pre.get("current_symbol"),
            "side": pre.get("side"),
            "entry": pre.get("entry"),
            "remaining_qty": pre.get("remaining_qty"),
            "qty": pre.get("qty"),
            "realized_pnl_usdt": pre.get("realized_pnl_usdt"),
            "tp1_hit": pre.get("tp1_hit"),
        }
        res["pre_restart"] = pre_snapshot
        res["pre_restart"]["tokens"] = sorted(self.tokens.get("WTI", set()))
        res["pre_restart"]["venv_qty_after_partial"] = (pp_venue or {}).get("qty")
        res["pre_restart"]["venv_remaining_after_partial"] = (pp_venue or {}).get("remaining_qty")
        res["pre_restart"]["perf"] = self._book_perf()
        res["pre_restart"]["venue_mirror_present"] = bool(pp_venue)

        # ---- Simulated process restart: STATE resets; the venue (paper
        #      position ledger) retains the open runner exactly like an
        #      exchange would report the remaining contracts.
        with _TRADE_LOCK_LIKE():
            E.STATE.clear()
            if getattr(E, "_base_state_snapshot", None) is not None:
                E.STATE.update(copy.deepcopy(E._base_state_snapshot))
            E.TRADE_STATE.clear()
            E.DASHBOARD_STATE["live_trade_mode"] = False
        pm2 = PortfolioManager(6, E)
        pm2.bind(E)
        self.pm = pm2
        pm2.restore_from_exchange()
        self._ingest()

        if "WTI" in pm2.contexts:
            post = pm2.contexts["WTI"].state
        else:
            post = E.STATE
        post_snapshot = {
            "trade_id": post.get("trade_id"),
            "symbol": post.get("current_symbol"),
            "side": post.get("side"),
            "entry": post.get("entry"),
            "remaining_qty": post.get("remaining_qty"),
            "qty": post.get("qty"),
            "realized_pnl_usdt": post.get("realized_pnl_usdt"),
            "synthetic_sl": post.get("synthetic_sl"),
            "synthetic_tp1": post.get("synthetic_tp1"),
            "tp2_price": post.get("tp2_price"),
            "native_sl_state": post.get("native_sl_state"),
            "tokens": sorted(self.tokens.get("WTI", set())),
        }
        res["post_restore"] = post_snapshot
        res["restore_journal"] = True
        venv_rem = res["pre_restart"].get("venv_remaining_after_partial")
        rec_rem = post_snapshot.get("remaining_qty")
        res["blowback_detected"] = bool(venv_rem is not None and rec_rem is not None and rec_rem > venv_rem)
        res["blowback_detail"] = {"venue_remaining_after_partial": venv_rem, "recovered_remaining": rec_rem}

        # Continue management to exit after recovery.
        self._step({"WTI": 0.940}, "S6-post-ramp")
        if "WTI" in self.pm.contexts:
            self.pm.close_symbol("WTI")
        self._ingest()
        res["post_exit"] = {
            "perf": self._book_perf(),
            "tokens": sorted(self.tokens.get("WTI", set())),
            "margin": self._margin_invariant()[1],
        }
        res["margin_ok"] = self._margin_invariant()[0]
        self.results["scenario_6"] = res
        return res

    # ---------------------------------------------------------- injections
    def _mini_book(self, symbol, side, price, asset_class):
        self._reset_runtime()
        self._reset_book()
        spec = {"symbol": symbol, "side": side, "price": price, "asset_class": asset_class}
        self._prime([spec])
        assert self.pm.open_top([self._cand(spec)], slots=1) == 1
        return spec

    def run_phase_b(self):
        from unittest.mock import patch
        res = {}
        # ---- A/B/J: TP1 partial rejected / verification unavailable ------
        rec = {}
        self._mini_book("BTC/USDT:USDT", "BUY", 60000.0, "CRYPTO")
        basis = self.pm.contexts["BTC/USDT:USDT"].state
        rec["basis"] = {"remaining_qty": basis.get("remaining_qty"), "realized": basis.get("realized_pnl_usdt")}
        with patch.object(E, "close_partial", side_effect=lambda ratio: False):
            self._step(self._rest_all({"BTC/USDT:USDT": 1.03}), "INJ-A-step")
        rec["tokens_after"] = sorted(self.tokens.get("BTC/USDT:USDT", set()))
        rec["tp1_state"] = self.pm.contexts["BTC/USDT:USDT"].state.get("tp1_state")
        rec["tp1_hit"] = self.pm.contexts["BTC/USDT:USDT"].state.get("tp1_hit")
        rec["remaining_qty"] = self.pm.contexts["BTC/USDT:USDT"].state.get("remaining_qty")
        rec["realized"] = self.pm.contexts["BTC/USDT:USDT"].state.get("realized_pnl_usdt")
        self.pm.close_symbol("BTC/USDT:USDT")
        res["A_partial_rejected"] = rec

        # ---- C: ticker unavailable for one manage cycle (venue PAUSED) -----
        rec = {}
        self._mini_book("ETH/USDT:USDT", "SELL", 3000.0, "CRYPTO")
        saved = self.live["ETH/USDT:USDT"]
        self.live["ETH/USDT:USDT"] = None
        self._advance_clock()
        self.pm.manage_all()
        self._ingest()
        rec["still_open"] = "ETH/USDT:USDT" in self.pm.contexts
        rec["remaining_qty"] = self.pm.contexts["ETH/USDT:USDT"].state.get("remaining_qty")
        rec["realized"] = self.pm.contexts["ETH/USDT:USDT"].state.get("realized_pnl_usdt")
        self.live["ETH/USDT:USDT"] = saved
        rec["tokens"] = sorted(self.tokens.get("ETH/USDT:USDT", set()))
        ok = self.pm.close_symbol("ETH/USDT:USDT")
        rec["recovered_after_pause"] = ok
        res["C_ticker_unavailable"] = rec

        # ---- D: OHLCV unavailable for one cycle ----------------
        rec = {}
        self._mini_book("XAUUSD", "BUY", 2300.0, "GOLD")
        rec["basis_remaining"] = self.pm.contexts["XAUUSD"].state.get("remaining_qty")
        with patch.object(E, "get_ohlcv_safe", side_effect=lambda sym, limit=120, htf=False: None):
            self._step(self._rest_all({"XAUUSD": 1.015}), "INJ-D-step")
        rec["still_open"] = "XAUUSD" in self.pm.contexts
        rec["remaining_qty"] = self.pm.contexts["XAUUSD"].state.get("remaining_qty") if "XAUUSD" in self.pm.contexts else None
        rec["tokens"] = sorted(self.tokens.get("XAUUSD", set()))
        if "XAUUSD" in self.pm.contexts:
            self.pm.close_symbol("XAUUSD")
        res["D_ohlcv_unavailable"] = rec

        # ---- E: delayed/mismatched EventBus force-close is symbol-bound ----
        rec = {}
        self._mini_book("US500/USDT:USDT", "BUY", 5000.0, "INDEX")
        self._step(self._rest_all({"US500/USDT:USDT": 1.01}), "INJ-E-pre")
        E._event_bus.emit("force_close_local", {"symbol": "BOGUS/USDT:USDT"})
        time.sleep(0.4)  # allow bus thread to drain
        self._ingest()
        rec["still_open"] = "US500/USDT:USDT" in self.pm.contexts
        rec["foreign_close_ignored"] = rec["still_open"]
        rec["tokens"] = sorted(self.tokens.get("US500/USDT:USDT", set()))
        if "US500/USDT:USDT" in self.pm.contexts:
            self.pm.close_symbol("US500/USDT:USDT")
        res["E_symbol_bound_force_close"] = rec

        # ---- F: management cadence gate (fresh tick within interval) ------
        rec = {}
        self._mini_book("US500/USDT:USDT", "BUY", 5000.0, "INDEX")
        t0 = set(self.tokens.get("US500/USDT:USDT", set()))
        self.pm.activate("US500/USDT:USDT")  # re-seat ENGINE STATE from this context
        m2 = self.pm.contexts["US500/USDT:USDT"].live_manager
        state0 = dict(E.STATE)
        count = {"n": 0}
        _orig_apply = m2._apply_management
        def _counting(sym, now):
            count["n"] += 1
            return _orig_apply(sym, now)
        m2._apply_management = _counting
        m2.last_management_ts = time.time()           # tick fired ms ago -> gated
        E.STATE["mark_price"] = 5000.0 * 0.85         # would trip synthetic SL if managed
        m2.manage_live_trade()
        rec["gated_skip_apply"] = (count["n"] == 0)
        rec["still_open_after_gated_manage"] = "US500/USDT:USDT" in self.pm.contexts
        m2.last_management_ts = 0.0                   # cadence expired -> manage fires
        m2.manage_live_trade()                        # released: SL must fire
        rec["released_runs_apply"] = (count["n"] == 1)
        self._ingest()
        t1 = set(self.tokens.get("US500/USDT:USDT", set()))
        rec["released_and_closed"] = _tj.TRADE_CLOSED in (t1 - t0)
        rec["tokens"] = sorted(self.tokens.get("US500/USDT:USDT", set()))
        if "US500/USDT:USDT" in self.pm.contexts:
            self.pm.close_symbol("US500/USDT:USDT")
        res["F_management_cadence_gate"] = rec

        # ---- H: duplicate close request (manager-level dedup) -------------
        rec = {}
        self._mini_book("WTI", "SELL", 75.0, "OIL")
        def _wti_closed():
            return [r for r in self._journal_dump()
                    if r.get("decision") == _tj.TRADE_CLOSED and r.get("symbol") == "WTI"]
        base_count = len(_wti_closed())
        base_trades = E.PERF["trades"]
        base_pnl = E.PERF["total_pnl_usdt"]
        first = self.pm.close_symbol("WTI")
        self._ingest()
        after_first = len(_wti_closed()) - base_count
        first_trades = E.PERF["trades"] - base_trades
        first_pnl = E.PERF["total_pnl_usdt"] - base_pnl
        second = self.pm.close_symbol("WTI")
        self._ingest()
        after_second = len(_wti_closed()) - base_count
        second_trades = E.PERF["trades"] - base_trades - first_trades
        second_pnl = E.PERF["total_pnl_usdt"] - base_pnl - first_pnl
        rec["first"] = first
        rec["second"] = second
        rec["closed_records:first,second"] = [after_first, after_second]
        rec["perf_delta:first,second"] = [[first_trades, first_pnl], [second_trades, second_pnl]]
        rec["no_duplicate_close"] = (first is True and second is False
                                     and after_first == 1 and after_second == 1
                                     and first_trades == 1 and second_trades == 0
                                     and abs(second_pnl) < 1e-9)
        res["H_duplicate_close_request"] = rec

        # ---- I: reconcile right after partial must not regrow size ---------
        rec = {}
        self._mini_book("BTC/USDT:USDT", "BUY", 60000.0, "CRYPTO")
        self._ramp({"BTC/USDT:USDT": 1.04}, steps=3, tag="INJ-I-ramp")
        rec["remaining_after_ramp"] = self.pm.contexts["BTC/USDT:USDT"].state.get("remaining_qty")
        rec["tp1_hit"] = self.pm.contexts["BTC/USDT:USDT"].state.get("tp1_hit")
        ctx = self.pm.contexts["BTC/USDT:USDT"]
        E._exchange_sync._last_reconcile = 0.0
        E._exchange_sync.reconcile("BTC/USDT:USDT", ctx.state)
        prior = ctx.state.get("remaining_qty")
        self._ingest()
        rec["remaining_after_reconcile"] = self.pm.contexts["BTC/USDT:USDT"].state.get("remaining_qty")
        rec["no_growth_after_reconcile"] = (self.pm.contexts["BTC/USDT:USDT"].state.get("remaining_qty") == prior)
        rec["tokens"] = sorted(self.tokens.get("BTC/USDT:USDT", set()))
        if "BTC/USDT:USDT" in self.pm.contexts:
            self.pm.close_symbol("BTC/USDT:USDT")
        res["I_reconcile_after_partial"] = rec

        # ---- J: post-loss shock boundary -> entry must be refused -----------
        rec = {}
        self._mini_book("USTECH/USDT:USDT", "SELL", 17000.0, "INDEX")
        self._ramp({"USTECH/USDT:USDT": 1.05}, steps=3, tag="INJ-J-ramp")
        before_loss = "USTECH/USDT:USDT" in self.pm.contexts
        candidate = self._cand({"symbol": "BTC/USDT:USDT", "side": "BUY", "price": 60000.0, "asset_class": "CRYPTO"})
        verdict = self.pm.can_open("BTC/USDT:USDT", "CRYPTO") if hasattr(self.pm, "can_open") else None
        rec["attempted_new_entry_after_loss"] = bool(verdict)
        rec["entry_refused_after_loss"] = (verdict is False)
        if verdict is False and hasattr(self.pm, "risk_guard"):
            snap = self.pm.risk_guard.snapshot(self.pm.count())
            rec["guard_reason"] = snap.get("reason")
            rec["guard_allowed"] = snap.get("allowed")
            rec["consecutive_losses"] = snap.get("consecutive_losses")
            rec["cooldown_until"] = snap.get("cooldown_until")
        rec["position_still_open_at_refusal"] = before_loss
        res["J_shock_boundary_entry_refused"] = rec

        self.results["phase_b"] = res
        return res

    # ------------------------------------------------------------ reporting
    def _journal_dump(self):
        if not JOURNAL.exists():
            return []
        out = []
        with JOURNAL.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        return out

    def journal_summary(self):
        recs = self._journal_dump()
        by = {}
        for r in recs:
            by.setdefault(r.get("symbol", ""), []).append(r.get("decision"))
        ok, count, status = _verify_journal()
        return {"valid_chain": ok, "record_count": count, "status": status,
                "per_symbol_decisions": {k: sorted(set(v)) for k, v in by.items()},
                "total_records": len(recs)}

    def run_all(self):
        start = time.time()
        self._journal_line = 0
        JOURNAL.write_text("", encoding="utf-8")  # each run owns its journal
        self._reset_runtime()
        self._start_patches()
        try:
            for name, fn in (("phase_a", self.run_phase_a),
                             ("scenario_6", self.run_scenario_six),
                             ("phase_b", self.run_phase_b)):
                try:
                    fn()
                except Exception as exc:
                    import traceback as _tb
                    self.results[name] = {"error": f"{type(exc).__name__}: {exc}",
                                          "traceback": _tb.format_exc()}
        finally:
            self._stop_patches()
            E.log_execution = self._orig_log
        E.get_ohlcv_safe = _ORIG["ohlcv"]
        E.get_ticker_safe = _ORIG["ticker"]
        E.get_orderbook_cached = _ORIG["orderbook"]
        E.get_balance_safe = _ORIG["balance"]
        self.results["meta"] = {"duration_sec": round(time.time() - start, 2),
                                "start_balance": 10000.0, "journal": str(JOURNAL)}
        EVIDENCE.write_text(json.dumps(self.results, indent=2, default=str), encoding="utf-8")
        return self.results


_verified = {"chain": None}


def _verify_journal():
    try:
        from core.decision_journal import verify_file
        ok, count, status = verify_file(JOURNAL)
    except Exception as exc:
        ok, count, status = False, 0, f"verify_error:{type(exc).__name__}"
    _verified["chain"] = ok
    return ok, count, status


def _TRADE_LOCK_LIKE():
    """Harness-side neutral lock context for the simulated restart wipe."""
    import contextlib

    @contextlib.contextmanager
    def _ctx():
        yield
    return _ctx()


def main():
    E._base_state_snapshot = copy.deepcopy(E.STATE)
    har = SixPositionRuntimeValidation()
    results = har.run_all()
    print("=" * 78)
    print("SIX_POSITION_RUNTIME_VALIDATION")
    print("=" * 78)
    pa = results.get("phase_a", {})
    ok_margin = pa.get("invariants", {}).get("margin_tol_1e-6", False)
    chain_ok = pa.get("journal", {}).get("valid_chain", False)
    jm = pa.get("journal", {})
    print(f"[PHASE A] margin_invariant={ok_margin} journal_chain={chain_ok} "
          f"records={jm.get('total_records')} status={jm.get('status')}")
    sc = pa.get("scenarios", {})
    for name, snap in sc.items():
        print(f"  {name}: final_open={snap.get('final_open')} "
              f"tokens={snap.get('tokens')}")
        if snap.get("final_api"):
            print(f"        remaining_qty={snap['final_api'].get('remaining_qty')} "
                  f"realized={snap['final_api'].get('realized_pnl_usdt')} "
                  f"exit={snap['final_api'].get('exit_reason')} close={snap['final_api'].get('close_reason')}")
    s6 = results.get("scenario_6", {})
    print(f"[SCENARIO 6] pre_trade_id={s6.get('pre_restart',{}).get('trade_id')} "
          f"post_trade_id={s6.get('post_restore',{}).get('trade_id')}")
    print(f"  pre_remaining={s6.get('pre_restart',{}).get('remaining_qty')} "
          f"post_remaining={s6.get('post_restore',{}).get('remaining_qty')}")
    print(f"  venv_qty_after_partial={s6.get('pre_restart',{}).get('venv_qty_after_partial')} "
          f"margin_ok={s6.get('margin_ok')}")
    print(f"[PHASE B] injections: {', '.join(results.get('phase_b', {}).keys())}")
    for name, rec in (results.get("phase_b", {}) or {}).items():
        if isinstance(rec, dict) and "error" in rec:
            print(f"  {name}: ERROR {rec['error']}")
            continue
        if isinstance(rec, dict):
            print(f"  {name}: {json.dumps(rec, default=str)[:600]}")
        else:
            print(f"  {name}: {rec}")
    print(f"evidence written: {EVIDENCE}")
    iso = pa.get("isolation_after_loss", {})
    iso_delta = set(iso.get("before", []) or []) - set(iso.get("after", []) or [])
    risk = pa.get("risk_guard_after_loss", {}) or {}
    required_inj = ("A_partial_rejected", "C_ticker_unavailable", "D_ohlcv_unavailable",
                    "E_symbol_bound_force_close", "F_management_cadence_gate",
                    "H_duplicate_close_request", "I_reconcile_after_partial",
                    "J_shock_boundary_entry_refused")
    ver = all([
        ok_margin,
        chain_ok,
        pa.get("sweep", {}).get("count_after") == 0,
        iso.get("intact", False) is True and iso_delta == {"USTECH/USDT:USDT"},
        risk.get("consecutive_losses", 0) >= 1 and risk.get("allowed") is False,
        (s6.get("pre_restart", {}) or {}).get("trade_id") == (s6.get("post_restore", {}) or {}).get("trade_id"),
        s6.get("margin_ok") is True,
        s6.get("blowback_detected") is False,
        all(k in (results.get("phase_b", {}) or {}) for k in required_inj),
    ])
    verdict = "PASS" if ver else "FAIL"
    print(f"SIX_POSITION_RUNTIME_VALIDATION = {verdict}")
    return 0 if ver else 1


if __name__ == "__main__":
    sys.exit(main())