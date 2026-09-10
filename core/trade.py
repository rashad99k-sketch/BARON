"""Trade entity — single source of truth for each open/closed position.

This module defines the Trade dataclass that replaces the old activate/deactivate
state-swapping pattern. Each trade is an independent, persistent object with its
own lifecycle state, protection levels, partial-close history, and execution
metadata.

DESIGN PRINCIPLES:
  1. Trade is the ONLY source of truth for a position's state.
  2. No global mutable STATE dict — each Trade owns its fields.
  3. Thread-safe via explicit versioning and copy-on-read.
  4. Serializable to/from exchange state and journal for restart recovery.
  5. Idempotent via client_order_id tracking.
"""
from __future__ import annotations

import enum
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class TradeStatus(str, enum.Enum):
    PENDING = "PENDING"          # Intent registered, order not yet sent
    SUBMITTED = "SUBMITTED"      # Order sent to exchange, awaiting fill
    FILLED = "FILLED"            # Order filled, position active
    PARTIAL_CLOSE = "PARTIAL_CLOSE"  # Partially closed (TP1 / scaling)
    CLOSING = "CLOSING"          # Close order sent, awaiting confirmation
    CLOSED = "CLOSED"            # Fully closed and finalized
    FAILED = "FAILED"            # Order failed / rejected
    RECOVERED = "RECOVERED"      # Restored from exchange after restart


class ProfitStage(str, enum.Enum):
    NONE = "NONE"
    OPENED = "OPENED"
    PROFIT_DETECTED = "PROFIT_DETECTED"
    TP1_ELIGIBLE = "TP1_ELIGIBLE"
    TP1_EXECUTED = "TP1_EXECUTED"
    PROFIT_LOCKED = "PROFIT_LOCKED"
    TRAILING_ACTIVE = "TRAILING_ACTIVE"
    TP2_EXECUTED = "TP2_EXECUTED"
    CLOSED = "CLOSED"


class ProtectionState(str, enum.Enum):
    NONE = "NONE"
    BREAKEVEN = "BREAKEVEN"
    PROFIT_LOCK = "PROFIT_LOCK"
    TRAILING = "TRAILING"


class TradeStyle(str, enum.Enum):
    TREND = "TREND"            # Riding a trend with trailing
    SCALP = "SCALP"            # Quick profit, tight targets
    REVERSAL = "REVERSAL"      # Counter-trend entry
    NEWS = "NEWS"              # News-driven catalyst
    INSTITUTIONAL = "INSTITUTIONAL"


class ExitReason(str, enum.Enum):
    TP1 = "TP1"
    TP2 = "TP2"
    STOP_LOSS = "STOP_LOSS"
    BREAKEVEN = "BREAKEVEN"
    TRAILING_STOP = "TRAILING_STOP"
    PROFIT_LOCK = "PROFIT_LOCK"
    REVERSAL = "REVERSAL"
    INSTITUTIONAL_REVERSAL = "INSTITUTIONAL_REVERSAL"
    THESIS_FAILURE = "THESIS_FAILURE"
    EXHAUSTION = "EXHAUSTION"
    SCALP_TARGET = "SCALP_TARGET"
    MANUAL = "MANUAL"
    KILL_SWITCH = "KILL_SWITCH"
    EXTERNAL = "EXTERNAL"
    TIMEOUT = "TIMEOUT"


@dataclass
class PartialCloseLeg:
    """Record of a single partial-close execution."""
    leg_id: int
    qty: float
    price: float
    realized_pnl_usdt: float
    realized_pnl_pct: float
    timestamp: float
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "leg_id": self.leg_id,
            "qty": self.qty,
            "price": self.price,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "realized_pnl_pct": self.realized_pnl_pct,
            "timestamp": self.timestamp,
            "reason": self.reason,
        }


