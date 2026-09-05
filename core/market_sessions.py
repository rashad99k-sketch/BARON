"""Session-aware market context for multi-asset BingX instruments.

The TradingView reference used by this project exposes four execution windows
(American/New-York timezone): Asia 20:00-00:00, London 02:00-05:00, NY AM
08:30-11:00 and NY PM 13:30-16:00.  Those windows are retained as an
indicator-alignment layer; they are *not* blindly treated as exchange hours.

This module adds a second layer using real timezone-aware market centres.  That
lets the engine understand why USD/CAD, gold, oil, indices and stocks should
not all be treated as if they have identical liquidity at every clock time.
It is advisory by default.  A hard gate is opt-in via environment variables.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

UTC = timezone.utc
BERLIN = ZoneInfo("Europe/Berlin")
NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")
SYDNEY = ZoneInfo("Australia/Sydney")

# Exact windows from the supplied TradingView indicator.
INDICATOR_WINDOWS_DEFAULT = {
    "ASIA": (dtime(20, 0), dtime(0, 0)),
    "LONDON": (dtime(2, 0), dtime(5, 0)),
    "NY_AM": (dtime(8, 30), dtime(11, 0)),
    "NY_PM": (dtime(13, 30), dtime(16, 0)),
}


def indicator_windows() -> Dict[str, Tuple[dtime, dtime]]:
    """Return the supplied indicator's exact windows, with safe HH:MM env overrides."""
    out = dict(INDICATOR_WINDOWS_DEFAULT)
    for key in tuple(out):
        raw = os.getenv(f"SESSION_{key}", "").strip()
        if raw and "-" in raw:
            a, b = raw.split("-", 1)
            out[key] = (_hhmm(a, out[key][0]), _hhmm(b, out[key][1]))
    return out

# Broader market-centre windows, represented in each centre's own timezone so
# DST is handled automatically.
CENTRE_WINDOWS = {
    "SYDNEY": (SYDNEY, dtime(8, 0), dtime(17, 0)),
    "TOKYO": (TOKYO, dtime(9, 0), dtime(18, 0)),
    "LONDON": (LONDON, dtime(8, 0), dtime(17, 0)),
    "NEW_YORK": (NEW_YORK, dtime(8, 0), dtime(17, 0)),
    "US_EQUITY": (NEW_YORK, dtime(9, 30), dtime(16, 0)),
}

CURRENCY_CENTRE = {
    "USD": "NEW_YORK",
    "CAD": "NEW_YORK",  # Toronto liquidity is ET; NY session is the correct centre.
    "EUR": "LONDON",
    "GBP": "LONDON",
    "CHF": "LONDON",
    "JPY": "TOKYO",
    "AUD": "SYDNEY",
    "NZD": "SYDNEY",
}

# Symbols observed on BingX can contain wrappers such as NCFXUSD2CAD/USDT.
_FX_RE = re.compile(r"(?:FX)?(?P<base>USD|CAD|EUR|GBP|CHF|JPY|AUD|NZD)(?:2|/|-)?(?P<quote>USD|CAD|EUR|GBP|CHF|JPY|AUD|NZD)", re.I)


