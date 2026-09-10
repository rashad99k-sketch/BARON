"""Trade Council — independent decision-making body for each trade.

Each open trade gets its own TradeCouncil instance. The council:
  1. Reads the trade's current state + live market data
  2. Evaluates trend strength, profit levels, scalping opportunities
  3. Issues binding decisions: hold, partial-close, full-close, adjust protection
  4. Never mutates the trade directly — returns a TradeDecision for the
     coordinator to execute atomically.

DESIGN:
  - Councils are stateless per call — they read Trade + market data and return decisions.
  - The trade's `board_decisions` dict stores the latest council verdict for observability.
  - Multiple councils can run in parallel (one per trade) without shared state.
  - All close/partial decisions go through the coordinator for idempotency.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from core.trade import (
    Trade, TradeStatus, ProfitStage, ProtectionState,
    TradeStyle, ExitReason,
)


@dataclass
class MarketSnapshot:
    """Live market data for council evaluation."""
    price: float
    bid: float = 0.0
    ask: float = 0.0
    spread_pct: float = 0.0
    atr: float = 0.0
    atr_pct: float = 0.0
    adx: float = 0.0
    rsi: float = 0.0
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_200: float = 0.0
    volume_ratio: float = 1.0
    momentum: float = 0.0
    trend_strength: float = 0.0  # -1.0 (strong downtrend) to 1.0 (strong uptrend)
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_width: float = 0.0
    vwap: float = 0.0
    funding_rate: float = 0.0
    open_interest_change: float = 0.0
    df: Any = None  # Raw OHLCV DataFrame for advanced analysis


@dataclass
class TradeDecision:
    """Binding decision from the council. Coordinator executes atomically."""
    action: str  # "HOLD", "PARTIAL_CLOSE", "FULL_CLOSE", "ADJUST_SL", "ADJUST_TRAIL"
    reason: str
    confidence: float = 0.0
    close_ratio: float = 0.0   # For PARTIAL_CLOSE: 0.0-1.0
    new_sl: float = 0.0        # For ADJUST_SL
    exit_reason: Optional[ExitReason] = None
    is_scalp_exit: bool = False
    is_trend_follow: bool = False
    board_notes: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CouncilMemberVote:
    """One named council member's verdict on the current trade snapshot.

    score is an ACTION URGENCY 0-100 (not a bullishness score): holding votes
    stay low (<50), exits scale toward 100. The decision cascade remains the
    authoritative executor; votes are the audit/observability layer that the
    trade board renders to the operator.
    """
    name: str          # e.g. "TrendRider"
    role: str          # e.g. "trend continuation"
    vote: str          # HOLD / PARTIAL_CLOSE / FULL_CLOSE / ADJUST_SL
    score: float       # 0-100 action urgency
    rationale: str     # one-line why


class TradeCouncil:
    """Independent trade management council.

    Each trade gets its own instance. The council evaluates the trade's
    health and issues decisions. It NEVER mutates the trade directly.
    """

    def __init__(self, trade: Trade, engine=None):
        self.trade = trade
        self.engine = engine
        self._config = self._load_config()

    def _load_config(self) -> dict:
        return {
            # Scalping detection
            "scalp_roi_threshold": float(os.getenv("SCALP_ROI_THRESHOLD", "0.3")),
            "scalp_time_limit_sec": float(os.getenv("SCALP_TIME_LIMIT_SEC", "300")),
            "scalp_rsi_overbought": float(os.getenv("SCALP_RSI_OVERBOUGHT", "75")),
            "scalp_rsi_oversold": float(os.getenv("SCALP_RSI_OVERSOLD", "25")),
            # Trend following
            "trend_adx_min": float(os.getenv("TREND_ADX_MIN", "25")),
            "trend_trail_atr_mult": float(os.getenv("TREND_TRAIL_ATR_MULT", "2.0")),
            "trend_tp1_atr_mult": float(os.getenv("TREND_TP1_ATR_MULT", "1.5")),
            "trend_tp2_atr_mult": float(os.getenv("TREND_TP2_ATR_MULT", "3.0")),
            # Profit taking
            "tp1_partial_ratio": float(os.getenv("TP1_PARTIAL_RATIO", "0.5")),
            "tp2_full_close": os.getenv("TP2_FULL_CLOSE", "True").lower() in ("true", "1", "yes"),
            # VPA profit defense: high-effort/law-result is NOT strength.
            "vpa_bank_roi_pct": float(os.getenv("VPA_BANK_ROI_PCT", "18.0")),
            "vpa_bank_ratio": float(os.getenv("VPA_BANK_RATIO", "0.4")),
            "vpa_strict_roe_ceiling": float(os.getenv("VPA_STRICT_ROE_CEILING", "0.5")),
            "breakeven_roi_pct": float(os.getenv("BREAKEVEN_ROI_PCT", "0.2")),
            "profit_lock_roi_pct": float(os.getenv("PROFIT_LOCK_ROI_PCT", "0.5")),
            # Aggressive closing
            "thesis_failure_threshold": float(os.getenv("THESIS_FAILURE_THRESHOLD", "-0.3")),
            "exhaustion_rsi_extreme": float(os.getenv("EXHAUSTION_RSI_EXTREME", "80")),
            "max_hold_time_sec": float(os.getenv("MAX_HOLD_TIME_SEC", "86400")),
            "reversal_exit_enabled": os.getenv("REVERSAL_EXIT_ENABLED", "True").lower() in ("true", "1"),
            # Hard limits
            "hard_stop_roe_pct": float(os.getenv("HARD_STOP_ROE_PCT", "-2.0")),
            "force_close_after_sec": float(os.getenv("FORCE_CLOSE_AFTER_SEC", "172800")),
        }

    def evaluate(self, market: MarketSnapshot) -> TradeDecision:
        """Main evaluation entry point. Returns a binding decision."""
        trade = self.trade

        if not trade.is_active:
            return TradeDecision(action="HOLD", reason="trade_not_active")

        # Update live market data on trade
        trade.mark_price = market.price
        self._compute_unrealized(market)

        # Advisory layer: run the five named council members (read-only). The
        # verdicts are stored for the operator board / audit; the strict
        # cascade below remains the binding executor.
        votes = self.board_votes(market)
        trade.board_decisions["council_votes"] = [
            {"name": v.name, "role": v.role, "vote": v.vote,
             "score": round(v.score, 1), "rationale": v.rationale}
            for v in votes
        ]

        # Priority-ordered decision cascade
        decision = self._check_hard_limits(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_scalp_exit(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_tp_execution(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_protection_update(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_vpa_profit_defense(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_trend_management(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_reversal_exit(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_thesis_failure(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_exhaustion(market)
        if decision:
            return self._attach_votes(decision, votes)

        decision = self._check_max_hold_time(market)
        if decision:
            return self._attach_votes(decision, votes)

        return self._attach_votes(self._hold(market), votes)

    def _attach_votes(self, decision: TradeDecision,
                      votes: List[CouncilMemberVote]) -> TradeDecision:
        """Attach the member verdicts to a decision for the board renderer."""
        decision.board_notes["council_votes"] = [
            {"name": v.name, "role": v.role, "vote": v.vote,
             "score": round(v.score, 1), "rationale": v.rationale}
            for v in votes
        ]
        return decision

    # ──────────────────────────────────────────────────────────────────────
    # ADVISORY named council members. Read-only: they never mutate the Trade.
    # Each returns a (vote, urgency 0-100, rationale) for the operator board.
    # ──────────────────────────────────────────────────────────────────────

    def _member_trend_rider(self, market: MarketSnapshot) -> CouncilMemberVote:
        """TrendRider — trend continuation lens."""
        trade = self.trade
        c = self._config
        strong_aligned = (abs(market.trend_strength) >= 0.6 and
                          market.adx >= c["trend_adx_min"])
        weak = abs(market.trend_strength) < 0.3
        if strong_aligned:
            return CouncilMemberVote(
                "TrendRider", "trend continuation", "HOLD", 25,
                f"trend aligned (strength={market.trend_strength:.2f} adx={market.adx:.0f})")
        if weak and trade.roe_pct > 0.5 and trade.remaining_ratio > 0.5:
            return CouncilMemberVote(
                "TrendRider", "trend continuation", "PARTIAL_CLOSE", 58,
                f"trend fading (strength={market.trend_strength:.2f}) bank {trade.roe_pct:.1f}%")
        if (market.ema_fast > 0 and market.ema_slow > 0 and trade.trade_style != TradeStyle.SCALP):
            against = (trade.side == "BUY" and market.ema_fast < market.ema_slow) or \
                      (trade.side == "SELL" and market.ema_fast > market.ema_slow)
            if against and market.adx > 20:
                return CouncilMemberVote(
                    "TrendRider", "trend continuation", "FULL_CLOSE", 72,
                    f"EMA cross against ({market.ema_fast:.1f}/{market.ema_slow:.1f})")
        return CouncilMemberVote(
            "TrendRider", "trend continuation", "HOLD", 35,
            f"no trend edge (adx={market.adx:.0f} strength={market.trend_strength:.2f})")

    def _member_profit_guardian(self, market: MarketSnapshot) -> CouncilMemberVote:
        """ProfitGuardian — TP / ratchet / exhaustion lens."""
        trade = self.trade
        c = self._config
        # TP1 / TP2 (same geometry as the cascade, read-only)
        if trade.tp1_state != "EXECUTED" and trade.tp1_price > 0 and \
                self._price_crossed_level(market, trade.tp1_price, "TP1"):
            return CouncilMemberVote(
                "ProfitGuardian", "profit protection", "PARTIAL_CLOSE", 96,
                f"TP1 {trade.tp1_price:.6f} reached")
        if trade.tp2_state != "EXECUTED" and trade.tp2_price > 0 and \
                trade.tp1_state == "EXECUTED" and \
                self._price_crossed_level(market, trade.tp2_price, "TP2"):
            return CouncilMemberVote(
                "ProfitGuardian", "profit protection", "FULL_CLOSE", 98,
                f"TP2 {trade.tp2_price:.6f} reached")
        # Profit experience
        if trade.roe_pct >= c["profit_lock_roi_pct"] and market.atr > 0 and \
                trade.protection_state in (ProtectionState.BREAKEVEN, ProtectionState.PROFIT_LOCK):
            if market.adx >= c["trend_adx_min"]:
                return CouncilMemberVote(
                    "ProfitGuardian", "profit protection", "ADJUST_SL", 68,
                    f"ride profit roe={trade.roe_pct:.1f}% adx={market.adx:.0f}")
        if trade.protection_state == ProtectionState.NONE and \
                trade.roe_pct >= c["breakeven_roi_pct"]:
            return CouncilMemberVote(
                "ProfitGuardian", "profit protection", "ADJUST_SL", 60,
                f"ratchet to breakeven roe={trade.roe_pct:.1f}%")
        # Exhaustion
        extreme = c["exhaustion_rsi_extreme"]
        exhausted = (trade.side == "BUY" and market.rsi >= extreme) or \
                    (trade.side == "SELL" and market.rsi <= (100 - extreme))
        if exhausted and trade.roe_pct > 0.3:
            return CouncilMemberVote(
                "ProfitGuardian", "profit protection", "PARTIAL_CLOSE", 62,
                f"exhaustion rsi={market.rsi:.1f}")
        return CouncilMemberVote(
            "ProfitGuardian", "profit protection", "HOLD", 30,
            f"protect roe={trade.roe_pct:.2f}% stage={trade.profit_stage.value}")

    def _member_risk_officer(self, market: MarketSnapshot) -> CouncilMemberVote:
        """RiskOfficer — absolute-risk lens (hard stop / SL / trailing / stale)."""
        trade = self.trade
        c = self._config
        if trade.roe_pct <= c["hard_stop_roe_pct"]:
            return CouncilMemberVote(
                "RiskOfficer", "absolute risk", "FULL_CLOSE", 100,
                f"hard stop roe={trade.roe_pct:.2f}%")
        if trade.synthetic_sl > 0:
            sl_hit = (trade.side == "BUY" and market.price <= trade.synthetic_sl) or \
                     (trade.side == "SELL" and market.price >= trade.synthetic_sl)
            if sl_hit:
                return CouncilMemberVote(
                    "RiskOfficer", "absolute risk", "FULL_CLOSE", 100,
                    f"protective SL {trade.synthetic_sl:.6f} touched")
        if trade.protection_floor_sl > 0:
            trail_hit = (trade.side == "BUY" and market.price <= trade.protection_floor_sl) or \
                        (trade.side == "SELL" and market.price >= trade.protection_floor_sl)
            if trail_hit and trade.protection_state == ProtectionState.TRAILING:
                return CouncilMemberVote(
                    "RiskOfficer", "absolute risk", "FULL_CLOSE", 92,
                    f"trailing floor {trade.protection_floor_sl:.6f} hit")
        if trade.entry_time > 0:
            hold_sec = time.time() - trade.entry_time
            if hold_sec > c["force_close_after_sec"]:
                return CouncilMemberVote(
                    "RiskOfficer", "absolute risk", "FULL_CLOSE", 85,
                    f"forced exit after {hold_sec:.0f}s")
        return CouncilMemberVote(
            "RiskOfficer", "absolute risk", "HOLD", 18,
            f"risk planar sl={trade.synthetic_sl:.6f}")

    def _member_thesis_officer(self, market: MarketSnapshot) -> CouncilMemberVote:
        """ThesisOfficer — thesis-validity lens."""
        trade = self.trade
        c = self._config
        threshold = c["thesis_failure_threshold"]
        evidence = 0
        if trade.side == "BUY":
            if market.rsi < 30 and market.adx > 25:
                evidence += 1
            if market.price <= trade.synthetic_sl * 0.99:
                evidence += 1
        else:
            if market.rsi > 70 and market.adx > 25:
                evidence += 1
            if market.price >= trade.synthetic_sl * 1.01:
                evidence += 1
        if trade.roe_pct <= threshold * 2 and evidence >= 2:
            return CouncilMemberVote(
                "ThesisOfficer", "thesis validity", "FULL_CLOSE", 88,
                f"thesis broken roe={trade.roe_pct:.2f}% evidence={evidence}")
        if trade.roe_pct <= threshold and evidence >= 1:
            return CouncilMemberVote(
                "ThesisOfficer", "thesis validity", "PARTIAL_CLOSE", 74,
                f"thesis failing roe={trade.roe_pct:.2f}%")
        return CouncilMemberVote(
            "ThesisOfficer", "thesis validity", "HOLD", 32,
            f"thesis holds roe={trade.roe_pct:.2f}%")

    def _member_scalp_desk(self, market: MarketSnapshot) -> CouncilMemberVote:
        """ScalpDesk — rapid-rotation lens (only engaged for SCALP styles)."""
        trade = self.trade
        c = self._config
        if trade.trade_style != TradeStyle.SCALP:
            return CouncilMemberVote(
                "ScalpDesk", "scalp exits", "HOLD", 12, "desk idle (non-scalp)")
        hold_sec = time.time() - trade.entry_time if trade.entry_time > 0 else 0
        if trade.roe_pct >= c["scalp_roi_threshold"]:
            return CouncilMemberVote(
                "ScalpDesk", "scalp exits", "FULL_CLOSE", 90,
                f"scalp target roe={trade.roe_pct:.2f}%")
        if hold_sec > c["scalp_time_limit_sec"]:
            if trade.roe_pct <= 0:
                return CouncilMemberVote(
                    "ScalpDesk", "scalp exits", "FULL_CLOSE", 84,
                    f"timebox {hold_sec:.0f}s no profit")
            return CouncilMemberVote(
                "ScalpDesk", "scalp exits", "PARTIAL_CLOSE", 76,
                f"timebox {hold_sec:.0f}s lock roe={trade.roe_pct:.2f}%")
        if trade.side == "BUY" and market.rsi >= c["scalp_rsi_overbought"] and trade.roe_pct > 0:
            return CouncilMemberVote(
                "ScalpDesk", "scalp exits", "PARTIAL_CLOSE", 70,
                f"overbought rsi={market.rsi:.1f}")
        if trade.side == "SELL" and market.rsi <= c["scalp_rsi_oversold"] and trade.roe_pct > 0:
            return CouncilMemberVote(
                "ScalpDesk", "scalp exits", "PARTIAL_CLOSE", 70,
                f"oversold rsi={market.rsi:.1f}")
        return CouncilMemberVote(
            "ScalpDesk", "scalp exits", "HOLD", 20,
            f"scalp open roe={trade.roe_pct:.2f}% hold={hold_sec:.0f}s")

    def _member_volume_truth(self, market: MarketSnapshot) -> CouncilMemberVote:
        """VolumeTruth — Effort-vs-Result lens: high effort with weak result is
        absorption/distribution pressure, never strength; it votes to bank or
        exit, never to add."""
        trade = self.trade
        eff = self._vpa_effort(market)
        status = str(eff.get("status", "STALL"))
        vr = float(eff.get("volume_ratio", 1.0))
        if trade.trade_style == TradeStyle.SCALP:
            return CouncilMemberVote("VolumeTruth", "effort/result", "HOLD", 15,
                                     f"desk idle (non-directional scalp)")
        if status == "WEAK_RESULT" and trade.roe_pct >= self._config["vpa_bank_roi_pct"]:
            return CouncilMemberVote(
                "VolumeTruth", "effort/result", "PARTIAL_CLOSE", 66,
                f"high effort, stale result roe={trade.roe_pct:.1f}% vol={vr:.2f}x")
        if status == "WEAK_RESULT" and trade.roe_pct <= self._config["vpa_strict_roe_ceiling"]:
            opp = self._vpa_effort(market, opposing=True)
            if opp.get("status") == "CONFIRMED":
                return CouncilMemberVote(
                    "VolumeTruth", "effort/result", "FULL_CLOSE", 89,
                    f"opposing displacement confirmed roe={trade.roe_pct:.2f}%")
            return CouncilMemberVote(
                "VolumeTruth", "effort/result", "PARTIAL_CLOSE", 58,
                f"eroding thesis on volume roe={trade.roe_pct:.2f}% vol={vr:.2f}x")
        if status == "CONFIRMED" and abs(market.trend_strength) < 0.3:
            return CouncilMemberVote(
                "VolumeTruth", "effort/result", "HOLD", 40,
                f"effort confirmed vol={vr:.2f}x")
        return CouncilMemberVote(
            "VolumeTruth", "effort/result", "HOLD", 22,
            f"effort={status} vol={vr:.2f}x")

    def board_votes(self, market: MarketSnapshot) -> List[CouncilMemberVote]:
        """Run all named council members. Returns their verdicts."""
        return [
            self._member_trend_rider(market),
            self._member_profit_guardian(market),
            self._member_risk_officer(market),
            self._member_thesis_officer(market),
            self._member_scalp_desk(market),
            self._member_volume_truth(market),
        ]

    def _compute_unrealized(self, market: MarketSnapshot) -> None:
        """Compute unrealized PnL and update trade snapshot."""
        trade = self.trade
        if trade.entry_price <= 0 or market.price <= 0:
            return
        if trade.side == "BUY":
            pnl_pct = (market.price - trade.entry_price) / trade.entry_price * 100
        else:
            pnl_pct = (trade.entry_price - market.price) / trade.entry_price * 100
        trade.unrealized_pnl_pct = pnl_pct
        trade.unrealized_pnl_usdt = pnl_pct / 100 * (trade.remaining_qty * trade.entry_price)
        if trade.original_qty > 0:
            margin_per_unit = trade.margin / trade.original_qty if trade.original_qty > 0 else 0
            trade.roe_pct = (trade.unrealized_pnl_usdt / (margin_per_unit * trade.remaining_qty) * 100) if margin_per_unit > 0 and trade.remaining_qty > 0 else pnl_pct
        else:
            trade.roe_pct = pnl_pct
        if trade.roe_pct > trade.peak_roe:
            trade.peak_roe = trade.roe_pct
        if market.price > trade.peak_price or trade.peak_price <= 0:
            trade.peak_price = market.price

    def _check_hard_limits(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Absolute safety: hard stop-loss and max hold time."""
        trade = self.trade
        # Hard ROE stop
        if trade.roe_pct <= self._config["hard_stop_roe_pct"]:
            return TradeDecision(
                action="FULL_CLOSE",
                reason=f"hard_stop_roe={trade.roe_pct:.2f}%",
                confidence=1.0,
                exit_reason=ExitReason.STOP_LOSS,
                board_notes={"hard_stop": True, "roe": trade.roe_pct},
            )
        # Force close after max time
        if trade.entry_time > 0:
            hold_sec = time.time() - trade.entry_time
            if hold_sec > self._config["force_close_after_sec"]:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"force_close_hold_time={hold_sec:.0f}s",
                    confidence=1.0,
                    exit_reason=ExitReason.TIMEOUT,
                    board_notes={"force_timeout": True, "hold_sec": hold_sec},
                )
        return None

    def _check_scalp_exit(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Detect scalp opportunity: quick profit target or time-based exit."""
        trade = self.trade
        if trade.trade_style != TradeStyle.SCALP:
            return None
        hold_sec = time.time() - trade.entry_time if trade.entry_time > 0 else 0
        # Scalp ROI target hit
        if trade.roe_pct >= self._config["scalp_roi_threshold"]:
            return TradeDecision(
                action="FULL_CLOSE",
                reason=f"scalp_target_roi={trade.roe_pct:.2f}%",
                confidence=0.9,
                exit_reason=ExitReason.SCALP_TARGET,
                is_scalp_exit=True,
                board_notes={"scalp_exit": True, "roi": trade.roe_pct, "hold_sec": hold_sec},
            )
        # Scalp time limit exceeded — close at whatever PnL
        if hold_sec > self._config["scalp_time_limit_sec"]:
            action = "FULL_CLOSE" if trade.roe_pct <= 0 else "PARTIAL_CLOSE"
            ratio = 1.0 if action == "FULL_CLOSE" else 0.7
            return TradeDecision(
                action=action,
                reason=f"scalp_time_limit={hold_sec:.0f}s, roi={trade.roe_pct:.2f}%",
                confidence=0.8,
                close_ratio=ratio,
                exit_reason=ExitReason.SCALP_TARGET if trade.roe_pct > 0 else ExitReason.TIMEOUT,
                is_scalp_exit=True,
                board_notes={"scalp_timeout": True, "hold_sec": hold_sec, "roi": trade.roe_pct},
            )
        # RSI extreme during scalp — take profit
        if market.rsi > 0:
            if trade.side == "BUY" and market.rsi >= self._config["scalp_rsi_overbought"]:
                if trade.roe_pct > 0:
                    return TradeDecision(
                        action="PARTIAL_CLOSE",
                        reason=f"scalp_rsi_overbought={market.rsi:.1f}",
                        confidence=0.7,
                        close_ratio=0.5,
                        exit_reason=ExitReason.SCALP_TARGET,
                        is_scalp_exit=True,
                        board_notes={"scalp_rsi_exit": True, "rsi": market.rsi},
                    )
            elif trade.side == "SELL" and market.rsi <= self._config["scalp_rsi_oversold"]:
                if trade.roe_pct > 0:
                    return TradeDecision(
                        action="PARTIAL_CLOSE",
                        reason=f"scalp_rsi_oversold={market.rsi:.1f}",
                        confidence=0.7,
                        close_ratio=0.5,
                        exit_reason=ExitReason.SCALP_TARGET,
                        is_scalp_exit=True,
                        board_notes={"scalp_rsi_exit": True, "rsi": market.rsi},
                    )
        return None

    def _check_tp_execution(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Evaluate TP1/TP2 levels for partial and full close."""
        trade = self.trade
        if trade.tp1_price <= 0 and trade.tp2_price <= 0:
            return None

        # TP1 evaluation
        if trade.tp1_state != "EXECUTED" and trade.tp1_price > 0:
            tp1_hit = self._price_crossed_level(market, trade.tp1_price, "TP1")
            if tp1_hit:
                trade.tp1_state = "EXECUTED"
                trade.tp1_exec_price = trade.tp1_price
                trade.tp1_fill_qty = trade.remaining_qty * self._config["tp1_partial_ratio"]
                trade.tp1_event_ts = time.time()
                return TradeDecision(
                    action="PARTIAL_CLOSE",
                    reason=f"tp1_hit={trade.tp1_price:.6f}",
                    confidence=0.95,
                    close_ratio=self._config["tp1_partial_ratio"],
                    exit_reason=ExitReason.TP1,
                    is_trend_follow=True,
                    board_notes={"tp1_executed": True, "tp1_price": trade.tp1_price},
                )

        # TP2 evaluation (only after TP1)
        if trade.tp2_state != "EXECUTED" and trade.tp2_price > 0 and trade.tp1_state == "EXECUTED":
            tp2_hit = self._price_crossed_level(market, trade.tp2_price, "TP2")
            if tp2_hit:
                trade.tp2_state = "EXECUTED"
                trade.tp2_event_ts = time.time()
                if self._config["tp2_full_close"]:
                    return TradeDecision(
                        action="FULL_CLOSE",
                        reason=f"tp2_hit={trade.tp2_price:.6f}",
                        confidence=0.98,
                        exit_reason=ExitReason.TP2,
                        is_trend_follow=True,
                        board_notes={"tp2_executed": True, "tp2_price": trade.tp2_price},
                    )
                else:
                    return TradeDecision(
                        action="PARTIAL_CLOSE",
                        reason=f"tp2_hit={trade.tp2_price:.6f}",
                        confidence=0.95,
                        close_ratio=0.5,
                        exit_reason=ExitReason.TP2,
                        is_trend_follow=True,
                        board_notes={"tp2_partial": True, "tp2_price": trade.tp2_price},
                    )

        return None

    def _check_protection_update(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Evaluate protection ratchet: breakeven -> profit lock -> trailing."""
        trade = self.trade
        entry = trade.entry_price
        if entry <= 0:
            return None

        # Stage 1: Move to breakeven after small profit
        if trade.protection_state == ProtectionState.NONE:
            if trade.roe_pct >= self._config["breakeven_roi_pct"]:
                new_sl = entry
                if trade.ratchet_sl(new_sl):
                    trade.protection_state = ProtectionState.BREAKEVEN
                    trade.advance_stage(ProfitStage.PROFIT_LOCKED)
                    return TradeDecision(
                        action="ADJUST_SL",
                        reason=f"breakeven_ratchet={entry:.6f}",
                        confidence=0.9,
                        new_sl=new_sl,
                        board_notes={"breakeven_ratchet": True, "sl": entry},
                    )

        # Stage 2: Lock partial profit
        if trade.protection_state == ProtectionState.BREAKEVEN:
            if trade.roe_pct >= self._config["profit_lock_roi_pct"]:
                if trade.side == "BUY":
                    profit_lock = entry + (market.price - entry) * 0.3
                else:
                    profit_lock = entry - (entry - market.price) * 0.3
                if trade.ratchet_sl(profit_lock):
                    trade.protection_state = ProtectionState.PROFIT_LOCK
                    return TradeDecision(
                        action="ADJUST_SL",
                        reason=f"profit_lock={profit_lock:.6f}",
                        confidence=0.85,
                        new_sl=profit_lock,
                        board_notes={"profit_lock": True, "sl": profit_lock, "roe": trade.roe_pct},
                    )

        # Stage 3: Activate trailing stop
        if trade.protection_state in (ProtectionState.BREAKEVEN, ProtectionState.PROFIT_LOCK):
            if market.adx >= self._config["trend_adx_min"] and trade.roe_pct >= self._config["profit_lock_roi_pct"]:
                trail_mult = self._config["trend_trail_atr_mult"]
                if market.atr > 0:
                    if trade.side == "BUY":
                        trail_sl = market.price - market.atr * trail_mult
                    else:
                        trail_sl = market.price + market.atr * trail_mult
                    # Only activate if trail is better than current protection
                    if trade.ratchet_sl(trail_sl):
                        trade.trail_stop = trail_sl
                        trade.trail_activation_price = market.price
                        trade.protection_state = ProtectionState.TRAILING
                        trade.advance_stage(ProfitStage.TRAILING_ACTIVE)
                        return TradeDecision(
                            action="ADJUST_SL",
                            reason=f"trailing_activated={trail_sl:.6f}",
                            confidence=0.8,
                            new_sl=trail_sl,
                            board_notes={"trailing_active": True, "trail_sl": trail_sl, "adx": market.adx},
                        )

        # Stage 4: Update trailing stop as price moves
        if trade.protection_state == ProtectionState.TRAILING and market.atr > 0:
            trail_mult = self._config["trend_trail_atr_mult"]
            if trade.side == "BUY":
                new_trail = market.price - market.atr * trail_mult
            else:
                new_trail = market.price + market.atr * trail_mult
            if trade.ratchet_sl(new_trail):
                trade.trail_stop = new_trail
                return TradeDecision(
                    action="ADJUST_SL",
                    reason=f"trailing_update={new_trail:.6f}",
                    confidence=0.7,
                    new_sl=new_trail,
                    board_notes={"trail_update": True, "new_trail": new_trail},
                )
            # Trailing stop hit
            if trade.side == "BUY" and market.price <= trade.protection_floor_sl:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"trailing_stop_hit={trade.protection_floor_sl:.6f}",
                    confidence=0.9,
                    exit_reason=ExitReason.TRAILING_STOP,
                    board_notes={"trailing_stop_hit": True, "sl": trade.protection_floor_sl},
                )
            elif trade.side == "SELL" and market.price >= trade.protection_floor_sl:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"trailing_stop_hit={trade.protection_floor_sl:.6f}",
                    confidence=0.9,
                    exit_reason=ExitReason.TRAILING_STOP,
                    board_notes={"trailing_stop_hit": True, "sl": trade.protection_floor_sl},
                )

        # SL hit check (synthetic stop)
        if trade.synthetic_sl > 0:
            if trade.side == "BUY" and market.price <= trade.synthetic_sl:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"synthetic_sl_hit={trade.synthetic_sl:.6f}",
                    confidence=0.95,
                    exit_reason=ExitReason.STOP_LOSS,
                    board_notes={"sl_hit": True, "sl": trade.synthetic_sl},
                )
            elif trade.side == "SELL" and market.price >= trade.synthetic_sl:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"synthetic_sl_hit={trade.synthetic_sl:.6f}",
                    confidence=0.95,
                    exit_reason=ExitReason.STOP_LOSS,
                    board_notes={"sl_hit": True, "sl": trade.synthetic_sl},
                )

        return None

    def _vpa_effort(self, market: MarketSnapshot, opposing: bool = False) -> dict:
        """Effort-vs-Result of the last bars for the thesis side (or its
        opposite). Falls back to STALL when live OHLCV is unavailable."""
        side = ("SELL" if self.trade.side == "BUY" else "BUY") if opposing else self.trade.side
        df = getattr(market, "df", None)
        if df is None or market.atr <= 0 or getattr(df, "empty", True):
            return {"status": "STALL", "volume_ratio": 1.0}
        try:
            from core.vpa_volume import effort_result
            eff = effort_result(df, side, market.atr, lookback=3) or {}
            if not isinstance(eff, dict):
                eff = {}
            eff.setdefault("volume_ratio", 1.0)
            return eff
        except Exception:
            return {"status": "STALL", "volume_ratio": 1.0}

    def _check_vpa_profit_defense(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """VPA steering — high effort with a weak result is NOT strength.

        * Healthy trend (strong + adx) rides untouched (trend mgmt owns it).
        * ROE >= VPA_BANK_ROI_PCT (+18% default) with WEAK_RESULT (effort without
          result / distribution pressure): bank a partial PROFIT_PARTIAL, tighten
          the lock (ratchet 50% of the run), then let protection trail. NOT an
          instant full close.
        * ROE already eroding (<= vpa_strict_roe_ceiling) while the OPPOSING side
          shows CONFIRMED displacement + structure failure: institutional
          reversal -> STRICT_CLOSE.
        * ROE below the thesis threshold with adverse volume (>=1.5x): full close.
        """
        trade = self.trade
        if trade.trade_style == TradeStyle.SCALP:
            return None
        c = self._config
        if abs(market.trend_strength) >= 0.7 and market.adx >= c["trend_adx_min"]:
            return None
        eff = self._vpa_effort(market)
        status = str(eff.get("status", "STALL"))
        if status != "WEAK_RESULT":
            return None
        vr = float(eff.get("volume_ratio", 1.0))
        if trade.roe_pct >= c["vpa_bank_roi_pct"] and trade.remaining_ratio > 0.3:
            if trade.side == "BUY":
                lock = trade.entry_price + (market.price - trade.entry_price) * 0.5
            else:
                lock = trade.entry_price - (trade.entry_price - market.price) * 0.5
            if trade.ratchet_sl(lock):
                if trade.protection_state not in (ProtectionState.PROFIT_LOCK, ProtectionState.TRAILING):
                    trade.protection_state = ProtectionState.PROFIT_LOCK
                if trade.profit_stage == ProfitStage.NONE or trade.profit_stage is None:
                    trade.advance_stage(ProfitStage.TRAILING_ACTIVE)
            return TradeDecision(
                action="PARTIAL_CLOSE",
                reason=f"bank_profit_effort_failure roe={trade.roe_pct:.1f}% vol={vr:.2f}x",
                confidence=0.85,
                close_ratio=c["vpa_bank_ratio"],
                exit_reason=ExitReason.PROFIT_LOCK,
                board_notes={"vpa_bank": True, "effort_status": status,
                             "volume_ratio": vr, "roe": trade.roe_pct, "tightened_lock": lock},
            )
        if trade.roe_pct <= c["vpa_strict_roe_ceiling"]:
            struct_failed = (
                (trade.side == "BUY" and market.ema_fast > 0 and market.ema_slow > 0
                 and market.ema_fast < market.ema_slow) or
                (trade.side == "SELL" and market.ema_fast > 0 and market.ema_slow > 0
                 and market.ema_fast > market.ema_slow))
            opp = self._vpa_effort(market, opposing=True)
            if opp.get("status") == "CONFIRMED" and struct_failed:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"strict_close_institutional_reversal roe={trade.roe_pct:.2f}%",
                    confidence=0.95,
                    exit_reason=ExitReason.INSTITUTIONAL_REVERSAL,
                    board_notes={"strict_close": True, "effort_status": status,
                                 "opposing_effort": opp.get("status"),
                                 "structure_failure": True, "volume_ratio": vr},
                )
            if trade.roe_pct <= c["thesis_failure_threshold"] and vr >= 1.5:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"adverse_volume_thesis_failure roe={trade.roe_pct:.2f}% vol={vr:.2f}x",
                    confidence=0.9,
                    exit_reason=ExitReason.INSTITUTIONAL_REVERSAL,
                    board_notes={"adverse_volume": True, "effort_status": status,
                                 "volume_ratio": vr},
                )
        return None

    def _check_trend_management(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Trend-following: ride strong trends, bank at exhaustion."""
        trade = self.trade
        if trade.trade_style == TradeStyle.SCALP:
            return None

        # Strong trend: keep riding, don't intervene unless TP hit
        if abs(market.trend_strength) >= 0.7 and market.adx >= self._config["trend_adx_min"]:
            return None  # Let the trend run

        # Weak trend after profit: consider partial close
        if abs(market.trend_strength) < 0.3 and trade.roe_pct > 0.5:
            if trade.tp1_state != "EXECUTED" and trade.remaining_ratio > 0.5:
                return TradeDecision(
                    action="PARTIAL_CLOSE",
                    reason=f"weak_trend_partial_close, strength={market.trend_strength:.2f}",
                    confidence=0.6,
                    close_ratio=0.3,
                    exit_reason=ExitReason.PROFIT_LOCK,
                    board_notes={"weak_trend": True, "strength": market.trend_strength},
                )

        return None

    def _check_reversal_exit(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Detect trend reversal and exit if enabled."""
        trade = self.trade
        if not self._config["reversal_exit_enabled"]:
            return None

        # EMA crossover reversal
        if market.ema_fast > 0 and market.ema_slow > 0:
            if trade.side == "BUY" and market.ema_fast < market.ema_slow:
                if market.adx > 20 and trade.roe_pct < 0:
                    return TradeDecision(
                        action="FULL_CLOSE",
                        reason=f"ema_bearish_cross, fast={market.ema_fast:.2f}<slow={market.ema_slow:.2f}",
                        confidence=0.75,
                        exit_reason=ExitReason.REVERSAL,
                        board_notes={"ema_reversal": True, "adx": market.adx},
                    )
            elif trade.side == "SELL" and market.ema_fast > market.ema_slow:
                if market.adx > 20 and trade.roe_pct < 0:
                    return TradeDecision(
                        action="FULL_CLOSE",
                        reason=f"ema_bullish_cross, fast={market.ema_fast:.2f}>slow={market.ema_slow:.2f}",
                        confidence=0.75,
                        exit_reason=ExitReason.REVERSAL,
                        board_notes={"ema_reversal": True, "adx": market.adx},
                    )

        return None

    def _check_thesis_failure(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Detect thesis failure: trade thesis invalidated by market action."""
        trade = self.trade
        threshold = self._config["thesis_failure_threshold"]
        if trade.roe_pct <= threshold:
            # Thesis failure with strong evidence
            failure_score = 0
            notes: Dict[str, Any] = {"roe": trade.roe_pct}
            if trade.side == "BUY":
                if market.rsi < 30 and market.adx > 25:
                    failure_score += 2
                    notes["rsi_breakdown"] = market.rsi
                if market.price < trade.synthetic_sl * 0.99:
                    failure_score += 3
                    notes["below_sl_zone"] = True
            else:
                if market.rsi > 70 and market.adx > 25:
                    failure_score += 2
                    notes["rsi_breakup"] = market.rsi
                if market.price > trade.synthetic_sl * 1.01:
                    failure_score += 3
                    notes["above_sl_zone"] = True
            if failure_score >= 2:
                # Partial close on thesis failure
                return TradeDecision(
                    action="PARTIAL_CLOSE",
                    reason=f"thesis_failure, roe={trade.roe_pct:.2f}%, score={failure_score}",
                    confidence=0.7,
                    close_ratio=0.5,
                    exit_reason=ExitReason.THESIS_FAILURE,
                    board_notes=notes,
                )
            # Weak thesis failure: full close if deep enough
            if trade.roe_pct <= threshold * 2:
                return TradeDecision(
                    action="FULL_CLOSE",
                    reason=f"deep_thesis_failure, roe={trade.roe_pct:.2f}%",
                    confidence=0.8,
                    exit_reason=ExitReason.THESIS_FAILURE,
                    board_notes=notes,
                )
        return None

    def _check_exhaustion(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Detect exhaustion: overextended move about to reverse.

        Only bites BEFORE the TP ladder: once TP1 has banked the initial
        slice, the runner is managed by the trail/protection machinery and an
        RSI-squeeze top must not carve repeated slices off it.
        """
        trade = self.trade
        if trade.roe_pct <= 0:
            return None
        if trade.tp1_hit:
            return None

        cooldown = float(
            os.getenv("EXHAUSTION_COOLDOWN_SEC", self._config.get("exhaustion_cooldown_sec", 20))
        )
        now = time.time()
        if now - float(getattr(trade, "last_exhaustion_ts", 0) or 0) < cooldown:
            return None

        def _bank_exhaustion(reason: str, ratio: float, notes: dict) -> TradeDecision:
            trade.last_exhaustion_ts = now
            return TradeDecision(
                action="PARTIAL_CLOSE",
                reason=reason,
                confidence=0.65,
                close_ratio=ratio,
                exit_reason=ExitReason.EXHAUSTION,
                board_notes=notes,
            )

        # RSI extreme in profit direction
        if trade.side == "BUY" and market.rsi >= self._config["exhaustion_rsi_extreme"]:
            if trade.roe_pct > 0.3:
                return _bank_exhaustion(
                    f"exhaustion_rsi={market.rsi:.1f}", 0.4,
                    {"exhaustion": True, "rsi": market.rsi, "roe": trade.roe_pct},
                )
        elif trade.side == "SELL" and market.rsi <= (100 - self._config["exhaustion_rsi_extreme"]):
            if trade.roe_pct > 0.3:
                return _bank_exhaustion(
                    f"exhaustion_rsi={market.rsi:.1f}", 0.4,
                    {"exhaustion": True, "rsi": market.rsi, "roe": trade.roe_pct},
                )

        # BB squeeze exhaustion
        if market.bb_width > 0 and market.atr > 0:
            if market.bb_width < market.atr * 0.5 and trade.roe_pct > 0.5:
                return _bank_exhaustion(
                    f"bb_squeeze_exhaustion, width={market.bb_width:.4f}", 0.3,
                    {"bb_squeeze": True, "bb_width": market.bb_width},
                )

        return None

    def _check_max_hold_time(self, market: MarketSnapshot) -> Optional[TradeDecision]:
        """Warn and force-close after extended hold without progress."""
        trade = self.trade
        hold_sec = time.time() - trade.entry_time if trade.entry_time > 0 else 0
        max_warn = self._config["max_hold_time_sec"]
        if hold_sec > max_warn and trade.roe_pct < 0.1:
            # Stale position with no profit — close
            return TradeDecision(
                action="FULL_CLOSE",
                reason=f"max_hold_stale={hold_sec:.0f}s, roe={trade.roe_pct:.2f}%",
                confidence=0.8,
                exit_reason=ExitReason.TIMEOUT,
                board_notes={"max_hold_timeout": True, "hold_sec": hold_sec},
            )
        return None

    def _hold(self, market: MarketSnapshot) -> TradeDecision:
        """Default: hold the position, update board notes."""
        trade = self.trade
        notes = {
            "action": "HOLD",
            "roe": trade.roe_pct,
            "protection": trade.protection_state.value,
            "stage": trade.profit_stage.value,
            "adx": market.adx,
            "trend": market.trend_strength,
            "rsi": market.rsi,
        }
        return TradeDecision(
            action="HOLD",
            reason="no_exit_signal",
            confidence=0.5,
            board_notes=notes,
        )

    def _price_crossed_level(self, market: MarketSnapshot, level: float, label: str) -> bool:
        """Check if price has crossed a TP level."""
        trade = self.trade
        if level <= 0:
            return False
        # Use the current mark price vs the level
        if trade.side == "BUY":
            return market.price >= level
        else:
            return market.price <= level

    @staticmethod
    def classify_trade_style(trade: Trade, market: MarketSnapshot) -> TradeStyle:
        """Classify whether this trade should be managed as TREND or SCALP."""
        # Already classified
        if trade.trade_style != TradeStyle.INSTITUTIONAL:
            return trade.trade_style
        # Strong trend -> TREND management
        if abs(market.trend_strength) >= 0.6 and market.adx >= 25:
            return TradeStyle.TREND
        # Weak trend + quick profit -> SCALP
        if trade.entry_time > 0:
            hold_sec = time.time() - trade.entry_time
            if hold_sec < 300 and trade.roe_pct > 0.2:
                return TradeStyle.SCALP
        # Default: keep institutional classification
        return TradeStyle.INSTITUTIONAL
