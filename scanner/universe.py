"""Multi-market universe construction for BingX TradFi + crypto.

The connected BingX venue exposes crypto plus TradFi perpetual/CFD-style
markets. This module classifies instruments without inventing symbols and
builds a balanced radar universe so crypto cannot crowd out stocks, indices,
gold, or oil.
"""
from __future__ import annotations
import os
from collections import defaultdict
from typing import Dict, Iterable, List

ASSET_CLASSES = ("CRYPTO", "STOCK", "INDEX", "METAL", "GOLD", "OIL", "FOREX", "ENERGY")

STOCKS = set(x.strip().upper() for x in os.getenv("STOCK_SYMBOL_HINTS", "".join([
    "AAPL,AMZN,GOOGL,MSFT,NVDA,META,TSLA,NFLX,AMD,INTC,AVGO,ORCL,CRM,ADBE,",
    "JPM,BAC,GS,MS,MU,PLTR,MSTR,COIN,HOOD,SOFI,UBER,SHOP,DELL,MRVL,SNDK,",
    "CRCL,SMCI,ARM,TSM,QCOM,CSCO,IBM,LLY,NVO,COST,WMT,DIS,NKE,BA,CAT"
])).split(",") if x.strip())

INDEX_HINTS = ("US500", "USSTECH", "USTECH", "US30", "DJI", "SPX", "SP500",
               "NASDAQ", "NDX", "DAX", "DE40", "GER40", "FTSE", "UK100", "CAC",
               "FRA40", "NIKKEI", "JP225", "HSI", "HK50", "AUS200", "EU50", "STOXX")
GOLD_HINTS = ("XAU", "GOLD")
METAL_HINTS = ("XAG", "SILVER", "PALLADIUM", "PLATINUM", "NICKEL",
               "ALUMINIUM", "ALUMINUM", "ZINC", "LEAD", "COPPER")
OIL_HINTS = ("OIL", "WTI", "BRENT", "CRUDE", "HEATINGOIL", "OILHEATING", "GASOLINE")
ENERGY_HINTS = ("NATURALGAS", "NATGAS",
                "COFFEE", "COCOA", "SOYBEAN", "SOYBEANS", "SUGAR", "WHEAT", "CORN",
                "GASOLINE", "COAL")
FOREX_HINTS = ("EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD", "SEK", "NOK",
               "PLN", "MXN", "ZAR", "TRY", "CNH", "HKD", "SGD", "THB", "DKK")


def _text(symbol: str, market: dict) -> str:
    info = market.get("info") or {}
    values = [symbol, market.get("base", ""), market.get("quote", ""),
              market.get("type", ""), market.get("name", "")]
    if isinstance(info, dict):
        for k in ("name", "displayName", "symbol", "asset", "assetType", "category",
                  "type", "contractType"):
            values.append(str(info.get(k, "")))
    return " ".join(map(str, values)).upper()


# Canonical prefix resolution: the venue uses NCSI for indices, NCSK for stocks,
# NCCO for commodity/energy, NCCX/O for gold crosses.
NCSI_PREFIX = "NCSI"
NCSK_PREFIX = "NCSK"
NCCO_PREFIX = "NCCO"


def canonical_symbol(symbol: str) -> str:
    """Normalize NCSI/NCSK-prefixed tradfi symbols to their canonical entity."""
    if not symbol:
        return symbol
    plain = str(symbol).upper().replace("/USDT", "").replace(":USDT", "").replace("USDT", "")
    for prefix in (NCSI_PREFIX, NCSK_PREFIX, NCCO_PREFIX):
        if plain.startswith(prefix):
            plain = plain[len(prefix):]
            break
    if plain.endswith("2USD"):
        plain = plain[:-4]
    return plain


def classify(symbol: str, market: dict) -> tuple:
    """Classify an instrument with confidence metadata.

    Returns (asset_class, source, confidence):
      source = "metadata" | "pattern" | "unknown"
      confidence in [0.0, 1.0]; asset_class = "UNKNOWN" when low.
    """
    text = _text(symbol, market)
    base = str(market.get("base", symbol)).upper().split("/")[0].replace("-USDT", "")
    info = market.get("info") or {}
    display = str(info.get("displayName", "")).upper()
    canon = canonical_symbol(symbol)
    # 1. Metadata: canonical prefix → class, highest confidence
    if base.startswith(NCSI_PREFIX):
        return "INDEX", "metadata", 1.0
    if base.startswith(NCSK_PREFIX):
        return "STOCK", "metadata", 1.0
    # 2. commodities/energy/gold/metal/oil — resolved against hint tables
    if base.startswith(NCCO_PREFIX):
        if any(h in text for h in GOLD_HINTS):
            return "GOLD", "metadata", 0.95
        if any(h in text for h in METAL_HINTS):
            return "METAL", "metadata", 0.95
        if any(h in text for h in OIL_HINTS):
            return "OIL", "metadata", 0.90
        if any(h in text for h in ENERGY_HINTS):
            return "ENERGY", "metadata", 0.90
    # 3. Weak pattern fallback, lower confidence
    if any(h in text for h in GOLD_HINTS):
        return "GOLD", "pattern", 0.6
    if any(h in text for h in METAL_HINTS):
        return "METAL", "pattern", 0.6
    if any(h in text for h in INDEX_HINTS):
        return "INDEX", "pattern", 0.55
    if any(h in text for h in OIL_HINTS):
        return "OIL", "pattern", 0.55
    if any(h in text for h in ENERGY_HINTS):
        return "ENERGY", "pattern", 0.55
    if base in STOCKS or any(k in text for k in ("STOCK", "EQUITY", "SHARE")):
        return "STOCK", "pattern", 0.5
    if any(h in base for h in FOREX_HINTS) and (base.startswith(NCCO_PREFIX) or
                                              base.endswith(("AUD","EUR","CHF","GBP","JPY","CAD","NZD","USD"))):
        return "FOREX", "pattern", 0.55
    return "CRYPTO", "pattern", 0.4