def _leg_to_legacy_state(leg: PartialCloseLeg, side: str, entry: float) -> dict:
    """Map a PartialCloseLeg to the legacy engine STATE partial-leg schema.

    The legacy engine reads ``pnl_usdt``/``pnl_pct`` in its booked subtraction
    (``finalize_trade_with_reality``) and appends via ``_record_partial_leg``;
    exporting the Trade's legs in that exact schema keeps scoped finalize math
    consistent with the partial accruals already credited to PERF.
    """
    return {
        "side": side,
        "qty": float(leg.qty),
        "price": float(leg.price),
        "entry": float(entry),
        "pnl_pct": float(leg.realized_pnl_pct),
        "pnl_usdt": float(leg.realized_pnl_usdt),
        "ts": float(leg.timestamp),
        "mode": "SCOPED",
        "leg_id": int(leg.leg_id),
        "reason": leg.reason,
    }


@dataclass
class Trade:
    """Immutable-identity, mutable-state trade entity.

    Each trade carries its own complete lifecycle state. No global dict
    swapping — the Trade IS the state.
    """
    # === Identity (immutable after creation) ===
    trade_id: str = ""
    symbol: str = ""
    side: str = ""                # "BUY" or "SELL"
    asset_class: str = "CRYPTO"
    trade_style: TradeStyle = TradeStyle.INSTITUTIONAL
    created_at: float = field(default_factory=time.time)

    # === Execution metadata ===
    client_order_id: Optional[str] = None
    venue_position_id: Optional[str] = None
    entry_price: float = 0.0
    entry_time: float = 0.0
    entry_atr: float = 0.0
    entry_score: float = 0.0
    entry_reason: str = ""

    # === Position sizing ===
    original_qty: float = 0.0
    remaining_qty: float = 0.0
    margin: float = 0.0

    # === Protection levels (monotonic — never move backward) ===
    synthetic_sl: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    protection_floor_sl: float = 0.0
    trail_stop: float = 0.0
    trail_activation_price: float = 0.0

    # === Lifecycle state ===
    status: TradeStatus = TradeStatus.PENDING
    profit_stage: ProfitStage = ProfitStage.NONE
    protection_state: ProtectionState = ProtectionState.NONE

    # === TP execution state ===
    tp1_state: str = "NONE"     # NONE | ELIGIBLE | EXECUTED | FAILED
    tp1_exec_price: float = 0.0
    tp1_fill_qty: float = 0.0
    tp1_event_ts: float = 0.0
    tp2_state: str = "NONE"
    tp2_event_ts: float = 0.0

    # === Partial close history ===
    partial_legs: List[PartialCloseLeg] = field(default_factory=list)
    realized_pnl_usdt: float = 0.0
    realized_pnl_pct: float = 0.0
    realized_roe_pct: float = 0.0
    last_exhaustion_ts: float = 0.0

    # === Live market snapshot (updated each management cycle) ===
    mark_price: float = 0.0
    unrealized_pnl_usdt: float = 0.0
    unrealized_pnl_pct: float = 0.0
    roe_pct: float = 0.0
    peak_roe: float = 0.0
    peak_price: float = 0.0

    # === Close / finalize ===
    exit_reason: Optional[ExitReason] = None
    exit_reason_detail: str = ""
    final_result_class: Optional[str] = None  # WIN / LOSS / BREAKEVEN
    close_time: float = 0.0
    duration_sec: float = 0.0

    # === Recovery ===
    recovered: bool = False
    recovery_ts: float = 0.0
    position_status: str = "OPEN"

    # === Native exchange protection ===
    native_sl_state: str = "NONE"
    native_sl_order_id: Optional[str] = None
    native_sl_price: float = 0.0

    # === Version for optimistic concurrency ===
    version: int = 1

    # === Signal context (for council decisions) ===
    trade_type: str = "INSTITUTIONAL"
    classification: str = "SNIPER"
    market_phase: str = ""
    zone_behaviour: str = ""
    narrative: str = ""
    confidence: float = 0.0

    # === Management board decisions (updated by council) ===
    board_decisions: Dict[str, Any] = field(default_factory=dict)

    # === Live per-trade context captured inside the engine scope ===
    # (smart money, momentum, distribution, thesis, health... ) consumed by the
    # professional position/risk boards. Advisory presentation only.
    board_data: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.trade_id:
            self.trade_id = self._generate_id()
        if self.remaining_qty <= 0 and self.original_qty > 0:
            self.remaining_qty = self.original_qty

    @staticmethod
    def _generate_id() -> str:
        token = uuid.uuid4().hex[:8]
        return f"T-{int(time.time() * 1000)}-{token}"

    @property
    def is_open(self) -> bool:
        return self.status in (
            TradeStatus.PENDING, TradeStatus.SUBMITTED,
            TradeStatus.FILLED, TradeStatus.PARTIAL_CLOSE,
        )

    @property
    def is_active(self) -> bool:
        return self.status in (TradeStatus.FILLED, TradeStatus.PARTIAL_CLOSE)

    @property
    def tp1_hit(self) -> bool:
        return self.tp1_state == "EXECUTED"

    @property
    def remaining_ratio(self) -> float:
        if self.original_qty <= 0:
            return 0.0
        return max(0.0, min(1.0, self.remaining_qty / self.original_qty))

    @property
    def realized_ratio(self) -> float:
        return 1.0 - self.remaining_ratio

    def can_advance_stage(self, target: ProfitStage) -> bool:
        """Check if the trade can advance to the target profit stage."""
        stage_order = list(ProfitStage)
        try:
            current_idx = stage_order.index(self.profit_stage)
            target_idx = stage_order.index(target)
            return target_idx > current_idx
        except ValueError:
            return False

    def advance_stage(self, target: ProfitStage) -> bool:
        """Advance profit stage if allowed (monotonic, forward-only)."""
        if self.can_advance_stage(target):
            self.profit_stage = target
            self.version += 1
            return True
        return False

    def ratchet_sl(self, new_sl: float) -> bool:
        """Move protective SL only in the profitable direction."""
        if self.side == "BUY":
            if new_sl > self.protection_floor_sl:
                self.protection_floor_sl = new_sl
                self.version += 1
                return True
        elif self.side == "SELL":
            if new_sl < self.protection_floor_sl or self.protection_floor_sl == 0:
                self.protection_floor_sl = new_sl
                self.version += 1
                return True
        return False

    def add_partial_leg(self, qty: float, price: float, reason: str = "",
                        adjust_remaining: bool = True) -> PartialCloseLeg:
        """Record a partial-close leg.

        ``adjust_remaining`` defaults to True (subtract the leg from the
        remaining quantity). Set it False when the engine already reduced the
        remaining size on the venue/paper ledger and this leg is only the
        bookkeeping mirror of that close.
        """
        leg_id = len(self.partial_legs) + 1
        if self.side == "BUY":
            pnl_pct = ((price - self.entry_price) / self.entry_price * 100) if self.entry_price > 0 else 0.0
        else:
            pnl_pct = ((self.entry_price - price) / self.entry_price * 100) if self.entry_price > 0 else 0.0
        pnl_usdt = pnl_pct / 100 * (qty * self.entry_price) if self.entry_price > 0 else 0.0
        leg = PartialCloseLeg(
            leg_id=leg_id,
            qty=qty,
            price=price,
            realized_pnl_usdt=pnl_usdt,
            realized_pnl_pct=pnl_pct,
            timestamp=time.time(),
            reason=reason,
        )
        self.partial_legs.append(leg)
        if adjust_remaining:
            self.remaining_qty = max(0.0, self.remaining_qty - qty)
        self.realized_pnl_usdt += pnl_usdt
        self.realized_pnl_pct += pnl_pct
        self.version += 1
        if self.remaining_qty <= 0:
            self.status = TradeStatus.CLOSED
        else:
            self.status = TradeStatus.PARTIAL_CLOSE
        return leg

    def finalize(self, exit_reason: ExitReason, result_class: str, detail: str = "") -> None:
        """Finalize the trade after full close."""
        self.status = TradeStatus.CLOSED
        self.exit_reason = exit_reason
        self.exit_reason_detail = detail
        self.final_result_class = result_class
        self.close_time = time.time()
        self.duration_sec = self.close_time - self.entry_time if self.entry_time > 0 else 0
        self.version += 1

    def to_state_dict(self) -> dict:
        """Export to the legacy engine.STATE format for backward compat."""
        return {
            "open": self.is_open,
            "side": self.side,
            "current_symbol": self.symbol,
            "entry": self.entry_price,
            "entry_time": self.entry_time,
            "qty": self.original_qty,
            "remaining_qty": self.remaining_qty,
            "qty_initial": self.original_qty,
            "margin": self.margin,
            "mark_price": self.mark_price,
            "trade_id": self.trade_id,
            "trade_type": self.trade_type,
            "classification": self.classification,
            "entry_atr": self.entry_atr,
            "entry_score": self.entry_score,
            "synthetic_sl": self.synthetic_sl,
            "sl": self.synthetic_sl,
            "synthetic_tp1": self.tp1_price,
            "synthetic_tp2": self.tp2_price,
            "tp2_price": self.tp2_price,
            "protection_floor_sl": self.protection_floor_sl,
            "trail_stop": self.trail_stop,
            "trail_activation_price": self.trail_activation_price,
            "profit_stage": self.profit_stage.value,
            "protection_state": self.protection_state.value,
            "tp1_state": self.tp1_state,
            "tp1_price": self.tp1_price,
            "tp1_exec_price": self.tp1_exec_price,
            "tp1_fill_qty": self.tp1_fill_qty,
            "tp1_event_ts": self.tp1_event_ts,
            "tp2_state": self.tp2_state,
            "tp2_event_ts": self.tp2_event_ts,
            "tp1_hit": self.tp1_state == "EXECUTED",
            "tp2_hit": self.tp2_state == "EXECUTED",
            "trail_activated": self.protection_state == ProtectionState.TRAILING,
            "unrealized_pnl_usdt": self.unrealized_pnl_usdt,
            "roe_pct": self.roe_pct,
            "peak_roe": self.peak_roe,
            "peak_price": self.peak_price,
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "realized_pnl_pct": self.realized_pnl_pct,
            "realized_roe_pct": self.realized_roe_pct,
            "realized_legs": len(self.partial_legs),
            "partial_realized": [_leg_to_legacy_state(leg, self.side, self.entry_price)
                                  for leg in self.partial_legs],
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "final_result_class": self.final_result_class,
            "close_reason": self.exit_reason_detail,
            "duration_sec": self.duration_sec,
            "recovered": self.recovered,
            "recovery_ts": self.recovery_ts,
            "position_status": self.position_status,
            "native_sl_state": self.native_sl_state,
            "native_sl_order_id": self.native_sl_order_id,
            "native_sl_price": self.native_sl_price,
            "market_phase": self.market_phase,
            "zone_behaviour": self.zone_behaviour,
            "narrative": self.narrative,
            "current_confidence": self.confidence,
            "trade_style": self.trade_style.value,
            "asset_class": self.asset_class,
            "entry_type": self.classification,
            "entry_reason": self.entry_reason,
        }

    @classmethod
    def from_exchange_position(cls, pos: dict, trade_id: str = "",
                               client_order_id: str = "",
                               asset_class: str = "CRYPTO") -> "Trade":
        """Create a Trade from an exchange position dict (restart recovery)."""
        symbol = pos.get("symbol", "")
        raw_side = str(pos.get("side", "BUY")).upper()
        side = "BUY" if raw_side in ("LONG", "BUY") else "SELL"
        entry_price = float(pos.get("entryPrice", 0) or 0)
        qty = float(pos.get("contracts", 0) or 0)
        mark = float(pos.get("markPrice", 0) or 0)
        unrealized = float(pos.get("unrealizedPnl", 0) or 0)
        return cls(
            trade_id=trade_id or cls._generate_id(),
            symbol=symbol,
            side=side,
            asset_class=asset_class,
            client_order_id=client_order_id or None,
            entry_price=entry_price,
            entry_time=time.time(),
            original_qty=qty,
            remaining_qty=qty,
            mark_price=mark,
            unrealized_pnl_usdt=unrealized,
            status=TradeStatus.RECOVERED,
            recovered=True,
            recovery_ts=time.time(),
            position_status="RECOVERED",
        )

    def to_dict(self) -> dict:
        """Full serialization for persistence."""
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "asset_class": self.asset_class,
            "trade_style": self.trade_style.value,
            "created_at": self.created_at,
            "client_order_id": self.client_order_id,
            "venue_position_id": self.venue_position_id,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time,
            "entry_atr": self.entry_atr,
            "entry_score": self.entry_score,
            "entry_reason": self.entry_reason,
            "original_qty": self.original_qty,
            "remaining_qty": self.remaining_qty,
            "margin": self.margin,
            "synthetic_sl": self.synthetic_sl,
            "tp1_price": self.tp1_price,
            "tp2_price": self.tp2_price,
            "protection_floor_sl": self.protection_floor_sl,
            "trail_stop": self.trail_stop,
            "trail_activation_price": self.trail_activation_price,
            "status": self.status.value,
            "profit_stage": self.profit_stage.value,
            "protection_state": self.protection_state.value,
            "tp1_state": self.tp1_state,
            "tp1_exec_price": self.tp1_exec_price,
            "tp1_fill_qty": self.tp1_fill_qty,
            "tp1_event_ts": self.tp1_event_ts,
            "tp2_state": self.tp2_state,
            "tp2_event_ts": self.tp2_event_ts,
            "partial_legs": [leg.to_dict() for leg in self.partial_legs],
            "realized_pnl_usdt": self.realized_pnl_usdt,
            "realized_pnl_pct": self.realized_pnl_pct,
            "realized_roe_pct": self.realized_roe_pct,
            "mark_price": self.mark_price,
            "unrealized_pnl_usdt": self.unrealized_pnl_usdt,
            "roe_pct": self.roe_pct,
            "peak_roe": self.peak_roe,
            "peak_price": self.peak_price,
            "exit_reason": self.exit_reason.value if self.exit_reason else None,
            "exit_reason_detail": self.exit_reason_detail,
            "final_result_class": self.final_result_class,
            "close_time": self.close_time,
            "duration_sec": self.duration_sec,
            "recovered": self.recovered,
            "recovery_ts": self.recovery_ts,
            "position_status": self.position_status,
            "native_sl_state": self.native_sl_state,
            "native_sl_order_id": self.native_sl_order_id,
            "native_sl_price": self.native_sl_price,
            "version": self.version,
            "trade_type": self.trade_type,
            "classification": self.classification,
            "market_phase": self.market_phase,
            "zone_behaviour": self.zone_behaviour,
            "narrative": self.narrative,
            "confidence": self.confidence,
            "board_data": self.board_data,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Trade":
        """Deserialize from a dict (e.g. from journal recovery)."""
        partials = []
        for leg_data in data.get("partial_legs", []):
            partials.append(PartialCloseLeg(
                leg_id=leg_data.get("leg_id", 0),
                qty=leg_data.get("qty", 0.0),
                price=leg_data.get("price", 0.0),
                realized_pnl_usdt=leg_data.get("realized_pnl_usdt", 0.0),
                realized_pnl_pct=leg_data.get("realized_pnl_pct", 0.0),
                timestamp=leg_data.get("timestamp", 0.0),
                reason=leg_data.get("reason", ""),
            ))
        try:
            status = TradeStatus(data.get("status", "FILLED"))
        except (ValueError, KeyError):
            status = TradeStatus.FILLED
        try:
            profit_stage = ProfitStage(data.get("profit_stage", "NONE"))
        except (ValueError, KeyError):
            profit_stage = ProfitStage.NONE
        try:
            protection_state = ProtectionState(data.get("protection_state", "NONE"))
        except (ValueError, KeyError):
            protection_state = ProtectionState.NONE
        try:
            trade_style = TradeStyle(data.get("trade_style", "INSTITUTIONAL"))
        except (ValueError, KeyError):
            trade_style = TradeStyle.INSTITUTIONAL
        exit_reason = None
        if data.get("exit_reason"):
            try:
                exit_reason = ExitReason(data["exit_reason"])
            except (ValueError, KeyError):
                pass
        return cls(
            trade_id=data.get("trade_id", ""),
            symbol=data.get("symbol", ""),
            side=data.get("side", "BUY"),
            asset_class=data.get("asset_class", "CRYPTO"),
            trade_style=trade_style,
            created_at=data.get("created_at", 0.0),
            client_order_id=data.get("client_order_id"),
            venue_position_id=data.get("venue_position_id"),
            entry_price=data.get("entry_price", 0.0),
            entry_time=data.get("entry_time", 0.0),
            entry_atr=data.get("entry_atr", 0.0),
            entry_score=data.get("entry_score", 0.0),
            entry_reason=data.get("entry_reason", ""),
            original_qty=data.get("original_qty", 0.0),
            remaining_qty=data.get("remaining_qty", 0.0),
            margin=data.get("margin", 0.0),
            synthetic_sl=data.get("synthetic_sl", 0.0),
            tp1_price=data.get("tp1_price", 0.0),
            tp2_price=data.get("tp2_price", 0.0),
            protection_floor_sl=data.get("protection_floor_sl", 0.0),
            trail_stop=data.get("trail_stop", 0.0),
            trail_activation_price=data.get("trail_activation_price", 0.0),
            status=status,
            profit_stage=profit_stage,
            protection_state=protection_state,
            tp1_state=data.get("tp1_state", "NONE"),
            tp1_exec_price=data.get("tp1_exec_price", 0.0),
            tp1_fill_qty=data.get("tp1_fill_qty", 0.0),
            tp1_event_ts=data.get("tp1_event_ts", 0.0),
            tp2_state=data.get("tp2_state", "NONE"),
            tp2_event_ts=data.get("tp2_event_ts", 0.0),
            partial_legs=partials,
            realized_pnl_usdt=data.get("realized_pnl_usdt", 0.0),
            realized_pnl_pct=data.get("realized_pnl_pct", 0.0),
            realized_roe_pct=data.get("realized_roe_pct", 0.0),
            mark_price=data.get("mark_price", 0.0),
            unrealized_pnl_usdt=data.get("unrealized_pnl_usdt", 0.0),
            roe_pct=data.get("roe_pct", 0.0),
            peak_roe=data.get("peak_roe", 0.0),
            peak_price=data.get("peak_price", 0.0),
            exit_reason=exit_reason,
            exit_reason_detail=data.get("exit_reason_detail", ""),
            final_result_class=data.get("final_result_class"),
            close_time=data.get("close_time", 0.0),
            duration_sec=data.get("duration_sec", 0.0),
            recovered=data.get("recovered", False),
            recovery_ts=data.get("recovery_ts", 0.0),
            position_status=data.get("position_status", "OPEN"),
            native_sl_state=data.get("native_sl_state", "NONE"),
            native_sl_order_id=data.get("native_sl_order_id"),
            native_sl_price=data.get("native_sl_price", 0.0),
            version=data.get("version", 1),
            trade_type=data.get("trade_type", "INSTITUTIONAL"),
            classification=data.get("classification", "SNIPER"),
            market_phase=data.get("market_phase", ""),
            zone_behaviour=data.get("zone_behaviour", ""),
            narrative=data.get("narrative", ""),
            confidence=data.get("confidence", 0.0),
            board_data=data.get("board_data", {}) or {},
        )
