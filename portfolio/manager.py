"""Runtime portfolio orchestration for multiple independent positions."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Optional, Any, List
import copy
import os
import threading
import time

from portfolio.risk import PortfolioRiskGuard

try:
    from core import trade_journal as _tj
except Exception:
    _tj = None


@dataclass
class PositionContext:
    symbol: str
    state: dict
    trade_state: dict
    live_manager: Any
    paper_position: Any = None
    opened_at: float = 0.0
    client_order_id: Optional[str] = None
    asset_class: Optional[str] = None


class PortfolioManager:
    def __init__(self, max_positions: int = 6, engine=None):
        self.max_positions = max(1, int(max_positions))
        self.engine = engine
        self.contexts: Dict[str, PositionContext] = {}
        self.active_symbol: Optional[str] = None
        self._lock = threading.RLock()
        self._base_state = None
        self._base_trade_state = None
        self.risk_guard = PortfolioRiskGuard(engine)
        self._last_perf_trade_count = 0
        if engine is not None:
            self.bind(engine)

    def bind(self, engine):
        self.engine = engine
        self.risk_guard.engine = engine
        if self._base_state is None:
            self._base_state = copy.deepcopy(engine.STATE)
        if self._base_trade_state is None:
            self._base_trade_state = copy.deepcopy(engine.TRADE_STATE)

    def count(self) -> int:
        return len(self.contexts)

    def symbols(self):
        return list(self.contexts.keys())

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
        """Per-class slot capacity for the 6-market model.

        Source of truth is portfolio.allocator.DEFAULT_CLASS_CAPS, so the
        allocator report and the open authority always agree:
        CRYPTO:2 / INDEX:2 / GOLD:1 / OIL:1, plus the independent NEWS:1 slot.
        Classes outside the market model (STOCK, ...) get cap 0 -- discovered
        but never opened as a portfolio slot.
        The env master override MAX_POSITIONS_PER_ASSET_CLASS applies to EVERY
        class when explicitly set only (no 999 fake default).
        """
        env = os.getenv("MAX_POSITIONS_PER_ASSET_CLASS", "").strip()
        if env:
            return max(1, int(env))
        from portfolio.allocator import DEFAULT_CLASS_CAPS
        return int(DEFAULT_CLASS_CAPS.get(str(cls).upper(), 0))

    def _ctx_class(self, pos) -> str:
        """Class of an open context: prefer the EXPLICIT class stored at OPEN
        (e.g. the NEWS slot on a symbol whose name would classify as CRYPTO),
        falling back to symbol derivation for legacy contexts."""
        stored = getattr(pos, "asset_class", None)
        return stored if stored else self._asset_class(pos.symbol)

    def can_open(self, symbol: str, asset_class: str | None = None) -> bool:
        with self._lock:
            if symbol in self.contexts or len(self.contexts) >= self.max_positions:
                return False
            if not self.risk_guard.can_open(symbol, len(self.contexts)):
                return False
            cls = self._asset_class(symbol, asset_class)
            current = sum(1 for pos in self.contexts.values() if self._ctx_class(pos) == cls)
            return current < self._class_cap(cls)

    def _capture(self):
        if not self.engine or not self.active_symbol:
            return
        ctx = self.contexts.get(self.active_symbol)
        if ctx is None:
            return
        ctx.state = copy.deepcopy(self.engine.STATE)
        ctx.trade_state = copy.deepcopy(self.engine.TRADE_STATE)
        ctx.live_manager = self.engine._live_manager
        paper = getattr(self.engine, "paper", None)
        if isinstance(paper, dict):
            ctx.paper_position = copy.deepcopy(paper.get("position"))

    def _blank(self):
        if self._base_state is None:
            self._base_state = copy.deepcopy(getattr(self.engine, "STATE", {}) or {})
        if self._base_trade_state is None:
            self._base_trade_state = copy.deepcopy(getattr(self.engine, "TRADE_STATE", {}) or {})
        self.engine.STATE.clear()
        self.engine.STATE.update(copy.deepcopy(self._base_state or {}))
        self.engine.TRADE_STATE.clear()
        self.engine.TRADE_STATE.update(copy.deepcopy(self._base_trade_state or {}))
        paper = getattr(self.engine, "paper", None)
        if isinstance(paper, dict):
            paper["position"] = None

    def activate(self, symbol: Optional[str]):
        if not self.engine:
            raise RuntimeError("PortfolioManager is not bound to core.engine")
        with self._lock:
            self._capture()
            self.active_symbol = symbol
            if symbol is None:
                self._blank()
                return
            ctx = self.contexts.get(symbol)
            if ctx is None:
                self._blank()
                ctx_manager = self.engine.LiveTradeManager(
                    self.engine._event_bus,
                    self.engine._exchange_sync,
                    self.engine._recovery_guard,
                )
                self.engine._live_manager = ctx_manager
                return
            self.engine.STATE.clear()
            self.engine.STATE.update(copy.deepcopy(ctx.state))
            self.engine.TRADE_STATE.clear()
            self.engine.TRADE_STATE.update(copy.deepcopy(ctx.trade_state))
            self.engine._live_manager = ctx.live_manager
            paper = getattr(self.engine, "paper", None)
            if isinstance(paper, dict):
                paper["position"] = copy.deepcopy(ctx.paper_position)

    def deactivate(self):
        with self._lock:
            self._capture()
            self.active_symbol = None
            self._blank()

    def _store_after_open(self, symbol: str, manager, asset_class: Optional[str] = None):
        if asset_class is None:
            asset_class = self._asset_class(symbol)
        self.contexts[symbol] = PositionContext(
            symbol=symbol,
            state=copy.deepcopy(self.engine.STATE),
            trade_state=copy.deepcopy(self.engine.TRADE_STATE),
            live_manager=manager,
            paper_position=copy.deepcopy(
                self.engine.paper.get("position")
            ) if isinstance(getattr(self.engine, "paper", None), dict) else None,
            opened_at=time.time(),
            asset_class=asset_class,
        )

    def open_candidate(self, candidate: dict) -> bool:
        symbol = candidate["symbol"]
        if not self.can_open(symbol, candidate.get("asset_class")):
            return False
        self.activate(symbol)
        try:
            # Trade type / classification come from the source that produced the
            # candidate (e.g. the NEWS slot -> trade_type="NEWS"). Technical
            # candidates do not set these, so they keep the legacy defaults
            # (INSTITUTIONAL / SNIPER) exactly as before. This is the point that
            # preserves trade_type=NEWS from creation through management.
            cand_trade_type = candidate.get("trade_type") or "INSTITUTIONAL"
            cand_classification = candidate.get("classification") or "SNIPER"
            ok = bool(self.engine.execute_entry(
                candidate["side"], symbol, candidate["price"],
                candidate["sl"], candidate["tp1"], candidate["tp2"],
                candidate["score"],
                f"PORTFOLIO:{candidate.get('trade_id','UNKNOWN')}",
                candidate["atr"],
                cand_trade_type,
                "PORTFOLIO_MANAGER",
                cand_classification,
            ))
            if ok and self.engine.STATE.get("open"):
                self._store_after_open(symbol, self.engine._live_manager, candidate.get("asset_class"))
                if cand_trade_type == "NEWS":
                    self._log_news_open(symbol, candidate)
                else:
                    self.engine.log_execution(
                        f"[PORTFOLIO] Opened {symbol} {candidate['side']} | "
                        f"slot {len(self.contexts)}/{self.max_positions}",
                        "SUCCESS",
                    )
                return True
            return False
        finally:
            self.deactivate()

    def _log_news_open(self, symbol: str, candidate: dict) -> None:
        """Emit the dedicated, unambiguous NEWS open block so the runtime log
        shows the trade was created as NEWS (trade_type + independent slot +
        impact + direction) from the very first moment, before management."""
        side = str(candidate.get("side", "BUY"))
        try:
            # Keep both the machine-readable uppercase contract and the
            # human-readable NEWS block.  This is intentionally redundant:
            # downstream log parsers use the uppercase fields while operators
            # can still scan the compact NEWS line.
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
            if not self.can_open(candidate["symbol"], candidate.get("asset_class")):
                continue
            if self.open_candidate(candidate):
                opened += 1
        return opened

    def manage_all(self):
        if not self.engine:
            return
        for symbol in list(self.contexts.keys()):
            self.activate(symbol)
            try:
                if not self.engine.STATE.get("open"):
                    self.contexts.pop(symbol, None)
                    continue
                self.engine.sync_position_state(symbol)
                if self.engine.STATE.get("open"):
                    self.engine._live_manager.manage_live_trade()
                if self.engine.STATE.get("open"):
                    price = self.engine.get_ticker_safe(symbol)
                    if price:
                        df = self.engine.get_ohlcv_safe(symbol, 50)
                        if df is not None:
                            try:
                                if self.engine.council_exit(df, price):
                                    # council_exit may already have closed AND
                                    # finalized via close_position_full(); only
                                    # finalize again if the position is STILL
                                    # open, otherwise the same runner's PnL is
                                    # realized twice (double-counted loss/win).
                                    if self.engine.STATE.get("open"):
                                        self.engine.finalize_trade_with_reality(symbol)
                            except Exception as exc:
                                self.engine.log_execution(
                                    f"[PORTFOLIO] council_exit {symbol}: {exc}", "WARN"
                                )
                if not self.engine.STATE.get("open"):
                    self.contexts.pop(symbol, None)
                else:
                    self._capture()
            except Exception as exc:
                self.engine.log_execution(f"[PORTFOLIO] manage {symbol}: {exc}", "ERROR")
            finally:
                self.risk_guard.sync_closed_trades()
                self.engine.MEMORY["portfolio_risk"] = self.risk_guard.snapshot(self.count())
                self.deactivate()

    def restore_from_exchange(self):
        if not self.engine:
            return
        try:
            positions = self.engine._exchange_sync.fetch_all_open_positions()
            for pos in positions:
                symbol = pos.get('symbol')
                if not symbol or symbol in self.contexts:
                    continue
                # Create a new context from the position data
                self.activate(symbol)
                # The engine state is blank; set it from position data
                self.engine.STATE["open"] = True
                self.engine.STATE["side"] = pos.get('side', 'BUY')
                self.engine.STATE["entry"] = float(pos.get('entryPrice', 0))
                self.engine.STATE["qty"] = float(pos.get('contracts', 0))
                self.engine.STATE["remaining_qty"] = float(pos.get('contracts', 0))
                self.engine.STATE["mark_price"] = float(pos.get('markPrice', 0))
                self.engine.STATE["current_symbol"] = symbol
                self.engine.STATE["entry_time"] = time.time()
                # Recalculate SL/TP from current ATR (best effort)
                df = self.engine.get_ohlcv_safe(symbol, 50)
                if df is not None and len(df) > 14:
                    atr = self.engine.compute_atr(df).iloc[-1]
                else:
                    atr = self.engine.STATE["entry"] * 0.02
                self.engine.STATE["entry_atr"] = atr
                sl, tp1, tp2 = self.engine.compute_sl_tp(
                    self.engine.STATE["entry"],
                    self.engine.STATE["side"],
                    "REVERSAL",
                    atr,
                    df
                )
                # P1-3: enforce directional TP geometry BEFORE persisting so a
                # persisted stop/target never violates TP1 > entry > SL.
                try:
                    sl, tp1, tp2 = self.engine._enforce_sl_tp_geometry(
                        self.engine.STATE["side"], self.engine.STATE["entry"],
                        sl, tp1, tp2, atr, symbol=symbol)
                except Exception:
                    pass
                self.engine.STATE["synthetic_sl"] = sl
                self.engine.STATE["synthetic_tp1"] = tp1
                self.engine.STATE["tp2_price"] = tp2
                self.engine._live_manager = self.engine.LiveTradeManager(
                    self.engine._event_bus,
                    self.engine._exchange_sync,
                    self.engine._recovery_guard
                )
                self.engine._live_manager.set_entry_atr(atr)
                self._store_after_open(symbol, self.engine._live_manager)
                # ---- Trade Management Safety: journaled, idempotent recovery ----
                # Recover the pre-restart trade_id from the tamper-evident journal;
                # never claim a partial banked without a journaled TP1 record. No
                # realized PnL is fabricated here — the exchange simply reported an
                # open position and the runner re-arms protective levels only.
                _sid = str(self.engine.STATE.get("side", "BUY"))
                _recovered_id = None
                if _tj is not None:
                    try:
                        _recovered_id = _tj.recover_trade_id(symbol)
                    except Exception:
                        _recovered_id = None
                if _recovered_id:
                    self.engine.STATE["trade_id"] = _recovered_id
                else:
                    try:
                        self.engine.STATE["trade_id"] = self.engine._generate_trade_id(symbol)
                    except Exception:
                        pass
                self.engine.STATE["recovered"] = True
                self.engine.STATE["recovery_ts"] = time.time()
                self.engine.STATE["position_status"] = "RECOVERED"
                self.engine.STATE["close_reason"] = None
                try:
                    self.engine._journal_trade_event(
                        _tj.RESTART_RECOVERY, symbol=symbol, side=_sid,
                        reason=f"restart discovered open {symbol} position on venue; "
                               f"trade_id={'recovered' if _recovered_id else 'new'}",
                        state=dict(self.engine.STATE), level="WARN",
                        dedup_key=f"restart_recovery_{symbol}", dedup_sec=60,
                    )
                except Exception:
                    pass
                # Native protective stop is restored so a restart never leaves
                # the runner unprotected (P0-3).
                try:
                    self.engine.place_native_sl(symbol)
                except Exception:
                    pass
                try:
                    self.engine._journal_trade_event(
                        _tj.POSITION_RECOVERED, symbol=symbol, side=_sid,
                        reason=f"position {symbol} reactivated; protective stop restored at {sl:.4f}",
                        state=dict(self.engine.STATE), level="INFO",
                        dedup_key=f"position_recovered_{symbol}", dedup_sec=60,
                    )
                except Exception:
                    pass
                self.engine.log_execution(f"[RECOVERY] Restored position for {symbol}", "INFO")
                self.deactivate()
        except Exception as e:
            self.engine.log_execution(f"[RECOVERY] Error: {e}", "ERROR")

    def risk_snapshot(self):
        # Integration dependency: core/runtime health payload and the security
        # controls test consume the portfolio risk view through this method.
        return self.risk_guard.snapshot(self.count())

    def close_symbol(self, symbol: str) -> bool:
        if symbol not in self.contexts:
            return False
        self.activate(symbol)
        try:
            if self.engine.STATE.get("open"):
                self.engine.close_position_full()
            closed = not self.engine.STATE.get("open")
            if closed:
                self.contexts.pop(symbol, None)
            else:
                self._capture()
            return closed
        finally:
            self.deactivate()

    def snapshot(self):
        out = []
        for symbol in list(self.contexts.keys()):
            ctx = self.contexts[symbol]
            payload = canonical_position_payload(symbol, ctx.state, self._ctx_class(ctx))
            out.append(payload)
        return out


def canonical_position_payload(symbol: str, s: dict, asset_class: Optional[str] = None):
    """P1 canonical portfolio-position payload. Every documented key is always
    present; genuinely unknown values are None/0.0/False -- never the string
    'undefined' and never fake placeholders."""
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
        # ---- P1-4 / hardening: realized vs unrealized NEVER mixed, plus the
        # per-trade lifecycle identifiers surfaced on the canonical payload. ----
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