"""Runtime portfolio orchestration for multiple independent positions.

v2: Replaces the activate/deactivate state-swapping pattern with:
  - Trade entity as the single source of truth per position
  - TradeExecutionCoordinator as the single execution authority
  - TradeCouncil for per-trade management decisions
  - dict[trade_id, Trade] instead of dict[symbol, PositionContext]

Backward-compatible: same public API as v1 (open_candidate, manage_all,
close_symbol, snapshot, etc.) but internally uses the new architecture.
"""
from __future__ import annotations

import copy
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from portfolio.risk import PortfolioRiskGuard
from portfolio.coordinator import TradeExecutionCoordinator, MarketSnapshot
from portfolio.trade_board import _board_ctx
from core.trade import Trade, TradeStatus, ExitReason, ProfitStage, ProtectionState

try:
    from core import trade_journal as _tj
except Exception:
    _tj = None

try:
    from contextlib import nullcontext as _nullcontext
except Exception:  # pragma: no cover - py<3.7
    class _nullcontext(object):
        def __enter__(self):
            return None

        def __exit__(self, *exc):
            return False


def _engine_lifecycle_state():
    """Lazily import the engine's trade lifecycle enum (no circular import)."""
    try:
        from core import engine as _eng
        return getattr(_eng, "TradeLifecycleState", None)
    except Exception:  # pragma: no cover - defensive
        return None


# Live per-trade context keys the position/risk boards consume. Captured from
# the engine's SCOPED STATE at the deepest point of the brain pass so each
# trade board shows ITS OWN live signals, never a global/other symbol's.
_BOARD_CTX_KEYS = (
    "smart_money", "momentum_flow", "advisory_trend_health",
    "advisory_structure_aligned", "advisory_struct_shift",
    "continuation_probability", "continuation_pressure", "continuation_reasons",
    "thesis_failure_score", "drawdown_from_peak", "trade_state",
    "market_phase", "trade_style", "position_health", "position_action",
    "position_health_components", "position_trade_type", "position_asset_class",
    "tp1_hold_score", "exit_warning", "trade_board", "trade_intelligence",
    "entry_timing", "zone_behaviour", "leverage", "mode", "adx_live",
    "position_rsi", "position_macd_hist",
)


def _capture_board_data(state: dict) -> dict:
    """Float the scoped STATE's live signals onto the Trade for board rendering."""
    data = {}
    for k in _BOARD_CTX_KEYS:
        if k in state:
            v = state.get(k)
            data[k] = copy.deepcopy(v) if isinstance(v, (dict, list)) else v
    return data


# Live-manager sub-engines that keep cross-tick state. Because the engine runs a
# module-level LIVE manager SINGLETON across all scoped positions, these inner
# engines would otherwise carry one position's accumulation (profit-lock
# engagement, thesis/exhaustion memory, regime cache) into the next position's
# brain pass. They are re-seeded per TRADE INSTANCE (never between consecutive
# ticks of the same trade, so real continuous management keeps its memory).
_LIVE_MANAGER_SUBENGINES = {
    "continuation_pressure_engine": "ContinuationPressureEngine",
    "thesis_failure_engine": "ThesisFailureEngine",
    "confidence_engine": "ConfidenceEngine",
    "regime_classifier": "MarketRegimeClassifier",
    "position_profile": "DynamicPositionProfile",
    "brain": "InstitutionalTradeBrain",
    "health_engine": "PositionHealthScore",
}


def _reseed_live_manager_for_trade(lm, engine, trade):
    """Re-seed the live manager's inner engines on a NEW trade instance."""
    if lm is None or engine is None or trade is None:
        return
    if getattr(lm, "_portfolio_scoped_trade", None) is trade:
        return
    for attr, clsname in _LIVE_MANAGER_SUBENGINES.items():
        if clsname is None:
            continue
        cls = getattr(engine, clsname, None)
        if cls is None:
            continue
        try:
            setattr(lm, attr, cls())
        except Exception:  # pragma: no cover - defensive
            pass
    lm._portfolio_scoped_trade = trade
    # Drop per-symbol live-bar extreme caches so a NEW trade instance never
    # inherits another trade's synthetic high/low (same-symbol frame replay
    # would otherwise collide on timestamp buckets).
    reset = getattr(engine, "reset_live_hybrid_cache", None)
    if callable(reset):
        try:
            reset(trade.symbol)
        except Exception:  # pragma: no cover - defensive
            pass


def _resolve_exit_reason(reason: str) -> ExitReason:
    """Map an engine close_reason string to the canonical ExitReason."""
    key = str(reason or "").upper()
    mapping = {
        "TP1": ExitReason.TP1,
        "TAKE_PROFIT": ExitReason.TP1,
        "TAKE_PROFIT_TP1": ExitReason.TP1,
        "TP2": ExitReason.TP2,
        "TAKE_PROFIT_TP2": ExitReason.TP2,
        "STOP_LOSS": ExitReason.STOP_LOSS,
        "SYNTHETIC_SL": ExitReason.STOP_LOSS,
        "BREAKEVEN": ExitReason.BREAKEVEN,
        "TRAILING_STOP": ExitReason.TRAILING_STOP,
        "PROFIT_LOCK": ExitReason.PROFIT_LOCK,
        "HARD_EXIT": ExitReason.PROFIT_LOCK,
        "THESIS_FAILURE": ExitReason.THESIS_FAILURE,
        "REVERSAL": ExitReason.REVERSAL,
        "SCALP_TARGET": ExitReason.SCALP_TARGET,
        "KILL_SWITCH": ExitReason.KILL_SWITCH,
        "TIMEOUT": ExitReason.TIMEOUT,
    }
    if key in mapping:
        return mapping[key]
    if "PROFIT" in key or "TP1" in key:
        return ExitReason.PROFIT_LOCK
    if "TP2" in key:
        return ExitReason.TP2
    if "STOP" in key or "SL" in key:
        return ExitReason.STOP_LOSS
    return ExitReason.EXTERNAL


