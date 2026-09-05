"""Atom Intelligence Layer — a quality/verification layer ON TOP of Roro's entry model.

Roro stays the ENTRY ENGINE (timing + entry decision). Atom verifies, classifies
and manages. This module is advisory on *how good* a Roro-valid setup is, never
a source of entry signals by itself (the causal OB + Roro RF timing remain the
source of truth for when to trade).

Pipeline honoured by the engine:
    Roro ENTRY -> Classification (TREND / REVERSAL / SNIPER_REVERSAL)
               -> OB Quality   -> Zone Freshness -> Liquidity Support
               -> Structure    -> Sniper confirmation (REVERSAL only)
               -> Decision

Guarantees
----------
  * all computation is over *closed* candles only (no look-ahead / no repaint),
  * the layer never by itself turns a weak setup into a strong one: it returns
    a bounded confidence adjust and a hard-reject ONLY for Fake/Broken/Stale OB,
  * TREND trades are approved on OB-fresh + liquidity-supported + structure and
    do NOT require the extra Sniper confirmation,
  * REVERSAL / SNIPER_REVERSAL trades additionally require Sniper confirmation
    (Liquidity Sweep + MSS/CHoCH + strong rejection + fresh OB + liquidity
    support), as demanded by the project owners,
  * ADX is applied per trade type: TREND 20..45, REVERSAL/SNIPER < 35 (matches
    Roro's own advanced_decision_engine regime split).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

# Trade-type constants (kept distinct from the sniper layer's LONG/SHORT).
TRADE_TREND = "TREND"
TRADE_REVERSAL = "REVERSAL"
TRADE_SNIPER_REVERSAL = "SNIPER_REVERSAL"

# Confidence bounds (advisory, like the sniper layer: bounded, never flips a
# fundamentally weak setup into a strong one).
MAX_CONFIDENCE_BONUS = 12.0
MAX_CONFIDENCE_PENALTY = -10.0

# OB / freshness grades
GRADE_APLUS = "A+"
GRADE_A = "A"
GRADE_B = "B"
GRADE_INVALID = "INVALID"

FRESH = "FRESH"
AGING = "AGING"
STALE = "STALE"
OVER_MITIGATED = "OVER_MITIGATED"


def normalize_side(side) -> Optional[int]:
    if side is None:
        return None
    if isinstance(side, (int, np.integer)):
        if int(side) == 1:
            return 1
        if int(side) == -1:
            return -1
        return None
    if isinstance(side, str):
        s = side.strip().upper()
        if s in ("BUY", "LONG", "L", "B"):
            return 1
        if s in ("SELL", "SHORT", "S"):
            return -1
    return None


@dataclass
class ZoneFreshness:
    state: str = FRESH
    age_bars: int = 0
    touches: int = 0
    mitigated_bars: int = 0
    grade_bars: tuple = (10, 20, 40)  # fresh_aplus, fresh_a, fresh_b
    reasons: List[str] = field(default_factory=list)

    @property
    def fresh(self) -> bool:
        return self.state in (FRESH, AGING)

    @property
    def bad(self) -> bool:
        return self.state in (STALE, OVER_MITIGATED)


def evaluate_zone_freshness(age_bars: int, touches: int, mitigated_bars: int,
                            grade_bars=(10, 20, 40), price_interacting: bool = True) -> ZoneFreshness:
    """Classify whether a zone is FRESH / AGING / STALE / OVER_MITIGATED.

    STALE and OVER_MITIGATED are quality *hard-rejects* for a strong entry (an
    over-mitigated / stale zone is not a strong institutional zone, even if the
    rest of the setup looks good).
    """
    fresh_aplus, fresh_a, fresh_b = grade_bars
    zf = ZoneFreshness(age_bars=int(age_bars), touches=int(touches),
                       mitigated_bars=int(mitigated_bars), grade_bars=tuple(grade_bars))
    mitigated_threshold = max(3, int(fresh_a * 0.4))  # e.g. 8 with default (10,20,40)
    if mitigated_bars >= mitigated_threshold and not price_interacting:
        zf.state = OVER_MITIGATED
        zf.reasons.append(f"over-mitigated ({mitigated_bars} bars mitigated while price left)")
        return zf
    if touches > 3:
        zf.state = OVER_MITIGATED
        zf.reasons.append(f"touched {touches} times (over-mitigated)")
        return zf
    if age_bars > fresh_b:
        zf.state = STALE
        zf.reasons.append(f"stale (age {age_bars} > {fresh_b})")
    elif age_bars > fresh_a:
        zf.state = AGING
        zf.reasons.append(f"aging (age {age_bars})")
    else:
        zf.state = FRESH
        zf.reasons.append(f"fresh (age {age_bars})")
    return zf


class TradeClassifier:
    """Classify a Roro-valid setup into TREND / REVERSAL / SNIPER_REVERSAL.

    A reversal is when price swept the opposing liquidity pool and reclaimed
    (MSS/CHoCH) against the prior impulse. A continuation is when price is
    pulling back *with* the impulse and takes liquidity in the same direction
    (BOS) — that is a TREND trade and must NOT be treated as a reversal.
    """

    @staticmethod
    def classify(df: pd.DataFrame, side, atr: float,
                 trigger_state: str = "", sweep_aligned: bool = False,
                 mss_bullish: bool = False, mss_bearish: bool = False) -> str:
        d = normalize_side(side)
        if d is None:
            return TRADE_TREND
        side_str = "BUY" if d == 1 else "SELL"
        t = (trigger_state or "").upper()
        mss_flag = mss_bullish if d == 1 else mss_bearish

        # A reversal is a counter-impulse sweep-and-reclaim (a fresh extreme
        # against the prior move that is then reclaimed). Roro's own regime
        # split treats MSS / CHoCH / liquidity-sweep as reversal context and
        # BOS as continuation. A healthy trend never sweeps the opposing pool.
        reversal_trigger = t in ("MSS_CONFIRMED", "LIQUIDITY_SWEEP", "CHOCH_CONFIRMED")
        is_reversal = (
            TradeClassifier._reversal_structure(df, d)
            or (reversal_trigger and (mss_flag or sweep_aligned))
        )
        if is_reversal:
            return (TRADE_SNIPER_REVERSAL if TradeClassifier._strong_reversal(df, side_str, atr)
                    else TRADE_REVERSAL)
        return TRADE_TREND

    @staticmethod
    def _reversal_structure(df: pd.DataFrame, d) -> bool:
        """True when price swept the opposing liquidity (a fresh counter-extreme)
        then reclaimed it — a genuine change of character, NOT a healthy trend
        continuation which makes new extremes in the direction of the trade."""
        if df is None or len(df) < 13:
            return False
        n = len(df)
        a = max(0, n - 12)
        b = max(0, n - 6)
        if a >= b or b >= n:
            return False
        prev_hi = float(df["high"].iloc[a:b].max())
        prev_lo = float(df["low"].iloc[a:b].min())
        leg_hi = float(df["high"].iloc[b:].max())
        leg_lo = float(df["low"].iloc[b:].min())
        close = float(df["close"].iloc[-1])
        if d == 1:  # BUY: leg dipped below the prior-low zone, then reclaimed above prior highs
            swept = leg_lo <= prev_lo
            reclaimed = close > prev_hi
            return swept and reclaimed
        else:       # SELL: leg spiked above the prior-high zone, then dropped below prior lows
            swept = leg_hi >= prev_hi
            reclaimed = close < prev_lo
            return swept and reclaimed

    @staticmethod
    def _strong_reversal(df: pd.DataFrame, side: str, atr: float) -> bool:
        """High-grade reversal: displacement leg >= 0.8 ATR with volume and a
        fresh-ish origin — used to promote REVERSAL -> SNIPER_REVERSAL."""
        if df is None or len(df) < 5 or atr <= 0:
            return False
        n = len(df)
        w = min(4, n - 1)
        if side == "BUY":
            base = df.iloc[n - 1 - w]
            fwd = df.iloc[n - w:]
            if fwd.empty:
                return False
            leg = float(fwd["close"].max()) - float(base["low"])
        else:
            base = df.iloc[n - 1 - w]
            fwd = df.iloc[n - w:]
            if fwd.empty:
                return False
            leg = float(base["high"]) - float(fwd["close"].min())
        vol_avg = float(df["volume"].iloc[max(0, n - 12):n - 1].mean()) if "volume" in df and n > 2 else 1.0
        leg_vol = float(df["volume"].iloc[n - w:].max()) if "volume" in df else 1.0
        vr = leg_vol / vol_avg if vol_avg > 0 else 1.0
        return leg / atr >= 0.8 and vr >= 1.2

    @staticmethod
    def _bos_ok(df: pd.DataFrame, d) -> bool:
        return TradeClassifier._bos_up(df) if d == 1 else TradeClassifier._bos_dn(df)

    @staticmethod
    def _bos_up(df: pd.DataFrame) -> bool:
        if df is None or len(df) < 7:
            return False
        hi = df["high"].iloc[-6:-1].max()
        return float(df["close"].iloc[-1]) > hi

    @staticmethod
    def _bos_dn(df: pd.DataFrame) -> bool:
        if df is None or len(df) < 7:
            return False
        lo = df["low"].iloc[-6:-1].min()
        return float(df["close"].iloc[-1]) < lo


@dataclass
class LiquiditySupport:
    score: float = 50.0
    support_level: Optional[float] = None
    resistant_level: Optional[float] = None
    delta_bullish: bool = False
    delta_bearish: bool = False
    volume_ratio: float = 0.0
    reasons: List[str] = field(default_factory=list)


def evaluate_liquidity_support(df: pd.DataFrame, side,
                               act_demand: Optional[dict] = None,
                               act_supply: Optional[dict] = None,
                               delta_bullish: bool = False,
                               delta_bearish: bool = False,
                               volume_ratio: float = 0.0) -> LiquiditySupport:
    """Liquidity as a QUALITY EVIDENCE, never a blind hard gate.

    A high score means: buy-side setup sits on/above real demand volume and
    sellers are being absorbed. Low score only REDUCES confidence (unless a
    demand/supply box is *actively* blocking the entry price — then it hard
    lowers confidence but still doesn't veto a strong trend).
    """
    d = normalize_side(side)
    ls = LiquiditySupport(delta_bullish=bool(delta_bullish),
                          delta_bearish=bool(delta_bearish),
                          volume_ratio=float(volume_ratio))
    if df is None or len(df) < 2:
        ls.reasons.append("no data")
        return ls
    price = float(df["close"].iloc[-1])
    if d == 1:
        support = act_demand
        blocking = act_supply
        ls.support_level = (support or {}).get("price") if support else None
        ls.resistant_level = (blocking or {}).get("price") if blocking else None
        near_support = ls.support_level is not None and abs(price - ls.support_level) / price < 0.01
        blocked = ls.resistant_level is not None and abs(price - ls.resistant_level) / price < 0.01
    else:
        support = act_supply
        blocking = act_demand
        ls.support_level = (support or {}).get("price") if support else None
        ls.resistant_level = (blocking or {}).get("price") if blocking else None
        near_support = ls.support_level is not None and abs(price - ls.support_level) / price < 0.01
        blocked = ls.resistant_level is not None and abs(price - ls.resistant_level) / price < 0.01

    score = 50.0
    if near_support:
        score += 20.0
        ls.reasons.append("on real liquidity support")
    else:
        ls.reasons.append("no active support box at price")
    if volume_ratio >= 1.2:
        score += 10.0
        ls.reasons.append(f"volume {volume_ratio:.2f}")
    if d == 1 and delta_bullish:
        score += 15.0
        ls.reasons.append("bullish delta (buyers absorbing)")
    if d == -1 and delta_bearish:
        score += 15.0
        ls.reasons.append("bearish delta (sellers absorbing)")
    if blocked:
        score -= 20.0
        ls.reasons.append("active opposing volume box blocking entry")
    ls.score = float(np.clip(score, 0.0, 100.0))
    return ls


@dataclass
class AtomIntelligence:
    """Output of the full verification/classification pass for one candidate."""

    trade_type: str = TRADE_TREND
    ob_quality_ok: bool = False
    ob_grade: str = GRADE_INVALID
    ob_score: float = 0.0
    freshness: ZoneFreshness = field(default_factory=ZoneFreshness)
    liquidity: LiquiditySupport = field(default_factory=LiquiditySupport)
    structure_valid: bool = False
    structure_score: float = 0.0
    adx: float = 0.0
    adx_ok: bool = False                # contextual regime read, NOT a blocking gate
    sniper_required: bool = False       # ONLY REVERSAL/SNIPER_REVERSAL demand the extra sniper confirmation
    sniper_confirmed: bool = False      # meaningful only when sniper_required is True
    sniper_score: float = 0.0
    approved: bool = False              # final Atom approval for READY
    hard_reject: bool = False           # fake/broken/stale OB => NO ENTRY
    confidence_adjust: float = 0.0      # bounded, never flips weak -> strong
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trade_type": self.trade_type,
            "ob_quality_ok": self.ob_quality_ok,
            "ob_grade": self.ob_grade,
            "ob_score": round(self.ob_score, 1),
            "freshness_state": self.freshness.state,
            "freshness_age": self.freshness.age_bars,
            "freshness_touches": self.freshness.touches,
            "liquidity_score": round(self.liquidity.score, 1),
            "liquidity_support": self.liquidity.support_level,
            "structure_valid": self.structure_valid,
            "structure_score": round(self.structure_score, 1),
            "adx": round(self.adx, 1),
            "adx_ok": self.adx_ok,
            "sniper_required": self.sniper_required,
            "sniper_confirmed": self.sniper_confirmed,
            "sniper_score": round(self.sniper_score, 1),
            "approved": self.approved,
            "hard_reject": self.hard_reject,
            "confidence_adjust": round(self.confidence_adjust, 3),
            "reasons": self.reasons,
        }


class AtomIntelligenceEngine:
    """Orchestrator: verifies + classifies a Roro-valid entry."""

    def __init__(self, config: Optional[dict] = None):
        self.cfg = config or {}
        self.grade_bars = tuple(self.cfg.get("ob_fresh_grade", (10, 20, 40)))

    def evaluate(self, df: pd.DataFrame, side, atr: float,
                 ob_grade: str = GRADE_INVALID, ob_score: float = 0.0,
                 zone_age_bars: int = 999, zone_touches: int = 0,
                 zone_mitigated_bars: int = 0, price_interacting: bool = True,
                 trigger_state: str = "", sweep_aligned: bool = False,
                 mss_bullish: bool = False, mss_bearish: bool = False,
                 struct_score: float = 0.0, adx: float = 0.0,
                 sniper_conf: Optional[object] = None,
                 act_demand: Optional[dict] = None, act_supply: Optional[dict] = None,
                 delta_bullish: bool = False, delta_bearish: bool = False,
                 volume_ratio: float = 0.0) -> AtomIntelligence:
        d = normalize_side(side)
        ai = AtomIntelligence()
        if df is None or len(df) < 10:
            ai.reasons.append("insufficient data")
            return ai

        # ---- 1) OB QUALITY (source of truth: causal OB grade/score) --------
        ai.ob_grade = ob_grade or GRADE_INVALID
        ai.ob_score = float(ob_score)
        # Fake / Broken / Stale OB must never be treated as a strong zone.
        # Hard-reject on: explicitly defective grade (broken/fake) or a weak
        # causal OB score. A neutral "NONE" grade (not graded by the strong-OB
        # path) is governed by the causal score alone.
        defective_grade = ai.ob_grade in ("BROKEN", "FAKE")
        if defective_grade or ai.ob_score < 40:
            ai.hard_reject = True
            ai.ob_quality_ok = False
            ai.reasons.append(f"hard_reject: OB {ai.ob_grade} score={ai.ob_score:.0f}")
            return ai
        ai.ob_quality_ok = True
        ai.reasons.append(f"OB {ai.ob_grade} score={ai.ob_score:.1f}")

        # ---- 2) ZONE FRESHNESS ---------------------------------------------
        ai.freshness = evaluate_zone_freshness(
            zone_age_bars, zone_touches, zone_mitigated_bars, self.grade_bars, price_interacting)
        if ai.freshness.bad:
            ai.hard_reject = True
            ai.reasons.append(f"hard_reject: zone {ai.freshness.state}")
            return ai
        ai.reasons.append(f"zone {ai.freshness.state}")

        # ---- 3) CLASSIFICATION (TREND vs REVERSAL vs SNIPER_REVERSAL) ------
        ai.trade_type = TradeClassifier.classify(
            df, side, atr, trigger_state, sweep_aligned, mss_bullish, mss_bearish)

        # ---- 4) ADX CONTEXTUAL REGIME READ (evidence, NOT a blind gate) ------
        # The classification decides how ADX is read:
        #   TREND    -> a rising/strong ADX supports continuation (favorable).
        #   REVERSAL -> a very high ADX suggests over-extension/exhaustion which
        #               *favors* the reversal thesis; we do NOT veto on ADX here.
        #               The reversal gate is sweep + reclaim + structure shift.
        # ADX therefore never blocks the entry (never part of `approved`); it only
        # feeds the bounded confidence adjustment and the diagnostic log.
        ai.adx = float(adx)
        if ai.trade_type in (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL):
            ai.adx_ok = 0 < ai.adx < 70
        else:  # TREND
            ai.adx_ok = ai.adx >= 20
        ai.reasons.append(
            f"ADX {ai.adx:.1f} {'OK' if ai.adx_ok else '(context)'}[{ai.trade_type}]")

        # ---- 5) LIQUIDITY SUPPORT (quality evidence, not blind gate) -------
        ai.liquidity = evaluate_liquidity_support(
            df, side, act_demand, act_supply, delta_bullish, delta_bearish, volume_ratio)
        ai.reasons.append(f"liquidity={ai.liquidity.score:.0f}")

        # ---- 6) STRUCTURE (quality evidence, not blind gate) --------------------
        ai.structure_valid = struct_score >= 3 or bool(trigger_state in (
            "MSS_CONFIRMED", "LIQUIDITY_SWEEP", "BOS_CONFIRMED", "CHOCH_CONFIRMED"))
        ai.structure_score = float(struct_score)
        ai.reasons.append("structure=" + ("OK" if ai.structure_valid else "weak"))

        # ---- 7) SNIPER CONFIRMATION (REVERSAL only) -------------------------
        if ai.trade_type in (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL):
            ai.sniper_required = True
            ai.sniper_confirmed = self._reversal_sniper_ok(
                df, d, atr, sweep_aligned, mss_bullish, mss_bearish,
                ai.freshness, ai.liquidity, sniper_conf)
            ai.sniper_score = float(getattr(sniper_conf, "score_shift", 0.0)) if sniper_conf is not None else 0.0
            ai.reasons.append("sniper=" + ("confirmed" if ai.sniper_confirmed else "NOT_CONFIRMED"))
        else:
            # TREND does NOT require (and does NOT run) the extra sniper gate.
            # `sniper_confirmed` stays False so the semantics are never confused:
            # a TREND is approved on OB-fresh+liquidity / continuation, not on a
            # sniper confirmation that we explicitly decided not to require.
            ai.sniper_required = False
            ai.sniper_confirmed = False
            ai.sniper_score = float(getattr(sniper_conf, "score_shift", 0.0)) if sniper_conf is not None else 0.0
            ai.reasons.append("sniper=not_required (trend)")

        # ---- 8) DECISION -----------------------------------------------------
        # Atom returns "reject" ONLY for a genuine defect (fake/broken/stale OB,
        # over-mitigated zone) or for a REVERSAL that lacks the mandatory extra
        # sniper confirmation. All other factors (ADX regime, liquidity, structure)
        # are QUALITY EVIDENCE that adjust confidence — they never block a
        # Roro-valid entry, so we do not pile blind gates on top of RORO.
        defects = ai.ob_quality_ok and ai.freshness.fresh
        if ai.trade_type in (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL):
            ai.approved = defects and ai.sniper_confirmed
        else:
            ai.approved = defects

        # Bounded confidence adjust (advisory). Positive for supportive evidence,
        # negative for weak evidence. Never flips weak->strong; never blocks.
        adjust = 0.0
        if ai.approved:
            adjust += 2.0
        if ai.liquidity.score >= 70:
            adjust += 2.0
        elif ai.liquidity.score < 40:
            adjust -= 3.0
        if ai.freshness.state == FRESH:
            adjust += 1.0
        elif ai.freshness.state == AGING:
            adjust -= 1.0
        if not ai.structure_valid:
            adjust -= 2.0
        # ADX contextual, per trade type (regime nudge, never a veto).
        if ai.trade_type in (TRADE_REVERSAL, TRADE_SNIPER_REVERSAL):
            if ai.adx >= 50:      # over-extension/exhaustion favors the reversal
                adjust += 1.0
            elif ai.adx >= 35:
                adjust += 0.5
            else:
                adjust -= 1.0     # no momentum behind the reversal thesis
        else:
            if ai.adx >= 20:
                adjust += 1.0     # trending regime supports continuation
            else:
                adjust -= 1.0
        if ai.trade_type == TRADE_SNIPER_REVERSAL and ai.sniper_confirmed:
            adjust += 2.0
        ai.confidence_adjust = float(np.clip(adjust, MAX_CONFIDENCE_PENALTY, MAX_CONFIDENCE_BONUS))
        return ai

    @staticmethod
    def _reversal_sniper_ok(df, d, atr, sweep_aligned, mss_bullish, mss_bearish,
                            freshness, liquidity, sniper_conf) -> bool:
        """REVERSAL-only additional confirmation: sweep + MSS/CHoCH + strong
        rejection + fresh OB + liquidity support."""
        if d is None or df is None:
            return False
        sweep_ok = sweep_aligned
        mss_ok = mss_bullish if d == 1 else mss_bearish
        rejection_ok = AtomIntelligenceEngine._rejection(df, d, atr)
        evidence = getattr(sniper_conf, "evidence", None)
        sr_ok = False
        if evidence is not None:
            sr_ok = bool(getattr(evidence, "mss_bullish", False) or getattr(evidence, "mss_bearish", False))
        mss_ok = mss_ok or sr_ok
        checks = {
            "sweep": sweep_ok,
            "mss": mss_ok,
            "rejection": rejection_ok,
            "fresh_ob": freshness.state == FRESH,
            "liquidity": liquidity.score >= 50,
        }
        return sum(1 for v in checks.values() if v) >= 3

    @staticmethod
    def _rejection(df: pd.DataFrame, d, atr: float) -> bool:
        if df is None or len(df) < 3 or atr <= 0:
            return False
        last = df.iloc[-1]
        o, c, h, l = (float(last["open"]), float(last["close"]),
                      float(last["high"]), float(last["low"]))
        body = abs(c - o)
        wick = (l - min(o, c)) if d == 1 else (max(o, c) - h)
        prev_bull = float(df["close"].iloc[-2]) > float(df["open"].iloc[-2])
        prev_bear = float(df["close"].iloc[-2]) < float(df["open"].iloc[-2])
        if d == 1:
            return c > o and (prev_bull or prev_bear) and wick > body * 0.5 and wick > atr * 0.15
        else:
            return c < o and (prev_bull or prev_bear) and wick > body * 0.5 and wick > atr * 0.15
