"""Pure, dependency-light institutional setup and trade-management intelligence.

This module intentionally does not place orders or mutate exchange state.  It
turns OHLCV into a structured decision snapshot so the existing engine remains
the execution authority.
"""
from __future__ import annotations

import math
import time
from typing import Dict, Optional

import numpy as np
import pandas as pd

from core.market_sessions import session_allows_entry


def _num(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def _atr(df, period=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h-l).abs(), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=period).mean()


def _ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=min(n, len(s))).mean()


def _vwma(s, vol, n):
    return (s * vol).rolling(n, min_periods=n).sum() / (vol.rolling(n, min_periods=n).sum() + 1e-12)


def _rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).rolling(n, min_periods=n).mean()
    dn = (-d.clip(upper=0)).rolling(n, min_periods=n).mean()
    rs = up / (dn + 1e-12)
    return 100 - (100 / (1 + rs))


def _volume_ratio(df, n=20):
    avg = df["volume"].rolling(n, min_periods=max(5, n//2)).mean().iloc[-1]
    return _num(df["volume"].iloc[-1]) / max(_num(avg, 1.0), 1e-12)


def _directional_adx(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    down = -l.diff()
    plus_dm = up.where((up > down) & (up > 0), 0.0)
    minus_dm = down.where((down > up) & (down > 0), 0.0)
    prev = c.shift()
    tr = pd.concat([(h-l).abs(), (h-prev).abs(), (l-prev).abs()], axis=1).max(axis=1)
    atr = tr.rolling(n, min_periods=n).mean()
    pdi = 100 * plus_dm.rolling(n, min_periods=n).sum() / (atr * n + 1e-12)
    mdi = 100 * minus_dm.rolling(n, min_periods=n).sum() / (atr * n + 1e-12)
    dx = 100 * (pdi-mdi).abs() / (pdi+mdi+1e-12)
    adx = dx.rolling(n, min_periods=n).mean()
    return _num(adx.iloc[-1], 0), _num(pdi.iloc[-1], 0), _num(mdi.iloc[-1], 0), _num(adx.iloc[-1] - adx.iloc[-4], 0) if len(adx) >= 4 else 0


def _swing_levels(df, window=5):
    highs, lows = [], []
    start = max(window, len(df)-80)
    for i in range(start, len(df)-window):
        h = _num(df["high"].iloc[i]); l = _num(df["low"].iloc[i])
        if h >= _num(df["high"].iloc[i-window:i+window+1].max()):
            highs.append((i, h))
        if l <= _num(df["low"].iloc[i-window:i+window+1].min()):
            lows.append((i, l))
    return highs, lows


def _liquidity_sweep(df, side, lookback=12):
    highs, lows = _swing_levels(df, 3)
    last_i = len(df)-1
    # Search recent bars so the sweep may precede the displacement/retest.
    hi_levels = [v for i,v in highs if i < last_i]
    lo_levels = [v for i,v in lows if i < last_i]
    for i in range(max(1, len(df)-lookback), len(df)):
        row = df.iloc[i]
        if side == "BUY":
            candidates = [v for v in lo_levels if _num(row["low"]) < v <= _num(df["high"].iloc[max(0,i-1)])]
            if candidates and _num(row["close"]) > _num(row["low"]):
                return True, "SELL_SIDE_SWEEP", i, max(candidates)
        else:
            candidates = [v for v in hi_levels if _num(row["high"]) > v >= _num(df["low"].iloc[max(0,i-1)])]
            if candidates and _num(row["close"]) < _num(row["high"]):
                return True, "BUY_SIDE_SWEEP", i, min(candidates)
    return False, "NONE", -1, 0.0


def _structure(df, side, lookback=30):
    highs, lows = _swing_levels(df, 3)
    if len(highs) < 2 or len(lows) < 2:
        return {"bos": False, "mss": False, "direction": "NEUTRAL", "level": 0.0}
    last = _num(df["close"].iloc[-1])
    ph = highs[-2][1]; lh = highs[-1][1]
    pl = lows[-2][1]; ll = lows[-1][1]
    bull_bos = last > ph
    bear_bos = last < pl
    higher = lh > ph and ll > pl
    lower = lh < ph and ll < pl
    direction = "BULLISH" if bull_bos or higher else "BEARISH" if bear_bos or lower else "NEUTRAL"
    aligned = (side == "BUY" and direction == "BULLISH") or (side == "SELL" and direction == "BEARISH")
    return {"bos": bool((side == "BUY" and bull_bos) or (side == "SELL" and bear_bos)),
            "mss": bool(aligned and (higher or lower)), "direction": direction,
            "level": ph if side == "BUY" else pl}


def _fvg(df, side, lookback=20):
    for i in range(len(df)-1, max(1, len(df)-lookback), -1):
        if i < 2:
            break
        a = df.iloc[i-2]; c = df.iloc[i]
        if side == "BUY" and _num(c["low"]) > _num(a["high"]):
            return True, _num(a["high"]), _num(c["low"]), i
        if side == "SELL" and _num(c["high"]) < _num(a["low"]):
            return True, _num(c["high"]), _num(a["low"]), i
    return False, 0.0, 0.0, -1


def _zone(df, side, atr, price):
    """Find the most causal opposing candle before a strong directional move."""
    n = len(df)
    search = min(50, n-2)
    best = None
    avg_vol = _num(df["volume"].iloc[-20:].mean(), 1.0)
    for j in range(n-search-1, n-2):
        c = df.iloc[j]
        nxt = df.iloc[j+1:min(n, j+5)]
        if len(nxt) == 0:
            continue
        body = abs(_num(c["close"]) - _num(c["open"]))
        if side == "BUY" and _num(c["close"]) >= _num(c["open"]):
            continue
        if side == "SELL" and _num(c["close"]) <= _num(c["open"]):
            continue
        future_move = (_num(nxt["close"].iloc[-1]) - _num(c["high"])) if side == "BUY" else (_num(c["low"]) - _num(nxt["close"].iloc[-1]))
        displacement = future_move / max(atr, 1e-12)
        if displacement < 0.6:
            continue
        low, high = _num(c["low"]), _num(c["high"])
        in_zone = low <= price <= high
        distance = min(abs(price-low), abs(price-high)) / max(price, 1e-12)
        vol_ratio = _num(c["volume"]) / max(avg_vol, 1e-12)
        freshness = max(0.0, 1.0 - (n-j)/max(search,1))
        score = 35 + min(25, displacement*8) + min(15, vol_ratio*5) + (15 if in_zone else max(0, 10-distance*1000)) + freshness*10
        item = {"low":low,"high":high,"mid":(low+high)/2,"score":min(100,score),"displacement_atr":displacement,
                "volume_ratio":vol_ratio,"in_zone":in_zone,"origin_bar":j,"freshness":freshness}
        if best is None or item["score"] > best["score"]:
            best = item
    return best or {"low":0,"high":0,"mid":price,"score":0,"displacement_atr":0,"volume_ratio":1,"in_zone":False,"origin_bar":-1,"freshness":0}


def _retest_state(df, side, zone):
    if not zone.get("low") or not zone.get("high"):
        return "NONE", 0.0
    touched = False; rejected = False; micro = False
    lo, hi = zone["low"], zone["high"]
    for i in range(max(0, len(df)-8), len(df)):
        r = df.iloc[i]
        touch = _num(r["low"]) <= hi and _num(r["high"]) >= lo
        if touch:
            touched = True
            body = abs(_num(r["close"])-_num(r["open"]))
            rng = max(_num(r["high"])-_num(r["low"]), 1e-12)
            if side == "BUY" and _num(r["close"]) > _num(r["open"]) and body/rng > 0.35:
                rejected = True
            if side == "SELL" and _num(r["close"]) < _num(r["open"]) and body/rng > 0.35:
                rejected = True
    if touched:
        # Micro-pullback = shallow counter candle(s) while remaining inside the
        # impulse's directional structure.
        last3 = df.iloc[-3:]
        counter = sum(1 for _,r in last3.iterrows() if (_num(r["close"]) < _num(r["open"]) if side=="BUY" else _num(r["close"]) > _num(r["open"])))
        micro = counter <= 2
    if rejected:
        return "RETEST_CONFIRMED", 1.0
    if touched and micro:
        return "MICRO_PULLBACK", 0.65
    if touched:
        return "PULLBACK", 0.45
    return "WAIT_RETEST", 0.0


def _phase(df, side, atr, adx, vol_ratio, structure, price):
    e50 = _num(_ema(df["close"],50).iloc[-1], price)
    e200 = _num(_ema(df["close"],200).iloc[-1], price) if len(df) >= 200 else price
    body = abs(_num(df["close"].iloc[-1])-_num(df["open"].iloc[-1])) / max(atr,1e-12)
    if vol_ratio >= 1.6 and body >= 1.8 and structure["bos"]:
        return "EXPANSION"
    if vol_ratio >= 1.2 and body >= 0.7 and (structure["bos"] or adx >= 20):
        return "EARLY_EXPANSION"
    if adx >= 25 and ((side=="BUY" and price>e50) or (side=="SELL" and price<e50)):
        return "TREND_BUILDING"
    if len(df) >= 200 and ((side=="BUY" and price>e200) or (side=="SELL" and price<e200)):
        return "BUILDING"
    return "COMPRESSION"


def _accumulation_distribution(df, side, zone, sweep, vol_ratio):
    recent = df.iloc[-10:]
    ranges = (recent["high"]-recent["low"]).replace(0,np.nan)
    compression = _num(ranges.mean()) < _num((df["high"]-df["low"]).iloc[-30:].mean()) * 0.85
    wick_buy = ((recent[["open","close"]].min(axis=1)-recent["low"]) / ranges).mean()
    wick_sell = ((recent["high"]-recent[["open","close"]].max(axis=1)) / ranges).mean()
    if side == "BUY" and sweep and compression and wick_buy > 0.35 and zone.get("score",0) >= 55:
        return "ACCUMULATION"
    if side == "SELL" and sweep and compression and wick_sell > 0.35 and zone.get("score",0) >= 55:
        return "DISTRIBUTION"
    if side == "BUY" and vol_ratio > 1.4 and wick_sell > 0.3 and zone.get("score",0) >= 65:
        return "DISTRIBUTION_RISK"
    if side == "SELL" and vol_ratio > 1.4 and wick_buy > 0.3 and zone.get("score",0) >= 65:
        return "ACCUMULATION_RISK"
    return "NEUTRAL"


def analyze_setup(df: pd.DataFrame, side: str, entry_price: float, atr: Optional[float] = None, symbol: Optional[str] = None) -> Dict:
    if df is None or not isinstance(df,pd.DataFrame) or len(df) < 40:
        return {"valid":False,"reason":"INSUFFICIENT_DATA","score":0.0}
    side = str(side).upper(); price = _num(entry_price, _num(df["close"].iloc[-1]))
    session_ok, session = session_allows_entry(symbol, None) if symbol else (True, {"state":"UNKNOWN","session_quality":1.0,"hard_blocked":False,"pair":None,"indicator_sessions":[]})
    atr_v = _num(atr, _num(_atr(df).iloc[-1], price*0.01))
    adx,pdi,mdi,adx_slope = _directional_adx(df)
    vol_ratio = _volume_ratio(df)
    structure = _structure(df,side)
    sweep, sweep_type, sweep_bar, sweep_level = _liquidity_sweep(df,side)
    fvg,fvg_low,fvg_high,fvg_bar = _fvg(df,side)
    zone = _zone(df,side,atr_v,price)
    retest,retest_score = _retest_state(df,side,zone)
    phase = _phase(df,side,atr_v,adx,vol_ratio,structure,price)
    behaviour = _accumulation_distribution(df,side,zone,sweep,vol_ratio)
    rsi = _num(_rsi(df["close"]).iloc[-1],50)
    e50 = _num(_ema(df["close"],50).iloc[-1],price)
    e200 = _num(_ema(df["close"],200).iloc[-1],price) if len(df)>=200 else None
    vwmas = {}
    for n in (8,13,21,34):
        if len(df)>=n: vwmas[n]=_num(_vwma(df["close"],df["volume"],n).iloc[-1],price)
    vwma_bull = len(vwmas)==4 and all(vwmas[a] > vwmas[b] for a,b in ((8,13),(13,21),(21,34)))
    vwma_bear = len(vwmas)==4 and all(vwmas[a] < vwmas[b] for a,b in ((8,13),(13,21),(21,34)))
    htf_bull = e200 is not None and price > e200 and e50 > e200
    htf_bear = e200 is not None and price < e200 and e50 < e200
    aligned = ((side=="BUY" and pdi>mdi) or (side=="SELL" and mdi>pdi))
    in_zone = bool(zone.get("in_zone"))
    distance_atr = min(abs(price-zone.get("low",price)),abs(price-zone.get("high",price))) / max(atr_v,1e-12) if zone.get("low") else 999
    evidence = {
        "liquidity_sweep": sweep, "sweep_type": sweep_type, "sweep_bar": sweep_bar,
        "structure_bos": structure["bos"], "structure_mss": structure["mss"],
        "structure_direction": structure["direction"], "fvg": fvg,
        "displacement": zone.get("displacement_atr",0)>=0.75,
        "zone": zone, "retest": retest, "retest_score": retest_score,
        "volume_ratio": round(vol_ratio,3), "adx": round(adx,2), "adx_slope": round(adx_slope,2),
        "pdi":round(pdi,2),"mdi":round(mdi,2),"rsi":round(rsi,2),
        "ema50":e50,"ema200":e200,"vwma_stack_bull":vwma_bull,"vwma_stack_bear":vwma_bear,
        "htf_bull":htf_bull,"htf_bear":htf_bear,"phase":phase,"behaviour":behaviour,
        "in_zone":in_zone,"distance_atr":round(distance_atr,3),"fvg_low":fvg_low,"fvg_high":fvg_high,
        "session": session,
    }
    score = 0.0
    score += 22 if sweep else 0
    score += 18 if structure["bos"] or structure["mss"] else 0
    score += min(18, max(0, zone.get("score",0)-40)*0.45)
    score += 12 if evidence["displacement"] else 0
    score += 10 if fvg else 0
    score += 8 if retest in ("RETEST_CONFIRMED","MICRO_PULLBACK") else 0
    score += 6 if vol_ratio >= 1.2 else 0
    score += 6 if aligned and adx >= 18 else 0
    score += 5 if ((side=="BUY" and vwma_bull) or (side=="SELL" and vwma_bear)) else 0
    score += 5 if ((side=="BUY" and htf_bull) or (side=="SELL" and htf_bear)) else 0
    if behaviour in ("DISTRIBUTION_RISK","ACCUMULATION_RISK"):
        score -= 18
    if distance_atr > 2.5:
        score -= 20
    if phase in ("EXPANSION",) and distance_atr > 1.5:
        score -= 15
    # Session awareness is a timing/ranking factor, not a universal hard gate.
    # FX/equity hard-gates are enforced separately when explicitly enabled.
    if session.get("state") == "PREFERRED":
        score += 4
    elif session.get("state") == "QUIET":
        score -= 8 if session.get("instrument") == "FOREX" else 2
    if session.get("hard_blocked"):
        score -= 25
    score = max(0,min(100,score))
    if behaviour == "DISTRIBUTION_RISK" or (side=="BUY" and rsi>75) or (side=="SELL" and rsi<25):
        timing = "LATE_OR_DISTRIBUTION"
    elif retest == "RETEST_CONFIRMED":
        timing = "RETEST_ENTRY"
    elif retest == "MICRO_PULLBACK":
        timing = "MICRO_PULLBACK_ENTRY"
    elif phase == "EARLY_EXPANSION":
        timing = "EARLY_EXPANSION"
    elif in_zone:
        timing = "ZONE_WAIT_CONFIRMATION"
    else:
        timing = "WAIT_RETEST"
    if phase == "EXPANSION" or (adx>=35 and vol_ratio>=1.5):
        trade_style = "SWING" if ((side=="BUY" and htf_bull) or (side=="SELL" and htf_bear)) else "TREND"
    elif timing in ("MICRO_PULLBACK_ENTRY","RETEST_ENTRY") and distance_atr <= 1.2:
        trade_style = "SCALP"
    else:
        trade_style = "SWING" if ((side=="BUY" and htf_bull) or (side=="SELL" and htf_bear)) else "SCALP"
    ready = (score >= 68 and sweep and (structure["bos"] or structure["mss"]) and zone.get("score",0)>=50
             and timing not in ("LATE_OR_DISTRIBUTION",) and session_ok)
    return {"valid":True,"score":round(score,1),"side":side,"price":price,"atr":atr_v,
            "trade_style":trade_style,"timing":timing,"phase":phase,"behaviour":behaviour,
            "ready":bool(ready),"evidence":evidence,"timestamp":time.time(),
            "session_ok": bool(session_ok), "session_state": session.get("state", "UNKNOWN"),
            "instrument": session.get("instrument", "UNKNOWN"), "pair": session.get("pair")}

class TradeManagementBoard:
    """Stateful, advisory 'board of directors' for an open position.

    It observes the original thesis and current market evidence. It never sends
    orders; the existing LiveTradeManager / Unified brain remains the only
    execution authority.
    """
    STAGES = ("ENTRY", "CONFIRMATION", "HEALTH", "PULLBACK", "CONTINUATION", "PROFIT", "DISTRIBUTION", "EXIT")

    def __init__(self):
        self.stage = "ENTRY"
        self.history = []
        self.last = None

    def evaluate(self, snapshot: Dict, *, roe: float = 0.0, thesis_failure: float = 0.0,
                 continuation_probability: float = 0.5, distribution_risk: float = 0.0,
                 drawdown_from_peak: float = 0.0) -> Dict:
        if not snapshot or not snapshot.get("valid"):
            result = {"stage":"ENTRY","verdict":"WAIT","score":0.0,"reason":"NO_MARKET_SNAPSHOT"}
        else:
            phase = snapshot.get("phase","COMPRESSION")
            timing = snapshot.get("timing","WAIT_RETEST")
            behaviour = snapshot.get("behaviour","NEUTRAL")
            retest = (snapshot.get("evidence") or {}).get("retest","NONE")
            ready = bool(snapshot.get("ready"))
            score = _num(snapshot.get("score"))
            if thesis_failure >= 70 or distribution_risk >= 80:
                stage, verdict = "EXIT", "DEFEND"
            elif distribution_risk >= 60 or behaviour == "DISTRIBUTION_RISK":
                stage, verdict = "DISTRIBUTION", "PROTECT_PROFIT"
            elif roe > 0 and (phase == "EXPANSION" or continuation_probability >= 0.75):
                stage, verdict = "CONTINUATION", "HOLD_RUNNER"
            elif roe > 0 and retest in ("PULLBACK","MICRO_PULLBACK","RETEST_CONFIRMED"):
                stage, verdict = "PULLBACK", "HOLD" if continuation_probability >= 0.58 else "PROTECT"
            elif roe > 0:
                stage, verdict = "PROFIT", "HOLD" if continuation_probability >= 0.58 else "PROTECT"
            elif ready and timing in ("EARLY_EXPANSION","RETEST_ENTRY","MICRO_PULLBACK_ENTRY"):
                stage, verdict = "CONFIRMATION", "HOLD_ENTRY_THESIS"
            else:
                stage, verdict = "HEALTH", "MONITOR"
            if drawdown_from_peak > 15 and roe > 0:
                verdict = "PROTECT" if stage not in ("EXIT", "DISTRIBUTION") else verdict
            result = {"stage":stage,"verdict":verdict,"score":round(score,1),
                      "reason":f"phase={phase}; timing={timing}; retest={retest}; continuation={continuation_probability:.2f}"}
        result["timestamp"] = time.time()
        result["history_size"] = len(self.history)+1
        self.stage = result["stage"]
        self.last = result
        self.history.append(result)
        if len(self.history) > 30:
            self.history = self.history[-30:]
        return result

    def to_dict(self):
        return {"stage":self.stage,"last":self.last,"history":self.history[-10:]}