def _mirror_partial_legs(legs) -> list:
    """Convert engine/coordinator partial-leg dicts to PartialCloseLeg."""
    try:
        from core.trade import PartialCloseLeg

        mirrored = []
        for i, leg in enumerate(legs or [], start=1):
            if not isinstance(leg, dict):
                continue
            qty = float(leg.get("qty", 0) or 0)
            price = float(leg.get("price", 0) or 0)
            pnl_usdt = float(leg.get("realized_pnl_usdt", 0) or
                             leg.get("pnl_usdt", 0) or 0)
            pnl_pct = float(leg.get("realized_pnl_pct", 0) or
                            leg.get("pnl_pct", 0) or 0)
            ts = float(leg.get("timestamp", 0) or leg.get("ts", 0) or 0)
            reason = str(leg.get("reason", "") or leg.get("mode", "") or "")
            mirrored.append(PartialCloseLeg(
                leg_id=int(leg.get("leg_id", 0) or i),
                qty=qty,
                price=price,
                realized_pnl_usdt=pnl_usdt,
                realized_pnl_pct=pnl_pct,
                timestamp=ts,
                reason=str(reason),
            ))
        return mirrored
    except Exception:
        return []


class PortfolioManager:
    def __init__(self, max_positions: int = 6, engine=None):
        self.max_positions = max(1, int(max_positions))
        self.engine = engine
        self._lock = threading.RLock()

        # v2: Trade entity storage (replaces PositionContext + activate/deactivate)
        self._trades: Dict[str, Trade] = {}  # trade_id -> Trade

        # Coordinator: single execution authority
        self.coordinator = TradeExecutionCoordinator(engine)

        # Risk guard: reads from coordinator's closure log
        self.risk_guard = PortfolioRiskGuard(engine, self.coordinator)

        # Legacy compatibility: expose contexts dict for allocator / dashboard
        # as a LIVE facade over the Trade entities (see PositionContext).
        self.contexts: Dict[str, PositionContext] = {}

        # Legacy compatibility fields
        self.active_symbol: Optional[str] = None
        self._last_perf_trade_count = 0

        if engine is not None:
            self.bind(engine)

    def bind(self, engine):
        self.engine = engine
        self.coordinator.engine = engine
        self.risk_guard.engine = engine
        self.risk_guard.coordinator = self.coordinator

    def _trade_lock(self):
        """Engine-wide RLock is the single cross-thread authority."""
        eng = self.engine
        if eng is not None and hasattr(eng, "_TRADE_LOCK"):
            return eng._TRADE_LOCK
        return self._lock

    def count(self) -> int:
        # Contexts mirror active trades (synced on open/close/cleanup), so the
        # dashboard/allocator/tests all share one membership view.
        return sum(1 for ctx in self.contexts.values()
                   if isinstance(ctx.state, dict) and ctx.state.get("open"))

    def symbols(self) -> List[str]:
        return [ctx.symbol for ctx in self.contexts.values()
                if isinstance(ctx.state, dict) and ctx.state.get("open") and ctx.symbol]

    def active_trade_count(self) -> int:
        return self.coordinator.count_active()

    @staticmethod
    def _asset_class(symbol: str, explicit: str | None = None) -> str:
        if explicit:
            return str(explicit).upper()
        text = str(symbol or "").upper()
        if any(x in text for x in ("XAU", "GOLD")):
            return "GOLD"
        if any(x in text for x in ("WTI", "BRENT", "OIL", "CRUDE")):
            return "OIL"
        if any(x in text for x in ("SP500", "US500", "NASDAQ", "USTECH", "US30", "DAX", "FTSE", "CAC", "NIKKEI", "INDEX")):
            return "INDEX"
        stock_hints = {"AAPL", "AMZN", "GOOGL", "MSFT", "NVDA", "META", "TSLA", "JPM", "ARM", "INTC", "CRCL", "COIN", "PLTR"}
        base = text.replace(":USDT", "").replace("/USDT", "").replace("-USDT", "")
        if base in stock_hints:
            return "STOCK"
        return "CRYPTO"

    @staticmethod
    def _class_cap(cls: str) -> int:
        from portfolio.allocator import class_cap_from_env
        return class_cap_from_env(str(cls).upper())

    def _ctx_class(self, trade_or_ctx) -> str:
        """Class of an open trade or legacy context."""
        if isinstance(trade_or_ctx, Trade):
            return trade_or_ctx.asset_class or self._asset_class(trade_or_ctx.symbol)
        stored = getattr(trade_or_ctx, "asset_class", None)
        return stored if stored else self._asset_class(getattr(trade_or_ctx, "symbol", ""))

    def can_open(self, symbol: str, asset_class: str | None = None) -> bool:
        with self._trade_lock():
            # Check if symbol already has an active trade
            for trade in self._trades.values():
                if trade.symbol == symbol and trade.is_active:
                    return False
            # Check position count
            if self.count() >= self.max_positions:
                return False
            # Risk guard
            if not self.risk_guard.can_open(symbol, self.count()):
                return False
            # Class cap
            cls = self._asset_class(symbol, asset_class)
            current = sum(1 for t in self._trades.values()
                         if t.is_active and self._ctx_class(t) == cls)
            return current < self._class_cap(cls)

    def open_candidate(self, candidate: dict) -> bool:
        """Open a trade via the coordinator. Single entry point."""
        with self._trade_lock():
            return self._open_candidate_impl(candidate)

    def _open_candidate_impl(self, candidate: dict) -> bool:
        symbol = candidate.get("symbol", "")
        if not self.can_open(symbol, candidate.get("asset_class")):
            return False

        # Build risk checker
        def risk_checker(sym, cls):
            return self.can_open(sym, cls)

        # Build engine execute callable
        def engine_execute(side, sym, price, sl, tp1, tp2, score, reason,
                          atr, trade_type, entry_type, classification):
            if self.engine:
                return self.engine.execute_entry(
                    side, sym, price, sl, tp1, tp2, score, reason,
                    atr, trade_type, entry_type, classification,
                )
            return False

        # Open via coordinator
        trade = self.coordinator.open_trade(candidate, risk_checker, engine_execute)

        if trade and trade.is_active:
            # Store in local trades dict
            self._trades[trade.trade_id] = trade
            # Sync legacy contexts dict for allocator compatibility
            self._sync_legacy_contexts()
            # Log
            trade_type = candidate.get("trade_type", "INSTITUTIONAL")
            if trade_type == "NEWS":
                self._log_news_open(symbol, candidate)
            elif self.engine:
                self.engine.log_execution(
                    f"[PORTFOLIO] Opened {symbol} {candidate.get('side', 'BUY')} | "
                    f"trade_id={trade.trade_id} | "
                    f"slot {self.count()}/{self.max_positions}",
                    "SUCCESS",
                )
            return True

        return False

    def _log_news_open(self, symbol: str, candidate: dict) -> None:
        side = str(candidate.get("side", "BUY"))
        try:
            if self.engine:
                self.engine.log_execution(
                    f"[NEWS] {symbol} TRADE_TYPE=NEWS REGIME=NEWS_DRIVEN "
                    f"SLOT=NEWS IMPACT={candidate.get('impact', 'MEDIUM')} "
                    f"DIRECTION={'LONG' if side == 'BUY' else 'SHORT'} "
                    f"trade_type=NEWS slot=NEWS "
                    f"impact={candidate.get('impact', 'MEDIUM')} "
                    f"direction={'LONG' if side == 'BUY' else 'SHORT'}",
                    "SUCCESS",
                )
        except Exception as exc:
            if self.engine:
                self.engine.log_execution(f"[NEWS] {symbol} open log error: {exc}", "WARN")

    def open_top(self, candidates: List[dict], slots: Optional[int] = None) -> int:
        opened = 0
        target = self.max_positions - self.count() if slots is None else min(
            int(slots), self.max_positions - self.count()
        )
        if target <= 0:
            return 0
        for candidate in candidates:
            if opened >= target:
                break
            if self.open_candidate(candidate):
                opened += 1
        return opened

    def manage_all(self):
        """Manage all active trades via the coordinator + trade councils."""
        if not self.engine:
            return

        for trade in list(self._trades.values()):
            if not trade.is_active:
                continue
            try:
                self._manage_one_trade(trade)
            except Exception as exc:
                if self.engine:
                    self.engine.log_execution(
                        f"[PORTFOLIO] manage {trade.symbol}: {exc}", "ERROR"
                    )
            finally:
                self.risk_guard.sync_closed_trades()
                self.engine.MEMORY["portfolio_risk"] = self.risk_guard.snapshot(self.count())

        # Clean up closed trades
        self._cleanup_closed()

        # v1 contract: when the book is FLAT after management the global legacy
        # state must truthfully reflect it (legacy consumers read STATE.open /
        # TRADE_STATE.in_position to drive scanners and dashboards).
        if not self._trades:
            self._flatten_engine_state()

    def _manage_one_trade(self, trade: Trade):
        """Manage a single trade through the REAL engine brain + council."""
        if not trade.is_active:
            return
        if self.engine is None:
            return

        # 1) Run the engine's real live-management brain scoped to THIS trade:
        #    profit-engine ladder, protection ratchet, trailing stop and strict
        #    closes all execute against the trade's own state (legacy engine
        #    manages one position via a scoped STATE; v1 did the same swap).
        closed = self._run_scoped_brain(trade)
        if closed:
            return
        if not trade.is_active:
            return

        # 2) Fetch market data for the council
        market = self._fetch_market_snapshot(trade.symbol)
        if market is None:
            return

        # 3) Run council and execute decisions on the TRADE's scope.
        def engine_close():
            return self._scoped_engine_call(
                trade, lambda: self.engine.close_position_full()
            )

        def engine_partial(ratio):
            return self._scoped_engine_call(
                trade, lambda: self.engine.close_partial(ratio)
            )

        decision = self.coordinator.manage_trade(
            trade.trade_id, market,
            engine_close=engine_close,
            engine_partial=engine_partial,
        )

        if decision and self.engine and decision.action != "HOLD":
            self.engine.log_execution(
                f"[COUNCIL] {trade.symbol} decision={decision.action} "
                f"reason={decision.reason}",
                "INFO",
            )

    def _run_scoped_brain(self, trade: Trade) -> bool:
        """Drive the engine's live brain for exactly one trade.

        Returns True when the brain itself (profit engine, protection lock,
        thesis failure, stopping levels) closed the position; in that case the
        trade is marked CLOSED and the council is skipped for this tick.
        """
        engine = self.engine
        lm = getattr(engine, "_live_manager", None)
        if lm is None or not hasattr(lm, "manage_live_trade"):
            try:
                self._sync_trade_from_engine(trade)
            except Exception:
                pass
            return not trade.is_active

        state_dict = trade.to_state_dict()
        state_dict["open"] = trade.remaining_qty > 0
        state_dict["current_symbol"] = trade.symbol
        original = copy.deepcopy(engine.STATE)
        lock = getattr(engine, "_TRADE_LOCK", None)

        try:
            with lock if lock is not None else _nullcontext():
                engine.STATE.clear()
                engine.STATE.update(state_dict)

                if hasattr(engine, "sync_position_state"):
                    try:
                        engine.sync_position_state(trade.symbol)
                    except Exception as exc:
                        if engine:
                            engine.log_execution(
                                f"[PORTFOLIO] sync {trade.symbol}: {exc}", "WARN",
                            )

                # Scope the legacy singleton lifecycle/cadence to THIS trade so
                # each position receives a full management pass every tick.
                try:
                    _reseed_live_manager_for_trade(lm, engine, trade)
                    _state = _engine_lifecycle_state()
                    if _state is not None:
                        lm.lifecycle_state = _state.LIVE
                    lm.last_management_ts = 0.0
                    lm.last_heavy_calc_ts = 0.0
                    lm.last_position_sync_ts = 0.0
                    lm.last_live_debug_ts = 0.0
                    lm.manage_live_trade()
                except Exception as exc:
                    if engine:
                        engine.log_execution(
                            f"[PORTFOLIO] brain {trade.symbol}: {exc}", "WARN",
                        )

                updated = engine.STATE
                trade.board_data = _capture_board_data(updated)
                self._read_back_state(trade, updated)

                if not updated.get("open"):
                    # The brain (or a protection/thesis/stop engine) closed it.
                    self._book_closed_from_scope(trade, updated)
                    return True
                return False
        finally:
            engine.STATE.clear()
            if original:
                engine.STATE.update(original)

    def _scoped_engine_call(self, trade: Trade, fn: Callable[[], Any]):
        """Execute an engine close/partial against the TRADE's own scope."""
        engine = self.engine
        if engine is None:
            return False

        state_dict = trade.to_state_dict()
        # The scope represents the POSITION, not the lifecycle tag: an explicit
        # close on a CLOSING-tagged trade must still build an open scope
        # (coordinator flips status to CLOSING before invoking engine_close).
        state_dict["open"] = trade.remaining_qty > 0
        state_dict["current_symbol"] = trade.symbol
        original = copy.deepcopy(engine.STATE)
        lock = getattr(engine, "_TRADE_LOCK", None)

        with lock if lock is not None else _nullcontext():
            try:
                engine.STATE.clear()
                engine.STATE.update(state_dict)
                if hasattr(engine, "sync_position_state"):
                    try:
                        engine.sync_position_state(trade.symbol)
                    except Exception:
                        pass
                result = fn()
                self._read_back_state(trade, engine.STATE, mirror_legs=False)
                if result and not engine.STATE.get("open") and trade.is_active:
                    self._book_closed_from_scope(trade, engine.STATE)
                return result
            finally:
                engine.STATE.clear()
                if original:
                    engine.STATE.update(original)

    def _read_back_state(self, trade: Trade, updated: dict,
                         mirror_legs: bool = True) -> None:
        """Persist the engine's scoped mutations back into the Trade entity.

        ``mirror_legs`` must be False for coordinator-driven partial closes
        (the coordinator records its own leg); True for brain-managed passes so
        engine partials are persisted into the Trade for correct final booking.
        """
        mark = float(updated.get("mark_price", 0) or 0)
        if mark > 0:
            trade.mark_price = mark
        trade.unrealized_pnl_usdt = float(updated.get("unrealized_pnl_usdt", 0) or 0)
        trade.roe_pct = float(updated.get("roe_pct", 0) or 0)
        if trade.roe_pct:
            trade.peak_roe = max(trade.peak_roe, trade.roe_pct)
        remaining = float(updated.get("remaining_qty", 0) or 0)
        if remaining > 0:
            trade.remaining_qty = remaining
        trade.margin = float(updated.get("margin", 0) or trade.margin)
        sl = float(updated.get("synthetic_sl", 0) or 0)
        if sl > 0:
            trade.synthetic_sl = sl
        ts = float(updated.get("trail_stop", 0) or 0)
        if ts > 0:
            trade.trail_stop = ts

        if updated.get("tp1_hit") or str(updated.get("tp1_state", "")).upper() == "EXECUTED":
            trade.tp1_state = "EXECUTED"
        if updated.get("tp2_hit") or str(updated.get("tp2_state", "")).upper() == "EXECUTED":
            trade.tp2_state = "EXECUTED"
        if updated.get("trail_activated"):
            trade.protection_state = ProtectionState.TRAILING
        # Mirror engine-booked partial legs into the Trade so a LATER full
        # close (scoped finalize) subtracts their realised value from the
        # session realised total instead of crediting it twice.
        if mirror_legs:
            legs = updated.get("partial_realized") or []
            if legs:
                mirrored = _mirror_partial_legs(legs)
                if mirrored:
                    trade.partial_legs = mirrored
                    try:
                        trade.realized_pnl_usdt = float(updated.get("realized_pnl_usdt", 0) or 0)
                        trade.realized_pnl_pct = float(updated.get("realized_pnl_pct", 0) or 0)
                    except (TypeError, ValueError):
                        pass

    def _book_closed_from_scope(self, trade: Trade, updated: dict) -> None:
        """Mark a trade closed when its scoped engine state flipped to open=False."""
        if trade.status == TradeStatus.CLOSED:
            return
        reason = str(
            updated.get("close_reason") or updated.get("exit_reason") or ""
        )
        trade.exit_reason = _resolve_exit_reason(reason)
        trade.exit_reason_detail = reason
        trade.status = TradeStatus.CLOSED
        trade.close_time = time.time()
        trade.remaining_qty = 0.0
        trade.unrealized_pnl_usdt = 0.0
        mirrored = _mirror_partial_legs(updated.get("partial_realized") or [])
        if mirrored:
            trade.partial_legs = mirrored
        if self.engine:
            self.engine.log_execution(
                f"[PORTFOLIO] {trade.symbol} closed by engine brain "
                f"(reason={reason or trade.exit_reason.name})",
                "INFO",
            )
        if trade.exit_reason == ExitReason.THESIS_FAILURE:
            self._emit_strict_close_board(trade)

    def _emit_strict_close_board(self, trade: Trade) -> None:
        """Emit the 🚨 STRICT CLOSE board when the engine BRAIN closed a trade
        on a failed thesis (an engine-direct path that skips the coordinator's
        close pipeline, so the close board must be pushed here too)."""
        bl = getattr(self.coordinator, "_board_logger", None)
        if bl is None:
            return
        board = bl()
        if board is None:
            return
        pnl = trade.realized_pnl_pct
        if pnl > 0.01:
            result = "WIN"
        elif pnl < -0.01:
            result = "LOSS"
        else:
            result = "BREAKEVEN"
        votes = []
        try:
            votes = self.coordinator._votes_from_notes(trade.board_decisions or {})
        except Exception:
            votes = []
        board.log_close(trade=trade, votes=votes, result=result,
                        strict=True, ctx=_board_ctx(trade))

    def _sync_trade_from_engine(self, trade: Trade) -> None:
        """Sync trade state from engine (for backward compat with engine.STATE)."""
        if not self.engine:
            return

        # Activate the trade's state in engine for sync
        state_dict = trade.to_state_dict()
        state_dict["open"] = trade.remaining_qty > 0
        original_state = copy.deepcopy(self.engine.STATE)

        try:
            self.engine.STATE.clear()
            self.engine.STATE.update(state_dict)

            if hasattr(self.engine, "sync_position_state"):
                self.engine.sync_position_state(trade.symbol)

            # Read back updated state
            updated = self.engine.STATE
            if updated.get("open"):
                trade.mark_price = float(updated.get("mark_price", 0) or trade.mark_price)
                trade.unrealized_pnl_usdt = float(updated.get("unrealized_pnl_usdt", 0) or 0)
                trade.roe_pct = float(updated.get("roe_pct", 0) or 0)
                trade.peak_roe = max(trade.peak_roe, trade.roe_pct)
                trade.remaining_qty = float(updated.get("remaining_qty", 0) or trade.remaining_qty)

                # Check for external close (position closed on exchange)
                if not updated.get("open") and trade.is_active:
                    trade.status = TradeStatus.CLOSED
                    trade.exit_reason = ExitReason.EXTERNAL
                    if self.engine:
                        self.engine.log_execution(
                            f"[PORTFOLIO] External close detected for {trade.symbol}",
                            "INFO",
                        )
            else:
                # Position closed externally
                if trade.is_active:
                    trade.status = TradeStatus.CLOSED
                    trade.exit_reason = ExitReason.EXTERNAL
        finally:
            # Restore original engine state
            self.engine.STATE.clear()
            if original_state:
                self.engine.STATE.update(original_state)

    def _fetch_market_snapshot(self, symbol: str) -> Optional[MarketSnapshot]:
        """Fetch live market data for council evaluation."""
        if not self.engine:
            return None
        try:
            price = self.engine.get_ticker_safe(symbol)
            if not price or price <= 0:
                return None

            market = MarketSnapshot(price=float(price))

            # Try to get OHLCV for advanced indicators
            df = self.engine.get_ohlcv_safe(symbol, 50)
            if df is not None and len(df) > 14:
                try:
                    market.atr = float(self.engine.compute_atr(df).iloc[-1])
                    if market.price > 0:
                        market.atr_pct = (market.atr / market.price * 100)
                except Exception:
                    pass
                try:
                    # RSI
                    if hasattr(self.engine, "compute_rsi"):
                        market.rsi = float(self.engine.compute_rsi(df).iloc[-1])
                except Exception:
                    pass
                try:
                    # ADX
                    if hasattr(self.engine, "compute_adx"):
                        market.adx = float(self.engine.compute_adx(df).iloc[-1])
                except Exception:
                    pass
                try:
                    # EMAs
                    if hasattr(self.engine, "compute_ema"):
                        market.ema_fast = float(self.engine.compute_ema(df, 9).iloc[-1])
                        market.ema_slow = float(self.engine.compute_ema(df, 21).iloc[-1])
                except Exception:
                    pass

            market.df = df
            return market
        except Exception:
            return None

    def _flatten_engine_state(self) -> None:
        """Mark the legacy global engine state as flat (no open position)."""
        engine = self.engine
        if engine is None:
            return
        try:
            if engine.STATE.get("open"):
                engine.STATE["open"] = False
            # v1 contract: a flat book must not leak stale position metrics.
            engine.STATE["remaining_qty"] = 0.0
            engine.STATE["qty"] = 0.0
            engine.STATE["unrealized_pnl_usdt"] = 0.0
            engine.STATE["roe_pct"] = 0.0
            engine.STATE.setdefault("close_reason",
                                    engine.STATE.get("close_reason") or "PORTFOLIO_FLAT")
            ts = getattr(engine, "TRADE_STATE", None)
            if ts is not None:
                ts["in_position"] = False
            lm = getattr(engine, "_live_manager", None)
            _state = _engine_lifecycle_state()
            if lm is not None and _state is not None and \
                    lm.lifecycle_state not in (_state.CLOSED, _state.IDLE):
                lm.lifecycle_state = _state.IDLE
        except Exception:
            pass

    def _cleanup_closed(self) -> None:
        """Remove closed trades from the active trades dict."""
        closed_ids = [
            tid for tid, trade in self._trades.items()
            if not trade.is_active
        ]
        for tid in closed_ids:
            self._trades.pop(tid, None)
        if closed_ids:
            self._sync_legacy_contexts()

    def _sync_legacy_contexts(self) -> None:
        """Sync the legacy contexts dict for allocator/dashboard compatibility.

        Each context is a LIVE facade over its Trade (state re-serializes on
        access), so no extra refresh is needed between syncs."""
        self.contexts.clear()
        for trade in self._trades.values():
            if trade.is_active:
                self.contexts[trade.symbol] = PositionContext(
                    trade, engine=self.engine,
                )

    def restore_from_exchange(self):
        """Reconstruct trades from exchange after restart."""
        if not self.engine:
            return
        with self._trade_lock():
            self._restore_from_exchange_impl()

    def _restore_from_exchange_impl(self):
        """Restore open positions from exchange using the coordinator."""
        try:
            positions = self.engine._exchange_sync.fetch_all_open_positions()
            if not positions:
                return

            recovered = self.coordinator.recover_from_exchange(
                positions, trade_journal=_tj,
            )

            for trade in recovered:
                self._trades[trade.trade_id] = trade

                # Journal the recovery
                if _tj:
                    try:
                        self.engine._journal_trade_event(
                            _tj.RESTART_RECOVERY,
                            symbol=trade.symbol,
                            side=trade.side,
                            trade_id=trade.trade_id,
                            reason=f"restart discovered open {trade.symbol}; "
                                   f"trade_id={'recovered' if trade.recovered else 'new'}",
                            state=trade.to_state_dict(),
                            level="WARN",
                            dedup_key=f"restart_recovery_{trade.symbol}",
                            dedup_sec=60,
                        )
                    except Exception:
                        pass

                # Restore native protective stop
                try:
                    self.engine.place_native_sl(trade.symbol)
                except Exception:
                    pass

                if self.engine:
                    self.engine.log_execution(
                        f"[RECOVERY] Restored position for {trade.symbol}", "INFO",
                    )

            self._sync_legacy_contexts()

        except Exception as e:
            if self.engine:
                self.engine.log_execution(f"[RECOVERY] Error: {e}", "ERROR")

    def risk_snapshot(self):
        return self.risk_guard.snapshot(self.count())

    def close_symbol(self, symbol: str) -> bool:
        """Close all trades for a symbol."""
        with self._trade_lock():
            for trade in list(self._trades.values()):
                if trade.symbol == symbol and trade.is_active:
                    def engine_close():
                        if self.engine and hasattr(self.engine, "close_position_full"):
                            return self._scoped_engine_call(
                                trade,
                                lambda: self.engine.close_position_full(),
                            )
                        return False

                    success = self.coordinator.close_trade(
                        trade.trade_id,
                        ExitReason.MANUAL,
                        "manual_close",
                        engine_close,
                    )
                    if success:
                        self._trades.pop(trade.trade_id, None)
                        self._sync_legacy_contexts()
                        return True
            return False

    def snapshot(self) -> List[dict]:
        """Export all active positions as canonical payloads.

        Reads the LIVE context facades (Trade-backed state re-serializes per
        access). Manually registered v1 contexts appear as well."""
        from portfolio.manager import canonical_position_payload
        result = []
        for ctx in self.contexts.values():
            symbol = getattr(ctx, "symbol", None)
            s = ctx.state if isinstance(ctx.state, dict) else {}
            if not s.get("open") or not symbol:
                continue
            asset_class = (getattr(ctx, "asset_class", None)
                           or self._asset_class(str(symbol)))
            result.append(canonical_position_payload(str(symbol), s, asset_class))
        return result

    # ──────────────────────────────────────────────────────────────────────
    # LEGACY COMPATIBILITY: activate/deactivate still available but no-ops
    # ──────────────────────────────────────────────────────────────────────

    def activate(self, symbol: Optional[str]):
        """Legacy compatibility: no-op in v2 (Trade owns its state)."""
        self.active_symbol = symbol

    def deactivate(self):
        """Legacy compatibility: no-op in v2."""
        self.active_symbol = None


