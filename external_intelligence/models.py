from __future__ import annotations
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List

@dataclass
class IntelligenceSignal:
    provider: str
    signal: str
    direction: str = "NEUTRAL"
    score: float = 0.0
    confidence: float = 0.0
    observed_at: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)
    source_url: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["score"] = round(float(self.score), 2)
        d["confidence"] = round(float(self.confidence), 3)
        return d

@dataclass
class IntelligenceSnapshot:
    ticker: str
    score: float = 0.0
    direction: str = "NEUTRAL"
    confidence: float = 0.0
    data_quality: str = "UNAVAILABLE"
    finviz: Dict[str, Any] = field(default_factory=dict)
    insider: Dict[str, Any] = field(default_factory=dict)
    sec: Dict[str, Any] = field(default_factory=dict)
    signals: List[IntelligenceSignal] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "score": round(float(self.score), 1),
            "direction": self.direction,
            "confidence": round(float(self.confidence), 3),
            "data_quality": self.data_quality,
            "finviz": self.finviz,
            "insider": self.insider,
            "sec": self.sec,
            "signals": [s.to_dict() for s in self.signals],
            "reasons": list(self.reasons),
            "updated_at": self.updated_at,
        }
