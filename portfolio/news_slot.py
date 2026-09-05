"""Independent NEWS-driven slot (RF Liquidity Pro / ATOM).

This is the dedicated, clearly-separated implementation of the independent
news-driven trade slot:

    1 Crypto + 1 Crypto + 1 Index + 1 Index + 1 Gold + 1 Oil  -> technical slots
    + 1 INDEPENDENT NEWS slot                                  -> this module

Design contract
---------------
* Fully independent: it is NOT counted against the Crypto / Index / Gold / Oil
  class quotas, and it never steals a technical class slot.
* Opens on ANY asset with a STRONG news event that supports a trade direction
  (BUY for bullish news, SELL for bearish news). The asset does not need to be
  the strongest technical Order Block / Zone.
* Strong news alone is NOT enough to force a reckless order: the candidate is
  routed through the unified safety path (kill switch, global slot cap,
  extreme news-risk block, sizing / SL / TP / position management) via
  PortfolioManager.open_candidate, which remains authoritative before any real
  order is placed.
* Exactly ONE news trade may be open at any time (slot cap = 1), and the total
  number of open positions never exceeds MAX_OPEN_POSITIONS (default 6).
* The feature is OPT-IN via env NEWS_SLOT_ENABLED=True. When off this module
  does nothing, so the existing technical pipeline and the project invariant
  ("news can never create a technical entry by itself") are preserved.
"""

from __future__ import annotations

import os
from typing import Optional

# News strength threshold (headline-level impact sufficient to drive the slot).
_MIN_IMPACT = float(os.getenv("NEWS_SLOT_MIN_IMPACT", "0.55"))


def news_strong(assessment) -> bool:
    """True when news is strong enough to drive the independent slot.

    Uses the strongest top-article impact when available, else the aggregate
    bias. Extreme news risk (risk >= NEWS_RISK_BLOCK) is treated as a blocker,
    never as an entry qualifier — mirroring production safety.
    """
    if assessment is None:
        return False
    risk = float(getattr(assessment, "risk", 0) or 0)
    block = float(os.getenv("NEWS_RISK_BLOCK", "80"))
    if risk >= block:
        return False

    headlines = getattr(assessment, "headlines", None) or []
    strongest = 0.0
    for h in headlines:
        try:
            imp = (1.0 if (h.get("impact_strength") == "STRONG")
                   else 0.6 if (h.get("impact_strength") == "MEDIUM") else 0.0)
        except Exception:
            imp = 0.0
        strongest = max(strongest, imp)
    if strongest >= _MIN_IMPACT:
        return True

    bias = str(getattr(assessment, "bias", "NEUTRAL")).upper()
    return bias in ("BULLISH", "BEARISH") and risk <= block * 0.6


def evaluate_news_direction(assessment) -> Optional[str]:
    """Return 'BUY' / 'SELL' / None from strong news.

    - strong BULLISH news -> 'BUY'
    - strong BEARISH news -> 'SELL'
    - weak / neutral / conflicting / extreme-risk -> None (no news trade)
    """
    if not news_strong(assessment):
        return None
    bias = str(getattr(assessment, "bias", "NEUTRAL")).upper()
    if bias == "BULLISH":
        return "BUY"
    if bias == "BEARISH":
        return "SELL"
    return None


def scan_for_news_candidate(watchlist) -> Optional[dict]:
    """Pick the best symbol+side driven by strong, direction-giving news.

    Returns {symbol, side, price, sl, tp1, tp2, atr, asset_class, news} or None.
    Ranking is by news confidence (impact), NOT technical OB/Zone score — this
    keeps the news slot independent of the technical comparison.
    """
    best = None
    best_conf = -1.0
    block = float(os.getenv("NEWS_RISK_BLOCK", "80"))
    for sym, entry in (watchlist or {}).items():
        if not isinstance(entry, dict):
            continue
        if float(entry.get("news_risk") or 0) >= block:
            continue  # extreme risk never qualifies the news slot
        assessment = entry.get("news", None)
        direction = evaluate_news_direction(assessment)
        if direction is None:
            continue
        price = float(entry.get("price") or 0)
        if not price:
            continue
        conf = _news_confidence(assessment)
        if conf > best_conf:
            best_conf = conf
            atr = float(entry.get("atr") or price * 0.01) or price * 0.01
            cand = {
                "symbol": sym,
                "side": direction,
                "price": price,
                "atr": atr,
                # PortfolioManager.open_candidate requires a bounded score.
                "score": round(min(100.0, max(0.0, conf * 100.0)), 1),
                # The NEWS slot's own classification: carried through OPEN so the
                # trade is created (and managed) as a NEWS trade, never silently
                # re-labelled TREND/REVERSAL/SNIPER by the technical pipeline.
                "asset_class": "NEWS",
                "trade_type": "NEWS",
                "classification": "NEWS",
                "impact": _impact_label(assessment),
                "direction": "LONG" if direction == "BUY" else "SHORT",
                "news": assessment.as_dict() if hasattr(assessment, "as_dict") else {},
            }
            cand.update(_risk_levels(price, atr, direction))
            best = cand
    return best


def _risk_levels(price: float, atr: float, side: str) -> dict:
    """ATR-based stop / targets for the news slot (unified sizing safety):
    stop = 1.5 x ATR away, tp1 = 2 x ATR, tp2 = 3 x ATR.
    """
    if side == "SELL":
        stop = price + atr * 1.5
        tp1 = price - atr * 2.0
        tp2 = price - atr * 3.0
    else:
        stop = price - atr * 1.5
        tp1 = price + atr * 2.0
        tp2 = price + atr * 3.0
    return {"sl": stop, "tp1": tp1, "tp2": tp2}


def _news_confidence(assessment) -> float:
    if assessment is None:
        return 0.0
    bias = str(getattr(assessment, "bias", "NEUTRAL")).upper()
    base = 1.0 if bias in ("BULLISH", "BEARISH") else 0.0
    risk = float(getattr(assessment, "risk", 0) or 0)
    return base + (1.0 - min(risk, 80.0) / 80.0) * 0.3


def _impact_label(assessment) -> str:
    """Headline-level impact label ('HIGH'/'MEDIUM'/'LOW') for the news log."""
    if assessment is None:
        return "MEDIUM"
    strongest = 0.0
    for h in (getattr(assessment, "headlines", None) or []):
        try:
            imp = (1.0 if (h.get("impact_strength") == "STRONG")
                   else 0.6 if (h.get("impact_strength") == "MEDIUM") else 0.0)
        except Exception:
            imp = 0.0
        strongest = max(strongest, imp)
    if strongest >= 0.9:
        return "HIGH"
    if strongest >= 0.5:
        return "MEDIUM"
    return "LOW"


def count_open_news(manager) -> int:
    """Number of currently-open positions classified as the NEWS slot."""
    from portfolio.manager import PositionContext
    n = 0
    for ctx in getattr(manager, "contexts", {}).values():
        if isinstance(ctx, PositionContext) and ctx.symbol:
            try:
                # Prefer the stored asset_class (captured at OPEN). Guard against
                # the legacy bug where forcing explicit="NEWS" made EVERY open
                # position count as a news position.
                cls = str(getattr(ctx, "asset_class", None) or "").upper()
                if not cls:
                    cls = manager._asset_class(ctx.symbol)
            except Exception:
                cls = "CRYPTO"
            if cls == "NEWS":
                n += 1
    return n