class _TradeProfileFacade:
    """Read-only per-trade profile view used by the legacy isolation readers.

    v2 keeps the Trade entity as the single source of truth, so a context's
    classification must NOT depend on the engine's single legacy live-manager
    profile (which only models one active symbol). This facade reports THIS
    trade's own trade_type / classification regardless of which symbol the
    legacy manager currently profiles.
    """

    def __init__(self, trade: Optional[Trade]):
        self._trade = trade

    @property
    def trade_type(self) -> str:
        if self._trade is not None:
            return self._trade.trade_type or self._trade.classification or "INSTITUTIONAL"
        return "INSTITUTIONAL"

    @property
    def classification(self) -> str:
        if self._trade is not None:
            return self._trade.classification or self.trade_type
        return self.trade_type

    def update(self, *a, **k):
        """No-op: the Trade is authoritative; advisory taxonomy may not
        re-classify the trade behind the council's back."""
        return self

    def __getattr__(self, name):
        return None


class _ContextLiveManagerProxy:
    """Per-context view of the engine's legacy live manager.

    Attribute reads/writes (clock fields, cadence) forward to the real engine
    live manager so legacy bookkeeping works; `position_profile` is the
    per-TRADE facade so classification reads stay isolation-correct.
    """

    def __init__(self, ctx: "PositionContext"):
        self._ctx = ctx

    def _real(self):
        eng = self._ctx._engine
        return getattr(eng, "_live_manager", None) if eng is not None else self._ctx._provided_lm

    @property
    def position_profile(self):
        if self._ctx._trade is not None:
            return _TradeProfileFacade(self._ctx._trade)
        real = self._real()
        if real is not None:
            return getattr(real, "position_profile", None)
        return None

    def manage_live_trade(self, *a, **k):
        real = self._real()
        if real is not None and hasattr(real, "manage_live_trade"):
            return real.manage_live_trade(*a, **k)
        return None

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        real = self._real()
        if real is not None and hasattr(real, name):
            return getattr(real, name)
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    def __setattr__(self, name, value):
        if name in ("_ctx", "_provided_lm"):
            object.__setattr__(self, name, value)
            return
        real = self._real()
        if real is not None and hasattr(real, name):
            setattr(real, name, value)
            return
        object.__setattr__(self, name, value)


