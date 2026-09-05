"""Strategy facade.

Keeps the proven RF/Institutional logic in the compatibility kernel while
exposing a stable, small API for future strategy extensions.
"""
from __future__ import annotations
from typing import Any, Dict

import core.engine as E


class StrategyEngine:
    """Stable strategy boundary around the preserved trading brain."""

    def analyze(self, symbol: str, side: str, df, orderbook=None) -> Dict[str, Any]:
        if df is None or len(df) < 30:
            return {"decision": "REJECT", "score": 0.0, "reason": "insufficient_data"}

        price = float(df["close"].iloc[-1])
        atr = float(E.compute_atr(df).iloc[-1]) if len(df) > 14 else price * 0.01
        narrative, narrative_score = E.evaluate_liquidity_narrative(df, orderbook, atr, side)
        smart = E.SmartMoneyEngine.analyze_smart_money(df)
        momentum = E.MomentumFlowEngine.analyze_momentum_flow(df)
        intent_score, intent_status, intent_details = E.InstitutionalIntentEngine.detect(
            df, orderbook, symbol
        )

        # Preserve the existing institutional sequence:
        # liquidity -> structure -> zone/retest -> volume/flow -> confirmation.
        score = float(narrative_score) + float(intent_score) / 20.0
        if smart.get("smart_money_dominant") and smart.get("institutional_bias") == side:
            score += 1.5
        if momentum.get("trend_expansion") and momentum.get("flow_bias") == side:
            score += 1.0
        score -= float(smart.get("distribution_risk", 0.0)) / 25.0
        score -= float(momentum.get("exhaustion_risk", 0.0)) / 30.0

        scenario = E.detect_scenario(df)
        return {
            "symbol": symbol,
            "side": side,
            "price": price,
            "atr": atr,
            "score": round(max(0.0, score), 3),
            "narrative_score": float(narrative_score),
            "intent_score": float(intent_score),
            "intent_status": intent_status,
            "intent_details": intent_details,
            "narrative": narrative,
            "smart_money": smart,
            "momentum": momentum,
            "scenario": scenario,
        }

    def entry_plan(self, analysis: Dict[str, Any]) -> Dict[str, Any]:
        side = analysis["side"]
        price = analysis["price"]
        atr = analysis["atr"]
        df = analysis["df"] if "df" in analysis else None
        if df is None:
            return {}
        sl, tp1, tp2 = E.compute_sl_tp(price, side, "REVERSAL", atr, df)
        return {"sl": sl, "tp1": tp1, "tp2": tp2}

    def execute(self, side: str, symbol: str, price: float, sl: float,
                tp1: float, tp2: float, score: float, reason: str,
                atr: float, trade_type: str = "INSTITUTIONAL",
                entry_type: str = "DEEP_SCANNER",
                classification: str = "SNIPER") -> bool:
        return bool(E.execute_entry(
            side, symbol, price, sl, tp1, tp2, score, reason, atr,
            trade_type, entry_type, classification
        ))