def _truthy(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _hhmm(value: str, fallback: dtime) -> dtime:
    try:
        h, m = value.strip().split(":", 1)
        return dtime(int(h), int(m))
    except Exception:
        return fallback


def _in_window(local_dt: datetime, start: dtime, end: dtime) -> bool:
    t = local_dt.timetz().replace(tzinfo=None)
    if start < end:
        return start <= t < end
    # Midnight-crossing window (e.g. 20:00-00:00).
    return t >= start or t < end


def detect_fx_pair(symbol: str) -> Optional[Tuple[str, str]]:
    raw = str(symbol or "").upper()
    # Remove common venue wrappers first, but retain the original token stream.
    compact = re.sub(r"[^A-Z0-9]", "", raw.replace("USDT", ""))
    # Explicit common forms first.
    for pair in ("USDCAD", "CADUSD", "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "NZDUSD", "USDCHF", "EURGBP", "EURJPY", "GBPJPY"):
        if pair in compact:
            return pair[:3], pair[3:]
    m = _FX_RE.search(compact)
    if m:
        return m.group("base").upper(), m.group("quote").upper()
    return None


def instrument_kind(symbol: str) -> str:
    up = str(symbol or "").upper()
    if detect_fx_pair(up):
        return "FOREX"
    if any(x in up for x in ("XAU", "GOLD")):
        return "GOLD"
    if any(x in up for x in ("WTI", "BRENT", "OIL", "CRUDE")):
        return "OIL"
    if any(x in up for x in ("SP500", "US500", "NASDAQ", "USTECH", "US30", "DAX", "FTSE", "CAC", "NIKKEI", "INDEX")):
        return "INDEX"
    if any(x in up for x in ("AAPL", "AMZN", "GOOGL", "GOOG", "MSFT", "NVDA", "META", "TSLA", "JPM", "NFLX", "PLTR", "INTC")):
        return "STOCK"
    return "CRYPTO"


def _indicator_session(now_et: datetime) -> List[str]:
    result = []
    for name, (start, end) in indicator_windows().items():
        if _in_window(now_et, start, end):
            result.append(name)
    return result


def _centre_open(now: datetime, centre: str, equity: bool = False) -> bool:
    tz, start, end = CENTRE_WINDOWS["US_EQUITY" if equity else centre]
    return _in_window(now.astimezone(tz), start, end)


def _fx_quality(base: str, quote: str, now: datetime, indicator_sessions: List[str]) -> Tuple[str, List[str], List[str]]:
    centres = []
    for ccy in (base, quote):
        c = CURRENCY_CENTRE.get(ccy)
        if c and c not in centres:
            centres.append(c)
    open_centres = [c for c in centres if _centre_open(now, c)]
    # USD/CAD is most liquid during the NY centre; the exact TradingView NY
    # windows are preferred execution windows for the indicator alignment.
    preferred = []
    if "NY_AM" in indicator_sessions or "NY_PM" in indicator_sessions:
        preferred.append("NY")
    if "LONDON" in indicator_sessions:
        preferred.append("LONDON")
    if base == "USD" and quote == "CAD" and "NEW_YORK" in open_centres:
        return ("PREFERRED" if "NY" in preferred else "ACTIVE"), centres, preferred
    if open_centres:
        return ("PREFERRED" if any(x in preferred for x in ("NY", "LONDON", "ASIA")) else "ACTIVE"), centres, preferred
    return "QUIET", centres, preferred


def _indicator_windows_in_zone(now_et: datetime, target_tz: ZoneInfo) -> Dict[str, str]:
    """Render the indicator windows in another timezone for operator visibility."""
    out = {}
    base_date = now_et.date()
    for name, (start, end) in indicator_windows().items():
        start_dt = datetime.combine(base_date, start, tzinfo=NEW_YORK)
        end_date = base_date + timedelta(days=1) if end <= start else base_date
        end_dt = datetime.combine(end_date, end, tzinfo=NEW_YORK)
        a = start_dt.astimezone(target_tz)
        b = end_dt.astimezone(target_tz)
        out[name] = f"{a.strftime('%H:%M')}-{b.strftime('%H:%M')}"
    return out


def session_context(symbol: str, now: Optional[datetime] = None) -> Dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    now = now.astimezone(UTC)
    et = now.astimezone(NEW_YORK)
    berlin = now.astimezone(BERLIN)
    kind = instrument_kind(symbol)
    pair = detect_fx_pair(symbol)
    indicator_sessions = _indicator_session(et)

    if pair:
        base, quote = pair
        state, centres, preferred = _fx_quality(base, quote, now, indicator_sessions)
        primary = [CURRENCY_CENTRE.get(base), CURRENCY_CENTRE.get(quote)]
        primary = list(dict.fromkeys(x for x in primary if x))
        hard_gate = _truthy("SESSION_FX_HARD_GATE", False)
    elif kind in {"STOCK", "INDEX"}:
        us_open = _centre_open(now, "NEW_YORK", equity=True)
        state = "PREFERRED" if us_open else "QUIET"
        centres = ["US_EQUITY"]
        preferred = ["US_EQUITY"] if us_open else []
        primary = ["US_EQUITY"]
        hard_gate = _truthy("SESSION_EQUITY_HARD_GATE", False)
    elif kind in {"GOLD", "OIL"}:
        ny = _centre_open(now, "NEW_YORK")
        london = _centre_open(now, "LONDON")
        overlap = ny and london
        state = "PREFERRED" if overlap else ("ACTIVE" if (ny or london) else "QUIET")
        centres = [c for c, ok in (("LONDON", london), ("NEW_YORK", ny)) if ok]
        preferred = ["LONDON_NY_OVERLAP"] if overlap else centres[:]
        primary = ["LONDON", "NEW_YORK"]
        hard_gate = _truthy("SESSION_COMMODITY_HARD_GATE", False)
    else:
        # Crypto derivatives are 24/7; session context is informational and
        # can still improve ranking around global liquidity overlaps.
        ny = _centre_open(now, "NEW_YORK")
        london = _centre_open(now, "LONDON")
        tokyo = _centre_open(now, "TOKYO")
        state = "PREFERRED" if ny and london else ("ACTIVE" if (ny or london or tokyo) else "QUIET")
        centres = [c for c, ok in (("TOKYO", tokyo), ("LONDON", london), ("NEW_YORK", ny)) if ok]
        preferred = ["LONDON_NY_OVERLAP"] if ny and london else centres[:]
        primary = ["TOKYO", "LONDON", "NEW_YORK"]
        hard_gate = False

    hard_blocked = bool(hard_gate and state == "QUIET")
    # Keep the exact indicator schedule visible in both ET and the user's
    # operational timezone. This is what the dashboard/logs should explain.
    return {
        "symbol": str(symbol),
        "instrument": kind,
        "pair": f"{pair[0]}/{pair[1]}" if pair else None,
        "utc": now.isoformat(),
        "new_york": et.isoformat(),
        "germany": berlin.isoformat(),
        "indicator_sessions": indicator_sessions,
        "indicator_timezone": "America/New_York",
        "indicator_windows": {k: f"{v[0].strftime('%H:%M')}-{v[1].strftime('%H:%M')}" for k, v in indicator_windows().items()},
        "indicator_windows_germany": _indicator_windows_in_zone(et, BERLIN),
        "active_market_centres": centres,
        "primary_centres": primary,
        "preferred_windows": preferred,
        "state": state,
        "hard_gate_enabled": hard_gate,
        "hard_blocked": hard_blocked,
        "session_quality": 1.0 if state == "PREFERRED" else 0.75 if state == "ACTIVE" else 0.45,
    }


def session_allows_entry(symbol: str, now: Optional[datetime] = None) -> Tuple[bool, Dict]:
    ctx = session_context(symbol, now)
    return not ctx["hard_blocked"], ctx
