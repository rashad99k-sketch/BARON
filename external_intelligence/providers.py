from __future__ import annotations
import html
import re
import time
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

from .http import BoundedHTTPClient

class SECProvider:
    BASE = "https://data.sec.gov"
    TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
    FORMS = {"8-K", "10-Q", "10-K", "20-F", "6-K", "13D", "13D/A", "13G", "13G/A", "4"}

    def __init__(self, client: BoundedHTTPClient):
        self.client = client
        self._ticker_map = None

    def ticker_cik(self, ticker: str) -> Optional[str]:
        ticker = ticker.upper().strip()
        if self._ticker_map is None:
            r = self.client.get(self.TICKERS_URL, cache_key="sec:ticker-map")
            data = r.json()
            self._ticker_map = {str(v.get("ticker", "")).upper(): str(v.get("cik_str", "")).zfill(10) for v in data.values() if v.get("ticker")}
        return self._ticker_map.get(ticker)

    def recent_filings(self, ticker: str, limit: int = 12) -> Dict[str, Any]:
        cik = self.ticker_cik(ticker)
        if not cik:
            return {"available": False, "reason": "CIK_NOT_FOUND", "filings": []}
        url = f"{self.BASE}/submissions/CIK{cik}.json"
        r = self.client.get(url, cache_key=f"sec:submissions:{cik}")
        data = r.json()
        recent = data.get("filings", {}).get("recent", {})
        rows = []
        n = len(recent.get("form", []))
        for i in range(n):
            form = recent["form"][i]
            if form not in self.FORMS:
                continue
            accession = recent.get("accessionNumber", [""])[i]
            filed = recent.get("filingDate", [""])[i]
            primary = recent.get("primaryDocument", [""])[i]
            rows.append({
                "form": form,
                "filed": filed,
                "accession": accession,
                "primary_document": primary,
                "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary}" if primary and accession else "",
            })
            if len(rows) >= limit:
                break
        # Presence is evidence of an event, not proof of bullish direction.
        return {"available": True, "cik": cik, "company": data.get("name", ticker), "filings": rows}

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: List[List[str]] = []
        self._row: List[str] = []
        self._cell = False
        self._buf: List[str] = []
    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("td", "th"):
            self._cell = True; self._buf = []
        elif tag.lower() == "tr":
            self._row = []
    def handle_data(self, data):
        if self._cell:
            self._buf.append(data)
    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in ("td", "th") and self._cell:
            self._row.append(" ".join("".join(self._buf).split())); self._cell = False
        elif tag == "tr" and self._row:
            self.rows.append(self._row)

class OpenInsiderProvider:
    BASE = "https://openinsider.com"
    def __init__(self, client: BoundedHTTPClient):
        self.client = client
    def latest(self, ticker: str, limit: int = 10) -> Dict[str, Any]:
        ticker = ticker.upper().strip()
        url = f"{self.BASE}/screener"
        try:
            r = self.client.get(url, params={"s": ticker}, cache_key=f"openinsider:{ticker}")
            parser = _TableParser(); parser.feed(r.text)
            matches = []
            for row in parser.rows:
                text = " | ".join(row)
                if ticker in text.upper():
                    matches.append(row)
                if len(matches) >= limit:
                    break
            buys = []
            sells = []
            for row in matches:
                low = " ".join(row).lower()
                if any(x in low for x in ("purchase", "buy")):
                    buys.append(row)
                if any(x in low for x in ("sale", "sell")):
                    sells.append(row)
            return {"available": True, "ticker": ticker, "rows": matches, "buy_count": len(buys), "sell_count": len(sells), "buy_rows": buys, "sell_rows": sells}
        except Exception as exc:
            return {"available": False, "reason": str(exc), "rows": [], "buy_count": 0, "sell_count": 0}

class FinvizProvider:
    BASE = "https://finviz.com/quote.ashx"
    def __init__(self, client: BoundedHTTPClient):
        self.client = client
    @staticmethod
    def _number(value: Any) -> Optional[float]:
        if value is None: return None
        s = str(value).replace(",", "").replace("%", "").strip()
        mult = 1.0
        if s.endswith("B"): mult = 1e9; s = s[:-1]
        elif s.endswith("M"): mult = 1e6; s = s[:-1]
        elif s.endswith("K"): mult = 1e3; s = s[:-1]
        try: return float(s) * mult
        except Exception: return None
    def quote(self, ticker: str) -> Dict[str, Any]:
        ticker = ticker.upper().strip()
        try:
            r = self.client.get(self.BASE, params={"t": ticker}, cache_key=f"finviz:{ticker}")
            text = html.unescape(re.sub(r"\s+", " ", r.text))
            fields = {}
            for field in ("Price", "Change", "Volume", "Average Volume", "Relative Volume", "ATR", "Market Cap", "Signal"):
                m = re.search(rf">\s*{re.escape(field)}\s*<.*?>\s*([^<]+)\s*<", text, re.I)
                if m:
                    fields[field.lower().replace(" ", "_")] = m.group(1).strip()
            # Fallback for modern markup: search visible field/value adjacency.
            for key, aliases in {"relative_volume":["Relative Volume"], "current_volume":["Current Volume"], "average_volume":["Average Volume"], "price":["Price"], "change":["Change"], "signal":["Signal"]}.items():
                if key not in fields:
                    for a in aliases:
                        m = re.search(rf"{re.escape(a)}.{0,120}?>([^<>]{{1,32}})<", text, re.I)
                        if m:
                            fields[key] = m.group(1).strip(); break
            numeric = {k: self._number(v) for k, v in fields.items() if k not in {"signal"}}
            fields.update(numeric)
            fields["available"] = bool(fields)
            fields["ticker"] = ticker
            return fields
        except Exception as exc:
            return {"available": False, "ticker": ticker, "reason": str(exc)}
