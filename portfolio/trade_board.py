"""Trade Board — professional box-formatted operator boards for trade lifecycle.

Renders institutional-grade double-line unicode boards (╔ ═ ╗ ║ ╠ ╣ ╚ ╝) that
give the operator the whole picture at a glance across the full trade life:

  * 🟢 TRADE OPENED   — the fill board: identity, sizing, initial protection,
                        the five named council members and execution proof.
  * 📊 POSITION BOARD — dynamic in-life status: peak/current/giveback ROE,
                        trend/momentum/distribution/structure/thesis labels and
                        the board decision ladder.
  * 🔴 RISK CURVE ALERT — a step-change / collapse alarm listing the concrete
                        failure flags and the board verdict.
  * 🚨 STRICT CLOSE   — the thesis-failure exit board: 5-member vote, executive
                        approval, the verified close checklist and status.

Boards are advisory: they render state that the engine/council already decided.
The coordinator logs them through the engine's execution logger, so they land
in the dashboard log stream exactly like operational events.
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from core.trade import ProtectionState, Trade, TradeStatus
from portfolio.trade_council import CouncilMemberVote, TradeDecision


# Disambiguating tags for the compact vote strip on the position board
# (TrendRider vs ThesisOfficer both start with "Tr").
_MEMBER_TAG = {
    "TrendRider": "TR",
    "ProfitGuardian": "PG",
    "RiskOfficer": "RO",
    "ThesisOfficer": "TO",
    "ScalpDesk": "SD",
}

_MEMBER_ICON = {
    "TrendRider": "🧭",
    "ProfitGuardian": "🛡",
    "RiskOfficer": "⚠️",
    "ThesisOfficer": "🧠",
    "ScalpDesk": "⚡",
}


def _num(v, default: float = 0.0) -> float:
    try:
        x = float(v)
        return x if x == x and x not in (float("inf"), float("-inf")) else default
    except Exception:
        return default


def _c(ctx: Optional[Any], key: str, default: Any = None) -> Any:
    """Read a context value from a dict or any attribute-bearing object."""
    if ctx is None:
        return default
    if isinstance(ctx, dict):
        v = ctx.get(key, default)
    else:
        v = getattr(ctx, key, default)
    try:
        return _num(v) if isinstance(v, (int, float)) else v
    except Exception:
        return v


class _Box:
    """Tiny ASCII box-builder: guaranteed 7-bit safe and monospaced-aligned."""

    @classmethod
    def render(cls, title: str, lines: List[str], width: int = 62) -> str:
        inner = max(6, width - 2)
        top = "+" + "-" * inner + "+"
        mid = "+" + "-" * inner + "+"
        head = "| " + title.ljust(inner - 1) + "|"
        out = [top, head, mid]
        for line in lines:
            body = line[2:] if line.startswith("| ") else line
            out.append("| " + body.ljust(inner - 2) + " |")
        out.append(top)
        return "\n".join(out)


class _ProBox:
    """Professional double-line unicode box (╔ ═ ╗ ║ ╠ ╣ ╚ ╝)."""

    _TL, _TR, _BL, _BR = "╔", "╗", "╚", "╝"
    _H, _V, _L, _R = "═", "║", "╠", "╣"

    @classmethod
    def render(cls, title: str, lines: List[str],
               min_width: int = 68, max_width: int = 112) -> str:
        content_w = max([len(title)] + [len(l) for l in lines])
        inner = max(min_width - 2, min(max_width - 2, content_w + 8))
        out = [cls._TL + cls._H * inner + cls._TR,
               cls._V + " " + title.ljust(inner - 2) + " " + cls._V,
               cls._L + cls._H * inner + cls._R]
        for line in lines:
            body = line[2:] if line.startswith("| ") else line
            out.append(cls._V + " " + body.ljust(inner - 2) + " " + cls._V)
        out.append(cls._BL + cls._H * inner + cls._BR)
        return "\n".join(out)


def _visible_len(s: str) -> int:
    return len(s)


_LW = 22


def _fit(value: Any, width: int) -> str:
    text = str(value)
    return text if len(text) <= width else text[: max(1, width - 1)] + "…"


def _two(label: str, value: Any) -> str:
    return f"{label:<{_LW}} {_fit(value, 60)}"


def _two_c(label: str, icon: str, value: Any) -> str:
    return f"{icon} {label:<{_LW - 2}} {_fit(value, 60)}"


def _sep(text: str, width: int = 52) -> str:
    text = f" {text} "
    n = max(0, width - len(text)) // 2
    return "═" * n + text + "═" * (width - len(text) - n)


def _risk_curve(frac: float) -> str:
    """frac 0..1 -> 10-char ASCII risk curve (closer to R = deeper red zone)."""
    frac = max(0.0, min(1.0, frac))
    base = ["-"] * 10
    idx = min(9, int(frac * 10))
    base[idx] = "R"
    return "".join(base)


def _urgency_bar(score: float) -> str:
    """0-100 score -> 1..10 block bar built purely from ASCII."""
    filled = max(1, min(10, int(round(_num(score) / 10.0))))
    return "#" * filled + "." * (10 - filled)


def _side_long(trade: Trade) -> bool:
    return str(trade.side).upper() == "BUY"


def _direction(trade: Trade) -> str:
    return "LONG" if _side_long(trade) else "SHORT"


def _leverage_of(trade: Trade, ctx: Optional[Any] = None) -> float:
    lev = _num(_c(ctx, "leverage"), 0.0)
    if lev > 0:
        return lev
    notional = trade.entry_price * trade.original_qty
    if trade.margin > 0 and notional > 0:
        return notional / trade.margin
    return 10.0


def _riper(trade: Trade, ctx: Optional[Any]) -> float:
    """Risk per trade as % of margin: stop-distance% x leverage."""
    lev = _leverage_of(trade, ctx)
    if trade.entry_price > 0 and trade.synthetic_sl > 0:
        dist = abs(trade.entry_price - trade.synthetic_sl) / trade.entry_price * 100.0
        return dist * lev
    return 0.0


def _giveback(trade: Trade) -> float:
    return max(0.0, trade.peak_roe - trade.roe_pct)


def _protected_roe(trade: Trade) -> Optional[float]:
    """ROE level protected by the current synthetic stop (sign-aware)."""
    if trade.protection_state.value in ("NONE",):
        return None
    if trade.entry_price > 0 and trade.synthetic_sl > 0:
        lev = _leverage_of(trade)
        if _side_long(trade):
            return (trade.synthetic_sl - trade.entry_price) / trade.entry_price * 100.0 * lev
        return (trade.entry_price - trade.synthetic_sl) / trade.entry_price * 100.0 * lev
    return None


def _trend_label(ctx: Optional[Any]) -> str:
    th = _num(_c(ctx, "advisory_trend_health"), 5.0)
    if th >= 7.5:
        return "HEALTHY"
    if th >= 5.5:
        return "FAIR"
    if th >= 3.5:
        return "WEAKENING"
    return "COLLAPSING"


def _momentum_label(ctx: Optional[Any]) -> str:
    flow = _c(ctx, "momentum_flow") or {}
    mh = _num((flow.get("momentum_health") if isinstance(flow, dict) else None),
              _num(_c(ctx, "momentum_health"), 50.0))
    if mh >= 60:
        return "STRONG"
    if mh >= 40:
        return "STEADY"
    if mh >= 25:
        return "COOLING"
    return "DECAYING"


def _distribution_label(ctx: Optional[Any]) -> str:
    sm = _c(ctx, "smart_money") or {}
    dist = _num((sm.get("distribution_risk") if isinstance(sm, dict) else None),
                _num(_c(ctx, "distribution_risk"), 0.0))
    if dist >= 75:
        return "HIGH"
    if dist >= 50:
        return "MODERATE"
    if dist >= 25:
        return "MILD"
    return "CLEAN"


def _structure_label(ctx: Optional[Any]) -> str:
    aligned = bool(_c(ctx, "advisory_structure_aligned", False))
    if aligned:
        return "INTACT"
    shift = _c(ctx, "advisory_struct_shift")
    return f"WEAKENED{(' ' + str(shift).upper()) if shift else ''}"[:18]


def _thesis_label(ctx: Optional[Any]) -> str:
    tf = _num(_c(ctx, "thesis_failure_score"), 0.0)
    if tf >= 70:
        return "FAILING"
    if tf >= 40:
        return "STRAINED"
    return "VALID"


def _momentum_health(ctx: Optional[Any]) -> float:
    flow = _c(ctx, "momentum_flow") or {}
    if isinstance(flow, dict) and flow.get("momentum_health") is not None:
        return _num(flow.get("momentum_health"))
    return _num(_c(ctx, "momentum_health"), 50.0)


def _distribution_risk(ctx: Optional[Any]) -> float:
    sm = _c(ctx, "smart_money") or {}
    if isinstance(sm, dict) and sm.get("distribution_risk") is not None:
        return _num(sm.get("distribution_risk"))
    return _num(_c(ctx, "distribution_risk"), 0.0)


def _continuation(ctx: Optional[Any]) -> float:
    return _num(_c(ctx, "continuation_probability"), 0.5)


def _counter_pressure(ctx: Optional[Any]) -> float:
    return _num(_c(ctx, "counter_pressure"), _num(_c(ctx, "continuation_pressure"), 0.0))


def _posture(trade: Trade, ctx: Optional[Any]) -> str:
    """Current management posture along the defense ladder."""
    roe = trade.roe_pct
    prot = trade.protection_state.value if trade.protection_state else "NONE"
    stage = trade.profit_stage.value if trade.profit_stage else "NONE"
    if prot == "TRAILING" and roe > 0:
        return "KEEP RUNNER"
    if prot in ("TRAILING", "PROFIT_LOCK") and roe >= 0:
        return "TIGHTEN TRAILING"
    if stage in ("PROFIT_LOCKED", "TRAILING_ACTIVE") or prot in ("BREAKEVEN", "PROFIT_LOCK"):
        return "LOCK PROFIT"
    if roe > 0:
        return "PROFIT_DEFENSE"
    return "INITIAL DEFENSE"


def _action_word(decision: Optional[TradeDecision]) -> str:
    if decision is None:
        return "KEEP RUNNER"
    a = decision.action
    if a == "FULL_CLOSE":
        return "EXIT POSITION"
    if a == "PARTIAL_CLOSE":
        return "DE-RISK PARTIAL"
    if a == "ADJUST_SL":
        return "RATCHET PROTECTION"
    return "KEEP RUNNER"


def _crisis_decision(trade: Trade, ctx: Optional[Any], action_word: str) -> str:
    tf = _num(_c(ctx, "thesis_failure_score"), 0.0)
    dist = _distribution_risk(ctx)
    dd = _num(_c(ctx, "drawdown_from_peak"), 0.0)
    roe = trade.roe_pct
    if tf >= 70 or dist >= 80:
        return "FULL EXIT — STRICT CLOSE"
    if dist >= 60 or (roe < 0 and dd >= 12) or _counter_pressure(ctx) >= 40:
        return "DE-RISK NOW"
    if roe < 0:
        return "PROTECT & RECONFIGURE"
    return action_word


def _warnings(ctx: Optional[Any]) -> List[str]:
    flags: List[str] = []
    if _momentum_health(ctx) < 35:
        flags.append("Momentum Decay")
    if _distribution_risk(ctx) >= 60:
        flags.append("Distribution Detected")
    if not bool(_c(ctx, "advisory_structure_aligned", False)):
        flags.append("Structure Weakening")
    if _continuation(ctx) < 0.5:
        flags.append("Continuation Probability Falling")
    if _counter_pressure(ctx) >= 30:
        flags.append("Opposing Pressure Increasing")
    if not flags:
        flags.append("Risk Curve Steepening")
    return flags


def _risk_collapsed(trade: Trade, ctx: Optional[Any]) -> bool:
    if trade.roe_pct >= 0:
        return False
    if _momentum_health(ctx) < 35 or _distribution_risk(ctx) >= 60:
        return True
    if _num(_c(ctx, "thesis_failure_score"), 0.0) >= 50:
        return True
    if _continuation(ctx) < 0.45:
        return True
    return False


# ---------------------------------------------------------------------------
# Context builders (used by coordinator / manager wiring)
# ---------------------------------------------------------------------------

def _board_ctx(trade: Trade, market: Optional[Any] = None) -> Dict[str, Any]:
    """Normalized context for board rendering.

    Precedence: per-trade live data captured inside the engine scope
    (trade.board_data) wins; the market snapshot fills whatever is missing.
    """
    ctx = dict(getattr(trade, "board_data", None) or {})
    if market is not None:
        for k in ("price", "atr", "atr_pct", "rsi", "adx", "trend_strength",
                  "momentum", "volume_ratio", "spread_pct", "momentum_health",
                  "distribution_risk", "continuation_probability",
                  "counter_pressure", "structure_aligned", "thesis_failure_score"):
            v = getattr(market, k, None)
            if v is not None and not isinstance(v, (dict, list)):
                ctx[k] = v
    return ctx


def _open_ctx(state: Optional[Dict[str, Any]], trade: Trade,
              candidate: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    state = state or {}
    leverage = _num(state.get("leverage")) or _num(state.get("mode_leverage")) \
        or _leverage_of(trade)
    mode = str(state.get("mode") or state.get("trading_mode") or "HEDGE").upper()
    order_id = state.get("order_id") or state.get("venue_position_id") \
        or trade.venue_position_id or "-"
    trailing = state.get("trailing_mode")
    if not trailing and trade.trail_activation_price > 0:
        trailing = f"ACTIV @ {trade.trail_activation_price:.4f}"
    return {
        "mode": mode,
        "leverage": leverage,
        "execution": "MARKET",
        "order_id": order_id,
        "risk_pct": _riper(trade, {"leverage": leverage}),
        "trailing": trailing or "STANDARD",
    }


# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------

def render_open_board(trade: Trade,
                      votes: Optional[List[CouncilMemberVote]] = None,
                      extra: Optional[Dict[str, Any]] = None,
                      ctx: Optional[Any] = None) -> str:
    """🟢 TRADE OPENED — the professional fill board.

    Identity + sizing, the initial protection panel, the five-member board at
    open, the board decision block and the execution verification panel.
    """
    votes = votes or []
    if isinstance(ctx, dict):
        ctx = dict(ctx or {})
    else:
        ctx = _board_ctx(trade)
    lines: List[str] = []

    # ---- Identity / sizing ----
    lines.append(_two("TRADE ID", trade.trade_id))
    lines.append(_two("SYMBOL", trade.symbol))
    lines.append(_two("SIDE", f"{_direction(trade)} ({trade.side})"))
    lines.append(_two("MODE", _fit(_c(ctx, "mode", "HEDGE"), 20)))
    lines.append(_two("EXECUTION", _fit(_c(ctx, "execution", "MARKET"), 20)))
    lines.append(_two("ENTRY", f"{trade.entry_price:.6f}"))
    lines.append(_two("QTY", f"{trade.original_qty:.6f}"))
    lines.append(_two("LEVERAGE", f"{_leverage_of(trade, ctx):.0f}x"))
    lines.append(_two("MARGIN", f"{trade.margin:.2f} USDT" if trade.margin > 0 else "-"))
    notional = trade.entry_price * trade.original_qty
    lines.append(_two("NOTIONAL", f"{notional:.2f} USDT" if notional > 0 else "-"))
    if extra:
        for k in ("spread_pct", "classification"):
            if k in extra and extra[k] is not None:
                lines.append(_two(k.upper(), extra[k]))

    # ---- Initial protection ----
    lines.append(_sep("🛡 INITIAL PROTECTION"))
    lines.append(_two("STOP LOSS", f"{trade.synthetic_sl:.6f}" if trade.synthetic_sl > 0 else "-"))
    lines.append(_two("RISK / TRADE", f"{_riper(trade, ctx):.2f}% of margin"))
    lines.append(_two("TAKE PROFIT 1", f"{trade.tp1_price:.6f}" if trade.tp1_price > 0 else "-"))
    lines.append(_two("TAKE PROFIT 2", f"{trade.tp2_price:.6f}" if trade.tp2_price > 0 else "-"))
    lines.append(_two("TRAILING", _fit(_c(ctx, "trailing", "STANDARD"), 30)))

    # ---- Position board at open ----
    lines.append(_sep("🧠 POSITION BOARD — OPEN"))
    if votes:
        for v in votes:
            lines.append(_two(
                f"{(v.name or 'MEMBER').upper()}",
                f"{str(v.vote):<14} score={v.score:>3.0f} "
                f"urgency {_urgency_bar(v.score)} {_fit(v.rationale, 26)}"))
    lines.append(_two("EXECUTION", "VERIFIED" if trade.is_active else "PENDING"))

    # ---- Board decision ----
    lines.append(_sep("⚖️  BOARD DECISION"))
    lines.append(_two("THESIS", _fit(trade.classification or trade.entry_reason or "ACCEPTED", 34)))
    lines.append(_two("RISK STATE", "ACCEPTED" if _riper(trade, ctx) <= 3.0 else "ELEVATED"))
    lines.append(_two("PROFIT STATE", trade.profit_stage.value if trade.profit_stage else "OPENED"))
    lines.append(_two("PEAK ROE", f"{trade.peak_roe:+.2f}%"))
    lines.append(_two("GIVEBACK", f"{_giveback(trade):+.2f}%"))
    lines.append(_two("ACTION", "ACCEPTED"))

    # ---- Execution verification ----
    lines.append(_sep("🔐 EXECUTION VERIFICATION"))
    lines.append(_two("ORDER ID", _fit(_c(ctx, "order_id", "-"), 36)))
    lines.append(_two("CLIENT ORDER ID", _fit(str(trade.client_order_id or "-"), 36)))
    lines.append(_two("STATUS", trade.status.value))
    lines.append(_two("POSITION CHECK", "OPEN" if trade.is_active else "PENDING"))
    lines.append(_two("STATE", "ACTIVE" if trade.is_active else trade.position_status))

    return _ProBox.render(f"🟢 BARON — TRADE OPENED  {trade.symbol}", lines)


def render_position_board(trade: Trade,
                          votes: Optional[List[CouncilMemberVote]] = None,
                          ctx: Optional[Any] = None) -> str:
    """📊 POSITION BOARD — dynamic in-life status.

    Peak / current / giveback ROE, the five live labels (trend, momentum,
    distribution, structure, thesis), the defense ladder and running state.
    """
    ctx = _board_ctx(trade) if ctx is None else ctx
    lines: List[str] = []

    lines.append(_two("STATUS", trade.status.value))
    lines.append(_two("PEAK ROE", f"{trade.peak_roe:+.2f}%"))
    lines.append(_two("CURRENT ROE", f"{trade.roe_pct:+.2f}%"))
    lines.append(_two("GIVEBACK", f"{_giveback(trade):+.2f}%"))
    lines.append(_two("MARK", f"{trade.mark_price:.6f}" if trade.mark_price else "-"))
    lines.append(_two("QTY", f"{trade.remaining_qty:.6f} / {trade.original_qty:.6f}"))

    lines.append(_sep("📈 POSITION STATE"))
    lines.append(_two("TREND", _trend_label(ctx)))
    lines.append(_two("MOMENTUM", _momentum_label(ctx)))
    lines.append(_two("DISTRIBUTION", _distribution_label(ctx)))
    lines.append(_two("STRUCTURE", _structure_label(ctx)))
    lines.append(_two("THESIS", _thesis_label(ctx)))

    posture = _posture(trade, ctx)
    ladder = "PROFIT_DEFENSE → LOCK PROFIT → TIGHTEN TRAILING → KEEP RUNNER"
    lines.append(_sep("🧠 BOARD DECISION"))
    lines.append(_fit(ladder, 62))
    lines.append(_two("POSTURE", f"{posture}  ◀ ACTIVE"))
    if votes:
        compact = "  ".join(
            f"{_MEMBER_TAG.get(v.name, v.name[:2])}={str(v.vote)[0]}" for v in votes
        )
        lines.append(_two("VOTES", compact))
        maxima = max((v for v in votes), key=lambda v: v.score, default=None)
        if maxima and maxima.vote != "HOLD":
            lines.append(_two("LEAD", f"{maxima.name}: {maxima.vote} "
                                     f"score={maxima.score:.0f} {_fit(maxima.rationale, 24)}"))
    lines.append(_two("PROTECTED ROE",
                      f"{_protected_roe(trade):+.2f}%" if _protected_roe(trade) is not None else "— (none)"))
    runner = trade.remaining_ratio * 100.0 if trade.original_qty > 0 else 0.0
    lines.append(_two("RUNNER", f"{runner:.0f}% of position"))
    lines += _profit_phase_rows(trade, ctx)
    return _ProBox.render(f"📊 POSITION BOARD  {trade.symbol}", lines)


def _profit_phase_rows(trade: Trade, ctx: Optional[Any] = None) -> List[str]:
    """The unified 50/50 two-phase profit-taking block.

    INITIAL 100% → TP1 banks 50% once (verified) → RUNNER rides trend →
    TP2 closes the remaining 50%. Runner partials are forbidden.
    """
    snap = trade.tp_phase_snapshot()
    side_label = f"{_direction(trade)} ({trade.side})" if trade.side else "-"
    rows: List[str] = []
    rows.append(_sep("🎯 PROFIT PHASE (50/50)"))
    rows.append(_two("POSITION", side_label))
    rows.append(_two("INITIAL SIZE",
                     f"100% ({float(snap['initial_size']):.6f})"))
    rows.append(_two("TP1 50%",
                     f"{snap['tp1_status']} (closed {float(snap['tp1_fill_qty']):.6f})"))
    rows.append(_two("RUNNER 50%",
                     f"{snap['runner_status']} ({float(snap['runner_qty']):.6f})"))
    rows.append(_two("TP2 (runner)",
                     f"{snap['tp2_status']} → remaining {float(snap['tp2_qty']):.6f} ({snap['tp2_pct']:.1f}%)"))
    rows.append(_two("PROFIT LOCK", snap["profit_lock"]))
    rows.append(_two("MANAGEMENT", _profit_management_label(trade, ctx)))
    return rows


def _profit_management_label(trade: Trade, ctx: Optional[Any] = None) -> str:
    """Management posture: RIDE TREND / PROTECT / EXIT (advisory only)."""
    ctx = _board_ctx(trade) if ctx is None else ctx
    if trade.status in (TradeStatus.CLOSED, TradeStatus.PARTIAL_CLOSE) and trade.remaining_qty <= 0:
        return "EXIT"
    if trade.tp2_state == "EXECUTED":
        return "EXIT"
    protecting = trade.protection_state in (
        ProtectionState.PROFIT_LOCK, ProtectionState.TRAILING)
    warnings = _warnings(ctx)
    exit_wanted = _crisis_decision(trade, ctx, "PROTECT").startswith("FULL") if warnings else False
    thesis_bad = float(getattr(ctx, "thesis_failure_score", 0) or 0) >= 60
    if protecting and thesis_bad:
        return "EXIT"
    if protecting:
        return "PROTECT"
    if thesis_bad:
        return "EXIT"
    return "RIDE TREND"


def render_risk_alert(trade: Trade, alert: str,
                      level: str = "WARNING",
                      ctx: Optional[Any] = None) -> str:
    """🔴 RISK CURVE ALERT — a collapse / step-change alarm board.

    Prints the risk-curve numbers, every failed defense flag and the board
    verdict so the operator sees exactly what is breaking and what to do.
    """
    ctx = _board_ctx(trade) if ctx is None else ctx
    lines: List[str] = []

    lines.append(_two("LEVEL", level.upper()))
    lines.append(_two("REASON", _fit(alert, 56)))

    lines.append(_sep("📉 RISK CURVE"))
    lines.append(_two("PEAK ROE", f"{trade.peak_roe:+.2f}%"))
    lines.append(_two("CURRENT ROE", f"{trade.roe_pct:+.2f}%"))
    lines.append(_two("GIVEBACK", f"{_giveback(trade):+.2f}%"))
    if trade.synthetic_sl > 0 and trade.mark_price > 0:
        dist = (trade.mark_price - trade.synthetic_sl) / trade.synthetic_sl * 100.0
        lines.append(_two("DIST TO SL", f"{dist:+.2f}%"))
    far = max(abs(trade.roe_pct), abs(trade.unrealized_pnl_pct), 1e-9)
    when = min(1.0, max(0.0, 0.5 + (trade.roe_pct / far if trade.roe_pct < 0 else 0.25)))
    lines.append(_two("RISK CURVE", f"{_risk_curve(when)} ({when * 100:.0f}%)"))

    lines.append(_sep("⚠️  FAILURE FLAGS"))
    for flag in _warnings(ctx):
        lines.append(_two("", f"⚠ {flag}"))

    lines.append(_sep("🧠 BOARD DECISION"))
    lines.append(_two("CALL", _crisis_decision(trade, ctx, "PROTECT")))
    lines.append(_two("RECOMMENDED",
                      "FULL_CLOSE" if _crisis_decision(trade, ctx, "PROTECT").startswith("FULL")
                      else "PARTIAL_DE-RISK"))
    return _ProBox.render(f"🔴 RISK CURVE ALERT  {trade.symbol}", lines)


def render_close_board(trade: Trade,
                       votes: Optional[List[CouncilMemberVote]] = None,
                       decision: Optional[TradeDecision] = None,
                       result: Optional[str] = None,
                       strict: Optional[bool] = None,
                       ctx: Optional[Any] = None) -> str:
    """Close board.

    standard  -> realizes the result with the 5-member vote tally.
    STRICT    -> thesis-failure exit: 5-member EXIT vote + executive approval,
                 the verified close checklist and CLOSED_CONFIRMED status.
    """
    votes = votes or []
    reason = ""
    if trade.exit_reason:
        reason = trade.exit_reason.value
    if decision and decision.exit_reason:
        reason = decision.exit_reason.value
    if decision and decision.reason and not reason:
        reason = decision.reason
    is_strict = bool(strict)
    is_strict = is_strict or \
        (trade.exit_reason is not None and trade.exit_reason.value == "THESIS_FAILURE") or \
        (decision is not None and decision.exit_reason is not None
         and decision.exit_reason.value == "THESIS_FAILURE")
    result = result or trade.final_result_class or "UNKNOWN"

    lines: List[str] = []
    lines.append(_two("TRADE ID", trade.trade_id))
    lines.append(_two("SYMBOL", trade.symbol))
    lines.append(_two("SIDE", f"{_direction(trade)} ({trade.side})"))
    lines.append(_two("EXIT REASON", reason or "MANUAL"))
    lines.append(_two("ENTRY", f"{trade.entry_price:.6f}"))
    lines.append(_two("EXIT", f"{trade.mark_price:.6f}" if trade.mark_price else "-"))
    lines.append(_two("REALIZED", f"{trade.realized_pnl_pct:+.2f}% / "
                                  f"{trade.realized_pnl_usdt:+.2f} USDT"))
    if trade.duration_sec > 0:
        lines.append(_two("DURATION", f"{trade.duration_sec:.0f}s"))
    legs = len(trade.partial_legs or [])
    if legs:
        lines.append(_two("PARTIAL LEGS", legs))

    if is_strict:
        lines.append(_sep("🧠 BOARD VOTE"))
        for v in votes:
            tally = "EXIT" if v.vote in ("FULL_CLOSE",) else \
                ("PARTIAL" if v.vote == "PARTIAL_CLOSE" else v.vote)
            lines.append(f"{_MEMBER_ICON.get(v.name, '▪')} {v.name:<14} "
                         f"{tally:<10} score={v.score:>3.0f}  {_fit(v.rationale, 26)}")
        if not votes:
            lines.append("▪ (no council votes attached — engine directive)")
        lines.append(_two("EXECUTION", "APPROVED ✔"))
        lines.append(_sep("🚨 STRICT CLOSE"))
        lines.append(_two("ACTION", "FULL_STRICT_CLOSE"))
        lines.append(_sep("✔ VERIFY CHECKLIST"))
        lines.append("   ✓ MARKET CLOSE → ✓ FILL VERIFICATION → ✓ POSITION RE-READ")
        lines.append("   ✓ REMAINING QTY CHECK → ✓ LOCAL STATE SYNC")
        lines.append(_two("STATUS", "CLOSED_CONFIRMED"))
    else:
        lines.append(_sep("5-MEMBER VOTE"))
        for v in votes:
            tally = "CLOSE" if v.vote in ("FULL_CLOSE",) else \
                ("PARTIAL" if v.vote == "PARTIAL_CLOSE" else v.vote)
            lines.append(f"{_MEMBER_ICON.get(v.name, '▪')} {v.name:<14} "
                         f"{tally:<10} score={v.score:>3.0f}  {_fit(v.rationale, 26)}")
        if not votes:
            lines.append("▪ (no council votes attached)")
    lines.append("")
    lines.append("RESULT " + _fit(result, 40))
    return _ProBox.render(f"🚨 STRICT CLOSE  {trade.symbol}", lines)


class TradeBoardLogger:
    """Logs the lifecycle boards through the engine execution logger.

    Usage: TradeBoardLogger(logger=engine.log_execution). The default logger
    signature is fn(text, level) — same shape as engine.log_execution.
    """

    def __init__(self, logger: Optional[Callable] = None,
                 engine=None, status_every_sec: float = 45.0,
                 risk_every_sec: float = 90.0):
        self._logger = logger
        self.engine = engine
        self.status_every_sec = status_every_sec
        self.risk_every_sec = risk_every_sec
        self._last_status: Dict[str, float] = {}
        self._last_risk: Dict[str, float] = {}

    @property
    def logger(self) -> Callable:
        if self._logger is not None:
            return self._logger
        if self.engine is not None and hasattr(self.engine, "log_execution"):
            return self.engine.log_execution
        return lambda text, level="INFO": print(text)

    def log_open(self, trade: Trade,
                 votes: Optional[List[CouncilMemberVote]] = None,
                 extra: Optional[Dict[str, Any]] = None,
                 ctx: Optional[Any] = None) -> None:
        self.logger(render_open_board(trade, votes, extra, ctx), "INFO")

    def log_status(self, trade: Trade,
                   votes: Optional[List[CouncilMemberVote]] = None,
                   force: bool = False,
                   ctx: Optional[Any] = None) -> bool:
        """Dynamic position board. Rate-limited by default; returns True if
        a board was actually emitted."""
        if not force:
            last = self._last_status.get(trade.trade_id, 0.0)
            if time.time() - last < self.status_every_sec:
                return False
        self._last_status[trade.trade_id] = time.time()
        self.logger(render_position_board(trade, votes, ctx), "INFO")
        return True

    def log_risk(self, trade: Trade, alert: str, level: str = "WARNING",
                 ctx: Optional[Any] = None, force: bool = True) -> bool:
        """Risk-curve alert. Rate-limited except when forced. Returns True if
        the alert was actually emitted."""
        if not force:
            last = self._last_risk.get(trade.trade_id, 0.0)
            if time.time() - last < self.risk_every_sec:
                return False
        self._last_risk[trade.trade_id] = time.time()
        self.logger(render_risk_alert(trade, alert, level, ctx), level)
        return True

    def log_close(self, trade: Trade,
                  decision: Optional[TradeDecision] = None,
                  votes: Optional[List[CouncilMemberVote]] = None,
                  result: Optional[str] = None,
                  strict: Optional[bool] = None,
                  ctx: Optional[Any] = None) -> None:
        # Defensive: older callers pass the votes list positionally where a
        # decision is expected. A list can never be a decision — shuffle it.
        if isinstance(decision, list):
            if votes is None:
                votes = decision
            decision = None
        result = result or trade.final_result_class or "UNKNOWN"
        # NOTE: render_close_board is (trade, votes=None, decision=None,
        # result=None) — keep that exact argument order.
        self.logger(render_close_board(trade, votes, decision, result, strict, ctx),
                    "SUCCESS" if result == "WIN" else "INFO")