def classify_legacy(symbol: str, market: dict) -> str:
    """Backwards-compatible single-string classification."""
    return classify(symbol, market)[0]


def market_status(market: dict) -> dict:
    """Normalized market-status from venue metadata (never fabricated).

    Uses apiStateOpen/apiStateClose/status/launchTime only; no session hours
    are exposed by the connected venue, so intraday windowing stays UNKNOWN.
    """
    info = (market or {}).get("info") or {}
    if not isinstance(info, dict) or not info:
        return {"market_status": "UNKNOWN", "source": "FALLBACK"}
    open_ok = str(info.get("apiStateOpen", "")).lower() == "true"
    close_ok = str(info.get("apiStateClose", "")).lower() == "true"
    status = info.get("status")
    if open_ok and close_ok and str(status) == "1":
        return {"market_status": "24_7", "source": "VENUE", "execution_available": True}
    if open_ok and not close_ok:
        return {"market_status": "OPEN", "source": "VENUE", "execution_available": True}
    if not open_ok and close_ok:
        return {"market_status": "CLOSED", "source": "VENUE", "execution_available": False}
    return {"market_status": "UNKNOWN", "source": "FALLBACK"}


def _configured(asset: str) -> List[str]:
    raw = os.getenv(f"DEEP_SCAN_{asset}_SYMBOLS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def build_balanced(markets: Dict[str, dict], radar_limit: int = 240,
                   activity: Dict[str, float] | None = None) -> List[dict]:
    buckets = defaultdict(list)
    for symbol, market in markets.items():
        if not market or market.get("active") is False:
            continue
        mtype = str(market.get("type", "")).lower()
        if mtype not in {"swap", "future"}:
            continue
        asset, cls_source, cls_conf = classify(symbol, market)
        buckets[asset].append({
            "symbol": symbol, "asset_class": asset,
            "source": "venue_discovery", "market": market,
            "classification_source": cls_source,
            "classification_confidence": round(cls_conf, 2),
        })

    # Add only configured symbols actually present on the venue.
    for asset in ASSET_CLASSES:
        present = {r["symbol"] for r in buckets[asset]}
        for symbol in _configured(asset):
            if symbol in markets and symbol not in present:
                buckets[asset].append({"symbol": symbol, "asset_class": asset,
                                       "source": "configured", "market": markets[symbol]})

    # Dynamic discovery: rank rows by a minimal but real activity signal
    # (discovery priority, not queue score). When the caller supplies live
    # ticker activity (|24h change| + quote volume) it dominates; otherwise we
    # fall back to venue metadata. Within each class the ranking is applied;
    # across classes, market activity dominates. The quota guard is optional
    # (default unlimited) and preserved for rate-limit control only.
    activity = activity or {}

    def activity_key(row):
        sym = row.get("symbol", "")
        if sym in activity:
            return float(activity[sym])
        m = row.get("market") or {}
        info = m.get("info") or {}
        vol = info.get("volume_24h", info.get("vol24h", info.get("volume", 0)))
        atr = info.get("atr", info.get("range", 0))
        news = info.get("news_score", 0)
        return (
            float(news or 0) * 1000 +
            float(vol or 0) * 1e-6 +
            float(atr or 0) * 100 +
            float(row.get("news_activity", 0) or 0)
        )

    # Sort every bucket by activity once, so the radar_limit cut keeps the most
    # active instruments per class instead of raw venue listing order.
    for asset in buckets:
        buckets[asset].sort(key=activity_key, reverse=True)

    classes_present = {asset for asset, _rows in buckets.items() if _rows}
    ranked_classes = sorted(
        classes_present,
        key=lambda a: max((activity_key(r) for r in buckets[a]), default=0),
        reverse=True,
    )

    quotas = {}
    for asset in ASSET_CLASSES:
        quotas[asset] = max(
            1,
            int(os.getenv(f"DEEP_SCAN_{asset}_QUOTA", str(len(buckets[asset]))))
        )

    out = []
    seen = set()
    limit = int(os.getenv("DEEP_MIN_REPRESENTATION", "1"))
    for round_no in range(max(quotas.values())):
        for asset in ranked_classes:
            bucket = buckets[asset]
            if round_no >= min(len(bucket), quotas[asset]):
                continue
            # Buckets are pre-sorted by live activity; the round-robin
            # oscillates across ranked classes on the same cycle.
            row = bucket[round_no]
            if row["symbol"] in seen:
                continue
            seen.add(row["symbol"])
            out.append(row)
            if len(out) >= radar_limit:
                return out
    return out
