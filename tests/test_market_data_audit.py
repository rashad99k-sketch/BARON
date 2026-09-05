import pandas as pd
from tools.market_data_audit import audit_ohlcv

def base():
    return pd.DataFrame({"timestamp":[1000,2000,3000],"open":[10,11,12],"high":[11,12,13],"low":[9,10,11],"close":[10.5,11.5,12.5],"volume":[100,120,130]})

def test_clean_data_passes(): assert audit_ohlcv(base(),now=4,max_age_sec=2).ok

def test_bad_ohlc_is_rejected():
    df=base(); df.loc[1,"high"]=5; r=audit_ohlcv(df); assert not r.ok and "INVALID_OHLC" in r.reasons

def test_duplicate_and_non_monotonic_are_rejected():
    df=base(); df.loc[2,"timestamp"]=1000; r=audit_ohlcv(df); assert not r.ok and "DUPLICATE_TIMESTAMPS" in r.reasons and "NON_MONOTONIC_TIMESTAMPS" in r.reasons

def test_stale_data_is_rejected_when_policy_supplied():
    r=audit_ohlcv(base(),now=3005,max_age_sec=2); assert not r.ok and "STALE_DATA" in r.reasons
