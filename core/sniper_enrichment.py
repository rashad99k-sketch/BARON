"""Ultimate Sniper Pro confluence layer for Atom.

Faithful, deterministic, closed-candle-only port of the evidence produced by
the TradingView "Ultimate Sniper Pro + LNL Trend System" indicator. This module
must NOT emit trade signals and must NOT become an order-block source. The
causal Order Block identified by the Atom engine stays the source of truth for
entries, grading and zone handling.

What this layer adds on top of that causal OB (weighted confluence only):

  * Demand / Supply high-volume boxes       (supportLevel / resistanceLevel)
  * Delta-volume context                     (upAndDownVolume, vol_hi/vol_lo)
  * SMC liquidity + sweep / fakeout / shift  (X, SFP, MSS)
  * Trend regime                             (EMA stack, Sniper/Trend EMA, ADX,
                                              VWAP, Chandelier direction)
  * Premium / Discount alignment
  * FVG (fair value gap) after displacement
  * Exhaustion evidence                       (end-of-move, for position
                                              management, never an entry signal)

Guarantees
----------
  * all computation is performed over *closed* candles only,
  * pivots are only trusted once confirmed (no look-ahead / no repainting),
  * the output is advisory: positive/negative confluence and detailed evidence.
    It can raise or lower confidence but can never by itself turn a weak setup
    into a strong one, and it never bypasses Entry Quality / Risk / Execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Tunable parameters. Everything defaults to the indicator's own values and is
# overridable via a config dict / env vars. Changing defaults never changes
# behaviour unless explicitly requested.
# ---------------------------------------------------------------------------
DEFAULT_PIVOT_LEN = 10          # prd (SMC / liquidity pivots)
DEFAULT_LOOKBACK = 20           # lookbackPeriod (SR box pivots)
DEFAULT_VOL_LEN = 2             # vol_len (delta volume filter)
DEFAULT_BOX_WIDTH = 1.0         # box_withd (ATR multiplier for box width)
DEFAULT_CE_LENGTH = 22          # Chandelier ATR period
DEFAULT_CE_MULT = 3.0           # Chandelier ATR multiplier
DEFAULT_SNIPER_EMA = 50
DEFAULT_TREND_EMA = 200
DEFAULT_EXHAUSTION_MULT = 1.5   # exhaustion ATR multiplier
DEFAULT_OVEREXTEND_MULT = 2.0   # overextension ATR multiplier
DEFAULT_MAX_ACTIVE_LINES = 5

# Confluence contribution limits (advisory only, capped so a strong causal OB
# keeps its ceiling). Expresses a signed score shift in "0..100" zone-score
# terms although it is only applied as a bounded bonus/penalty on top of the
# causal OB score; it never flips a fundamentally weak setup into a strong one.
MAX_CONFLUENCE_BONUS = 12.0
MAX_CONFLUENCE_PENALTY = -10.0

LONG = 1
SHORT = -1


def normalize_side(side) -> Optional[int]:
    """Coerce a side spec into LONG/SHORT (None when unknown/ambiguous).

    Accepts the canonical int constants as well as the engine's string forms
    ("BUY"/"SELL") and longhand ("LONG"/"SHORT") so callers can pass either.
    """
    if side is None:
        return None
    if isinstance(side, (int, np.integer)):
        if int(side) == LONG:
            return LONG
        if int(side) == SHORT:
            return SHORT
        return None
    if isinstance(side, str):
        s = side.strip().upper()
        if s in ("BUY", "LONG", "L", "B"):
            return LONG
        if s in ("SELL", "SHORT", "S"):
            return SHORT
    return None


@dataclass
class SniperEnrichmentConfig:
    """Adjustable tuning. All fields honour the indicator's defaults."""

    pivot_len: int = DEFAULT_PIVOT_LEN
    lookback: int = DEFAULT_LOOKBACK
    vol_len: int = DEFAULT_VOL_LEN
    box_width: float = DEFAULT_BOX_WIDTH
    ce_length: int = DEFAULT_CE_LENGTH
    ce_mult: float = DEFAULT_CE_MULT
    sniper_ema_len: int = DEFAULT_SNIPER_EMA
    trend_ema_len: int = DEFAULT_TREND_EMA
    exhaustion_mult: float = DEFAULT_EXHAUSTION_MULT
    overextend_mult: float = DEFAULT_OVEREXTEND_MULT
    max_active_lines: int = DEFAULT_MAX_ACTIVE_LINES

    @classmethod
    def from_env(cls, env=None) -> "SniperEnrichmentConfig":
        """Build config honouring uppercase SNIPER_* env vars (defaults kept)."""
        import os as _os
        e = env if env is not None else (_os.environ if hasattr(_os, "environ") else {})

        def _int(name, default):
            try:
                return int(e.get(name, default))
            except (TypeError, ValueError):
                return default

        def _float(name, default):
            try:
                return float(e.get(name, default))
            except (TypeError, ValueError):
                return default

        return cls(
            pivot_len=_int("SNIPER_PIVOT_LEN", DEFAULT_PIVOT_LEN),
            lookback=_int("SNIPER_LOOKBACK", DEFAULT_LOOKBACK),
            vol_len=_int("SNIPER_VOL_LEN", DEFAULT_VOL_LEN),
            box_width=_float("SNIPER_BOX_WIDTH", DEFAULT_BOX_WIDTH),
            ce_length=_int("SNIPER_CE_LENGTH", DEFAULT_CE_LENGTH),
            ce_mult=_float("SNIPER_CE_MULT", DEFAULT_CE_MULT),
            sniper_ema_len=_int("SNIPER_EMA_LEN", DEFAULT_SNIPER_EMA),
            trend_ema_len=_int("SNIPER_TREND_EMA_LEN", DEFAULT_TREND_EMA),
            exhaustion_mult=_float("SNIPER_EXHAUSTION_MULT", DEFAULT_EXHAUSTION_MULT),
            overextend_mult=_float("SNIPER_OVEREXTEND_MULT", DEFAULT_OVEREXTEND_MULT),
            max_active_lines=_int("SNIPER_MAX_ACTIVE_LINES", DEFAULT_MAX_ACTIVE_LINES),
        )

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "SniperEnrichmentConfig":
        cfg = cls.from_env()
        if not data:
            return cfg
        for key in ("pivot_len", "lookback", "vol_len", "box_width", "ce_length",
                    "ce_mult", "sniper_ema_len", "trend_ema_len", "exhaustion_mult",
                    "overextend_mult", "max_active_lines"):
            if key in data and data[key] is not None:
                setattr(cfg, key, data[key])
        return cfg


