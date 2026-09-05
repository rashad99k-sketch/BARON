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
        if symbol:
            return self.core.close_position_full()
        return self.core.close_position_full()
