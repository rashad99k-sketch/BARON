"""Trade Execution Coordinator — single authority for all trade lifecycle operations.

This module replaces the scattered open/close/modify paths with a unified
coordinator that:
  1. Registers intent and reserves risk BEFORE sending orders to the exchange
  2. Assigns a deterministic client_order_id for idempotency
  3. Routes ALL trade operations through one path
  4. Handles reconciliation on timeout (not blind retry)
  5. Manages the Trade entity lifecycle

DESIGN:
  - Single instance per bot (singleton pattern via module-level reference)
  - Thread-safe via engine _TRADE_LOCK
  - Backward-compatible: exposes the same interfaces PortfolioManager/Runtime expect
  - All close/partial operations go through here — never direct engine calls
"""
from __future__ import annotations

import hashlib
import os
import re
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from core.trade import (
    Trade, TradeStatus, ProfitStage, ProtectionState,
    TradeStyle, ExitReason,
)
from portfolio.trade_board import (
    TradeBoardLogger,
    _board_ctx,
    _open_ctx,
    _risk_collapsed,
)
from portfolio.trade_council import (
    TradeCouncil, MarketSnapshot, TradeDecision, CouncilMemberVote,
)


def _generate_client_order_id(symbol: str, side: str, trade_id: str, purpose: str = "ENTRY") -> str:
    """Deterministic clientOrderId for a trade intent: BARON_{trade_id}_{purpose}.

    The same trade_id ALWAYS maps to the same clientOrderId (never regenerated),
    so a timeout/reconciliation reuses a stable idempotency key on BingX and a
    blind retry can never double-fill. Only [A-Za-z0-9_] (the project's SAFE_CID
    contract) and fits BingX's 40-char limit.
    """
    tid = re.sub(r"[^A-Za-z0-9_]", "_",
             str(trade_id or "").replace("/", "-").replace(":", "-")).strip("_")
    # Keep the UNIQUE TAIL (the random trade token lives at the end) so two
    # long trade_ids sharing a prefix can never collapse into one key.
    tid = tid[-26:]
    purpose_tok = str(purpose or "ENTRY").upper().replace(" ", "_")[:6]
    return (f"BARON_{tid}_{purpose_tok}")[:40]