# ---------------------------------------------------------------------------
# Evidence containers
# ---------------------------------------------------------------------------
@dataclass
class DemandSupplyBox:
    kind: str                     # "demand" | "supply"
    top: float
    bottom: float
    pivot_price: float
    created_bar: int
    volume: float                 # delta volume that formed the box


@dataclass
class SniperEvidence:
    """Detailed per-zone evidence so we always know WHY a bonus/penalty fired."""

    # Demand / Supply
    in_demand_box: bool = False
    in_supply_box: bool = False
    nearest_demand: Optional[dict] = None
    nearest_supply: Optional[dict] = None
    demand_box_count: int = 0
    supply_box_count: int = 0

    # Delta volume
    delta_volume: float = 0.0
    volume_expansion: bool = False
    delta_bullish: bool = False
    delta_bearish: bool = False
    volume_ratio: float = 0.0

    # SMC
    liquidity_high: Optional[float] = None
    liquidity_low: Optional[float] = None
    sweep_buy: bool = False          # sell-side low swept (long context)
    sweep_sell: bool = False         # buy-side high swept (short context)
    sfp_buy: bool = False
    sfp_sell: bool = False
    mss_bullish: bool = False
    mss_bearish: bool = False

    # Trend regime
    ema_stack_bullish: bool = False
    ema_stack_bearish: bool = False
    price_vs_sniper_ema: int = 0     # 1 above, -1 below
    price_vs_trend_ema: int = 0
    adx: float = 0.0
    adx_bullish: bool = False
    adx_bearish: bool = False
    vwap_aligned_buy: bool = False
    vwap_aligned_sell: bool = False
    chandelier_dir: int = 0          # 1 long, -1 short, 0 flat

    # Premium / Discount
    premium_discount_buy: bool = False    # on discount side (long-favourable)
    premium_discount_sell: bool = False   # on premium side (short-favourable)

    # FVG
    fvg_bullish: bool = False
    fvg_bearish: bool = False

    # Exhaustion (advisory, for position management — NEVER an entry signal)
    exhaustion_buy: bool = False      # end-of-move at lows (was BUY end of move)
    exhaustion_sell: bool = False     # end-of-move at highs (was SELL end of move)

    def to_dict(self) -> dict:
        d = {}
        for attr in (
            "in_demand_box", "in_supply_box", "nearest_demand", "nearest_supply",
            "demand_box_count", "supply_box_count", "delta_volume",
            "volume_expansion", "delta_bullish", "delta_bearish", "volume_ratio",
            "liquidity_high", "liquidity_low", "sweep_buy", "sweep_sell",
            "sfp_buy", "sfp_sell", "mss_bullish", "mss_bearish",
            "ema_stack_bullish", "ema_stack_bearish", "price_vs_sniper_ema",
            "price_vs_trend_ema", "adx", "adx_bullish", "adx_bearish",
            "vwap_aligned_buy", "vwap_aligned_sell", "chandelier_dir",
            "premium_discount_buy", "premium_discount_sell",
            "fvg_bullish", "fvg_bearish", "exhaustion_buy", "exhaustion_sell",
        ):
            val = getattr(self, attr)
            if isinstance(val, (np.bool_, bool)):
                val = bool(val)
            elif isinstance(val, (np.integer,)):
                val = int(val)
            elif isinstance(val, (np.floating,)):
                val = round(float(val), 6)
            d[attr] = val
        return d


