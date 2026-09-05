from __future__ import annotations
from typing import Any, Dict
from .models import IntelligenceSignal, IntelligenceSnapshot

class IntelligenceFusion:
    """Evidence fusion. External data is advisory and never executes orders."""
    def __init__(self, min_score: float = 70.0):
        self.min_score = float(min_score)

    @staticmethod
    def _rvol(finviz: dict):
        return float(finviz.get("relative_volume") or 0.0)

    def fuse(self, ticker: str, finviz: dict, insider: dict, sec: dict) -> IntelligenceSnapshot:
        score = 0.0
        reasons = []
        signals = []
        evidence = 0

        rvol = self._rvol(finviz)
        if rvol >= 3:
            score += 28; evidence += 1; reasons.append(f"Relative Volume {rvol:.1f}x")
            signals.append(IntelligenceSignal("FINVIZ", "UNUSUAL_VOLUME", "BUY", 28, 0.9, details={"relative_volume": rvol}, source_url="https://finviz.com/"))
        elif rvol >= 2:
            score += 20; evidence += 1; reasons.append(f"Relative Volume {rvol:.1f}x")
            signals.append(IntelligenceSignal("FINVIZ", "HIGH_RELATIVE_VOLUME", "BUY", 20, 0.8, details={"relative_volume": rvol}, source_url="https://finviz.com/"))
        elif rvol >= 1.5:
            score += 10; evidence += 1; reasons.append(f"Relative Volume {rvol:.1f}x")

        ch = finviz.get("change")
        try: ch = float(ch)
        except Exception: ch = None
        if ch is not None and ch >= 5:
            score += 18; reasons.append(f"Price momentum +{ch:.1f}%")
        elif ch is not None and ch >= 2:
            score += 10; reasons.append(f"Price momentum +{ch:.1f}%")

        buys = int(insider.get("buy_count") or 0)
        sells = int(insider.get("sell_count") or 0)
        if buys:
            bonus = min(22.0, 8.0 + buys * 4.0)
            score += bonus; evidence += 1; reasons.append(f"Insider buying records: {buys}")
            signals.append(IntelligenceSignal("OPENINSIDER", "INSIDER_BUYING", "BUY", bonus, 0.8, details={"buy_count": buys, "sell_count": sells}, source_url="https://openinsider.com/"))
        if sells > buys * 2 and sells >= 3:
            penalty = min(18.0, 6.0 + sells * 2.0)
            score -= penalty; reasons.append(f"Heavy insider selling: {sells}")
            signals.append(IntelligenceSignal("OPENINSIDER", "INSIDER_SELLING_RISK", "SELL", -penalty, 0.75, details={"sell_count": sells}, source_url="https://openinsider.com/"))

        filings = sec.get("filings") or []
        recent_material = [x for x in filings if x.get("form") in {"8-K", "10-Q", "10-K", "13D", "13D/A", "13G", "13G/A"}]
        if recent_material:
            evidence += 1
            score += min(14.0, 6.0 + 2.0 * len(recent_material))
            reasons.append(f"Recent SEC filings: {len(recent_material)}")
            signals.append(IntelligenceSignal("SEC_EDGAR", "RECENT_FILING", "NEUTRAL", min(14.0, 6.0 + 2.0 * len(recent_material)), 0.95, details={"count": len(recent_material)}, source_url="https://www.sec.gov/edgar"))

        score = max(0.0, min(100.0, score))
        # Direction is only BUY when positive evidence + market confirmation exist.
        direction = "BUY" if score >= self.min_score and evidence >= 2 and (rvol >= 1.5 or (buys > 0 and ch is not None and ch > 0)) else "NEUTRAL"
        confidence = min(1.0, 0.35 + 0.15 * evidence + (0.15 if rvol >= 2 else 0.0))
        quality = "GOOD" if evidence >= 2 else ("PARTIAL" if evidence else "UNAVAILABLE")
        return IntelligenceSnapshot(ticker=ticker, score=score, direction=direction, confidence=confidence, data_quality=quality, finviz=finviz, insider=insider, sec=sec, signals=signals, reasons=reasons)
