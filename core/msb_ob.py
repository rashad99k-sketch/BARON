"""Isolated MSB-OB structural evidence engine.

Pure, side-effect-free port of the Pine Script semantics for
"Market Structure Break & Order Block" (MSB-OB, EmreKb). This module MUST
NOT emit trade signals. It computes closed-candle structural evidence:
zigzag swing extraction (to_up/to_down), trend state, fib_factor-gated
market breaks (bullish/bearish MSB), order-block zones (Bu-OB/Be-OB) and
breaker/mitigation block zones (Bu-BB & Bu-MB / Be-BB & Be-MB), plus
zone lifecycle statuses (ACTIVE/TOUCHED/MITIGATING/INVALIDATED/EXPIRED).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import pandas as pd

DEFAULT_ZIGZAG_LEN = 9
DEFAULT_FIB_FACTOR = 0.33
# Pine fills arrays of 5 with 0 so index access is safe on empty state; we
# mirror that behavior without needing arrays.
LONG = 1
SHORT = -1

STATUS_ACTIVE = "ACTIVE"
STATUS_TOUCHED = "TOUCHED"
STATUS_MITIGATING = "MITIGATING"
STATUS_INVALIDATED = "INVALIDATED"
STATUS_EXPIRED = "EXPIRED"


@dataclass
class SwingPoint:
    price: float
    index: int  # bar index of swing formation


@dataclass
class MSBEvent:
    direction: int  # LONG for bullish MSB, SHORT for bearish MSB
    price: float    # l0 for bullish MSB, h0 for bearish MSB
    index: int      # bar index where the break was confirmed
    fib_factor: float


@dataclass
class InstitutionalZone:
    symbol: str
    side: int                    # LONG for bullish, SHORT for bearish
    zone_type: str               # OB | BB | MB
    top: float
    bottom: float
    created_at: int              # bar index
    msb_direction: int
    msb_price: float
    swing_high: float
    swing_low: float
    fib_factor: float
    freshness: int = 0           # bars since creation (0 = just formed)
    touch_count: int = 0
    displacement_score: float = 0.0   # max move from zone after creation in ATR units
    volume_score: float = 0.0         # volume ratio vs rolling baseline at creation
    structure_score: float = 0.0      # kept fixed 1.0 for now (structure tag captured)
    liquidity_context: str = "NONE"   # optional free-text hook; default NONE
    zone_strength: float = 0.0
    status: str = STATUS_ACTIVE

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": "LONG" if self.side == LONG else "SHORT",
            "zone_type": self.zone_type,
            "top": self.top,
            "bottom": self.bottom,
            "created_at": self.created_at,
            "msb_direction": "BULL" if self.msb_direction == LONG else "BEAR",
            "msb_price": self.msb_price,
            "swing_high": self.swing_high,
            "swing_low": self.swing_low,
            "fib_factor": self.fib_factor,
            "freshness": self.freshness,
            "touch_count": self.touch_count,
            "displacement_score": self.displacement_score,
            "volume_score": self.volume_score,
            "liquidity_context": self.liquidity_context,
            "zone_strength": self.zone_strength,
            "status": self.status,
        }


class MSBOBEngine:
    """Reprocesses a dataframe and extracts structural zones.

    Runs wholly deterministically: on each call, it replays the Pine state
    machine over all closed candles. Only closed candles are consulted.
    """

    def __init__(self, zigzag_len: int = DEFAULT_ZIGZAG_LEN, fib_factor: float = DEFAULT_FIB_FACTOR,
                 max_zones: int = 5):
        if zigzag_len < 2:
            raise ValueError("zigzag_len must be >= 2")
        self.zigzag_len = int(zigzag_len)
        self.fib_factor = float(fib_factor)
        self.max_zones = int(max_zones)

    # ---------- Public entry point ----------
    def analyze(self, df: pd.DataFrame, symbol: str = "UNKNOWN") -> Dict:
        """Scan closed candles and return structured evidence."""
        result: Dict = {"zones": [], "msb_events": [], "market": None, "error": None}
        if df is None or len(df) < max(self.zigzag_len + 2, 12):
            result["error"] = "DATA_UNAVAILABLE"
            return result
        highs = list(df["high"].values)
        lows = list(df["low"].values)
        opens = list(df["open"].values)
        closes = list(df["close"].values)
        n = len(df)
        # Pine: to_up = high >= ta.highest(len); to_down = low <= ta.lowest(len)
        to_up = [highs[i] >= max(highs[max(0, i - self.zigzag_len + 1): i + 1]) for i in range(n)]
        to_down = [lows[i] <= min(lows[max(0, i - self.zigzag_len + 1): i + 1]) for i in range(n)]
        trend: List[int] = [LONG]  # Pine initial value 1 unless reversal happened
        high_points: List[SwingPoint] = []
        low_points: List[SwingPoint] = []
        market: List[int] = [LONG]
        zones: List[InstitutionalZone] = []
        highs_included = highs.copy()
        lows_included = lows.copy()
        for i in range(1, n):
            last_trend_up_since = self._barssince(to_up, i - 1) or 1
            last_trend_down_since = self._barssince(to_down, i - 1) or 1
            low_window = lows[max(0, i - last_trend_up_since): i + 1]
            high_window = highs[max(0, i - last_trend_down_since): i + 1]
            low_val = min(low_window)
            high_val = max(high_window)
            low_idx = max(0, i - last_trend_up_since) + low_window.index(low_val)
            high_idx = max(0, i - last_trend_down_since) + high_window.index(high_val)
            flipped = trend[-1]
            if trend[-1] == LONG and to_down[i - 1]:
                flipped = SHORT
                low_points.append(SwingPoint(low_val, low_idx))
            elif trend[-1] == SHORT and to_up[i - 1]:
                flipped = LONG
                high_points.append(SwingPoint(high_val, high_idx))
            else:
                flipped = trend[-1]
            trend.append(flipped)
            # Swing context h0/h1 (last two highs) l0/l1 (last two lows)
            h0 = high_points[-1].price if high_points else None
            h1 = high_points[-2].price if len(high_points) > 1 else None
            l0 = low_points[-1].price if low_points else None
            l1 = low_points[-2].price if len(low_points) > 1 else None
            if (h0 is None or l0 is None):
                market.append(market[-1])
                continue
            new_market = market[-1]
            if h1 is not None and l1 is not None:
                if market[-1] == LONG and l0 < l1:
                    new_market = SHORT if l0 < l1 - abs(h0 - l1) * self.fib_factor else LONG
                elif market[-1] == SHORT and h0 > h1:
                    new_market = LONG if h0 > h1 + abs(h1 - l0) * self.fib_factor else SHORT
            if new_market != market[-1]:
                if (l0 is None) or (h0 is None):
                    # Pine cannot form a zone without swings; skip zone emission
                    market.append(new_market)
                    continue
                msb = MSBEvent(direction=new_market, price=l0 if new_market == LONG else h0,
                               index=i, fib_factor=self.fib_factor)
                result["msb_events"].append(msb)
            market.append(new_market)
            if new_market == market[-2]:
                # No market break on this bar: no new structural zones
                continue
            # Zones: Bullish MSB → Bu-OB (last bearish candle in upswing→downswing window)
            # Zone boundaries are taken from the candle's high/low (top/bottom).
            if new_market == LONG and len(high_points) > 0 and len(low_points) > 0:
                h1i = high_points[-2].index if len(high_points) > 1 else high_points[-1].index
                l0i = low_points[-1].index
                # Pine walks h1i→l0i and takes the LAST bearish candle (open>close).
                zone = None
                for j in range(min(h1i, l0i), max(h1i, l0i) + 1):
                    if 0 <= j < n and opens[j] > closes[j]:
                        zone = j
                if zone is not None:
                    iz = self._build_zone(symbol, LONG, "OB", i, zone, highs, lows, closes, l0, h0, msb)
                    zones.append(iz)
                # Bu-BB / Bu-MB: walk l1i→h1i for the LAST bullish candle
                l1i = low_points[-2].index if len(low_points) > 1 else low_points[-1].index
                bb_zone = None
                for j in range(min(l1i, h1i), max(l1i, h1i) + 1):
                    if 0 <= j < n and opens[j] < closes[j]:
                        bb_zone = j
                if bb_zone is not None:
                    ztype = "BB" if (h1 is not None and l1 is not None and l0 < l1) else "MB"
                    iz = self._build_zone(symbol, LONG, ztype, i, bb_zone, highs, lows, closes, l0, h0, msb)
                    zones.append(iz)
            if new_market == SHORT and len(high_points) > 0 and len(low_points) > 0:
                l1i = low_points[-2].index if len(low_points) > 1 else low_points[-1].index
                h0i = high_points[-1].index
                # Be-OB: walk l1i→h0i and take the LAST bullish candle
                zone = None
                for j in range(min(l1i, h0i), max(l1i, h0i) + 1):
                    if 0 <= j < n and opens[j] < closes[j]:
                        zone = j
                if zone is not None:
                    iz = self._build_zone(symbol, SHORT, "OB", i, zone, highs, lows, closes, l0, h0, msb)
                    zones.append(iz)
                # Be-BB / Be-MB: walk h1i→l1i for the LAST bearish candle
                h1i = high_points[-2].index if len(high_points) > 1 else high_points[-1].index
                bb_zone = None
                for j in range(min(h1i, l1i), max(h1i, l1i) + 1):
                    if 0 <= j < n and opens[j] > closes[j]:
                        bb_zone = j
                if bb_zone is not None:
                    ztype = "BB" if (h1 is not None and h0 > h1) else "MB"
                    iz = self._build_zone(symbol, SHORT, ztype, i, bb_zone, highs, lows, closes, l0, h0, msb)
                    zones.append(iz)
        self._mark_statuses(zones, n, closes)
        zones = self._cap(zones)
        result["zones"] = [z.to_dict() for z in zones]
        result["market"] = market[-1]
        return result

    # ---------- Helpers ----------
    @staticmethod
    def _barssince(cond: List[bool], index: int) -> Optional[int]:
        for back in range(1, index + 1):
            if cond[index - back]:
                return back
        return None

    @staticmethod
    def _barssince_at_equal(vals: List[float], target, index: int) -> Optional[int]:
        for back in range(1, index + 1):
            if vals[index - back] == target:
                return back
        return None

    def _build_zone(self, symbol: str, side: int, ztype: str, created_at: int,
                    zone_bar: int, highs, lows, closes, swing_low, swing_high,
                    msb: MSBEvent) -> InstitutionalZone:
        return InstitutionalZone(
            symbol=symbol,
            side=side,
            zone_type=ztype,
            top=float(highs[zone_bar]),
            bottom=float(lows[zone_bar]),
            created_at=created_at,
            msb_direction=side,
            msb_price=float(msb.price),
            swing_high=float(highs[zone_bar]),
            swing_low=float(lows[zone_bar]),
            fib_factor=self.fib_factor,
            zone_strength=1.0,
        )

    def _mark_statuses(self, zones: List[InstitutionalZone], end_bar: int, closes) -> None:
        # Closed-candle invalidation: bull zones die when close < bottom;
        # bearish zones die when close > top (mirrors Pine box deletion).
        for z in zones:
            z.freshness = end_bar - 1 - z.created_at
            touched = False
            mitigating = False
            for i in range(z.created_at + 1, end_bar):
                close = closes[i]
                if z.side == LONG and close < z.bottom:
                    z.status = STATUS_INVALIDATED
                    break
                if z.side == LONG and close < z.top:
                    mitigating = True
                    z.touch_count += 1
                if z.side == SHORT and close > z.top:
                    z.status = STATUS_INVALIDATED
                    break
                if z.side == SHORT and close > z.bottom:
                    mitigating = True
                    z.touch_count += 1
            if z.status == STATUS_ACTIVE and mitigating:
                z.status = STATUS_MITIGATING
            elif z.status == STATUS_ACTIVE and z.touch_count > 0:
                z.status = STATUS_TOUCHED

    def _cap(self, zones: List[InstitutionalZone]) -> List[InstitutionalZone]:
        if len(zones) <= self.max_zones:
            return zones
        return zones[-self.max_zones:]


# Small deterministic entry used by downstream pipeline
def analyze_msb(df, symbol="UNKNOWN", zigzag_len=DEFAULT_ZIGZAG_LEN,
                fib_factor=DEFAULT_FIB_FACTOR, max_zones=5) -> Dict:
    """Convenience wrapper matching the existing engine style."""
    return MSBOBEngine(int(zigzag_len), float(fib_factor), max_zones).analyze(df, symbol)


# ---------------------------------------------------------------------------
# Institutional context assembly
# ---------------------------------------------------------------------------

LIQ_NONE = "NO_LIQUIDITY_EVENT"
LIQ_NEARBY = "LIQUIDITY_NEARBY"
LIQ_SWEPT = "LIQUIDITY_SWEPT"
LIQ_CONFIRMED = "LIQUIDITY_SWEEP_CONFIRMED"
LIQ_CONFLICT = "LIQUIDITY_CONFLICT"


@dataclass
class MSBInstitutionalContext:
    """Canonical explainable context for the single primary zone."""

    symbol: str
    side: int
    msb_direction: int
    msb_price: float
    zone_id: str
    zone_type: str
    zone_top: float
    zone_bottom: float
    zone_status: str
    created_at: int
    msb_event_id: str

    liquidity_side: str
    liquidity_swept: bool
    liquidity_price: float
    liquidity_distance: float   # ATRs between price and pool level
    sweep_recency: int          # bar-age of sweep; -1 if none
    sweep_before_msb: bool      # whether sweep happened BEFORE the structural break

    displacement_detected: bool
    displacement_score: float

    bos: bool
    choch: bool
    mss: bool

    rejection_detected: bool
    absorption_detected: bool

    volume_confirmation: bool
    flow_confirmation: bool

    existing_zone_strength: float
    existing_ob_quality: float
    zone_freshness: int
    zone_touch_count: int

    institutional_intent: float
    institutional_behaviour: str

    trend_alignment: float
    news_state: str

    context_confidence: float

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": "LONG" if self.side == LONG else "SHORT",
            "msb_direction": "BULL" if self.msb_direction == LONG else "BEAR",
            "msb_price": self.msb_price,
            "zone_id": self.zone_id,
            "zone_type": self.zone_type,
            "zone_top": self.zone_top,
            "zone_bottom": self.zone_bottom,
            "zone_status": self.zone_status,
            "created_at": self.created_at,
            "msb_event_id": self.msb_event_id,
            "liquidity_side": self.liquidity_side,
            "liquidity_swept": self.liquidity_swept,
            "liquidity_price": self.liquidity_price,
            "liquidity_distance": self.liquidity_distance,
            "sweep_recency": self.sweep_recency,
            "sweep_before_msb": self.sweep_before_msb,
            "displacement_detected": self.displacement_detected,
            "displacement_score": self.displacement_score,
            "bos": self.bos, "choch": self.choch, "mss": self.mss,
            "rejection_detected": self.rejection_detected,
            "absorption_detected": self.absorption_detected,
            "volume_confirmation": self.volume_confirmation,
            "flow_confirmation": self.flow_confirmation,
            "existing_zone_strength": self.existing_zone_strength,
            "existing_ob_quality": self.existing_ob_quality,
            "zone_freshness": self.zone_freshness,
            "zone_touch_count": self.zone_touch_count,
            "institutional_intent": self.institutional_intent,
            "institutional_behaviour": self.institutional_behaviour,
            "trend_alignment": self.trend_alignment,
            "news_state": self.news_state,
            "context_confidence": self.context_confidence,
        }


def msb_context(df, symbol: str, side: int, queue_engine, *,
                zone: Optional[dict] = None,
                msb_event: Optional[dict] = None,
                news_state: str = "NEWS_UNAVAILABLE",
                atr: float = 0.0) -> Optional[MSBInstitutionalContext]:
    """Build the canonical context object for a candidate.

    Consumes existing queue-engine evaluators (liquidity/structure/OB/trend)
    and the provided zone. No score is added; context_confidence is a
    transparency value for explainability, not a scoring term.
    """
    if df is None or atr <= 0 or zone is None:
        return None
    try:
        price = float(df["close"].iloc[-1])
    except Exception:
        return None

    eng = queue_engine
    liq_score, liq_ev = eng._evaluate_liquidity(df, "BUY" if side == LONG else "SELL", atr)
    liq_state = liq_ev.get("state", LIQ_NONE)
    sweep_age = liq_ev.get("sweep_age")
    liq_price = float(liq_ev.get("level", 0.0))
    liq_distance = float(liq_ev.get("distance_atr", 0.0))
    liquidity_swept = liq_ev.get("sweep", 0) >= 100

    pool = eng._build_liquidity_pools(df)
    eq_highs, eq_lows = eng._detect_equal_highs_lows(df)
    if side == LONG:
        liq_side = "SELL_SIDE"  # sell-side liquidity is the target for a long setup
        liq_nearby = bool(pool.get("low_pools")) or eq_lows
    else:
        liq_side = "BUY_SIDE"
        liq_nearby = bool(pool.get("high_pools")) or eq_highs

    struct_score, struct_type = eng._evaluate_structure(df, "BUY" if side == LONG else "SELL")
    bos = getattr(struct_type, "value", None) in ("BOS", "MSS")
    mss = getattr(struct_type, "value", None) == "MSS"
    # CHoCH is the two-legged shift (higher-high AND higher-low, or the
    # bearish mirror) -- exposed explicitly here instead of a hard-coded False.
    try:
        shift = eng._detect_structure_shift(df)
    except Exception:
        shift = None
    if side == LONG:
        choch = bool(shift == "bullish_shift")
    elif side == SHORT:
        choch = bool(shift == "bearish_shift")
    else:
        choch = False

    try:
        ob_score, _ = eng._evaluate_order_block(df, "BUY" if side == LONG else "SELL", atr)
    except Exception:
        ob_score = 0.0
    try:
        trend_score = eng._evaluate_trend_alignment(df, "BUY" if side == LONG else "SELL")
    except Exception:
        trend_score = 50.0
    try:
        inst_score = eng._evaluate_institutional(df, "BUY" if side == LONG else "SELL")
    except Exception:
        inst_score = 50.0

    # Volume confirmation: existing volume context in smart-money flow
    try:
        smart = eng.SmartMoneyEngine.analyze_smart_money(df)
        vol_confirm = bool(smart.get("smart_money_dominant", False))
        flow_confirm = (side == LONG and smart.get("institutional_bias") == "BUY") or \
                       (side == SHORT and smart.get("institutional_bias") == "SELL")
    except Exception:
        vol_confirm = False
        flow_confirm = False

    last = df.iloc[-1]
    rejected = (float(last["close"]) > float(last["open"])) if side == LONG \
               else (float(last["close"]) < float(last["open"]))
    absorb = bool(liq_ev.get("absorption", 0) >= 70)
    disp_score = float(liq_ev.get("displacement", 0))

    # Temporal check: the sweep's bar position vs the MSB event's bar position
    sweep_before_msb = False
    if msb_event is not None and sweep_age is not None:
        msb_idx = int(msb_event.get("index", 0))
        n = len(df)
        sweep_bar = n - 1 - int(sweep_age)
        sweep_before_msb = sweep_bar <= msb_idx

    # Map into explicit liquidity context states
    if liq_state in ("LIQUIDITY_SWEPT",) and sweep_before_msb:
        context_state = LIQ_CONFIRMED
    elif liquidity_swept:
        context_state = LIQ_SWEPT
    elif liq_nearby:
        context_state = LIQ_NEARBY
    else:
        context_state = LIQ_NONE

    zone_status = str(zone.get("status", "ACTIVE"))
    confidence = 0.0
    if context_state == LIQ_CONFIRMED:
        confidence += 0.30
    elif context_state == LIQ_SWEPT:
        confidence += 0.18
    elif context_state == LIQ_NEARBY:
        confidence += 0.08
    confidence += 0.20 if disp_score >= 40 else 0.10 if disp_score > 0 else 0.0
    confidence += 0.15 if (bos or mss) else 0.0
    confidence += 0.10 if rejected else 0.0
    confidence += 0.10 if absorb else 0.0
    confidence += 0.10 if vol_confirm else 0.0
    confidence += 0.10 if flow_confirm else 0.0
    confidence += 0.05 if ob_score >= 65 else 0.0
    confidence += 0.05 if trend_score >= 65 else 0.0
    confidence = max(0.0, min(1.0, confidence))

    zone_id = f"{symbol}:{zone.get('side', side)}:{zone.get('zone_type','?')}:{zone.get('created_at',0)}"
    msb_event_id = (f"{symbol}:MSB:{msb_event.get('direction','?')}:{msb_event.get('index',0)}"
                    if msb_event else f"{symbol}:MSB:NONE:0")

    return MSBInstitutionalContext(
        symbol=symbol, side=side, msb_direction=side,
        msb_price=float(msb_event.get("price", 0.0)) if msb_event else 0.0,
        zone_id=zone_id,
        zone_type=str(zone.get("zone_type", "OB")),
        zone_top=float(zone.get("top", 0.0)),
        zone_bottom=float(zone.get("bottom", 0.0)),
        zone_status=zone_status,
        created_at=int(zone.get("created_at", 0)),
        msb_event_id=msb_event_id,
        liquidity_side=liq_side,
        liquidity_swept=liquidity_swept,
        liquidity_price=liq_price,
        liquidity_distance=liq_distance,
        sweep_recency=int(sweep_age) if sweep_age is not None else -1,
        sweep_before_msb=sweep_before_msb,
        displacement_detected=disp_score > 0,
        displacement_score=disp_score,
        bos=bos, choch=choch, mss=mss,
        rejection_detected=rejected,
        absorption_detected=absorb,
        volume_confirmation=vol_confirm,
        flow_confirmation=flow_confirm,
        existing_zone_strength=float(liq_ev.get("composite", liq_score)),
        existing_ob_quality=float(ob_score),
        zone_freshness=int(zone.get("freshness", 0)),
        zone_touch_count=int(zone.get("touch_count", 0)),
        institutional_intent=float(inst_score),
        institutional_behaviour="INFERRED",
        trend_alignment=float(trend_score),
        news_state=str(news_state),
        context_confidence=round(confidence, 3),
    )


def rank_zones(zones: List[dict], side: int) -> Tuple[Optional[dict], Optional[dict]]:
    """Deterministically rank side-matched zones.

    Existing-zone-quality rule order: ACTIVE > TOUCHED > MITIGATING, then
    zone_strength, then smallest freshness, then highest touch_count.
    INVALIDATED/EXPIRED zones are excluded. Returns (primary, secondary).
    """
    usable = [z for z in zones
              if str(z.get("side", "")) in (("LONG" if side == LONG else "SHORT"),)
              and z.get("status") in (STATUS_ACTIVE, STATUS_TOUCHED, STATUS_MITIGATING)]
    usable.sort(key=lambda z: (
        0 if z.get("status") == STATUS_ACTIVE else (1 if z.get("status") == STATUS_TOUCHED else 2),
        -float(z.get("zone_strength", 0.0)),
        int(z.get("freshness", 0)),
        -int(z.get("touch_count", 0)),
        -float(z.get("top", 0.0)) - float(z.get("bottom", 0.0)),
    ))
    primary = usable[0] if usable else None
    secondary = usable[1] if len(usable) > 1 else None
    return primary, secondary


# ---------------------------------------------------------------------------
# Temporal / causal sequence validation
# ---------------------------------------------------------------------------

EVENT_LIQUIDITY_SWEEP = "LIQUIDITY_SWEEP"
EVENT_DISPLACEMENT = "DISPLACEMENT"
EVENT_MSB = "MSB"
EVENT_OB_CREATED = "OB_CREATED"
EVENT_RETEST = "RETEST"
EVENT_REJECTION = "REJECTION"
EVENT_CURRENT_STATE = "CURRENT_STATE"

SEQ_NONE = "NO_LIQUIDITY_SEQUENCE"
SEQ_LIQUIDITY_ONLY = "LIQUIDITY_ONLY"
SEQ_LIQUIDITY_THEN_STRUCTURE = "LIQUIDITY_THEN_STRUCTURE"
SEQ_STRUCT_ONLY = "STRUCTURAL_BREAK_WITHOUT_LIQUIDITY"
SEQ_FULL = "FULL_INSTITUTIONAL_SEQUENCE"
SEQ_INVALID = "INVALID_SEQUENCE"

_NOT_CONFIRMED_LABEL = "NOT CONFIRMED"


@dataclass
class EventTimelineEntry:
    event: str
    index: Optional[int]  # bar index, None if NOT CONFIRMED

    def to_dict(self) -> dict:
        return {"event": self.event,
                "index": None if self.index is None else int(self.index),
                "label": None if self.index is not None else _NOT_CONFIRMED_LABEL}


@dataclass
class TemporalSequenceAssessment:
    symbol: str
    side: int
    sequence: str
    timeline: List[EventTimelineEntry]
    sweep_bar: Optional[int]
    displacement_bar: Optional[int]
    msb_bar: Optional[int]
    ob_bar: Optional[int]
    retest_bar: Optional[int]
    rejection_bar: Optional[int]

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "side": "LONG" if self.side == LONG else "SHORT",
            "sequence": self.sequence,
            "timeline": [e.to_dict() for e in self.timeline],
            "bars": {
                "sweep": self.sweep_bar,
                "displacement": self.displacement_bar,
                "msb": self.msb_bar,
                "ob": self.ob_bar,
                "retest": self.retest_bar,
                "rejection": self.rejection_bar,
            },
        }


def _first_bar_in_zone(closes, side: int, top: float, bottom: float, start: int) -> Optional[int]:
    if side == LONG:
        for i in range(start, len(closes)):
            if closes[i] < top:
                return i
    else:
        for i in range(start, len(closes)):
            if closes[i] > bottom:
                return i
    return None


def _first_strong_rejection(opens, closes, side: int, start: int) -> Optional[int]:
    for i in range(start, len(closes)):
        if side == LONG and closes[i] > opens[i]:
            return i
        if side == SHORT and closes[i] < opens[i]:
            return i
    return None


def temporal_sequence(df, symbol: str, side: int, queue_engine,
                      *,
                      zone: Optional[dict] = None,
                      msb_event: Optional[dict] = None,
                      atr: float = 0.0) -> Optional[TemporalSequenceAssessment]:
    """Determine the causal sequence for a candidate using bar indices only.

    Emits a deterministic event timeline (missing events = NOT CONFIRMED)
    and a sequence classification:
      NO_LIQUIDITY_SEQUENCE / LIQUIDITY_ONLY / LIQUIDITY_THEN_STRUCTURE /
      STRUCTURAL_BREAK_WITHOUT_LIQUIDITY / FULL_INSTITUTIONAL_SEQUENCE /
      INVALID_SEQUENCE.
    """
    if df is None or atr <= 0:
        return None
    n = len(df)
    closes = list(df["close"].values)
    opens = list(df["open"].values)
    highs = list(df["high"].values)
    lows = list(df["low"].values)

    eng = queue_engine
    liq_score, liq_ev = eng._evaluate_liquidity(df, "BUY" if side == LONG else "SELL", atr)
    sweep_age = liq_ev.get("sweep_age")
    sweep_bar = (n - 1 - int(sweep_age)) if sweep_age is not None else None

    msb_bar = int(msb_event.get("index")) if msb_event else None
    ob_bar = int(zone.get("created_at")) if zone else None

    # Re-derive the sweep restricted to the temporal scope of the MSB event:
    # the engine's sweep_bar is the *most recent* sweep, which may sit after
    # the MSB. For causal validation we need the sweep that actually happened
    # before the MSB. We recompute directly from pool levels against the MSB bar.
    if msb_bar is not None:
        pool = eng._build_liquidity_pools(df)
        key = "low_pools" if side == LONG else "high_pools"
        candidates = []
        for idx, level in pool.get(key, []):
            for j in range(1, n):
                if side == LONG and lows[j] < level and lows[j - 1] >= level:
                    candidates.append(j)
                elif side == SHORT and highs[j] > level and highs[j - 1] <= level:
                    candidates.append(j)
        before = [j for j in candidates if j <= msb_bar]
        sweep_bar = max(before) if before else None
        sweep_age = (n - 1 - sweep_bar) if sweep_bar is not None else None

    # Forward-flow event detection restricted to bars < current bar
    retest_bar = None
    rejection_bar = None
    if zone is not None:
        retest_bar = _first_bar_in_zone(closes, side,
                                        float(zone.get("top", 0.0)),
                                        float(zone.get("bottom", 0.0)),
                                        (ob_bar + 1) if ob_bar is not None else 0)
        if retest_bar is not None:
            rejection_bar = _first_strong_rejection(opens, closes, side, retest_bar)

    displacement_bar = None
    if sweep_bar is not None and msb_bar is not None:
        # displacement exists between sweep and MSB if the direction move ≥ 1 ATR
        # across any bar strict-enough to still precede the MSB event.
        for j in range(sweep_bar, msb_bar + 1):
            if side == LONG:
                if closes[j] - lows[sweep_bar] >= atr:
                    displacement_bar = j
                    break
            else:
                if highs[sweep_bar] - closes[j] >= atr:
                    displacement_bar = j
                    break
        if displacement_bar is not None and displacement_bar > msb_bar:
            displacement_bar = None

    # Causal ordering checks
    ok_sweep = sweep_bar is not None
    ok_disp = displacement_bar is not None
    ok_msb = msb_bar is not None
    ok_ob = ob_bar is not None
    ok_retest = retest_bar is not None
    ok_reject = rejection_bar is not None

    full_ok = ok_sweep and ok_disp and ok_msb and ok_ob and (
        (ok_retest and ok_reject) or (not ok_reject and ok_retest))
    seq = SEQ_NONE
    if full_ok and ok_msb and ok_ob and (sweep_bar < displacement_bar <= msb_bar <= ob_bar <=
                    (retest_bar if ok_retest else ob_bar) <=
                    (rejection_bar if ok_reject else retest_bar if ok_retest else ob_bar)):
        seq = SEQ_FULL
    elif ok_sweep and (ok_msb or ok_ob) and ok_msb and sweep_bar <= msb_bar:
        seq = SEQ_LIQUIDITY_THEN_STRUCTURE
    elif ok_sweep and not (ok_msb or ok_ob):
        seq = SEQ_LIQUIDITY_ONLY
    elif (ok_msb or ok_ob) and not ok_sweep:
        seq = SEQ_STRUCT_ONLY

    events = [EventTimelineEntry(EVENT_LIQUIDITY_SWEEP, sweep_bar),
              EventTimelineEntry(EVENT_DISPLACEMENT, displacement_bar),
              EventTimelineEntry(EVENT_MSB, msb_bar),
              EventTimelineEntry(EVENT_OB_CREATED, ob_bar),
              EventTimelineEntry(EVENT_RETEST, retest_bar),
              EventTimelineEntry(EVENT_REJECTION, rejection_bar),
              EventTimelineEntry(EVENT_CURRENT_STATE, n - 1)]
    return TemporalSequenceAssessment(
        symbol=symbol, side=side, sequence=seq, timeline=events,
        sweep_bar=sweep_bar, displacement_bar=displacement_bar,
        msb_bar=msb_bar, ob_bar=ob_bar, retest_bar=retest_bar,
        rejection_bar=rejection_bar)