class TradeExecutionCoordinator:
    """Single authority for all trade lifecycle operations.

    All open/close/modify requests MUST go through this coordinator.
    It manages:
      - Trade entity creation and lifecycle
      - Client order ID assignment and idempotency
      - Risk reservation before execution
      - Reconciliation on timeout
      - Council-driven management decisions
    """

    def __init__(self, engine=None):
        self.engine = engine
        self._lock = threading.RLock()
        self._active_trades: Dict[str, Trade] = {}  # trade_id -> Trade
        self._pending_intents: Dict[str, Trade] = {}  # client_order_id -> Trade (pre-execution)
        self._closure_log: List[Dict[str, Any]] = []  # immutable record of closed trades
        self._trade_counter: int = 0
        self._board: Optional[TradeBoardLogger] = None

    def _board_logger(self) -> Optional[TradeBoardLogger]:
        """Lazy board logger. Boards are only emitted when an engine logger is
        attached (unit-test coordinators have no engine and stay silent)."""
        if self.engine is None:
            return None
        if self._board is None:
            self._board = TradeBoardLogger(engine=self.engine)
        return self._board

    @staticmethod
    def _votes_from_notes(board_notes: Dict[str, Any]) -> List[CouncilMemberVote]:
        """Rebuild council votes from the serialized board_notes dict."""
        votes = []
        for row in board_notes.get("council_votes", []):
            if not isinstance(row, dict):
                continue
            votes.append(CouncilMemberVote(
                name=str(row.get("name", "?")),
                role=str(row.get("role", "")),
                vote=str(row.get("vote", "HOLD")),
                score=float(row.get("score", 0) or 0),
                rationale=str(row.get("rationale", "")),
            ))
        return votes

    @property
    def active_trades(self) -> Dict[str, Trade]:
        return dict(self._active_trades)

    @property
    def closure_log(self) -> List[Dict[str, Any]]:
        return list(self._closure_log)

    def count_active(self) -> int:
        return len(self._active_trades)

    def get_trade(self, trade_id: str) -> Optional[Trade]:
        return self._active_trades.get(trade_id)

    def get_trade_by_symbol(self, symbol: str) -> Optional[Trade]:
        for trade in self._active_trades.values():
            if trade.symbol == symbol and trade.is_active:
                return trade
        return None

    def get_all_trades(self) -> List[Trade]:
        return list(self._active_trades.values())

    # ──────────────────────────────────────────────────────────────────────
    # OPEN: Intent -> Reserve Risk -> Execute -> Confirm -> Register
    # ──────────────────────────────────────────────────────────────────────

    def open_trade(self, candidate: dict, risk_checker: Callable, engine_execute: Callable) -> Optional[Trade]:
        """Unified open path. All opens go through here.

        Steps:
          1. Generate client_order_id (idempotency key)
          2. Register intent in _pending_intents
          3. Check risk (pre-flight)
          4. Create Trade entity
          5. Execute via engine (with client_order_id)
          6. On success: move to _active_trades
          7. On timeout: reconcile (don't blind-retry)
          8. On failure: clean up intent

        Returns: Trade object on success, None on failure.
        """
        with self._lock:
            symbol = candidate.get("symbol", "")
            side = candidate.get("side", "BUY")
            asset_class = candidate.get("asset_class", "CRYPTO")
            trade_type = candidate.get("trade_type", "INSTITUTIONAL")

            # 1. Idempotency: check if we already have an active trade for this symbol
            existing = self.get_trade_by_symbol(symbol)
            if existing and existing.is_active:
                if self.engine:
                    self.engine.log_execution(
                        f"[COORDINATOR] Blocked duplicate open for {symbol} "
                        f"(active trade: {existing.trade_id})", "WARN"
                    )
                return None

            # 2. Check capacity
            if risk_checker and not risk_checker(symbol, asset_class):
                return None

            # 3. Create Trade entity FIRST — its trade_id is the deterministic
            #    intent identity that also derives the clientOrderId.
            trade = Trade(
                symbol=symbol,
                side=side,
                asset_class=asset_class,
                trade_style=TradeStyle(trade_type.upper()) if trade_type.upper() in [s.value for s in TradeStyle] else TradeStyle.INSTITUTIONAL,
                entry_price=float(candidate.get("price", 0) or 0),
                entry_atr=float(candidate.get("atr", 0) or 0),
                entry_score=float(candidate.get("score", 0) or 0),
                entry_reason=candidate.get("reason", ""),
                synthetic_sl=float(candidate.get("sl", 0) or 0),
                tp1_price=float(candidate.get("tp1", 0) or 0),
                tp2_price=float(candidate.get("tp2", 0) or 0),
                trade_type=trade_type,
                classification=candidate.get("classification", "SNIPER"),
            )
            # Set initial protection floor
            trade.protection_floor_sl = trade.synthetic_sl

            # 4. Deterministic client order ID bound to the trade_id.
            client_oid = _generate_client_order_id(symbol, side, trade.trade_id, "ENTRY")
            trade.client_order_id = client_oid

            # 5. Register intent
            trade.status = TradeStatus.PENDING
            self._pending_intents[client_oid] = trade

            # 6. Execute via engine
            if self.engine:
                self.engine.log_execution(
                    f"[COORDINATOR] Opening {symbol} {side} | "
                    f"client_oid={client_oid} | trade_id={trade.trade_id}",
                    "INFO",
                )
            try:
                result = engine_execute(
                    side, symbol, float(candidate.get("price", 0) or 0),
                    float(candidate.get("sl", 0) or 0),
                    float(candidate.get("tp1", 0) or 0),
                    float(candidate.get("tp2", 0) or 0),
                    float(candidate.get("score", 0) or 0),
                    f"COORDINATOR:{trade.trade_id}",
                    float(candidate.get("atr", 0) or 0),
                    trade_type,
                    "COORDINATOR",
                    candidate.get("classification", "SNIPER"),
                )
            except Exception as exc:
                if self.engine:
                    self.engine.log_execution(
                        f"[COORDINATOR] Execute failed for {symbol}: {exc}", "ERROR"
                    )
                trade.status = TradeStatus.FAILED
                self._pending_intents.pop(client_oid, None)
                return None

            if not result:
                trade.status = TradeStatus.FAILED
                self._pending_intents.pop(client_oid, None)
                return None

            # 7. Success: read the engine STATE to populate the trade
            if self.engine and hasattr(self.engine, "STATE"):
                state = self.engine.STATE
                if state.get("open"):
                    trade.entry_price = float(state.get("entry", 0) or trade.entry_price)
                    trade.entry_time = float(state.get("entry_time", 0) or time.time())
                    trade.original_qty = float(state.get("qty", 0) or 0)
                    trade.remaining_qty = float(state.get("remaining_qty", 0) or trade.original_qty)
                    trade.margin = float(state.get("margin", 0) or 0)
                    trade.mark_price = trade.entry_price
                    # Read back SL/TP from engine (may have been adjusted)
                    trade.synthetic_sl = float(state.get("synthetic_sl", 0) or trade.synthetic_sl)
                    trade.tp1_price = float(state.get("synthetic_tp1", 0) or trade.tp1_price)
                    trade.tp2_price = float(state.get("tp2_price", 0) or trade.tp2_price)
                    # Capture trade_id from engine if it generated one (binds the
                    # Trade to journal records for restart recovery). The
                    # clientOrderId is NOT regenerated — it stays deterministic
                    # on the exchange side as the idempotency key.
                    engine_tid = state.get("trade_id")
                    if engine_tid:
                        trade.trade_id = engine_tid
                    # Update status
                    trade.status = TradeStatus.FILLED
                    trade.profit_stage = ProfitStage.OPENED

                    # 8. Register as active
                    self._active_trades[trade.trade_id] = trade
                    self._pending_intents.pop(client_oid, None)
                    self._trade_counter += 1

                    if self.engine:
                        self.engine.log_execution(
                            f"[COORDINATOR] Registered trade {trade.trade_id} "
                            f"for {symbol} | entry={trade.entry_price:.6f} "
                            f"qty={trade.original_qty:.4f}",
                            "SUCCESS",
                        )
                        board = self._board_logger()
                        if board:
                            votes = TradeCouncil(trade).board_votes(MarketSnapshot(
                                price=trade.entry_price or 0,
                                adx=float(candidate.get("adx", 0) or 0),
                                trend_strength=float(candidate.get("trend_strength", 0) or 0),
                            ))
                            ctx = _open_ctx(
                                getattr(self.engine, "STATE", None), trade, candidate,
                            )
                            board.log_open(trade, votes, extra={
                                "confidence": candidate.get("confidence"),
                                "classification": trade.classification,
                            }, ctx=ctx)
                            trade.board_decisions["entry_votes"] = [
                                {"name": v.name, "role": v.role, "vote": v.vote,
                                 "score": round(v.score, 1),
                                 "rationale": v.rationale} for v in votes
                            ]
                    return trade

            # Engine state not open — failure
            trade.status = TradeStatus.FAILED
            self._pending_intents.pop(client_oid, None)
            return None

    # ──────────────────────────────────────────────────────────────────────
    # CLOSE: Validate -> Execute -> Finalize -> Log
    # ──────────────────────────────────────────────────────────────────────

    def close_trade(self, trade_id: str, exit_reason: ExitReason,
                    detail: str = "", engine_close: Callable = None) -> bool:
        """Close a trade fully. Single close gate.

        Returns True on successful close.
        """
        with self._lock:
            trade = self._active_trades.get(trade_id)
            if not trade or not trade.is_active:
                return False
            if trade.status == TradeStatus.CLOSING:
                return False  # Already closing

            trade.status = TradeStatus.CLOSING
            if self.engine:
                self.engine.log_execution(
                    f"[COORDINATOR] Closing {trade.symbol} ({trade_id}) "
                    f"reason={exit_reason.value}", "INFO",
                )

            try:
                # Close via engine
                if engine_close:
                    success = engine_close()
                elif self.engine and hasattr(self.engine, "close_position_full"):
                    success = self.engine.close_position_full()
                else:
                    success = False

                if success:
                    self._finalize_trade(trade, exit_reason, detail)
                    return True
                else:
                    trade.status = TradeStatus.PARTIAL_CLOSE if trade.remaining_qty < trade.original_qty else TradeStatus.FILLED
                    if self.engine:
                        self.engine.log_execution(
                            f"[COORDINATOR] Close failed for {trade.symbol}", "WARN",
                        )
                    return False
            except Exception as exc:
                trade.status = TradeStatus.FILLED  # Revert to active
                if self.engine:
                    self.engine.log_execution(
                        f"[COORDINATOR] Close error for {trade.symbol}: {exc}", "ERROR",
                    )
                return False

    def partial_close_trade(self, trade_id: str, ratio: float,
                            exit_reason: ExitReason, detail: str = "",
                            engine_partial: Callable = None) -> bool:
        """Execute a partial close. Returns True on success."""
        with self._lock:
            trade = self._active_trades.get(trade_id)
            if not trade or not trade.is_active:
                return False
            if ratio <= 0 or ratio >= 1:
                return False

            if self.engine:
                self.engine.log_execution(
                    f"[COORDINATOR] Partial close {trade.symbol} ({trade_id}) "
                    f"ratio={ratio:.2f} reason={exit_reason.value}", "INFO",
                )

            try:
                # The engine's close_partial(ratio) derives the leg size from
                # the LIVE remaining size. Capture it before the call so the
                # bookkeeping leg matches exactly what the engine closed —
                # repeated partials must never over-subtract from a shrunk
                # remainder and accidentally finalize an open runner.
                remaining_before = trade.remaining_qty
                # Execute partial close via engine
                if engine_partial:
                    success = engine_partial(ratio)
                elif self.engine and hasattr(self.engine, "close_partial"):
                    success = self.engine.close_partial(ratio)
                else:
                    success = False

                if success:
                    close_qty = remaining_before * ratio
                    close_qty = max(0.0, min(close_qty, remaining_before))
                    close_price = trade.mark_price if trade.mark_price > 0 else trade.entry_price
                    # The engine already reduced the remaining size on the
                    # ledger; this leg is the bookkeeping mirror only.
                    # We still deduct here so the Trade is the single source
                    # of truth even with a non-mutating (fake) engine; the
                    # scoped engine read-back reconciles the true remainder
                    # on top in the real flow.
                    leg = trade.add_partial_leg(
                        close_qty, close_price, exit_reason.value,
                        adjust_remaining=False,
                    )
                    trade.remaining_qty = max(0.0, remaining_before - close_qty)
                    if self.engine:
                        self.engine.log_execution(
                            f"[COORDINATOR] Partial close {trade.symbol} "
                            f"leg={leg.leg_id} qty={close_qty:.4f} "
                            f"pnl={leg.realized_pnl_pct:.2f}%",
                            "SUCCESS",
                        )
                    # Check if fully closed after partial
                    if trade.remaining_qty <= 0:
                        self._finalize_trade(trade, exit_reason, detail)
                    return True
                return False
            except Exception as exc:
                if self.engine:
                    self.engine.log_execution(
                        f"[COORDINATOR] Partial close error: {exc}", "ERROR",
                    )
                return False

    def _finalize_trade(self, trade: Trade, exit_reason: ExitReason, detail: str = "") -> None:
        """Finalize a closed trade: compute result, log, archive."""
        # Compute result class
        total_pnl = trade.realized_pnl_pct
        if total_pnl > 0.01:
            result_class = "WIN"
        elif total_pnl < -0.01:
            result_class = "LOSS"
        else:
            result_class = "BREAKEVEN"

        trade.finalize(exit_reason, result_class, detail)

        # Archive to closure log
        self._closure_log.append({
            "trade_id": trade.trade_id,
            "symbol": trade.symbol,
            "side": trade.side,
            "asset_class": trade.asset_class,
            "entry_price": trade.entry_price,
            "exit_price": trade.mark_price,
            "realized_pnl_pct": trade.realized_pnl_pct,
            "realized_pnl_usdt": trade.realized_pnl_usdt,
            "result": result_class,
            "exit_reason": exit_reason.value,
            "duration_sec": trade.duration_sec,
            "partial_legs": len(trade.partial_legs),
            "closed_at": trade.close_time,
        })

        # Remove from active
        self._active_trades.pop(trade.trade_id, None)

        if self.engine:
            board = self._board_logger()
            if board:
                votes = self._votes_from_notes(trade.board_decisions)
                ctx = _board_ctx(trade)
                board.log_close(trade=trade, votes=votes, result=result_class,
                                ctx=ctx)
            self.engine.log_execution(
                f"[COORDINATOR] Finalized {trade.symbol} ({trade.trade_id}) "
                f"result={result_class} pnl={trade.realized_pnl_pct:.2f}% "
                f"duration={trade.duration_sec:.0f}s",
                "SUCCESS" if result_class == "WIN" else "INFO",
            )

    # ──────────────────────────────────────────────────────────────────────
    # MANAGEMENT: Council-driven per-trade management
    # ──────────────────────────────────────────────────────────────────────

    def manage_trade(self, trade_id: str, market: MarketSnapshot,
                     engine_sync: Callable = None,
                     engine_close: Callable = None,
                     engine_partial: Callable = None) -> Optional[TradeDecision]:
        """Run the trade council for a single trade and execute decisions.

        Returns the council decision for observability.
        """
        trade = self._active_trades.get(trade_id)
        if not trade or not trade.is_active:
            return None

        # Sync position from exchange
        if engine_sync:
            try:
                engine_sync(trade.symbol)
            except Exception:
                pass

        # Run council
        council = TradeCouncil(trade, self.engine)
        decision = council.evaluate(market)

        # Store decision on trade for observability
        trade.board_decisions = decision.board_notes

        # Live position board (rate-limited by the board logger)
        board = self._board_logger()
        if board:
            ctx = _board_ctx(trade, market)
            votes = self._votes_from_notes(decision.board_notes)
            board.log_status(trade, votes,
                             force=decision.action not in ("HOLD",),
                             ctx=ctx)
            adverse_exit = decision.action in ("FULL_CLOSE", "PARTIAL_CLOSE") and \
                decision.exit_reason in (
                    ExitReason.STOP_LOSS, ExitReason.BREAKEVEN,
                    ExitReason.TRAILING_STOP, ExitReason.TIMEOUT,
                    ExitReason.THESIS_FAILURE,
                )
            # Risk-curve alarm on any adverse exit posture (forced), or when
            # the position itself is collapsing under thesis/momentum pressure
            # (rate-limited so a steady downturn stays visible but not noisy).
            if adverse_exit:
                board.log_risk(
                    trade,
                    f"{decision.exit_reason.value} roe={trade.roe_pct:+.2f}%",
                    "WARNING", ctx=ctx, force=True,
                )
            elif _risk_collapsed(trade, ctx):
                board.log_risk(
                    trade,
                    f"risk curve collapse roe={trade.roe_pct:+.2f}% "
                    f"giveback={max(0.0, trade.peak_roe - trade.roe_pct):+.2f}%",
                    "WARNING", ctx=ctx, force=False,
                )

        # Execute decision
        if decision.action == "HOLD":
            return decision

        if decision.action == "PARTIAL_CLOSE":
            self.partial_close_trade(
                trade_id, decision.close_ratio,
                decision.exit_reason or ExitReason.PROFIT_LOCK,
                decision.reason,
                engine_partial,
            )
        elif decision.action == "FULL_CLOSE":
            self.close_trade(
                trade_id,
                decision.exit_reason or ExitReason.MANUAL,
                decision.reason,
                engine_close,
            )
        elif decision.action == "ADJUST_SL":
            # Update protection level on the trade
            if decision.new_sl > 0:
                trade.ratchet_sl(decision.new_sl)
                trade.version += 1

        return decision

    def manage_all(self, engine_sync: Callable = None,
                   engine_close: Callable = None,
                   engine_partial: Callable = None,
                   market_fetcher: Callable = None) -> Dict[str, TradeDecision]:
        """Manage all active trades. Returns decision per trade."""
        decisions = {}
        for trade_id in list(self._active_trades.keys()):
            trade = self._active_trades.get(trade_id)
            if not trade or not trade.is_active:
                continue
            try:
                # Fetch market data
                market = None
                if market_fetcher:
                    market = market_fetcher(trade.symbol)
                if market is None:
                    # Minimal market snapshot from trade state
                    market = MarketSnapshot(
                        price=trade.mark_price or trade.entry_price,
                    )
                decision = self.manage_trade(
                    trade_id, market, engine_sync, engine_close, engine_partial,
                )
                if decision:
                    decisions[trade_id] = decision
            except Exception as exc:
                if self.engine:
                    self.engine.log_execution(
                        f"[COORDINATOR] manage error {trade.symbol}: {exc}", "ERROR",
                    )
        return decisions

    # ──────────────────────────────────────────────────────────────────────
    # RECOVERY: Reconstruct trades from exchange + journal
    # ──────────────────────────────────────────────────────────────────────

    def recover_from_exchange(self, positions: List[dict],
                              trade_journal=None) -> List[Trade]:
        """Reconstruct Trade entities from exchange positions after restart.

        This replaces the old _restore_from_exchange_locked with proper
        state reconstruction from journal + exchange.
        """
        recovered = []
        for pos in positions:
            symbol = pos.get("symbol", "")
            if not symbol:
                continue
            # Check if already tracked
            if self.get_trade_by_symbol(symbol):
                continue

            # Try to recover trade_id from journal
            trade_id = ""
            if trade_journal:
                try:
                    trade_id = trade_journal.recover_trade_id(symbol) or ""
                except Exception:
                    pass

            # Create Trade from exchange position
            trade = Trade.from_exchange_position(pos, trade_id)

            # Reconstruct state from journal if available
            if trade_journal:
                self._reconstruct_from_journal(trade, trade_journal)

            # Compute SL/TP from current ATR if engine available
            if self.engine:
                self._recompute_levels(trade)

            trade.status = TradeStatus.RECOVERED
            trade.recovered = True
            trade.recovery_ts = time.time()
            trade.position_status = "RECOVERED"

            self._active_trades[trade.trade_id] = trade
            recovered.append(trade)

            if self.engine:
                self.engine.log_execution(
                    f"[COORDINATOR] Recovered {symbol} ({trade.trade_id}) "
                    f"entry={trade.entry_price:.6f} qty={trade.original_qty:.4f}",
                    "INFO",
                )

        return recovered

    def _reconstruct_from_journal(self, trade: Trade, trade_journal) -> None:
        """Reconstruct TP1 state, partial closes, protection from journal records."""
        try:
            records = trade_journal.reconstruct_trade_history(trade.symbol, trade.trade_id)
            if not records:
                return
            for rec in records:
                decision = rec.get("decision", "")
                meta = rec.get("metadata", {}) if isinstance(rec.get("metadata"), dict) else {}
                if decision == "TP1_EXECUTED":
                    trade.tp1_state = "EXECUTED"
                    trade.tp1_exec_price = float(meta.get("tp1_exec_price", 0) or 0)
                    trade.tp1_fill_qty = float(meta.get("tp1_fill_qty", 0) or 0)
                    trade.tp1_event_ts = float(rec.get("ts", 0) or 0)
                elif decision == "PARTIAL_CLOSE":
                    leg_data = meta.get("partial_close_leg", {})
                    if isinstance(leg_data, dict):
                        trade.partial_legs.append(
                            __import__("core.trade", fromlist=["PartialCloseLeg"]).PartialCloseLeg(
                                leg_id=leg_data.get("leg_id", len(trade.partial_legs) + 1),
                                qty=float(leg_data.get("qty", 0) or 0),
                                price=float(leg_data.get("price", 0) or 0),
                                realized_pnl_usdt=float(leg_data.get("realized_pnl_usdt", 0) or 0),
                                realized_pnl_pct=float(leg_data.get("realized_pnl_pct", 0) or 0),
                                timestamp=float(leg_data.get("timestamp", 0) or 0),
                                reason=leg_data.get("reason", ""),
                            )
                        )
                elif decision == "BREAKEVEN_RATCHET":
                    trade.protection_state = ProtectionState.BREAKEVEN
                    floor = float(meta.get("sl", 0) or 0)
                    if floor > 0:
                        trade.protection_floor_sl = floor
                elif decision == "PROFIT_LOCKED":
                    trade.protection_state = ProtectionState.PROFIT_LOCK
                elif decision == "TRAILING_ACTIVE":
                    trade.protection_state = ProtectionState.TRAILING
                    trail = float(meta.get("trail_stop", 0) or 0)
                    if trail > 0:
                        trade.trail_stop = trail
                elif decision == "PROTECTION_UPDATE":
                    floor = float(meta.get("protection_floor_sl", 0) or 0)
                    if floor > 0:
                        trade.protection_floor_sl = floor
        except Exception:
            pass

    def _recompute_levels(self, trade: Trade) -> None:
        """Recompute SL/TP from current ATR after restart recovery."""
        if not self.engine:
            return
        try:
            df = self.engine.get_ohlcv_safe(trade.symbol, 50)
            if df is not None and len(df) > 14:
                atr = float(self.engine.compute_atr(df).iloc[-1])
            else:
                atr = trade.entry_price * 0.02
            trade.entry_atr = atr
            sl, tp1, tp2 = self.engine.compute_sl_tp(
                trade.entry_price, trade.side, "REVERSAL", atr, df,
            )
            try:
                sl, tp1, tp2 = self.engine._enforce_sl_tp_geometry(
                    trade.side, trade.entry_price, sl, tp1, tp2, atr, symbol=trade.symbol,
                )
            except Exception:
                pass
            # Only update if protection not already active (don't regress)
            if trade.protection_state == ProtectionState.NONE:
                trade.synthetic_sl = sl
            if trade.tp1_price <= 0:
                trade.tp1_price = tp1
            if trade.tp2_price <= 0:
                trade.tp2_price = tp2
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────────
    # RECONCILIATION: Timeout handling with client_order_id
    # ──────────────────────────────────────────────────────────────────────

    def reconcile_pending(self, exchange_fetcher: Callable = None) -> List[Trade]:
        """Reconcile pending intents that may have timed out.

        Instead of blind retry, checks the exchange for the actual position
        using the client_order_id. This is the idempotency safety net.
        """
        reconciled = []
        stale = []
        for client_oid, trade in list(self._pending_intents.items()):
            if trade.status != TradeStatus.PENDING:
                continue
            age = time.time() - trade.created_at
            if age < 30:
                continue  # Too recent to reconcile

            # Check exchange for position
            if exchange_fetcher:
                try:
                    pos = exchange_fetcher(trade.symbol)
                    if pos and float(pos.get("contracts", 0) or 0) > 0:
                        # Position exists — fill the trade
                        trade.status = TradeStatus.FILLED
                        trade.entry_price = float(pos.get("entryPrice", 0) or trade.entry_price)
                        trade.original_qty = float(pos.get("contracts", 0) or 0)
                        trade.remaining_qty = trade.original_qty
                        trade.mark_price = float(pos.get("markPrice", 0) or trade.entry_price)
                        self._active_trades[trade.trade_id] = trade
                        reconciled.append(trade)
                        if self.engine:
                            self.engine.log_execution(
                                f"[COORDINATOR] Reconciled {trade.symbol} "
                                f"({trade.trade_id}) from exchange",
                                "INFO",
                            )
                    else:
                        # No position found — order failed
                        stale.append(client_oid)
                except Exception:
                    pass
            else:
                # No fetcher available — mark as stale after timeout
                if age > 60:
                    stale.append(client_oid)

        # Clean up stale intents
        for client_oid in stale:
            trade = self._pending_intents.pop(client_oid, None)
            if trade:
                trade.status = TradeStatus.FAILED
                if self.engine:
                    self.engine.log_execution(
                        f"[COORDINATOR] Stale intent {trade.symbol} "
                        f"({trade.trade_id}) cleaned up",
                        "WARN",
                    )

        return reconciled

    # ──────────────────────────────────────────────────────────────────────
    # SNAPSHOT: Export state for dashboard / risk
    # ──────────────────────────────────────────────────────────────────────

    def snapshot(self) -> List[dict]:
        """Export all active trades as canonical position payloads."""
        from portfolio.manager import canonical_position_payload
        result = []
        for trade in self._active_trades.values():
            if trade.is_active:
                state = trade.to_state_dict()
                result.append(canonical_position_payload(trade.symbol, state, trade.asset_class))
        return result

    def risk_snapshot(self) -> dict:
        """Export risk-relevant summary."""
        return {
            "active_trades": self.count_active(),
            "closure_count": len(self._closure_log),
            "pending_intents": len(self._pending_intents),
            "total_realized_pnl": sum(
                c.get("realized_pnl_usdt", 0) for c in self._closure_log
            ),
            "win_count": sum(1 for c in self._closure_log if c.get("result") == "WIN"),
            "loss_count": sum(1 for c in self._closure_log if c.get("result") == "LOSS"),
        }

    def get_closure_log_for_risk(self) -> List[Dict[str, Any]]:
        """Return the immutable closure log for risk engine consumption."""
        return list(self._closure_log)