class PositionContext:
    """Portfolio position facade shared by allocator / dashboard / tests.

    Two construction styles are supported for backward compatibility:

      * v2: ``PositionContext(trade)`` — Trade entity is the single source
        of truth; ``.state`` re-serializes live on every access.
      * v1: ``PositionContext(symbol=..., state=..., trade_state=...,
        live_manager=..., opened_at=..., asset_class=...)`` — raw dict facade
        (dashboard/testing only; no Trade backing).

    The facade is read-only by construction: consumers never mutate ``.state``.
    """

    def __init__(self, trade: Optional[Trade] = None, *,
                 symbol: Optional[str] = None,
                 state: Optional[Dict[str, Any]] = None,
                 trade_state: Optional[Dict[str, Any]] = None,
                 live_manager=None,
                 opened_at: Optional[float] = None,
                 asset_class: Optional[str] = None,
                 engine=None):
        self._trade = trade
        self._engine = engine
        self._provided_lm = live_manager
        self.trade_state = dict(trade_state) if trade_state else {}
        if trade is not None:
            self.symbol = trade.symbol
            self.asset_class = trade.asset_class
            self.opened_at = trade.created_at
            self.client_order_id = trade.client_order_id
            self._raw_state = None
        else:
            self.symbol = symbol
            self.asset_class = str(asset_class).upper() if asset_class else \
                (PortfolioManager._asset_class(symbol) if symbol else None)
            self.opened_at = opened_at if opened_at is not None else time.time()
            self.client_order_id = None
            self._raw_state = dict(state) if state else {}

    @property
    def trade(self) -> Optional[Trade]:
        return self._trade

    @property
    def engine(self):
        return self._engine

    @property
    def state(self) -> Dict[str, Any]:
        """Live state dict. Trade-backed contexts re-serialize from the Trade
        on every access so dashboards/management always see current truth."""
        if self._trade is not None:
            return self._trade.to_state_dict()
        return dict(self._raw_state) if self._raw_state else {}

    @property
    def live_manager(self):
        return _ContextLiveManagerProxy(self)


