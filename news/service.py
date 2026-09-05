"""Read-only market-news intelligence with bounded public fallbacks.

Design goals:
- no API key is required for the default providers;
- Yahoo Finance search is preferred for instrument-specific headlines;
- RSS/Google News is the fallback for macro, commodities and crypto;
- every request is bounded and cached;
- news can modify risk/bias, but can never create an entry by itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import os
import re
import threading
import time
from typing import Dict, List
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests


@dataclass
class NewsAssessment:
    risk: float = 0.0
    bias: str = "NEUTRAL"
    available: bool = False
    headlines: List[dict] = field(default_factory=list)
    event_types: List[str] = field(default_factory=list)
    macro_risk: float = 0.0
    symbol_risk: float = 0.0
    provider: str = "NONE"
    direct_count: int = 0
    macro_event: bool = False

    def as_dict(self):
        return {
            "risk": round(float(self.risk), 1),
            "bias": self.bias,
            "available": bool(self.available),
            "provider": self.provider,
            "headlines": self.headlines or [],
            "event_types": list(dict.fromkeys(self.event_types or [])),
            "macro_risk": round(float(self.macro_risk), 1),
            "symbol_risk": round(float(self.symbol_risk), 1),
            "direct_count": int(self.direct_count),
            "macro_event": bool(self.macro_event),
        }


class NewsService:
    """Bounded, cached financial-news intelligence."""

    def __init__(self):
        self.enabled = os.getenv("NEWS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.timeout = float(os.getenv("NEWS_TIMEOUT_SEC", "4"))
        self.cache_ttl = float(os.getenv("NEWS_CACHE_TTL_SEC", "300"))
        self.max_items = max(1, int(os.getenv("NEWS_MAX_ITEMS", "8")))
        self.max_age_hours = max(1.0, float(os.getenv("NEWS_MAX_AGE_HOURS", "24")))
        self.yahoo_enabled = os.getenv("NEWS_YAHOO_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.rss_enabled = os.getenv("NEWS_RSS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self.global_query = os.getenv(
            "NEWS_GLOBAL_QUERY",
            "FOMC CPI inflation NFP Fed tariffs sanctions war crypto markets gold oil stocks indices",
        )
        self.yahoo_url = os.getenv(
            "NEWS_YAHOO_SEARCH_URL",
            "https://query1.finance.yahoo.com/v1/finance/search",
        )
        raw_feeds = os.getenv("NEWS_RSS_FEEDS", "").strip()
        self.feeds = [x.strip() for x in raw_feeds.split(",") if x.strip()]
        if not self.feeds:
            self.feeds = [
                "https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en",
            ]
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.RLock()

        self.high_risk = re.compile(
            r"fed|fomc|interest rate|cpi|inflation|nfp|nonfarm|payroll|rate decision|"
            r"central bank|war|sanction|tariff|default|bank failure|lawsuit|hack|"
            r"liquidation|earnings warning|sec|etf approval|geopolitical|emergency|"
            r"attack|invasion|ceasefire|oil supply|opec|downgrade|bankruptcy",
            re.I,
        )
        self.macro_high = re.compile(
            r"fed|fomc|cpi|inflation|nfp|nonfarm|payroll|interest rate|central bank|"
            r"tariff|sanction|war|geopolitical|recession|opec|oil supply",
            re.I,
        )
        self.bullish = re.compile(
            r"beat estimates|raises guidance|approval|approved|inflows|surge|record high|"
            r"upgrade|upgraded|bullish|breakthrough|adoption|partnership|buyback|"
            r"strong demand|outperform|rally|soars|gains",
            re.I,
        )
        self.bearish = re.compile(
            r"misses estimates|cuts guidance|outflows|collapse|downgrade|downgraded|"
            r"bearish|fraud|probe|lawsuit|recall|default|bankruptcy|warning|decline|"
            r"liquidation|weak demand|underperform|plunge|falls|slump",
            re.I,
        )

    @staticmethod
    def _clean_symbol(symbol: str) -> str:
        return re.sub(r"[^A-Za-z0-9.\-]", " ", symbol.replace(":USDT", "").replace("/USDT", "").replace("-USDT", "")).strip()

    @classmethod
    def _entity_aliases(cls, symbol: str, asset_class: str) -> List[str]:
        """Aliases used to verify that a headline actually concerns the instrument.

        A headline that merely arrived from a query is NOT evidence; at least
        one alias must appear in the title/snippet for the article to count as
        direct instrument news.
        """
        base = cls._clean_symbol(symbol).lower()
        aliases = {base} if base else set()
        crypto_known = {
            "btc": ["bitcoin", "btc"],
            "eth": ["ethereum", "ether", "eth"],
            "sol": ["solana", "sol"],
            "xrp": ["xrp", "ripple"],
            "doge": ["dogecoin", "doge"],
            "ada": ["cardano", "ada"],
            "bch": ["bitcoin cash", "bch"],
            "ltc": ["litecoin", "ltc"],
            "link": ["chainlink", "link"],
            "dot": ["polkadot", "dot"],
            "avax": ["avalanche", "avax"],
        }
        equity_known = {
            "nvda": ["nvidia", "nvda"],
            "aapl": ["apple inc", "apple stock", "aapl"],
            "msft": ["microsoft", "msft"],
            "googl": ["google", "alphabet", "googl"],
            "amzn": ["amazon", "amzn"],
            "meta": ["meta platforms", "facebook", "meta"],
            "tsla": ["tesla", "tsla"],
            "spy": ["s&p 500 etf", "spy"],
            "qqq": ["nasdaq etf", "qqq"],
        }
        instrument_map = {
            "GOLD": ["gold", "xau", "bullion"],
            "OIL": ["oil", "wti", "brent", "crude", "opec"],
            "INDEX": ["nasdaq", "s&p", "sp500", "dow", "dow jones", "wall street"],
            "EQUITY": ["equity", "shares", "earnings", "stock"],
            "STOCK": [base] if base else [],
            "CRYPTO": [],
        }
        for alias in equity_known.get(base, []):
            aliases.add(alias)
        for alias in crypto_known.get(base, []):
            aliases.add(alias)
        for alias in instrument_map.get(asset_class.upper(), []):
            aliases.add(alias)
        alias_path = re.sub(r"[^a-z]+", " ", base.lower()).strip()
        if alias_path:
            aliases.add(alias_path)
        return sorted(a for a in aliases if a and len(a) > 1)

    @classmethod
    def _is_relevant(cls, article: dict, aliases: List[str]) -> bool:
        text = f"{article.get('title', '')} {article.get('snippet', '')}".lower()
        return any(alias.lower() in text for alias in aliases)

    @classmethod
    def _query(cls, symbol: str, asset_class: str) -> str:
        clean = cls._clean_symbol(symbol)
        mapping = {
            "GOLD": "gold XAU bullion",
            "OIL": "crude oil WTI Brent OPEC",
            "INDEX": "stock index futures S&P 500 Nasdaq",
            "STOCK": "stock earnings company",
            "CRYPTO": "crypto bitcoin blockchain",
        }
        return f"{clean} {mapping.get(asset_class.upper(), asset_class)}".strip()

    @staticmethod
    def _published_ts(value):
        if not value:
            return None
        try:
            if isinstance(value, (int, float)):
                return float(value)
            return parsedate_to_datetime(str(value)).timestamp()
        except Exception:
            try:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except Exception:
                return None

    @staticmethod
    def _snippet(text, limit=280):
        if not isinstance(text, str):
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = " ".join(text.split())
        return text if len(text) <= limit else text[:limit].rstrip() + "…"

    def _event_type(self, title: str) -> str:
        if self.macro_high.search(title):
            return "MACRO"
        if re.search(r"earnings|revenue|guidance|profit|forecast", title, re.I):
            return "EARNINGS"
        if re.search(r"sec|regulation|law|approval|ban|policy|etf", title, re.I):
            return "POLICY"
        if re.search(r"hack|exploit|breach|liquidation", title, re.I):
            return "SECURITY"
        if re.search(r"opec|oil|supply|inventory", title, re.I):
            return "COMMODITY"
        if self.bullish.search(title) or self.bearish.search(title):
            return "SENTIMENT"
        return "MARKET"

    def _normalize_article(self, title, link, source, published, snippet="", provider=""):
        title = self._snippet(title, 240)
        if not title:
            return None
        bullish = bool(self.bullish.search(title))
        bearish = bool(self.bearish.search(title))
        if bullish and not bearish:
            sentiment = "POSITIVE"
            sentiment_color = "GREEN"
        elif bearish and not bullish:
            sentiment = "NEGATIVE"
            sentiment_color = "RED"
        else:
            sentiment = "NEUTRAL"
            sentiment_color = "GRAY"
        hits = int(bullish) + int(bearish)
        strength = "STRONG" if hits and self.high_risk.search(title) else ("MEDIUM" if hits else "LOW")
        confidence = 0.90 if sentiment != "NEUTRAL" and self.high_risk.search(title) else (0.72 if sentiment != "NEUTRAL" else 0.35)
        ts = self._published_ts(published)
        cutoff = time.time() - self.max_age_hours * 3600.0
        if ts is not None and ts < cutoff:
            return None
        return {
            "title": title,
            "link": link or "",
            "source": source or provider or "Unknown",
            "published": published or "",
            "published_ts": ts,
            "event_type": self._event_type(title),
            "snippet": self._snippet(snippet),
            "provider": provider,
            "sentiment": sentiment,
            "sentiment_color": sentiment_color,
            "impact_strength": strength,
            "sentiment_confidence": round(confidence, 2),
        }

    def _fetch_yahoo(self, query: str) -> List[dict]:
        if not self.yahoo_enabled:
            return []
        try:
            response = requests.get(
                self.yahoo_url,
                params={"q": query, "newsCount": self.max_items, "quotesCount": 0},
                timeout=self.timeout,
                headers={"User-Agent": "RF-Liquidity-Pro/3.0"},
            )
            response.raise_for_status()
            payload = response.json()
            raw_news = payload.get("news", []) if isinstance(payload, dict) else []
            out = []
            for item in raw_news:
                if not isinstance(item, dict):
                    continue
                article = self._normalize_article(
                    item.get("title"), item.get("link"), item.get("publisher"),
                    item.get("providerPublishTime"), item.get("summary", ""), "YAHOO"
                )
                if article:
                    out.append(article)
            return self._dedupe(out)
        except Exception:
            return []

    def _fetch_rss(self, query: str) -> List[dict]:
        if not self.rss_enabled:
            return []
        out = []
        for template in self.feeds:
            try:
                url = template.format(query=quote(query))
                response = requests.get(
                    url,
                    timeout=self.timeout,
                    headers={"User-Agent": "RF-Liquidity-Pro/3.0"},
                )
                response.raise_for_status()
                root = ET.fromstring(response.content)
            except Exception:
                continue

            # RSS <item> plus Atom <entry> support.
            nodes = list(root.findall(".//item")) or list(root.findall(".//{http://www.w3.org/2005/Atom}entry"))
            for node in nodes[: self.max_items * 2]:
                title = node.findtext("title") or node.findtext("{http://www.w3.org/2005/Atom}title") or ""
                link = node.findtext("link") or ""
                if not link:
                    atom_link = node.find("{http://www.w3.org/2005/Atom}link")
                    link = atom_link.attrib.get("href", "") if atom_link is not None else ""
                pub = (
                    node.findtext("pubDate")
                    or node.findtext("published")
                    or node.findtext("updated")
                    or node.findtext("{http://www.w3.org/2005/Atom}published")
                    or node.findtext("{http://www.w3.org/2005/Atom}updated")
                    or ""
                )
                desc = node.findtext("description") or node.findtext("summary") or ""
                article = self._normalize_article(title, link, "RSS", pub, desc, "RSS")
                if article:
                    out.append(article)
                if len(out) >= self.max_items:
                    break
            if out:
                break
        return self._dedupe(out)

    @staticmethod
    def _dedupe(items):
        out = []
        seen = set()
        for item in items:
            key = (item.get("title", "").lower(), item.get("link", ""))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _fetch(self, query: str) -> tuple[List[dict], str]:
        # Provider order follows the Vibe-Trading pattern: instrument-specific
        # structured news first, then a bounded public RSS fallback.
        yahoo = self._fetch_yahoo(query)
        if yahoo:
            return yahoo[: self.max_items], "YAHOO"
        rss = self._fetch_rss(query)
        return rss[: self.max_items], ("RSS" if rss else "NONE")

    def _get_cached(self, key: str, query: str):
        now = time.time()
        with self._lock:
            cached = self._cache.get(key)
            if cached and now - cached[0] < self.cache_ttl:
                return cached[1], cached[2]
        headlines, provider = self._fetch(query)
        with self._lock:
            self._cache[key] = (now, headlines, provider)
        return headlines, provider

    def _score(self, headlines: List[dict], macro: bool = False):
        risk = 0.0
        bull = bear = 0
        for h in headlines:
            title = h.get("title", "")
            if self.high_risk.search(title):
                risk += 25.0 if macro else 15.0
            if self.bullish.search(title):
                bull += 1
            if self.bearish.search(title):
                bear += 1
        bias = "BULLISH" if bull > bear else "BEARISH" if bear > bull else "NEUTRAL"
        return min(100.0, risk), bias

    def assess(self, symbol: str, asset_class: str = "CRYPTO") -> NewsAssessment:
        if not self.enabled:
            return NewsAssessment(available=False)

        key = f"{asset_class}:{symbol}"
        symbol_news, symbol_provider = self._get_cached(key, self._query(symbol, asset_class))
        global_news, global_provider = self._get_cached("GLOBAL_MACRO", self.global_query)

        # Entity matching: only headlines that actually mention the instrument
        # (or its asset-class keywords for non-equities) count as symbol news.
        # Query-shaped noise (arbitrary Yahoo/RSS results) is discarded here.
        aliases = self._entity_aliases(symbol, asset_class)
        if aliases and symbol_news:
            symbol_news = [h for h in symbol_news if self._is_relevant(h, aliases)]
        elif not aliases:
            symbol_news = []

        if not symbol_news and not global_news:
            return NewsAssessment(available=False, provider="NONE")

        symbol_risk, symbol_bias = self._score(symbol_news, macro=False)
        macro_risk, macro_bias = self._score(global_news, macro=True)
        bias = symbol_bias if symbol_bias != "NEUTRAL" else macro_bias
        risk = min(100.0, symbol_risk + macro_risk * 0.60)

        merged = self._dedupe(symbol_news + global_news)[: self.max_items]
        for h in merged:
            h["scope"] = "DIRECT" if h in symbol_news else "MACRO"
        provider = symbol_provider if symbol_news else global_provider
        macro_event = any(self.macro_high.search(h.get("title", "")) for h in merged)
        return NewsAssessment(
            risk=risk,
            bias=bias,
            available=bool(merged),
            headlines=merged,
            event_types=[h.get("event_type", "MARKET") for h in merged],
            macro_risk=macro_risk,
            symbol_risk=symbol_risk,
            provider=provider,
            direct_count=len(symbol_news),
            macro_event=macro_event,
        )


def news_state_for_side(assessment: "NewsAssessment", side: str,
                        risk_block: float = 80.0) -> str:
    """Side-aware news state for a watchlist candidate.

    News is evidence, never an order signal:
      - risk above the block threshold -> NEWS_RISK
      - unavailable/no relevant articles -> NEWS_UNAVAILABLE
      - bias aligned with side -> NEWS_SUPPORT, opposed -> NEWS_CONFLICT
      - otherwise -> NEWS_NEUTRAL
    """
    if assessment is None or not getattr(assessment, "available", False):
        return "NEWS_UNAVAILABLE"
    if float(getattr(assessment, "risk", 0.0) or 0.0) >= risk_block:
        return "NEWS_RISK"
    # A directional claim requires direct instrument news or a real macro event;
    # generic macro-feed noise alone is NEUTRAL evidence.
    if not getattr(assessment, "direct_count", 0) and not getattr(assessment, "macro_event", False):
        return "NEWS_NEUTRAL"
    bias = getattr(assessment, "bias", "NEUTRAL")
    if bias == "NEUTRAL":
        return "NEWS_NEUTRAL"
    if (side == "BUY" and bias == "BULLISH") or (side == "SELL" and bias == "BEARISH"):
        return "NEWS_SUPPORT"
    return "NEWS_CONFLICT"
