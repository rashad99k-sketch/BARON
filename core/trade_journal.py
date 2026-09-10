"""Trade lifecycle journal for the BARON runtime.

This is a thin, ENGINE-DECOUPLED layer on top of core.decision_journal (the
tamper-evident hash-chained JSONL store). It exists so every profit-management
lifecycle transition is observable, machine-readable and tamper-evident without
creating a competing logging system.

Nothing in this module imports core.engine, so it can be unit tested in
isolation and reused by any component (equity/paper/portfolio).

Canonical event vocabulary (stage="TRADE", decision=<event>):

  TRADE_OPENED              trade opened with id + levels
  RESTART_RECOVERY          restart recovery began for a symbol
  POSITION_RECOVERED        restart recovery reconstructed a live position
  PROFIT_DETECTED           first profitable unseen tick (unrealized)
  TP1_ELIGIBLE              strategy chose to bank TP1
  TP1_EXECUTED              TP1 partial close filled + reconciled
  TP1_FAILED                TP1 partial close was attempted but not filled
  PARTIAL_CLOSE             any realized partial-close leg booked
  BREAKEVEN_RATCHET         protective stop moved to breakeven (entry)
  PROTECTION_UPDATE         protective stop ratcheted (never backward)
  PROFIT_LOCKED             profit lock activated only AFTER the ratchet landed
  PROFIT_LOCK_FAILED        profit lock could not be activated
  TRAILING_ACTIVE           trailing stop activated
  TP2_EXECUTED              runner banked at the second target (full close)
  TRADE_CLOSED              final trade summary, classified from REALIZED PnL
  EXTERNAL_CLOSE            position confirmed closed on the exchange
  NATIVE_SL_PLACED          exchange-native STOP_MARKET PLACED (Hedge positionSide)
  NATIVE_SL_UPDATED         exchange-native protective SL UPDATED (monotonic)
  NATIVE_SL_CANCELLED       exchange-native protective SL CANCELLED (best effort)
  NATIVE_SL_FAILED          exchange-native protective SL could not be placed
  POSITION_STATUS_UNKNOWN   exchange could not answer; position state is UNKNOWN
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from core.decision_journal import append_event

# -- Lifecycle event vocabulary (single source of truth for strings) ---------
TRADE_OPENED = "TRADE_OPENED"
RESTART_RECOVERY = "RESTART_RECOVERY"
POSITION_RECOVERED = "POSITION_RECOVERED"
PROFIT_DETECTED = "PROFIT_DETECTED"
TP1_ELIGIBLE = "TP1_ELIGIBLE"
TP1_EXECUTED = "TP1_EXECUTED"
TP1_FAILED = "TP1_FAILED"
PARTIAL_CLOSE = "PARTIAL_CLOSE"
BREAKEVEN_RATCHET = "BREAKEVEN_RATCHET"
PROTECTION_UPDATE = "PROTECTION_UPDATE"
PROFIT_LOCKED = "PROFIT_LOCKED"
PROFIT_LOCK_FAILED = "PROFIT_LOCK_FAILED"
TRAILING_ACTIVE = "TRAILING_ACTIVE"
TP2_EXECUTED = "TP2_EXECUTED"
TRADE_CLOSED = "TRADE_CLOSED"
EXTERNAL_CLOSE = "EXTERNAL_CLOSE"
NATIVE_SL_PLACED = "NATIVE_SL_PLACED"
NATIVE_SL_UPDATED = "NATIVE_SL_UPDATED"
NATIVE_SL_CANCELLED = "NATIVE_SL_CANCELLED"
NATIVE_SL_FAILED = "NATIVE_SL_FAILED"
POSITION_STATUS_UNKNOWN = "POSITION_STATUS_UNKNOWN"

# Profit-harvesting lifecycle stages (monotonic, forward-only).
STAGE_OPENED = "OPENED"
STAGE_PROFIT_DETECTED = "PROFIT_DETECTED"
STAGE_TP1_ELIGIBLE = "TP1_ELIGIBLE"
STAGE_TP1_EXECUTED = "TP1_EXECUTED"
STAGE_PROFIT_LOCKED = "PROFIT_LOCKED"
STAGE_TRAILING_ACTIVE = "TRAILING_ACTIVE"
STAGE_TP2_EXECUTED = "TP2_EXECUTED"
STAGE_CLOSED = "CLOSED"

# Protective-stop states.
PROTECTION_NONE = "NONE"
PROTECTION_BREAKEVEN = "BREAKEVEN"
PROTECTION_LOCKED = "PROFIT_LOCK"
PROTECTION_TRAILING = "TRAILING"

_RANK = {
    STAGE_OPENED: 0, STAGE_PROFIT_DETECTED: 1, STAGE_TP1_ELIGIBLE: 2,
    STAGE_TP1_EXECUTED: 3, STAGE_PROFIT_LOCKED: 4, STAGE_TRAILING_ACTIVE: 5,
    STAGE_TP2_EXECUTED: 6, STAGE_CLOSED: 7,
}

_DEDUP: dict[tuple[str, str], float] = {}


def make_trade_id(symbol: str = "") -> str:
    """Collision-resistant human-readable trade id."""
    base = str(symbol or "").replace("/", "-").replace(":", "-")
    token = uuid.uuid4().hex[:6]
    return f"{base}-{int(time.time() * 1000)}-{token}"


def classify_result(pnl_pct: float, breakeven_eps: float = 0.01) -> str:
    """Classify a CLOSED trade from its REALIZED PnL only."""
    try:
        value = float(pnl_pct or 0.0)
    except (TypeError, ValueError):
        value = 0.0
    if abs(value) <= breakeven_eps:
        return "BREAKEVEN"
    return "WIN" if value > 0 else "LOSS"


def tp_geometry_valid(side: str, entry: float, sl: float, tp1: float,
                      tp2: float, min_dist: float = 0.0) -> tuple[bool, list[str]]:
    """Canonical TP/SL geometry check (BUY: SL < entry < TP1 < TP2). Pure."""
    problems: list[str] = []
    side = str(side or "").upper()
    entry, sl, tp1, tp2 = (float(x or 0.0) for x in (entry, sl, tp1, tp2))
    if entry <= 0:
        problems.append("entry_lt_eq_zero")
        return False, problems
    if sl <= 0:
        problems.append("sl_lt_eq_zero")
    if side == "BUY":
        if not sl < entry:
            problems.append("sl_above_entry")
        if not entry < tp1:
            problems.append("tp1_below_entry")
        if not tp1 < tp2:
            problems.append("tp2_below_tp1")
    elif side == "SELL":
        if not sl > entry:
            problems.append("sl_below_entry")
        if not entry > tp1:
            problems.append("tp1_above_entry")
        if not tp1 > tp2:
            problems.append("tp2_above_tp1")
    else:
        problems.append("unknown_side")
    if min_dist and min_dist > 0:
        if tp1 > 0 and tp2 > 0 and side == "BUY" and tp2 - tp1 < min_dist:
            problems.append("tp2_too_close_tp1")
        if tp1 > 0 and tp2 > 0 and side == "SELL" and tp1 - tp2 < min_dist:
            problems.append("tp2_too_close_tp1")
    return not problems, problems


def build_trade_meta(state: dict[str, Any] | None) -> dict[str, Any]:
    """Canonical optional-field payload assembled from engine STATE.

    Every optional journal field is present; genuinely unknown values are None.
    Realized figures come from the exact same STATE counters the engine books.
    """
    s = state if isinstance(state, dict) else {}

    def _f(key: str, default: float | None = None) -> float | None:
        try:
            v = s.get(key)
            return default if v is None else float(v)
        except (TypeError, ValueError):
            return default

    def _s(key: str) -> Any:
        return s.get(key)

    return {
        "trade_id": _s("trade_id"),
        "entry": _f("entry", 0.0),
        "side": _s("side"),
        "current_price": _s("mark_price"),
        "current_size": _s("remaining_qty"),
        "original_size": _s("qty_initial"),
        "unrealized_pnl_usdt": _f("unrealized_pnl_usdt", 0.0),
        "unrealized_roe": _f("roe_pct", 0.0),
        "realized_pnl_usdt": _f("realized_pnl_usdt", 0.0),
        "realized_roe": _f("realized_roe_pct", 0.0),
        "peak_roe": _f("peak_roe", 0.0),
        "peak_unrealized_pnl": _f("peak_unrealized_pnl", 0.0),
        "peak_price": _f("peak_price", 0.0),
        "sl": _f("synthetic_sl", 0.0),
        "tp1": _f("synthetic_tp1", 0.0),
        "tp2": _f("tp2_price", 0.0),
        "profit_stage": _s("profit_stage"),
        "protection_state": _s("protection_state"),
        "trailing_active": bool(s.get("trail_activated", False)),
        "trail_stop": _f("trail_stop", 0.0),
        "tp1_done": bool(s.get("tp1_hit", False)),
        "tp2_done": bool(s.get("tp2_hit", False)),
        "tp1_state": _s("tp1_state"),
        "tp2_state": _s("tp2_state"),
        "native_sl_state": _s("native_sl_state"),
        "native_sl_price": _f("native_sl_price"),
        "native_sl_order_id": _s("native_sl_order_id"),
        "position_status": _s("position_status"),
        "exit_reason": _s("exit_reason"),
        "final_result": _s("final_result_class"),
        "duration_sec": _f("duration_sec"),
    }


def journal_trade_event(*, event: str, symbol: str = "", side: str = "",
                        trade_id: str = "", reason: str = "", detail: str = "",
                        score: float | None = None, state: dict[str, Any] | None = None,
                        metadata: dict[str, Any] | None = None,
                        dedup_key: str | None = None, dedup_sec: float = 0.0) -> dict | None:
    """Append one tamper-evident TRADE event to the decision journal.

    dedup_key/dedup_sec suppress noisy repeats of the same (event,symbol) within
    a window (e.g. repeated POLLS on a failed TP1), never canonical transitions.
    """
    if not event:
        return None
    if dedup_key is None:
        dedup_key = f"{event}:{symbol}"
    if dedup_sec and dedup_sec > 0:
        now = time.time()
        last = _DEDUP.get(dedup_key, 0.0)
        if now - last < float(dedup_sec):
            return None
        _DEDUP[dedup_key] = now
    meta = dict(metadata or {})
    base = build_trade_meta(state)
    for k, v in base.items():
        meta.setdefault(k, v)
    if trade_id:
        # Explicit trade_id always wins so restart recovery can bind records.
        meta["trade_id"] = trade_id
    try:
        return append_event(symbol=symbol, side=side, stage="TRADE",
                            decision=event, reason=reason, detail=detail,
                            score=score, metadata=meta)
    except Exception:
        return None


def _journal_path() -> Path:
    import os
    return Path(os.getenv("DECISION_JOURNAL_PATH", "logs/decision_journal.jsonl"))


def recover_trade_id(symbol: str, state_key: str = "open") -> str | None:
    """Best-effort recovery of a RESTART trade id.

    Opens the journal, scans the newest records for a matching symbol whose
    TRADE_OPENED has no later TRADE_CLOSED, and returns its trade_id. Any read
    error yields None (a fresh id is generated by the caller).
    """
    symbol = str(symbol or "")
    if not symbol:
        return None
    try:
        path = _journal_path()
        if not path.exists():
            return None
        opened: dict[str, str] = {}
        closed: set[str] = set()
        for line in reversed(path.read_text(encoding="utf-8").splitlines()):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if str(rec.get("stage", "")) != "TRADE":
                continue
            if str(rec.get("symbol", "")) != symbol:
                continue
            decision = str(rec.get("decision", ""))
            meta = rec.get("metadata") if isinstance(rec.get("metadata"), dict) else {}
            tid = str(meta.get("trade_id") or "") or str(rec.get("trade_id") or "")
            if not tid:
                continue
            if decision in (TRADE_CLOSED,):
                closed.add(tid)
            elif decision == TRADE_OPENED:
                opened[tid] = rec.get("ts", 0.0)
        for tid in sorted(opened, key=lambda k: opened[k], reverse=True):
            if tid not in closed and state_key == "open":
                return tid
        return None
    except Exception:
        return None