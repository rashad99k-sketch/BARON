"""VPA / Volume Intelligence layer (Effort-vs-Result) for BARON.

Additive analysis built ONLY on closed candles. It never replaces the causal
Order Block engine -- it interrogates it (OB Volume DNA). Core doctrine:

  * Effort  = Volume.  Result = price displacement + range + close position.
  * High effort + low result while advancing against the thesis = ABSORPTION /
    distribution risk (possible accumulation/distribution, never "high volume =
    strength").
  * A retest of a bullish OB is HEALTHY when volume shrinks, selling candles
    shrink, lower wicks appear and the candle rejects. It is UNDER_ATTACK when
    price closes through the OB with expanding directional volume (demand
    failure / supply override).
  * A climax candle (record relative volume with a wide range that fails to
    hold) signals effort exhaustion.

All functions are pure: they take a pandas DataFrame with columns
(open, high, low, close, volume) plus side / atr / optional zone bounds and
return plain dicts, so the module stays side-effect free and unit-testable.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

import pandas as pd


def _guard(df: Optional[pd.DataFrame], need_volume: bool = True) -> bool:
    if df is None or not isinstance(df, pd.DataFrame) or df.empty:
        return False
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            return False
    if need_volume and "volume" not in df.columns:
        return False
    return True


def atr_proxy(df: pd.DataFrame, atr: float, candles: int = 14) -> float:
    """Fallback ATR when the caller did not provide one."""
    if atr and atr > 0:
        return float(atr)
    if not _guard(df, need_volume=False) or len(df) < 2:
        return 0.0
    trs = []
    for i in range(max(0, len(df) - candles), len(df)):
        row = df.iloc[i]
        prev_close = float(df["close"].iloc[i - 1]) if i > 0 else float(row["open"])
        tr = max(float(row["high"]) - float(row["low"]),
                 abs(float(row["high"]) - prev_close),
                 abs(float(row["low"]) - prev_close))
        trs.append(tr)
    return float(sum(trs) / len(trs)) if trs else 0.0


def volume_ratio(df: pd.DataFrame, last: int = 1, base: int = 20) -> float:
    """Average volume of the last `last` bars / average of the pre-window base."""
    if not _guard(df) or len(df) < base + last:
        return 1.0
    base_avg = float(df["volume"].iloc[len(df) - base - last:len(df) - last].mean())
    if base_avg <= 0:
        return 1.0
    win_avg = float(df["volume"].iloc[len(df) - last:].mean())
    return float(win_avg / base_avg)


def _candle_shapes(df: pd.DataFrame, side: str, atr: float, lookback: int = 3) -> dict:
    """Window aggregates over the last `lookback` completed candles.

    Returns directional body sum, efficiency, wick-against-thesis, down/up
    volume and range — everything Effort/Result needs.
    """
    side = str(side).upper()
    n = len(df)
    lo = max(0, n - lookback)
    window = df.iloc[lo:n]
    sign = 1.0 if side == "BUY" else -1.0
    body_sum = 0.0
    range_sum = 0.0
    eff_sum = 0.0
    wick_against = 0.0
    vol_sum = 0.0
    down_vol = 0.0
    up_vol = 0.0
    count = 0
    for _, c in window.iterrows():
        body = (float(c["close"]) - float(c["open"])) * sign
        rng = float(c["high"]) - float(c["low"])
        body_sum += body
        range_sum += rng
        vol_sum += float(c["volume"])
        if rng > 0:
            eff_sum += body / rng
        # Wick against the thesis (resistance above for BUY wicks, below for SELL).
        if side == "BUY":
            wick_against += max(0.0, float(c["high"]) - max(float(c["open"]), float(c["close"])))
        else:
            wick_against += max(0.0, min(float(c["open"]), float(c["close"])) - float(c["low"]))
        if float(c["close"]) < float(c["open"]):
            down_vol += float(c["volume"])
        else:
            up_vol += float(c["volume"])
        count += 1
    eff = eff_sum / count if count else 0.0
    return {
        "count": count,
        "body_sum": body_sum,
        "range_sum": range_sum,
        "efficiency": eff,
        "wick_against_atr": wick_against / atr if atr > 0 else 0.0,
        "vol": vol_sum,
        "down_vol": down_vol,
        "up_vol": up_vol,
        "net_atr": body_sum / atr if atr > 0 else 0.0,
    }


def effort_result(df: pd.DataFrame, side: str, atr: float, lookback: int = 3) -> dict:
    """Classify the Effort-vs-Result of the latest `lookback` closed bars.

    status:
      CONFIRMED       price progressed >=0.8 ATR with effective bodies (healthy
                      volume either high-result or low-effort continuation).
      WEAK_RESULT     high effort, weak/negative result -> absorption or
                      distribution risk. Never treat as strength.
      CLIMAX          >=1.8 ATR move on expanded volume (>=2.0x) with wide range
                      that stalls -> effort exhaustion / blow-off.
      STALL           flat price with contracting effort.
      EXHAUSTION      volume collapse with prior strong move (petering out).
    """
    if not _guard(df) or len(df) < lookback + 2:
        return {"status": "STALL", "net_atr": 0.0, "volume_ratio": 1.0,
                "efficiency": 0.0, "wick_against_atr": 0.0, "reasons": ["insufficient_data"]}
    atr = atr_proxy(df, atr)
    if atr <= 0:
        return {"status": "STALL", "net_atr": 0.0, "volume_ratio": 1.0,
                "efficiency": 0.0, "wick_against_atr": 0.0, "reasons": ["no_atr"]}
    shapes = _candle_shapes(df, side, atr, lookback)
    vr = volume_ratio(df, last=lookback, base=20)
    net = shapes["net_atr"]
    eff = shapes["efficiency"]
    wick = shapes["wick_against_atr"]
    reasons: List[str] = []
    status = "STALL"
    if vr >= 2.0 and net >= 1.8:
        status = "CLIMAX"
        reasons.append(f"blowoff net={net:.2f}ATR vol={vr:.2f}x")
    elif vr >= 1.5 and net < 0.4:
        status = "WEAK_RESULT"
        reasons.append(f"high_effort_low_result vol={vr:.2f}x net={net:.2f}ATR")
    elif net <= -0.3:
        status = "WEAK_RESULT"
        reasons.append(f"price_fighting_thesis net={net:.2f}ATR")
    elif vr >= 1.5 and net >= 0.8 and eff >= 0.45:
        status = "CONFIRMED"
        reasons.append(f"effort_confirmed vol={vr:.2f}x net={net:.2f}ATR")
    elif vr < 0.8 and net >= 0.8:
        status = "CONFIRMED"
        reasons.append(f"low_effort_clean_move vol={vr:.2f}x net={net:.2f}ATR")
    elif vr < 0.7:
        status = "STALL"
        reasons.append(f"no_effort vol={vr:.2f}x")
    elif wick >= 0.8 and net < 0.8:
        status = "WEAK_RESULT"
        reasons.append(f"wick_against_thesis={wick:.2f}ATR")
    else:
        status = "STALL"
        reasons.append(f"mixed vol={vr:.2f}x net={net:.2f}ATR")
    return {
        "status": status,
        "net_atr": round(net, 2),
        "volume_ratio": round(vr, 2),
        "efficiency": round(eff, 2),
        "wick_against_atr": round(wick, 2),
        "reasons": reasons,
    }


def _near_zone(price: float, zone_low: float, zone_high: float, atr: float,
               tolerance_atr: float = 0.5) -> bool:
    if zone_low <= 0 or zone_high <= 0 or zone_high < zone_low:
        return price is not None
    if zone_low <= price <= zone_high:
        return True
    tol = max(atr * tolerance_atr, 1e-12)
    return min(abs(price - zone_low), abs(price - zone_high)) <= tol


def retest_health(df: pd.DataFrame, side: str, atr: float,
                  zone_low: float, zone_high: float, lookback: int = 6) -> dict:
    """Grade the most recent interaction with a causal OB zone.

    status:
      HEALTHY       price returns to the zone, selling/demand pressure shrinks,
                    a rejection wick appears and the candle holds the zone.
      UNDER_ATTACK  price closes through the zone with expanding directional
                    volume (bullish OB: close below + big bearish volume ->
                    demand failure / supply override).
      NOT_IN_ZONE   price is not currently interacting with the zone.
      NEUTRAL       interacting but ambiguous.
    """
    if not _guard(df) or len(df) < max(3, lookback):
        return {"status": "NEUTRAL", "reasons": ["insufficient_data"]}
    atr = atr_proxy(df, atr)
    if atr <= 0 or zone_low <= 0 or zone_high <= 0 or zone_high < zone_low:
        return {"status": "NEUTRAL", "reasons": ["bad_zone_or_atr"]}
    side = str(side).upper()
    last = df.iloc[-1]
    price = float(last["close"])
    if not _near_zone(price, zone_low, zone_high, atr, tolerance_atr=0.75):
        return {"status": "NOT_IN_ZONE", "reasons": [f"price={price:.5g} outside zone"]}

    window = df.iloc[max(0, len(df) - lookback):]
    touch_vol = float(window["volume"].mean()) if "volume" in window else 0.0
    base_avg = float(df["volume"].iloc[max(0, len(df) - 3 * lookback):len(df) - lookback].mean()) \
        if "volume" in df and len(df) >= 3 * lookback else touch_vol
    vol_ratio = touch_vol / base_avg if base_avg > 0 else 1.0

    if side == "BUY":
        sell_vol = sum(float(c["volume"]) for _, c in window.iterrows() if float(c["close"]) < float(c["open"]))
        sell_frac = sell_vol / float(window["volume"].sum()) if float(window["volume"].sum()) > 0 else 0.5
        close_below = price < zone_low
        lower_wick = min(float(last["open"]), float(last["close"])) - float(last["low"])
        pin = lower_wick >= max(0.6 * atr, 1.2 * abs(float(last["close"]) - float(last["open"])))
        hold = float(last["close"]) > float(last["open"])
    else:
        sell_vol = sum(float(c["volume"]) for _, c in window.iterrows() if float(c["close"]) > float(c["open"]))
        sell_frac = sell_vol / float(window["volume"].sum()) if float(window["volume"].sum()) > 0 else 0.5
        close_below = price > zone_high
        lower_wick = float(last["high"]) - max(float(last["open"]), float(last["close"]))
        pin = lower_wick >= max(0.6 * atr, 1.2 * abs(float(last["close"]) - float(last["open"])))
        hold = float(last["close"]) < float(last["open"])

    reasons: List[str] = []
    if close_below and vol_ratio >= 1.3 and sell_frac >= 0.6:
        reasons.append(f"close_through_zone vol={vol_ratio:.2f}x sell_frac={sell_frac:.2f}")
        return {"status": "UNDER_ATTACK", "vol_ratio": round(vol_ratio, 2),
                "sell_frac": round(sell_frac, 2), "reasons": reasons,
                "close_side": ("BELOW" if side == "BUY" else "ABOVE")}
    if close_below:
        reasons.append(f"close_through_zone price={price:.5g}")
        return {"status": "UNDER_ATTACK", "vol_ratio": round(vol_ratio, 2),
                "sell_frac": round(sell_frac, 2), "reasons": reasons,
                "close_side": ("BELOW" if side == "BUY" else "ABOVE")}
    if pin and hold and sell_frac <= 0.55:
        reasons.append(f"rejection_pin wick={lower_wick / atr:.2f}ATR vol={vol_ratio:.2f}x")
        return {"status": "HEALTHY", "vol_ratio": round(vol_ratio, 2),
                "sell_frac": round(sell_frac, 2), "reasons": reasons, "close_side": "HELD"}
    if vol_ratio <= 0.85 and sell_frac <= 0.45:
        reasons.append(f"shrinking_pressure vol={vol_ratio:.2f}x sell_frac={sell_frac:.2f}")
        return {"status": "HEALTHY", "vol_ratio": round(vol_ratio, 2),
                "sell_frac": round(sell_frac, 2), "reasons": reasons, "close_side": "HELD"}
    reasons.append(f"ambiguous_at_zone vol={vol_ratio:.2f}x sell_frac={sell_frac:.2f}")
    return {"status": "NEUTRAL", "vol_ratio": round(vol_ratio, 2),
            "sell_frac": round(sell_frac, 2), "reasons": reasons, "close_side": "HELD"}


def peak_volume_ratio(df: pd.DataFrame, start: int, end: int, base_window: int = 10) -> float:
    """Peak volume in [start, end] / average of the pre-base window."""
    if not _guard(df) or start < 0 or end < start or end >= len(df):
        return 1.0
    base_avg = float(df["volume"].iloc[max(0, start - base_window):start].mean())
    if base_avg <= 0:
        return 1.0
    peak = float(df["volume"].iloc[start:end + 1].max())
    return float(peak / base_avg)


def _latest_causal_ob(df: pd.DataFrame, side: str, atr: float) -> Optional[dict]:
    """Compact causal-OB scan mirroring the engine's `_select_strong_ob` shape
    (bounded, cheap): finds the most recent base candle that displaced with a
    confirmed directional result. Returns zone bounds + displacement + volume."""
    side = str(side).upper()
    n = len(df)
    lookback_start = max(2, n - 35)
    best = None
    for i in range(lookback_start, n - 2):
        base = df.iloc[i]
        body = abs(float(base["close"]) - float(base["open"]))
        rng = max(float(base["high"]) - float(base["low"]), 1e-12)
        if body / rng > 0.75:
            continue
        if side == "BUY" and float(base["close"]) >= float(base["open"]):
            continue
        if side == "SELL" and float(base["close"]) <= float(base["open"]):
            continue
        future = df.iloc[i + 1:min(n, i + 4)]
        if future.empty:
            continue
        if side == "BUY":
            displacement = float(future["close"].max()) - float(base["high"])
            directional = float(future["close"].iloc[-1]) > float(base["high"])
            zl, zh = float(base["low"]), float(base["open"])
        else:
            displacement = float(base["low"]) - float(future["close"].min())
            directional = float(future["close"].iloc[-1]) < float(base["low"])
            zl, zh = float(base["open"]), float(base["high"])
        disp_atr = displacement / atr if atr > 0 else 0.0
        if disp_atr < 0.6 or not directional:
            continue
        vol_avg = float(df["volume"].iloc[max(0, i - 10):i].mean()) if "volume" in df else 0.0
        disp_vol = float(df["volume"].iloc[i + 1:min(n, i + 4)].max()) if "volume" in df else 0.0
        vr = disp_vol / vol_avg if vol_avg > 0 else 1.0
        price = float(df["close"].iloc[-1])
        dist = min(abs(price - zl), abs(price - zh))
        rec = {"bar": i, "zone_low": zl, "zone_high": zh,
               "displacement_atr": round(disp_atr, 2), "volume_ratio": round(vr, 2),
               "dist_atr": round(dist / atr, 2) if atr > 0 else 0.0}
        if best is None or rec["bar"] > best["bar"]:
            best = rec
    return best


def opposing_ob_conflict(df: pd.DataFrame, side: str, atr: float,
                         zone_low: float, zone_high: float) -> dict:
    """Does a strong opposing OB sit nearby and 'own' the current price?

    A freshly-displaced opposing OB (>=0.8 ATR displacement, decent volume)
    whose domain still reaches the current price is a direct conflict:
    entering into it risks supply/demand override. Returns
    {present, side, displacement_atr, volume_ratio, dist_atr, zone_low, zone_high}.
    """
    if not _guard(df) or atr <= 0:
        return {"present": False}
    opp_side = "SELL" if str(side).upper() == "BUY" else "BUY"
    ob = _latest_causal_ob(df, opp_side, atr)
    if ob is None or ob.get("displacement_atr", 0) < 0.8 or ob.get("volume_ratio", 0) < 1.2:
        return {"present": False}
    price = float(df["close"].iloc[-1])
    zl, zh = float(ob["zone_low"]), float(ob["zone_high"])
    if not _near_zone(price, zl, zh, atr, tolerance_atr=1.0) and \
            not (zl - atr * 0.5 <= price <= zh + atr * 0.5):
        return {"present": False}
    return {
        "present": True,
        "side": opp_side,
        "displacement_atr": ob["displacement_atr"],
        "volume_ratio": ob["volume_ratio"],
        "dist_atr": ob["dist_atr"],
        "zone_low": round(zl, 6),
        "zone_high": round(zh, 6),
    }


def volume_validation(df: pd.DataFrame, side: str, atr: float,
                      displacement_atr: float, ob_volume_ratio: float,
                      retest: dict) -> Tuple[str, dict]:
    """Map raw volume + retest evidence onto the STRONG-tier Volume gate.

    Returns one of:
      VOLUME_CONFIRMED                  healthy effort/result during displacement.
      DISPLACEMENT_VOLUME_CONFIRMED     displacement + expanded volume.
      ABSORPTION_CONFIRMED              healthy retest / absorption at the zone.
      UNDER_ATTACK                      retest is being overridden (BLOCK).
      NO_CONFIRMATION                   nothing has actually been confirmed yet.
    """
    status = "UNDER_ATTACK" if (retest or {}).get("status") == "UNDER_ATTACK" else ""
    reasons: List[str] = []
    if status == "UNDER_ATTACK":
        reasons.append("retest_under_attack")
        return status, {"status": status, "reasons": reasons}
    eff = effort_result(df, side, atr, lookback=3)
    if displacement_atr >= 0.8 and ob_volume_ratio >= 1.5:
        status = "DISPLACEMENT_VOLUME_CONFIRMED"
        reasons.append(f"disp={displacement_atr:.2f}ATR vol={ob_volume_ratio:.2f}x")
    elif displacement_atr >= 1.0 and ob_volume_ratio >= 1.2:
        status = "VOLUME_CONFIRMED"
        reasons.append(f"disp={displacement_atr:.2f}ATR vol={ob_volume_ratio:.2f}x")
    elif (retest or {}).get("status") == "HEALTHY" or (retest or {}).get("status") == "NEUTRAL":
        if eff.get("status") in ("CONFIRMED", "STALL", "WEAK_RESULT"):
            status = "ABSORPTION_CONFIRMED"
            reasons.append("healthy_retest_absorption")
        else:
            status = "NO_CONFIRMATION"
            reasons.append(f"effort={eff.get('status')}")
    else:
        status = "NO_CONFIRMATION"
        reasons.append(f"retest={retest.get('status')} eff={eff.get('status')}")
    return status, {"status": status, "reasons": reasons, "effort_result": eff.get("status")}


def ob_volume_dna(df: pd.DataFrame, side: str, atr: float,
                  displacement_start: int = -1, zone_low: float = 0.0,
                  zone_high: float = 0.0, base_window: int = 10) -> dict:
    """Full OB Volume DNA: pull the volume-format of the causal OB.

    Composes base volume, displacement volume, ratio, effort/result, retest
    health, volume climax, demand/supply confirmation, retest volume and
    opposing OBs into one interrogator dict for OB grading / STRONG tier /
    profit management. Always returns a dict; never raises.
    """
    if not _guard(df) or atr <= 0 or len(df) < 25:
        return {"complete": False}
    side = str(side).upper()
    try:
        if displacement_start < 0:
            ob = _latest_causal_ob(df, side, atr)
            dstart = ob["bar"] if ob else max(0, len(df) - 8)
        else:
            dstart = displacement_start
        dstart = max(0, int(dstart))
        dend = min(len(df) - 1, dstart + 3)
        base_vol = float(df["volume"].iloc[max(0, dstart - base_window):dstart].mean()) \
            if "volume" in df else 0.0
        disp_vol = float(df["volume"].iloc[min(dstart, len(df) - 1):dend + 1].max()) \
            if "volume" in df else 0.0
        vr = disp_vol / base_vol if base_vol > 0 else 1.0
        eff = effort_result(df, side, atr, lookback=3)
        retest = retest_health(df, side, atr, zone_low, zone_high)
        climax = vr >= 2.0
        demand_conf = vr >= 1.5 and eff.get("status") == "CONFIRMED"
        absorption = eff.get("status") == "WEAK_RESULT" and (retest.get("status") in ("HEALTHY", "NEUTRAL"))
        retest_vol = float(df["volume"].iloc[max(0, len(df) - 6):].mean()) / base_vol if base_vol > 0 else 1.0
        with_supply = opposing_ob_conflict(df, side, atr, zone_low, zone_high)
        try:
            eff_status = eff.get("status", "STALL")
        except Exception:
            eff_status = "STALL"
        return {
            "complete": True,
            "base_volume": round(base_vol, 4),
            "displacement_volume": round(disp_vol, 4),
            "volume_ratio": round(vr, 2),
            "volume_climax": climax,
            "effort_result": eff_status,
            "effort_detail": eff,
            "absorption": absorption,
            "demand_confirmation": demand_conf,
            "retest_volume": round(retest_vol, 2),
            "retest": retest,
            "under_attack": retest.get("status") == "UNDER_ATTACK",
            "opposing_conflict": with_supply,
        }
    except Exception:
        return {"complete": False}