@dataclass
class SniperConfluence:
    """Signed advisory confluence for a given trade side.

    score_shift > 0 raises confidence, < 0 lowers it. Applied as a *bounded*
    bonus/penalty on top of the causal OB score only — it never bypasses the
    Entry Quality / Risk / Execution gates.
    """

    side: int                       # LONG / SHORT
    score_shift: float = 0.0
    primary_energy: str = "NEUTRAL"
    reasons: List[str] = field(default_factory=list)
    evidence: SniperEvidence = field(default_factory=SniperEvidence)

    def to_dict(self) -> dict:
        return {
            "side": "LONG" if self.side == LONG else "SHORT",
            "score_shift": round(self.score_shift, 3),
            "primary_energy": self.primary_energy,
            "reasons": self.reasons,
            "evidence": self.evidence.to_dict(),
        }


# ---------------------------------------------------------------------------
# Deterministic helpers (closed-candle only, no look-ahead)
# ---------------------------------------------------------------------------
def _ema(series: pd.Series, period: int) -> pd.Series:
    if period <= 0:
        return series * np.nan
    return series.ewm(span=int(period), adjust=False).mean()


def _rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothed moving average (matches ta.rma / rma)."""
    if len(series) == 0 or period <= 0:
        return series * np.nan
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    if df is None or len(df) < 2:
        return pd.Series([0.0] * len(df), index=df.index)
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    out = _rma(tr, max(1, int(period))).bfill().ffill().fillna(tr.mean())
    return out.clip(lower=1e-9)


def _adx(df: pd.DataFrame, period: int = 14) -> float:
    if df is None or len(df) < period * 2:
        return 0.0
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = _rma(tr, period) + 1e-9
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0), index=df.index)
    plus_di = 100 * _rma(plus_dm, period) / (atr + 1e-9)
    minus_di = 100 * _rma(minus_dm, period) / (atr + 1e-9)
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di + 1e-9)) * 100
    adx = _rma(dx, period).bfill().ffill().fillna(0)
    return float(adx.iloc[-1])


def _cum_vwap(df: pd.DataFrame) -> pd.Series:
    """Cumulative VWAP over the full closed history."""
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = df["volume"].replace(0, np.nan)
    cum_pv = (tp * vol).cumsum()
    cum_v = vol.cumsum()
    return (cum_pv / cum_v).bfill().ffill()


def _pivots(df: pd.DataFrame, period: int):
    """Return confirmed swing high/low (bar_idx, price) pairs.

    A pivot needs `period` closed bars on each side (mirrors
    ta.pivothigh/low). Only pivots strictly before the last `period` bars are
    "confirmed" so the trailing edge never repaints.
    """
    period = max(1, int(period))
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    n = len(df)
    piv_high = []
    piv_low = []
    for i in range(period, n - period):
        hi = highs[i]
        lo = lows[i]
        if np.all(highs[i - period:i] < hi) and np.all(highs[i + 1:i + period + 1] < hi):
            piv_high.append((i, hi))
        if np.all(lows[i - period:i] > lo) and np.all(lows[i + 1:i + period + 1] > lo):
            piv_low.append((i, lo))
    cut = n - period - 1
    return [(i, p) for (i, p) in piv_high if i <= cut], [(i, p) for (i, p) in piv_low if i <= cut]


def _signed_volume(df: pd.DataFrame) -> pd.Series:
    """Per-bar directional volume (momentum-running attribution).

    Port of upAndDownVolume(): once a bar sets the buying/selling regime it
    keeps attributing volume to that regime until it flips. Positive bar value
    = net buy pressure that bar, negative = net sell pressure. This is the raw
    per-bar signal used to detect high-volume demand/supply pivots.
    """
    o = df["open"].to_numpy()
    c = df["close"].to_numpy()
    v = df["volume"].to_numpy()
    n = len(df)
    signed = np.empty(n, dtype=float)
    regime = None
    for i in range(n):
        if c[i] > o[i]:
            regime = True
        elif c[i] < o[i]:
            regime = False
        # doji: keep previous regime (default buy when no regime yet)
        signed[i] = v[i] if (regime if regime is not None else True) else -v[i]
    return pd.Series(signed, index=df.index)


def _delta_volume(df: pd.DataFrame) -> pd.Series:
    """Cumulative net directional volume (context: net buy vs net sell pressure).

    Positive = cumulative net buy pressure, negative = cumulative net sell
    pressure, computed from the running per-bar signed volume.
    """
    return _signed_volume(df).cumsum()


# ---------------------------------------------------------------------------
# Main analyzer
# ---------------------------------------------------------------------------
class SniperEnrichmentEngine:
    def __init__(self, config: Optional[SniperEnrichmentConfig] = None):
        self.config = config if config is not None else SniperEnrichmentConfig.from_env()

    # ---- public entry point -------------------------------------------------
    def analyze(self, df: pd.DataFrame, symbol: str = "UNKNOWN",
                side: Optional[int] = None) -> SniperConfluence:
        """Compute sniper confluence for a given side (LONG/SHORT).

        If side is None it computes both and returns the stronger side.
        """
        if df is None or not isinstance(df, pd.DataFrame) or len(df) < 40:
            return SniperConfluence(side=LONG, score_shift=0.0,
                                    primary_energy="NEUTRAL",
                                    reasons=["insufficient_data"])
        side = normalize_side(side)
        ev = SniperEvidence()
        self._fill_trend_evidence(df, ev)
        self._fill_volume_evidence(df, ev)
        boxes = self._build_sr_boxes(df)
        self._fill_box_evidence(df, boxes, ev)
        self._fill_smc_evidence(df, ev)
        self._fill_fvg_evidence(df, ev)
        atr_14 = float(_atr(df, 14).iloc[-1])
        self._fill_exhaustion(df, ev, atr_14)

        if side is not None:
            target = side
        else:
            buy = self._confluence_for_side(LONG, ev)
            sell = self._confluence_for_side(SHORT, ev)
            target = LONG if buy.score_shift >= sell.score_shift else SHORT
        conf = self._confluence_for_side(target, ev)
        return conf

    # ---- trend -------------------------------------------------------------
    def _fill_trend_evidence(self, df, ev: SniperEvidence) -> None:
        cfg = self.config
        close = df["close"]
        last = float(close.iloc[-1])
        e8 = _ema(close, 8)
        e13 = _ema(close, 13)
        e21 = _ema(close, 21)
        e34 = _ema(close, 34)
        v8, v13, v21, v34 = (float(e8.iloc[-1]), float(e13.iloc[-1]),
                             float(e21.iloc[-1]), float(e34.iloc[-1]))
        ev.ema_stack_bullish = bool(v8 > v13 > v21 > v34)
        ev.ema_stack_bearish = bool(v8 < v13 < v21 < v34)
        sniper_ema = float(_ema(close, cfg.sniper_ema_len).iloc[-1])
        trend_ema = float(_ema(close, cfg.trend_ema_len).iloc[-1])
        ev.price_vs_sniper_ema = 1 if last > sniper_ema else (-1 if last < sniper_ema else 0)
        ev.price_vs_trend_ema = 1 if last > trend_ema else (-1 if last < trend_ema else 0)
        ev.adx = _adx(df, 14)
        ev.adx_bullish = bool(ev.adx > 20 and ev.price_vs_sniper_ema > 0)
        ev.adx_bearish = bool(ev.adx > 20 and ev.price_vs_sniper_ema < 0)
        vwap_last = float(_cum_vwap(df).iloc[-1])
        ev.vwap_aligned_buy = bool(last >= vwap_last)
        ev.vwap_aligned_sell = bool(last <= vwap_last)
        # Chandelier direction (scan back over closed candles)
        atr_ce = cfg.ce_mult * _atr(df, cfg.ce_length)
        longest = df["close"].rolling(cfg.ce_length).max()
        lowstop = df["close"].rolling(cfg.ce_length).min()
        long_stop = (longest - atr_ce).shift(1)
        short_stop = (lowstop + atr_ce).shift(1)
        dir_ = 0
        idx = len(df) - 1
        while idx >= 1:
            c_prev = float(close.iloc[idx])
            ls = float(long_stop.iloc[idx])
            ss = float(short_stop.iloc[idx])
            if not np.isnan(ss) and c_prev > ss:
                dir_ = 1
                break
            if not np.isnan(ls) and c_prev < ls:
                dir_ = -1
                break
            idx -= 1
        ev.chandelier_dir = dir_

    # ---- volume ------------------------------------------------------------
    def _fill_volume_evidence(self, df, ev: SniperEvidence) -> None:
        vol = df["volume"]
        vol_ma = vol.rolling(20).mean()
        cur_vol = float(vol.iloc[-1])
        base = float(vol_ma.iloc[-2]) if len(vol_ma) >= 2 and not np.isnan(vol_ma.iloc[-2]) else 0.0
        if base <= 0:
            base = float(vol_ma.iloc[-1]) if len(vol_ma) >= 1 and not np.isnan(vol_ma.iloc[-1]) else 0.0
        ev.volume_ratio = (cur_vol / base) if base > 0 else 0.0
        ev.volume_expansion = bool(cur_vol > base * 1.5)
        delta = _delta_volume(df)
        ev.delta_volume = float(delta.iloc[-1])
        ev.delta_bullish = bool(ev.delta_volume > 0)
        ev.delta_bearish = bool(ev.delta_volume < 0)

    # ---- SR boxes ----------------------------------------------------------
    def _build_sr_boxes(self, df) -> List[DemandSupplyBox]:
        cfg = self.config
        period = max(1, cfg.vol_len)
        signed = _signed_volume(df).to_numpy()
        atr_sr = float(_atr(df, 200).iloc[-1]) if len(df) > 200 else float(_atr(df, 14).iloc[-1])
        width = atr_sr * cfg.box_width
        piv_high, piv_low = _pivots(df, cfg.lookback)
        boxes: List[DemandSupplyBox] = []
        for (idx, price) in piv_low:
            lo = float(np.sum(signed[idx - period + 1: idx + 1])) if idx >= period - 1 else 0.0
            if lo > 0:
                boxes.append(DemandSupplyBox(
                    kind="demand", top=float(price + width), bottom=float(price - width),
                    pivot_price=float(price), created_bar=int(idx),
                    volume=float(np.sum(signed[max(0, idx - period + 1): idx + 1]))))
        for (idx, price) in piv_high:
            hi = float(np.sum(signed[idx - period + 1: idx + 1])) if idx >= period - 1 else 0.0
            if hi < 0:
                boxes.append(DemandSupplyBox(
                    kind="supply", top=float(price + width), bottom=float(price - width),
                    pivot_price=float(price), created_bar=int(idx),
                    volume=float(np.sum(signed[max(0, idx - period + 1): idx + 1]))))
        return boxes

    def _fill_box_evidence(self, df, boxes: List[DemandSupplyBox], ev: SniperEvidence) -> None:
        price = float(df["close"].iloc[-1])
        demand = [b for b in boxes if b.kind == "demand"]
        supply = [b for b in boxes if b.kind == "supply"]
        ev.demand_box_count = len(demand)
        ev.supply_box_count = len(supply)
        best_d = None
        best_gap = None
        for b in demand:
            if b.top >= price >= b.bottom:
                ev.in_demand_box = True
                best_d = b
                break
            gap = abs(price - (b.top + b.bottom) / 2.0)
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_d = b
        if best_d is not None:
            ev.nearest_demand = {"top": round(best_d.top, 6), "bottom": round(best_d.bottom, 6),
                                 "pivot": round(best_d.pivot_price, 6), "volume": round(best_d.volume, 4)}
        best_s = None
        best_gap = None
        for b in supply:
            if b.top >= price >= b.bottom:
                ev.in_supply_box = True
                best_s = b
                break
            gap = abs(price - (b.top + b.bottom) / 2.0)
            if best_gap is None or gap < best_gap:
                best_gap = gap
                best_s = b
        if best_s is not None:
            ev.nearest_supply = {"top": round(best_s.top, 6), "bottom": round(best_s.bottom, 6),
                                 "pivot": round(best_s.pivot_price, 6), "volume": round(best_s.volume, 4)}

    # ---- SMC ---------------------------------------------------------------
    def _fill_smc_evidence(self, df, ev: SniperEvidence) -> None:
        cfg = self.config
        piv_high, piv_low = _pivots(df, cfg.pivot_len)
        lows = df["low"].to_numpy()
        highs = df["high"].to_numpy()
        n = len(df)
        last_low_pivot = None
        last_high_pivot = None
        for (idx, price) in piv_low:
            broken = False
            for j in range(idx + 1, min(n, idx + 1 + cfg.pivot_len)):
                if lows[j] < price:
                    broken = True
                    break
            if not broken:
                last_low_pivot = price
        for (idx, price) in piv_high:
            broken = False
            for j in range(idx + 1, min(n, idx + 1 + cfg.pivot_len)):
                if highs[j] > price:
                    broken = True
                    break
            if not broken:
                last_high_pivot = price
        ev.liquidity_low = last_low_pivot
        ev.liquidity_high = last_high_pivot
        close = df["close"].to_numpy()
        price = float(close[-1])
        prev_price = float(close[-2]) if n > 1 else price
        if ev.liquidity_low is not None:
            if prev_price >= float(ev.liquidity_low) and price < float(ev.liquidity_low):
                ev.sweep_buy = True
        # liquidity_low is the value at/below which a X/sweep marks a low tag
        # complement sweep_sell on the high side
        if ev.liquidity_high is not None:
            if prev_price <= float(ev.liquidity_high) and price > float(ev.liquidity_high):
                ev.sweep_sell = True
        # MSS — break of the most recent confirmed opposite-side pivot
        if piv_high and price > float(piv_high[-1][1]):
            ev.mss_bullish = bool(prev_price <= float(piv_high[-1][1]))
        if piv_low and price < float(piv_low[-1][1]):
            ev.mss_bearish = bool(prev_price >= float(piv_low[-1][1]))
        # SFP — sweep that fails to close through the level
        if ev.sweep_buy and ev.liquidity_low is not None and not (price < float(ev.liquidity_low)):
            ev.sfp_buy = True
        if ev.sweep_sell and ev.liquidity_high is not None and not (price > float(ev.liquidity_high)):
            ev.sfp_sell = True
        # Premium / discount over the recent trading range
        lb = max(1, cfg.lookback)
        rng_hi = float(df["high"].iloc[-lb:].max())
        rng_lo = float(df["low"].iloc[-lb:].min())
        mid = (rng_hi + rng_lo) / 2.0
        ev.premium_discount_buy = bool(price <= mid)     # discount
        ev.premium_discount_sell = bool(price >= mid)    # premium

    # ---- FVG ---------------------------------------------------------------
    def _fill_fvg_evidence(self, df, ev: SniperEvidence) -> None:
        if len(df) < 3:
            return
        prev2 = df.iloc[-3]
        prev1 = df.iloc[-2]
        if float(prev1["low"]) > float(prev2["high"]):
            ev.fvg_bullish = True
        if float(prev1["high"]) < float(prev2["low"]):
            ev.fvg_bearish = True

    # ---- exhaustion (advisory only) ----------------------------------------
    def _fill_exhaustion(self, df, ev: SniperEvidence, atr_14: float) -> None:
        cfg = self.config
        if len(df) < 2 or atr_14 <= 0:
            return
        last = df.iloc[-1]
        body = abs(float(last["close"]) - float(last["open"]))
        vol_ma = df["volume"].rolling(20).mean()
        base = float(vol_ma.iloc[-2]) if len(vol_ma) >= 2 and not np.isnan(vol_ma.iloc[-2]) else 0.0
        is_expansion = base > 0 and float(last["volume"]) > base * 1.5
        is_strong = bool(body > (atr_14 * cfg.exhaustion_mult))
        sniper_ema = float(_ema(df["close"], cfg.sniper_ema_len).iloc[-1])
        distance = abs(float(last["close"]) - sniper_ema)
        overextended = bool(distance > (atr_14 * cfg.overextend_mult))
        upper_wick = float(last["high"]) - max(float(last["open"]), float(last["close"]))
        lower_wick = min(float(last["open"]), float(last["close"])) - float(last["low"])
        if is_strong and is_expansion and ev.in_demand_box and upper_wick > body * 0.5 and overextended:
            ev.exhaustion_buy = True
        if is_strong and is_expansion and ev.in_supply_box and lower_wick > body * 0.5 and overextended:
            ev.exhaustion_sell = True
        if is_strong and is_expansion and overextended:
            if upper_wick > body * 0.5 and lower_wick <= body * 0.5:
                ev.exhaustion_buy = True
            if lower_wick > body * 0.5 and upper_wick <= body * 0.5:
                ev.exhaustion_sell = True

    # ---- side scoring -------------------------------------------------------
    def _confluence_for_side(self, side: int, ev: SniperEvidence) -> SniperConfluence:
        score = 0.0
        reasons = []
        if side == LONG:
            if ev.in_demand_box:
                score += 4.0
                reasons.append("in_demand_box")
            if ev.sweep_buy:
                score += 3.0
                reasons.append("liquidity_sweep_low")
            if ev.mss_bullish:
                score += 3.0
                reasons.append("mss_bullish")
            if ev.sfp_buy:
                score += 1.5
                reasons.append("sfp_buy")
            if ev.delta_bullish:
                score += 2.0
                reasons.append("delta_bullish")
            if ev.volume_expansion:
                score += 1.5
                reasons.append("volume_expansion")
            if ev.premium_discount_buy:
                score += 2.0
                reasons.append("premium_discount_discount")
            if ev.fvg_bullish:
                score += 1.5
                reasons.append("fvg_bullish")
            if ev.ema_stack_bullish and ev.adx_bullish:
                score += 2.0
                reasons.append("trend_aligned")
            if ev.price_vs_sniper_ema > 0 and ev.price_vs_trend_ema > 0:
                score += 1.5
                reasons.append("price_above_emas")
            if ev.vwap_aligned_buy:
                score += 1.0
                reasons.append("vwap_aligned")
            if ev.in_supply_box and not ev.sweep_buy:
                score -= 2.5
                reasons.append("in_supply_box")
            if ev.mss_bearish:
                score -= 2.0
                reasons.append("mss_bearish_conflict")
            if ev.delta_bearish:
                score -= 1.5
                reasons.append("delta_bearish")
            if ev.ema_stack_bearish:
                score -= 1.5
                reasons.append("trend_against")
        else:
            if ev.in_supply_box:
                score += 4.0
                reasons.append("in_supply_box")
            if ev.sweep_sell:
                score += 3.0
                reasons.append("liquidity_sweep_high")
            if ev.mss_bearish:
                score += 3.0
                reasons.append("mss_bearish")
            if ev.sfp_sell:
                score += 1.5
                reasons.append("sfp_sell")
            if ev.delta_bearish:
                score += 2.0
                reasons.append("delta_bearish")
            if ev.volume_expansion:
                score += 1.5
                reasons.append("volume_expansion")
            if ev.premium_discount_sell:
                score += 2.0
                reasons.append("premium_discount_premium")
            if ev.fvg_bearish:
                score += 1.5
                reasons.append("fvg_bearish")
            if ev.ema_stack_bearish and ev.adx_bearish:
                score += 2.0
                reasons.append("trend_aligned")
            if ev.price_vs_sniper_ema < 0 and ev.price_vs_trend_ema < 0:
                score += 1.5
                reasons.append("price_below_emas")
            if ev.vwap_aligned_sell:
                score += 1.0
                reasons.append("vwap_aligned")
            if ev.in_demand_box and not ev.sweep_sell:
                score -= 2.5
                reasons.append("in_demand_box")
            if ev.mss_bullish:
                score -= 2.0
                reasons.append("mss_bullish_conflict")
            if ev.delta_bullish:
                score -= 1.5
                reasons.append("delta_bullish")
            if ev.ema_stack_bullish:
                score -= 1.5
                reasons.append("trend_against")
        score = max(MAX_CONFLUENCE_PENALTY, min(MAX_CONFLUENCE_BONUS, score))
        energy = "NEUTRAL"
        if side == LONG:
            if score >= 8:
                energy = "STRONG_BULLISH"
            elif score > 2:
                energy = "BULLISH"
            elif score < -3:
                energy = "BEARISH"
        else:
            if score >= 8:
                energy = "STRONG_BEARISH"
            elif score > 2:
                energy = "BEARISH"
            elif score < -3:
                energy = "BULLISH"
        return SniperConfluence(side=side, score_shift=round(score, 3),
                                primary_energy=energy, reasons=reasons, evidence=ev)


# ---------------------------------------------------------------------------
# Small convenience wrapper (mirrors the engine's analyze_msb style)
# ---------------------------------------------------------------------------
def analyze_sniper(df, symbol="UNKNOWN", side=None,
                   config: Optional[SniperEnrichmentConfig] = None) -> SniperConfluence:
    """Convenience entry point: compute sniper confluence for a side."""
    eng = SniperEnrichmentEngine(
        config if config is not None else SniperEnrichmentConfig.from_env())
    return eng.analyze(df, symbol, side)
