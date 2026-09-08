"""Global portfolio allocation, concentration, and capital-rotation logic.

This module answers "Where is the best institutional opportunity across the
whole market right now?" without fabricating signals. It reads the production
queue/watchlist structures and imposes three portfolio-level gates on top of
the existing institutional gates:

1. Asset-class exposure caps (per-class instrument count).
2. Directional exposure caps (BUY/SELL count).
3. A Global Allocation Rank (queue priority minus concentration penalty).

Unused slots are NEVER implicitly filled — the engine deliberately returns a
reason string for each unused slot so the dashboard can explain them.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence
import os
import threading
import time

from portfolio.manager import PortfolioManager


# Class caps are expressed in counts (not score) to prevent a single class
# from consuming all open positions, while remaining intentionally loose.
#
# 6-slot technical distribution: 2 Crypto + 2 Index + 1 Gold + 1 Oil.
# The NEWS cap is fully independent: a news-driven position never consumes a
# technical class cap, and total open positions are still bounded by
# MAX_OPEN_POSITIONS (default 6).
# Asset classes outside this market model (e.g. STOCK) have no slot: they are
# discovered and analyzed, but never consume a technical portfolio slot.
DEFAULT_CLASS_CAPS = {
    "CRYPTO": 2,
    "INDEX": 2,
    "GOLD": 1,
    "OIL": 1,
    "NEWS": 1,
}


def class_cap_from_env(cls: str, default: Optional[int] = None) -> int:
    """Effective per-class capacity after environment overrides (ONE rule).

    Precedence: ``MAX_<CLASS>_POSITIONS`` (per class) >
    ``MAX_POSITIONS_PER_ASSET_CLASS`` (applied to EVERY class) >
    ``DEFAULT_CLASS_CAPS`` / the provided default.

    The dashboard, the open authority (``PortfolioManager._class_cap``) and the
    allocator all read this single rule, so reports and real opens always agree —
    even when the environment is tuned at runtime (tests configure it per-case).
    """
    name = str(cls).strip().upper()
    specific = os.getenv(f"MAX_{name}_POSITIONS", "").strip()
    if specific:
        return max(1, int(specific))
    master = os.getenv("MAX_POSITIONS_PER_ASSET_CLASS", "").strip()
    if master:
        return max(1, int(master))
    if default is not None:
        return int(default)
    return int(DEFAULT_CLASS_CAPS.get(name, 0))


def effective_class_caps() -> Dict[str, int]:
    """Live snapshot of the per-class caps honoring the env overrides."""
    return {cls: class_cap_from_env(cls, default)
            for cls, default in DEFAULT_CLASS_CAPS.items()}


@dataclass
class AllocationDecision:
    symbol: str
    side: str
    asset_class: str
    score: float
    allowed: bool
    reason: str
    penalty: float


@dataclass
class PortfolioAllocationReport:
    decisions: List[AllocationDecision]
    class_bias: Dict[str, int]
    direction_bias: Dict[str, int]
    concentration: str
    unused_slots: int
    slot_reason: str
    rotation_regime: str

    def to_dict(self) -> dict:
        return {
            "decisions": [
                {
                    "symbol": d.symbol,
                    "side": d.side,
                    "asset_class": d.asset_class,
                    "score": round(d.score, 3),
                    "allowed": d.allowed,
                    "reason": d.reason,
                    "penalty": round(d.penalty, 3),
                } for d in self.decisions
            ],
            "class_bias": self.class_bias,
            "direction_bias": self.direction_bias,
            "concentration": self.concentration,
            "unused_slots": self.unused_slots,
            "slot_reason": self.slot_reason,
            "rotation_regime": self.rotation_regime,
        }


class GlobalAssetAllocator:
    """Deterministic allocator for the queue candidates + current positions."""

    CLASS_CAPS = dict(DEFAULT_CLASS_CAPS)
    SIDE_CAPS = {"BUY": 4, "SELL": 4}

    def __init__(self, manager, engine):
        self.manager = manager
        self.engine = engine
        self._lock = threading.RLock()

    # ---------- helpers ----------
    @staticmethod
    def _classify(symbol: str, explicit: Optional[str] = None) -> str:
        return PortfolioManager._asset_class(symbol, explicit)

    # ---------- allocation ----------
    def allocate(self, candidates: Sequence[dict], limit: int = 6) -> PortfolioAllocationReport:
        """Filter candidates the risk manager/gates accept and rank them."""
        with self._lock:
            current = self.manager.count()
            list_cands = [c for c in candidates if isinstance(c, dict)]
            list_cands.sort(key=lambda c: float(c.get("priority_score", c.get("zone_score", 0.0))), reverse=True)
            decisions: List[AllocationDecision] = []
            class_bias: Dict[str, int] = {}
            direction_bias: Dict[str, int] = {}
            # Existing open positions occupy their own bias counts.
            for pos in self.manager.contexts.values():
                stored = getattr(pos, "asset_class", None)
                cls = self._classify(pos.symbol, stored)
                side = (pos.state.get("side") or "BUY").upper()
                class_bias[cls] = class_bias.get(cls, 0) + 1
                direction_bias[side] = direction_bias.get(side, 0) + 1

            chosen = 0
            caps = effective_class_caps()
            for cand in list_cands:
                sym = str(cand.get("symbol", ""))
                side = str(cand.get("side", "")).upper()
                cls = self._classify(sym, cand.get("asset_class"))
                score = float(cand.get("priority_score", cand.get("zone_score", 0.0)))
                allowed = True
                reason = "OK"
                if len(decisions) + current >= limit:
                    allowed = False
                    reason = "SLOT_CAP"
                class_cap = caps.get(cls, 0)
                side_cap = self.SIDE_CAPS.get(side, 0)
                if class_bias.get(cls, 0) >= class_cap:
                    allowed = False
                    reason = f"{cls}_CAP"
                if direction_bias.get(side, 0) >= side_cap:
                    allowed = False
                    reason = f"{side}_CAP"
                penalty = 0.0
                if allowed:
                    class_bias[cls] = class_bias.get(cls, 0) + 1
                    direction_bias[side] = direction_bias.get(side, 0) + 1
                    chosen += 1
                else:
                    penalty = 1.0
                decisions.append(
                    AllocationDecision(sym, side, cls, score, allowed, reason, penalty))
            # If any slot is unused, explain why.
            total = len([d for d in decisions if d.allowed])
            unused = limit - (current + total)
            if unused > 0:
                # Find the first non-OK reason among rejected candidates
                reasons = [d.reason for d in decisions if not d.allowed]
                slot_reason = (reasons[0] if reasons else
                              "no additional candidates passed institutional + liquidity + portfolio-risk gates.")
            else:
                slot_reason = "OK"
            max_class = max(class_bias.values()) if class_bias else 0
            concentration = (
                "HIGH" if class_bias and max_class >= max(caps.get(c, 6) for c in class_bias)
                else ("MEDIUM" if max_class >= 2 else "LOW"))
            return PortfolioAllocationReport(
                decisions=decisions, class_bias=class_bias, direction_bias=direction_bias,
                concentration=concentration, unused_slots=unused, slot_reason=slot_reason,
                rotation_regime=self._rotation(decisions))

    # ---------- rotation ----------
    def _rotation(self, decisions: List[AllocationDecision]) -> str:
        """Evidence-based rotation label from the classes actually chosen."""
        accepted = {d.asset_class for d in decisions if d.allowed}
        if not accepted:
            return "NO_FLOW"
        safe_haven = {"GOLD", "OIL", "ENERGY"}
        risk_on = {"CRYPTO"}
        if accepted & safe_haven and not (accepted & risk_on):
            return "RISK_OFF"
        if accepted & risk_on and not (accepted & safe_haven):
            return "RISK_ON"
        if len(accepted) > 1:
            return "MIXED"
        return "NEUTRAL"
