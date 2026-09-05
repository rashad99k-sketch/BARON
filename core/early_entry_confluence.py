"""Early Entry Confluence Intelligence layer for Atom.

This layer answers a question the causal Roro engine does not answer on its
own: *is this the START of a move out of a fresh, liquidity-backed zone, or is
price already chasing a move that has run away?*

It is advisory ONLY. It reuses the indicator math already ported in
``core.sniper_enrichment`` (the "Ultimate Sniper Pro + LNL Trend System"
Demand/Supply boxes, EMA stack, ADX, VWAP, chandelier, delta volume, SMC
sweep/MSS/SFP, FVG) and adds the EARLY-ENTRY dimension on top:

  * ``distance_from_zone``  -- how far price sits from the causal zone,
    measured in ATR units with the boundaries derived from the envelope the
    zone itself implies (the indicator extends every box by 1 x ATR(200), so
    that ATR-derived width is the natural "inside / edge / far" ruler).
  * ``phase``               -- FIRST (early first confluence), DEVELOPING
                              (move started to confirm) or LATE (too far).
  * ``rf_aligned``          -- existing Range Filter direction agrees (RF is
                              part of the confluence, never a separate veto).
  * ``confidence`` (0..100) -- weighted evidence, and ``action``
                              EARLY_ENTRY / WAIT / SITOUT (LATE).

Guarantees
----------
  * all computation is over *closed* candles only (no look-ahead / no repaint);
  * this layer is advisory: it raises/lowers confidence, it NEVER blocks a
    Roro-valid entry by itself and it NEVER turns a weak setup into a strong
    one (no invented hard gates -- exactly the "lower confidence, don't add a
    gate" philosophy);
  * the causal Order Block / Zone stays the source of truth; indicators only
    confirm it, never create it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np
import pandas as pd

from core.sniper_enrichment import (
    SniperEnrichmentConfig,
    SniperEnrichmentEngine,
    normalize_side as _norm_side,
)
from core.sniper_enrichment import LONG as _LONG, SHORT as _SHORT

PHASE_FIRST = "FIRST"
PHASE_DEVELOPING = "DEVELOPING"
PHASE_LATE = "LATE"

ACTION_EARLY_ENTRY = "EARLY_ENTRY"
ACTION_WAIT = "WAIT"
ACTION_LATE = "SITOUT_LATE"

# Distance thresholds (in ATR units). The indicator extends every SR box by
# box_width * ATR(200); that ATR-derived envelope is the natural ruler for
# "inside the zone vs left it". These are expressed in normalized ATR units so
# they adapt to every symbol/timeframe instead of using arbitrary fixed prices.
DIST_INSIDE = 0.0      # inside the zone envelope
DIST_EDGE = 1.0        # within one base ATR of the zone edge -> EARLY
DIST_FAR = 2.5         # beyond this -> LATE (move already left the zone)

# Confidence contributions (advisory, bounded). Weights follow the design: the
# ZONE is the location, LIQUIDITY/FLOW the participation, the INDICATOR
# confluence is an early trigger and RF/STRUCTURE confirm. No single factor is
# mandatory, so we never force the engine to chase ten confirmations.
WEIGHT_ZONE = 20.0
WEIGHT_LIQUIDITY = 18.0
WEIGHT_FLOW = 14.0
WEIGHT_CONFLUENCE = 16.0
WEIGHT_RF = 12.0
WEIGHT_STRUCTURE = 10.0
WEIGHT_DISTANCE = 10.0
MAX_CONFIDENCE = 100.0

# Bounded advisory bonus/penalty applied to the engine's confluence_bonus
# (capped there at 5.0). Kept small: advisory only, never a gate.
MAX_SCORE_SHIFT = 5.0


@dataclass
class EarlyEntryEvidence:
    """The full per-candidate early-entry evidence bag.

    Mirrors the canonical schema required by the project (evidence + logs):
      detected, direction, phase, zone_type, zone_freshness, liquidity_support,
      crossover_stage, indicators_aligned, structure_aligned, displacement,
      distance_from_zone, confidence.
    """
    detected: bool = False
    direction: str = "NONE"                 # LONG / SHORT / NONE
    phase: str = PHASE_LATE
    zone_type: str = "NONE"                 # DEMAND / SUPPLY / OB
    zone_freshness: str = "UNKNOWN"
    liquidity_support: str = "UNKNOWN"
    liquidity_event: str = "NONE"           # SWEEP / STOP_HUNT / FAKE_BREAK ...
    crossover_stage: str = "NONE"           # FIRST / DEVELOPING / LATE / NONE
    indicators_aligned: bool = False
    ema_stack_aligned: bool = False
    adx_strong: bool = False
    # VWeb (the yellow line = ta.vwap(hlc3) in the supplied Pine source) is the
    # first and most local confluence: price on the favourably-aligned side of
    # the yellow line. Kept under a distinct name per design so it is never
    # mistaken for ADX/EDX.
    vweb_aligned: bool = False
    adx_aligned: bool = False
    vwap_aligned: bool = False
    # Zone role from the actual SR-box logic (hold vs breakout / role-flip:
    # resistance broken -> support, support broken -> resistance).
    zone_role: str = "NONE"                 # HOLD / BREAKOUT_UP / BREAKOUT_DN / ROLE_FLIP / NONE
    # Convergence strength across VWeb + Sniper/Trend EMA + ADX (0..4).
    # NOT "all must cross": it sums how many of the independent indicator reads
    # agree, so the earliest first-convergence still scores before a full stack.
    convergence_score: int = 0
    volume_flow: str = "NONE"               # EXPANSION / NORMAL / LOW
    flow_bullish: bool = False
    flow_bearish: bool = False
    structure_aligned: bool = False
    mss_aligned: bool = False
    displacement: str = "NONE"              # STARTING / CONFIRMED / NONE
    rf: str = "NONE"                        # BUY / SELL / NONE
    rf_aligned: bool = False
    distance_from_zone: float = 999.0       # in ATR units (0 == inside zone)
    distance_from_zone_atr: float = 0.0
    in_zone: bool = False
    confidence: float = 0.0
    action: str = ACTION_LATE
    reasons: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class EarlyEntryResult:
    """Composite advisory output for wiring into the engine."""

    side: int = _LONG
    evidence: EarlyEntryEvidence = field(default_factory=EarlyEntryEvidence)
    confidence: float = 0.0
    score_shift: float = 0.0
    action: str = ACTION_LATE
    log_line: str = ""

    def to_dict(self) -> dict:
        return {
            "side": "LONG" if self.side == _LONG else "SHORT",
            "confidence": round(self.confidence, 1),
            "score_shift": round(self.score_shift, 3),
            "action": self.action,
            "evidence": self.evidence.to_dict(),
        }


def evaluate_zone_distance(price: float, zone_low: Optional[float],
                           zone_high: Optional[float], atr: float,
                           envelope_atr: Optional[float] = None) -> float:
    """Distance of ``price`` from the causal zone, in ATR units.

    0.0 means price is inside the zone envelope. The envelope width is taken
    from the ATR the zone itself implies (the indicator marks every box with a
    box_width * ATR(200) extension); we use ``envelope_atr`` when provided and
    fall back to 1.0 x base ATR for a sane ruler.
    """
    if zone_low is None or zone_high is None or atr is None or atr <= 0:
        return float("inf")
    lo = float(zone_low)
    hi = float(zone_high)
    env = float(envelope_atr) if (envelope_atr and envelope_atr > 0) else float(atr)
    if lo <= price <= hi:
        return DIST_INSIDE
    if price > hi:
        return round(float(price - hi) / (float(atr) if atr > 0 else 1.0), 4)
    return round(float(lo - price) / (float(atr) if atr > 0 else 1.0), 4)


def classify_distance_phase(distance: float) -> str:
    """FIRST / DEVELOPING / LATE from the ATR-unit distance (no gate on input)."""
    if not np.isfinite(distance):
        return PHASE_LATE
    if distance <= DIST_EDGE:
        return PHASE_FIRST
    if distance <= DIST_FAR:
        return PHASE_DEVELOPING
    return PHASE_LATE


class EarlyEntryConfluenceEngine:
    """Advisory early-entry confluence: indicator evidence + zone distance + RF.

    Reuses the faithful indicator math from ``SniperEnrichmentEngine`` and adds
    the early-entry dimension (distance_from_zone, phase, RF alignment, action).
    It never blocks a Roro-valid entry -- it only advises confidence and phase.
    """

    def __init__(self, config: Optional[SniperEnrichmentConfig] = None):
        self._sniper = SniperEnrichmentEngine(
            config if config is not None else SniperEnrichmentConfig.from_env())

    def _base_atr(self, df: pd.DataFrame) -> float:
        if df is None or len(df) < 15:
            return 0.0
        from core.sniper_enrichment import _atr
        return float(_atr(df, 14).iloc[-1])

    def evaluate(self, df: pd.DataFrame, side,
                 zone_low: Optional[float] = None,
                 zone_high: Optional[float] = None,
                 zone_type: str = "OB",
                 zone_freshness: str = "UNKNOWN",
                 liquidity_support: str = "UNKNOWN",
                 rf_signal: Optional[str] = None,
                 symbol: str = "UNKNOWN") -> EarlyEntryResult:
        """Compute early-entry confluence for ``side``.

        ``rf_signal`` is the existing Range Filter direction ("BUY"/"SELL"/
        None) -- passed in so this layer stays decoupled from the heavy engine
        import and stays trivially unit-testable.
        """
        d = _norm_side(side)
        if df is None or not isinstance(df, pd.DataFrame) or len(df) < 40 or d is None:
            ev = EarlyEntryEvidence(direction=_norm_dir(d))
            ev.reasons.append("insufficient_data")
            return EarlyEntryResult(side=d, evidence=ev, action=ACTION_LATE)

        conf = self._sniper.analyze(df, symbol, side=d)
        ev = EarlyEntryEvidence(
            direction=_norm_dir(d),
            zone_type=_zone_kind(zone_type, d),
            zone_freshness=(zone_freshness or "UNKNOWN"),
            liquidity_support=(liquidity_support or "UNKNOWN"),
        )
        s = conf.evidence
        atr = self._base_atr(df)
        price = float(df["close"].iloc[-1])

        # --- Zone location / distance (the core early-entry measure) ----------
        ev.distance_from_zone = evaluate_zone_distance(
            price, zone_low, zone_high, atr,
            envelope_atr=float(atr or 1.0))
        ev.in_zone = ev.distance_from_zone <= DIST_INSIDE
        ev.distance_from_zone_atr = ev.distance_from_zone

        # --- Liquidity event (sweep / stop-hunt / fake-break) -----------------
        # The indicator tags a sweep / X (wick through a level then close back)
        # and an SFP (reclaim). We detect it BOTH from the sniper ported flags
        # and directly from the candle structure near the zone so a recovery
        # off a swept low is recognised even when the final close is back above.
        liq_event = _liquidity_event(s, d, df, zone_low, zone_high)
        ev.liquidity_event = liq_event["kind"]
        ev.liquidity_support = _liq_support(s, d, df, zone_low, zone_high, price)

        # --- Volume / flow ----------------------------------------------------
        if d == _LONG:
            ev.flow_bullish = bool(s.delta_bullish)
            ev.flow_bearish = bool(s.delta_bearish)
        else:
            ev.flow_bullish = bool(s.delta_bullish)
            ev.flow_bearish = bool(s.delta_bearish)
        ev.volume_flow = "EXPANSION" if s.volume_expansion else ("NORMAL" if s.volume_ratio > 0 else "LOW")

        # --- Indicator confluence (early trigger, NOT a gate) -----------------
        ema_up = s.ema_stack_bullish
        ema_dn = s.ema_stack_bearish
        ev.ema_stack_aligned = bool(ema_up if d == _LONG else ema_dn)
        ev.adx_strong = bool(s.adx > 20 and (s.adx_bullish if d == _LONG else s.adx_bearish))
        ev.indicators_aligned = bool(ev.ema_stack_aligned or ev.adx_strong)
        ev.mss_aligned = bool((s.mss_bullish and d == _LONG) or (s.mss_bearish and d == _SHORT))
        ev.structure_aligned = bool(ev.mss_aligned or ev.indicators_aligned or s.vwap_aligned_buy if d == _LONG else (ev.mss_aligned or ev.indicators_aligned or s.vwap_aligned_sell))

        # VWeb (yellow line) + VWAP + ADX alignment -- the first confluence.
        if d == _LONG:
            ev.vweb_aligned = bool(s.vwap_aligned_buy)
            ev.vwap_aligned = bool(s.vwap_aligned_buy)
        else:
            ev.vweb_aligned = bool(s.vwap_aligned_sell)
            ev.vwap_aligned = bool(s.vwap_aligned_sell)
        ev.adx_aligned = bool(s.adx > 20 and (s.adx_bullish if d == _LONG else s.adx_bearish))

        # Convergence strength: VWeb + EMA stack + ADX agreeing. Advisory only,
        # never a hard gate -- the earliest first-transfer counts (>=1), a full
        # stack is the strongest (==3). We deliberately do NOT require "all".
        ev.convergence_score = int(bool(ev.vweb_aligned)) \
            + int(bool(ev.ema_stack_aligned)) \
            + int(bool(ev.adx_aligned))

        # SR-box role / health (resistance as support, support as resistance).
        ev.zone_role = _zone_role(s, d, df, zone_low, zone_high)

        # Displacement = strong high-volume move out of the zone.
        disp_starting = _displacement_starting(s, d)
        ev.displacement = "STARTING" if disp_starting else "NONE"

        # --- RF alignment (part of confluence, never a separate veto) ---------
        rf = (rf_signal or "").strip().upper()
        ev.rf = rf if rf in ("BUY", "SELL") else "NONE"
        ev.rf_aligned = bool((rf == "BUY" and d == _LONG) or (rf == "SELL" and d == _SHORT))

        # --- Phase + crossover stage ------------------------------------------
        distance_phase = classify_distance_phase(ev.distance_from_zone)
        # A confluence genuinely "just started" when we are near/inside the zone
        # AND an early trigger fired; otherwise it degrades to developing/late.
        first_trigger = bool(ev.liquidity_event in ("SWEEP", "STOP_HUNT", "FAKE_BREAK")
                             or ev.indicators_aligned or ev.mss_aligned or disp_starting)
        if distance_phase == PHASE_LATE:
            ev.phase = PHASE_LATE
            ev.crossover_stage = "LATE"
        elif first_trigger and ev.distance_from_zone <= DIST_EDGE:
            ev.phase = PHASE_FIRST
            ev.crossover_stage = "FIRST"
        else:
            ev.phase = PHASE_DEVELOPING
            ev.crossover_stage = "DEVELOPING"

        # --- Confidence (weighted evidence, 0..100) ---------------------------
        conf_score = self._confidence(ev, d)
        ev.confidence = conf_score

        # --- Action -----------------------------------------------------------
        action = _action(ev, d)
        ev.action = action

        # --- Bounded advisory score shift -------------------------------------
        shift = _score_shift(ev, d)
        log_line = _log_line(ev)

        result = EarlyEntryResult(
            side=d, evidence=ev, confidence=conf_score,
            score_shift=shift, action=action, log_line=log_line)
        return result

    def _confidence(self, ev: EarlyEntryEvidence, d: int) -> float:
        score = 0.0
        reasons = []

        def _have_zone():
            return ev.zone_freshness not in ("UNKNOWN", "STALE", "OVER_MITIGATED", "BROKEN", "FAKE")

        if _have_zone() and ev.zone_freshness == "FRESH":
            score += WEIGHT_ZONE
            reasons.append("zone_fresh")
        elif _have_zone():
            score += WEIGHT_ZONE * 0.5
            reasons.append("zone_valid")
        else:
            reasons.append("zone_poor")

        # Liquidity: real participation near the zone, not random touch.
        if ev.liquidity_event in ("SWEEP", "STOP_HUNT", "FAKE_BREAK"):
            score += WEIGHT_LIQUIDITY
            reasons.append(ev.liquidity_event)
        elif ev.liquidity_support == "STRONG":
            score += WEIGHT_LIQUIDITY * 0.6
            reasons.append("liquidity_strong")

        # Volume / flow participation.
        if ev.volume_flow == "EXPANSION" and (ev.flow_bullish and d == _LONG or ev.flow_bearish and d == _SHORT):
            score += WEIGHT_FLOW
            reasons.append("flow_expansion")
        elif ev.volume_flow == "EXPANSION":
            score += WEIGHT_FLOW * 0.5
            reasons.append("volume_expansion")

        # Indicator confluence (early trigger, not a gate).
        if ev.indicators_aligned:
            score += WEIGHT_CONFLUENCE
            reasons.append("confluence")
        elif ev.ema_stack_aligned or ev.adx_strong:
            score += WEIGHT_CONFLUENCE * 0.5
            reasons.append("confluence_partial")

        # RF alignment.
        if ev.rf_aligned:
            score += WEIGHT_RF
            reasons.append("rf_aligned")

        # Structure confirmation.
        if ev.structure_aligned:
            score += WEIGHT_STRUCTURE
            reasons.append("structure")
        if ev.mss_aligned:
            score += WEIGHT_STRUCTURE * 0.5
            reasons.append("mss")

        # Distance: being at/inside the zone is the point of early entry.
        if ev.distance_from_zone <= DIST_EDGE:
            score += WEIGHT_DISTANCE
            reasons.append("near_zone")
        elif ev.distance_from_zone <= DIST_FAR:
            score += WEIGHT_DISTANCE * 0.5
            reasons.append("moving_away")
        # beyond DIST_FAR contributes nothing (late)

        c = float(np.clip(score, 0.0, MAX_CONFIDENCE))
        ev.reasons.extend(reasons)
        return round(c, 1)


# SmartZoneCrossoverIntelligence is the surfaced name of the same advisory
# engine. It reuses the faithful Pine-derived indicator math (VWeb/VWAP, ADX,
# EMA stack, SR boxes) from sniper_enrichment and never re-writes RORO entry /
# execution / risk. Keeping one engine avoids drift between two orchestrators.
SmartZoneCrossoverIntelligence = EarlyEntryConfluenceEngine


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _norm_dir(d) -> str:
    return "LONG" if d == _LONG else ("SHORT" if d == _SHORT else "NONE")


def _zone_kind(zone_type: str, d: int) -> str:
    zt = (zone_type or "OB").strip().upper()
    if zt in ("DEMAND", "SUPPLY"):
        return zt
    return "DEMAND" if d == _LONG else "SUPPLY"


def _zone_edges(zone_low, zone_high):
    if zone_low is None or zone_high is None or not np.isfinite(zone_low) or not np.isfinite(zone_high):
        return None, None
    return float(zone_low), float(zone_high)


def _liquidity_event(s, d: int, df=None, zone_low=None, zone_high=None) -> dict:
    """Classify the liquidity event for the trade side.

    Uses the sniper-ported flags first, then the raw candle structure near the
    zone the way the indicator tags X (wick through a level, close back) and
    SFP (reclaim). A BUY requires sell-side liquidity taken below the demand
    zone; a SELL requires buy-side liquidity taken above the supply zone.
    """
    if d == _LONG and (s.sweep_buy or s.sfp_buy):
        return {"kind": "STOP_HUNT" if s.sfp_buy else "SWEEP"}
    if d == _SHORT and (s.sweep_sell or s.sfp_sell):
        return {"kind": "STOP_HUNT" if s.sfp_sell else "SWEEP"}
    if d == _LONG and s.mss_bullish:
        return {"kind": "SWEEP"}
    if d == _SHORT and s.mss_bearish:
        return {"kind": "SWEEP"}
    # ---- Direct candle-structure sweep detection (indicator X / SFP) --------
    # Scan a short *recent* window (closed candles only) for a liquidity sweep:
    # a low/high that pierced the zone envelope then reclaimed it. This matches
    # the indicator tagging X/SFP while price is still near the zone, and is
    # still purely closed-candle (no look-ahead / no repaint).
    SWEEP_LOOKBACK = 3
    if df is None or len(df) < 2:
        return {"kind": "NONE"}
    zl, zh = _zone_edges(zone_low, zone_high)
    if zl is None or zh is None:
        return {"kind": "NONE"}
    window = df.iloc[-SWEEP_LOOKBACK:]
    last_close = float(df["close"].iloc[-1])
    recent_low = float(window["low"].min())
    recent_high = float(window["high"].max())
    prev_low_close = float(df["close"].iloc[-SWEEP_LOOKBACK - 1]) if len(df) > SWEEP_LOOKBACK else last_close
    if d == _LONG:
        # Sell-side liquidity taken below the demand zone and reclaimed.
        if recent_low <= zl and last_close > zl:
            return {"kind": "SWEEP"}
        if prev_low_close < zl and last_close > zl:
            return {"kind": "FAKE_BREAK"}
    else:
        # Buy-side liquidity taken above the supply zone and reclaimed.
        if recent_high >= zh and last_close < zh:
            return {"kind": "SWEEP"}
        if prev_low_close > zh and last_close < zh:
            return {"kind": "FAKE_BREAK"}
    return {"kind": "NONE"}


def _liq_support(s, d: int, df=None, zone_low=None, zone_high=None, price=None) -> str:
    # "Near the zone" is decided by the zone envelope when available, not only
    # by the sniper box flags, so participation right at the causal zone counts.
    near = (s.in_demand_box and d == _LONG) or (s.in_supply_box and d == _SHORT)
    if price is not None:
        zl, zh = _zone_edges(zone_low, zone_high)
        if zl is not None and zh is not None and zl <= price <= zh:
            near = True
    if near and s.volume_expansion:
        return "STRONG"
    if near:
        return "PRESENT"
    return "WEAK"


def _zone_role(s, d: int, df=None, zone_low=None, zone_high=None) -> str:
    """Map the actual Pine SR-box role logic (hold vs breakout / role-flip).

    From the supplied source:
      res_holds   = crossunder(high, resistanceLevel)      # rejected below R
      brekout_res = crossover(low,  resistanceLevel_1)     # closed above R -> R becomes support
      sup_holds   = crossover(low,  supportLevel)          # defended above D
      brekout_sup = crossunder(high, supportLevel_1)       # closed below D -> D becomes resistance

    For a LONG we care about the demand/support box: it HOLDS if price defends
    above it, and we get a bullish role-flip when price reclaimed above a level
    that was previously resistance (brekout_res => res_is_sup). For a SHORT the
    symmetric supply story applies. Everything is closed-candle only.
    """
    if df is None or len(df) < 3:
        return "NONE"
    price = float(df["close"].iloc[-1])
    window = df.iloc[-3:]
    recent_low = float(window["low"].min())
    recent_high = float(window["high"].max())
    zl, zh = _zone_edges(zone_low, zone_high)
    if d == _LONG:
        if zh is not None and price > zh and recent_low > zh:
            return "ROLE_FLIP"      # reclaim above a broken resistance (R -> D)
        if zl is not None and recent_low >= zl:
            return "HOLD"           # support defended
    else:
        if zl is not None and price < zl and recent_high < zl:
            return "ROLE_FLIP"      # reclaim below a broken support (D -> R)
        if zh is not None and recent_high <= zh:
            return "HOLD"           # resistance defended
    return "NONE"


def _displacement_starting(s, d: int) -> bool:
    # A strong candle with expansion + a rejection/flow flip at the zone edge
    # is treated as displacement starting (the exhaustion detector captures a
    # strong + expansion candle; reuse it as a directional displacement hint).
    sig = s.exhaustion_sell if d == _LONG else s.exhaustion_buy
    return bool(sig and s.volume_expansion)


def _action(ev: EarlyEntryEvidence, d: int) -> str:
    if ev.phase == PHASE_LATE:
        return ACTION_LATE
    if ev.phase == PHASE_FIRST and ev.confidence >= 60:
        return ACTION_EARLY_ENTRY
    if ev.phase == PHASE_FIRST:
        return ACTION_WAIT
    # DEVELOPING
    if ev.confidence >= 75 and ev.rf_aligned:
        return ACTION_EARLY_ENTRY
    return ACTION_WAIT


def _score_shift(ev: EarlyEntryEvidence, d: int) -> float:
    # Advisory, bounded. Positive for an early high-confidence read near the
    # zone, negative only when clearly late or the zone itself is poor.
    if ev.phase == PHASE_LATE or ev.zone_freshness in ("STALE", "OVER_MITIGATED", "BROKEN", "FAKE"):
        bonus = -3.0 if ev.confidence < 40 else -1.0
        return round(float(np.clip(bonus, -5.0, 5.0)), 3)
    bonus = (ev.confidence - 50.0) / 10.0
    return round(float(np.clip(bonus, -2.5, MAX_SCORE_SHIFT)), 3)


def _log_line(ev: EarlyEntryEvidence) -> str:
    line = (
        "[ATOM-EARLY] TYPE={dir} ZONE={zone} FRESHNESS={fresh} "
        "LIQUIDITY={liq} EVENT={evt} VOLUME={vol} CONFLUENCE={stage} "
        "PHASE={phase} STRUCTURE={struct} DISPLACEMENT={disp} RF={rf} "
        "DIST={dist:.2f}ATR CONF={conf:.0f} ACTION={act}".format(
            dir=ev.direction, zone=ev.zone_type, fresh=ev.zone_freshness,
            liq=ev.liquidity_support, evt=ev.liquidity_event, vol=ev.volume_flow,
            stage=ev.crossover_stage, phase=ev.phase,
            struct="ALIGNED" if ev.structure_aligned else "NA",
            disp=ev.displacement, rf=ev.rf, dist=ev.distance_from_zone,
            conf=ev.confidence, act=ev.action)
    )
    if ev.phase == PHASE_LATE:
        line += " REASON=TOO_FAR_FROM_ZONE"
    return line


def analyze_early_entry(df: pd.DataFrame, side, zone_low=None, zone_high=None,
                        zone_type="OB", zone_freshness="UNKNOWN",
                        liquidity_support="UNKNOWN", rf_signal=None,
                        symbol="UNKNOWN") -> EarlyEntryResult:
    """Convenience entry point (mirrors analyze_sniper style)."""
    eng = EarlyEntryConfluenceEngine()
    return eng.evaluate(df, side, zone_low=zone_low, zone_high=zone_high,
                        zone_type=zone_type, zone_freshness=zone_freshness,
                        liquidity_support=liquidity_support, rf_signal=rf_signal,
                        symbol=symbol)
