"""Point-in-time OHLCV integrity checks; no data fabrication or side effects."""
from __future__ import annotations
from dataclasses import dataclass, asdict
import time
import pandas as pd

@dataclass(frozen=True)
class DataAudit:
    ok: bool; rows: int; duplicate_timestamps: int; non_monotonic_timestamps: bool
    invalid_ohlc_rows: int; non_positive_rows: int; stale_seconds: float
    stale: bool; reasons: tuple[str, ...]
    def to_dict(self): return asdict(self)

def audit_ohlcv(df: pd.DataFrame, *, max_age_sec=None, now=None) -> DataAudit:
    required=("timestamp","open","high","low","close","volume")
    reasons=[]
    if not isinstance(df,pd.DataFrame) or df.empty:
        return DataAudit(False,0,0,False,0,0,float("inf"),True,("EMPTY_DATA",))
    missing=[c for c in required if c not in df.columns]
    if missing:
        return DataAudit(False,len(df),0,False,0,0,float("inf"),True,("MISSING_COLUMNS:"+",".join(missing),))
    x=df.loc[:,required].copy()
    for c in required[1:]: x[c]=pd.to_numeric(x[c],errors="coerce")
    ts=pd.to_numeric(x["timestamp"],errors="coerce")
    dup=int(ts.duplicated().sum()); mono=not bool(ts.dropna().is_monotonic_increasing)
    hi=x["high"]; lo=x["low"]; op=x["open"]; cl=x["close"]
    bad_ohlc=int(((hi < pd.concat([op,cl,lo],axis=1).max(axis=1)) | (lo > pd.concat([op,cl,hi],axis=1).min(axis=1))).fillna(True).sum())
    non_positive=int(((x[["open","high","low","close"]] <= 0).any(axis=1) | (x["volume"] < 0)).fillna(True).sum())
    last_ts=float(ts.dropna().iloc[-1]) if ts.notna().any() else 0.0
    last_sec=last_ts/1000.0 if last_ts>10_000_000_000 else last_ts
    age=max(0.0,(float(time.time()) if now is None else float(now))-last_sec) if last_sec else float("inf")
    stale=bool(max_age_sec is not None and age>float(max_age_sec))
    if dup: reasons.append("DUPLICATE_TIMESTAMPS")
    if mono: reasons.append("NON_MONOTONIC_TIMESTAMPS")
    if bad_ohlc: reasons.append("INVALID_OHLC")
    if non_positive: reasons.append("NON_POSITIVE_PRICE_OR_VOLUME")
    if stale: reasons.append("STALE_DATA")
    return DataAudit(not reasons,len(x),dup,mono,bad_ohlc,non_positive,age,stale,tuple(reasons))
