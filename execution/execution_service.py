"""Execution boundary.

All order placement still flows through the preserved core execution
semantics. Multi-position orchestration lives in portfolio.manager.
"""
from __future__ import annotations


class ExecutionService:
    def __init__(self, core_engine):
        self.core = core_engine

    def open(self, side, amount, symbol, *, sl=0.0, tp1=0.0, tp2=0.0,
             score=0.0, reason="SERVICE", atr=0.0,
             trade_type="INSTITUTIONAL", entry_type="SERVICE",
             classification="SNIPER"):
        price = self.core.get_ticker_safe(symbol)
        if not price:
            return False
        return bool(self.core.execute_entry(
            side, symbol, price, sl, tp1, tp2, score, reason, atr,
            trade_type, entry_type, classification
        ))

    def close(self, symbol=None):
        # E-07: symbol-bound close. The request must name the currently seated
        # symbol; a mismatched symbol must never close a different position.
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