# Legacy alias: earlier code (and validation tools) imported _LegacyContext.
_LegacyContext = PositionContext


def canonical_position_payload(symbol: str, s: dict, asset_class: Optional[str] = None):
    """P1 canonical portfolio-position payload. Unchanged from v1."""
    entry = float(s.get("entry", 0.0) or 0.0)
    tp1 = float(s.get("synthetic_tp1", 0.0) or 0.0)
    if tp1 <= 0:
        tp1 = float(s.get("dynamic_tp1", 0.0) or 0.0)
    if tp1 <= 0:
        tp1 = float(s.get("tp1_price", 0.0) or 0.0)
    tp2 = float(s.get("tp2_price", 0.0) or 0.0)
    if tp2 <= 0:
        tp2 = float(s.get("synthetic_tp2", 0.0) or 0.0)
    intel = s.get("trade_intelligence") if isinstance(s.get("trade_intelligence"), dict) else {}
    narrative = intel.get("narrative") or s.get("narrative_classification") or None
    session = s.get("market_session")
    if session is None:
        session = {}
    if isinstance(session, dict):
        label = s.get("session_label") or session.get("label") or session.get("current")
    else:
        label = s.get("session_label")
    return {
        "symbol": symbol,
        "asset_class": asset_class or PortfolioManager._asset_class(symbol),
        "side": s.get("side"),
        "entry": round(entry, 6),
        "mark_price": float(s.get("mark_price", 0.0) or 0.0),
        "current_price": float(s.get("mark_price", 0.0) or 0.0),
        "qty": float(s.get("qty", 0.0) or 0.0),
        "remaining_qty": float(s.get("remaining_qty", 0.0) or 0.0),
        "pnl": float(s.get("unrealized_pnl_usdt", 0.0) or 0.0),
        "roe": float(s.get("roe_pct", 0.0) or 0.0),
        "roe_pct": float(s.get("roe_pct", 0.0) or 0.0),
        "pnl_usdt": float(s.get("unrealized_pnl_usdt", 0.0) or 0.0),
        "sl": float(s.get("synthetic_sl", s.get("sl", 0.0)) or 0.0),
        "tp1": tp1,
        "tp2": tp2,
        "tp1_done": bool(s.get("tp1_hit", False)),
        "tp1_hit": bool(s.get("tp1_hit", False)),
        "tp2_hit": bool(s.get("tp2_hit", False)),
        "trailing_active": bool(s.get("trail_activated", False)),
        "trail_stop": float(s.get("trail_stop", 0.0) or 0.0),
        "trail_multiplier": float(s.get("smart_trail_mult", 1.5) or 1.5),
        "delay_tp1": bool(s.get("delay_tp1", False)),
        "location": s.get("location"),
        "zone": s.get("zone_info"),
        "zone_behaviour": s.get("zone_behaviour"),
        "narrative": narrative,
        "narrative_classification": s.get("narrative_classification"),
        "narrative_confidence": float(s.get("narrative_confidence", 0.0) or 0.0),
        "confidence": s.get("current_confidence"),
        "confidence_level": s.get("confidence_level"),
        "current_confidence": float(s.get("current_confidence", 0.0) or 0.0),
        "regime": s.get("market_regime"),
        "market_regime": s.get("market_regime"),
        "trade_state": s.get("trade_state"),
        "state": None,
        "market_phase": s.get("market_phase"),
        "entry_timing": s.get("entry_timing"),
        "classification": s.get("classification"),
        "trade_type": s.get("trade_type"),
        "entry_type": s.get("entry_type"),
        "trade_style": s.get("trade_style"),
        "score": s.get("trade_score", 0),
        "continuation_pressure": s.get("continuation_pressure", 50),
        "board": s.get("trade_board") if isinstance(s.get("trade_board"), dict) else None,
        "trade_board": s.get("trade_board") if isinstance(s.get("trade_board"), dict) else None,
        "market_session": session if isinstance(session, dict) else {},
        "session_label": label,
        "dynamic_tp1": float(s.get("dynamic_tp1", 0.0) or 0.0),
        "dynamic_tp2": float(s.get("dynamic_tp2", 0.0) or 0.0),
        "entry_atr": float(s.get("entry_atr", 0.0) or 0.0),
        "last_update_ts": s.get("last_update_ts") or s.get("entry_time"),
        "trade_id": s.get("trade_id"),
        "profit_stage": s.get("profit_stage"),
        "protection_state": s.get("protection_state"),
        "protection_floor_sl": float(s.get("protection_floor_sl", 0.0) or 0.0),
        "realized_pnl_usdt": float(s.get("realized_pnl_usdt", 0.0) or 0.0),
        "realized_pnl_pct": float(s.get("realized_pnl_pct", 0.0) or 0.0),
        "realized_roe_pct": float(s.get("realized_roe_pct", 0.0) or 0.0),
        "realized_legs": int(s.get("realized_legs", 0) or 0),
        "tp1_state": s.get("tp1_state"),
        "tp1_exec_price": float(s.get("tp1_exec_price", 0.0) or 0.0),
        "tp1_event_ts": s.get("tp1_event_ts"),
        "tp2_state": s.get("tp2_state"),
        "tp2_event_ts": s.get("tp2_event_ts"),
        "trail_activation_ts": s.get("trail_activation_ts"),
        "native_sl_state": s.get("native_sl_state"),
        "native_sl_price": float(s.get("native_sl_price", 0.0) or 0.0),
        "position_status": s.get("position_status"),
        "sync_status": s.get("sync_status"),
        "recovered": bool(s.get("recovered", False)),
        "exit_reason": s.get("exit_reason"),
        "final_result_class": s.get("final_result_class"),
        "last_trade_summary": (s.get("last_trade_summary")
                               if isinstance(s.get("last_trade_summary"), dict) else None),
    }
