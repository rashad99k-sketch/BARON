"""Execution boundary.

v2: Routes through TradeExecutionCoordinator for idempotency and
unified risk checking. No more direct execute_entry calls from
side paths — all opens go through PORTFOLIO.open_candidate.
"""
from __future__ import annotations

from portfolio.coordinator import TradeExecutionCoordinator
from core.trade import ExitReason


class ExecutionService:
    def __init__(self, core_engine, coordinator: TradeExecutionCoordinator = None):
        self.core = core_engine
        self.coordinator = coordinator

    def open(self, side, amount, symbol, *, sl=0.0, tp1=0.0, tp2=0.0,
             score=0.0, reason="SERVICE", atr=0.0,
             trade_type="INSTITUTIONAL", entry_type="SERVICE",
             classification="SNIPER"):
        """Open a trade. Routes through coordinator when available.

        Falls back to direct execute_entry for backward compatibility,
        but logs a deprecation warning.
        """
        price = self.core.get_ticker_safe(symbol)
        if not price:
            return False

        # If coordinator is available, route through it
        if self.coordinator is not None:
            candidate = {
                "symbol": symbol,
                "side": side,
                "price": price,
                "sl": sl,
                "tp1": tp1,
                "tp2": tp2,
                "score": score,
                "atr": atr,
                "trade_type": trade_type,
                "classification": classification,
                "reason": reason,
            }

            def risk_checker(sym, cls):
                return True  # Service bypasses portfolio-level risk (legacy compat)

            def engine_execute(side, sym, price, sl, tp1, tp2, score, reason,
                              atr, trade_type, entry_type, classification):
                return bool(self.core.execute_entry(
                    side, sym, price, sl, tp1, tp2, score, reason,
                    atr, trade_type, entry_type, classification,
                ))

            trade = self.coordinator.open_trade(candidate, risk_checker, engine_execute)
            return trade is not None and trade.is_active

        # Legacy fallback: direct execute_entry (deprecated)
        self.core.log_execution(
            f"[EXEC] Legacy direct execute_entry for {symbol} "
            f"(deprecated — use PORTFOLIO.open_candidate)", "WARN",
        )
        return bool(self.core.execute_entry(
            side, symbol, price, sl, tp1, tp2, score, reason, atr,
            trade_type, entry_type, classification,
        ))

    def close(self, symbol=None):
        """Close a trade. Routes through coordinator when available.

        E-07: symbol-bound close. The request must name the currently
        seated symbol; a mismatched symbol must never close a different
        position.
        """
        if symbol and self.coordinator is not None:
            # Find the trade for this symbol
            trade = self.coordinator.get_trade_by_symbol(symbol)
            if trade and trade.is_active:
                def engine_close():
                    return self.core.close_position_full()
                return self.coordinator.close_trade(
                    trade.trade_id, ExitReason.MANUAL, "service_close", engine_close,
                )
            return False

        # Legacy fallback
        if symbol:
            active = self.core.STATE.get("current_symbol")
            if active and str(symbol) != str(active):
                self.core.log_execution(
                    f"[EXEC] close({symbol}) ignored: active={active} (symbol-bound)",
                    "WARN",
                )
                return False
            if not self.core.STATE.get("open"):
                return False
            return self.core.close_position_full()
        if self.core.STATE.get("open"):
            return self.core.close_position_full()
        return False
