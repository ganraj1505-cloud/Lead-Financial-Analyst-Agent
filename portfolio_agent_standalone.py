#!/usr/bin/env python3
"""
Standalone Portfolio & Statement Simplifier Agent
-------------------------------------------------
Single-file Python implementation of:
- statement ingestion
- deterministic portfolio analysis
- proactive risk flags
- grounded follow-up Q&A
- live market price lookup (Yahoo chart API)

Usage:
  python portfolio_agent_standalone.py --file sample.csv
  python portfolio_agent_standalone.py --file sample.csv --ask "What is live price of TCS.NS?"
  python portfolio_agent_standalone.py --interactive
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import os
import re
import sys
from dataclasses import dataclass, asdict, field
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen


# -----------------------------
# Shared in-memory session state
# -----------------------------
CURRENT_SESSION: Dict[str, Any] = {
    "fileName": "",
    "rawText": "",
    "holdings": [],
    "analysis": None,
    "rag": None,
}


# -----------------------------
# Configuration
# -----------------------------
CURRENCY_DEFAULT = os.getenv("CURRENCY_DEFAULT", "INR")
SINGLE_POSITION_ALERT = float(os.getenv("SINGLE_POSITION_ALERT", "0.30"))
TOP3_CONCENTRATION_ALERT = float(os.getenv("TOP3_CONCENTRATION_ALERT", "0.60"))
UNDERPERFORM_PNL_PCT = float(os.getenv("UNDERPERFORM_PNL_PCT", "-0.10"))
SECTOR_CONCENTRATION_ALERT = float(os.getenv("SECTOR_CONCENTRATION_ALERT", "0.40"))
CHUNK_SIZE = int(os.getenv("CHUNK_SIZE", "600"))
CHUNK_OVERLAP = int(os.getenv("CHUNK_OVERLAP", "80"))
RETRIEVAL_K = int(os.getenv("RETRIEVAL_K", "3"))
MARKET_TIMEOUT_S = int(os.getenv("MARKET_TIMEOUT_S", "10"))

CURRENCY_BY_SUFFIX = {
    ".NS": "INR",
    ".BO": "INR",
    ".US": "USD",
    ".XNYS": "USD",
    ".NASDAQ": "USD",
    ".L": "GBP",
    ".HK": "HKD",
}

SECTOR_MAP = {
    "RELIANCE.NS": "Energy & Conglomerate",
    "INFY.NS": "IT Services",
    "TCS.NS": "IT Services",
    "HDFCBANK.NS": "Banking",
    "TATAMOTORS.NS": "Automobile",
    "SUNPHARMA.NS": "Pharma",
    "GOLDBEES.NS": "Gold ETF",
    "DMART.NS": "Retail",
    "ICICIBANK.NS": "Banking",
    "SBIN.NS": "Banking",
    "WIPRO.NS": "IT Services",
    "HCLTECH.NS": "IT Services",
    "ITC.NS": "FMCG",
    "LT.NS": "Infrastructure",
    "MARUTI.NS": "Automobile",
}

_SECTOR_KEYWORDS = [
    ("PHARMA", "Pharma"),
    ("BIO", "Pharma"),
    ("MED", "Pharma"),
    ("BANK", "Banking"),
    ("FIN", "Banking"),
    ("NIFTY", "Index"),
    ("IT", "IT Services"),
    ("TECH", "IT Services"),
    ("INFY", "IT Services"),
    ("AUTO", "Automobile"),
    ("MOTORS", "Automobile"),
    ("MARUTI", "Automobile"),
    ("GOLD", "Gold ETF"),
    ("ETF", "ETF/Index"),
    ("BEE", "ETF/Index"),
    ("RETAIL", "Retail"),
    ("DMART", "Retail"),
    ("BEES", "ETF/Index"),
    ("OIL", "Energy"),
    ("GAS", "Energy"),
    ("PETRO", "Energy"),
]


def sector_for(ticker: str) -> str:
    t = (ticker or "").upper()
    if t in SECTOR_MAP:
        return SECTOR_MAP[t]
    for kw, sector in _SECTOR_KEYWORDS:
        if kw in t:
            return sector
    return "Other"


# -----------------------------
# Portfolio parser + analyzer
# -----------------------------
@dataclass
class Holding:
    ticker: str
    quantity: float
    purchase_price: Optional[float] = None
    current_price: Optional[float] = None
    sector: str = ""

    @property
    def cost(self) -> Optional[float]:
        return None if self.purchase_price is None else self.quantity * self.purchase_price

    @property
    def value(self) -> Optional[float]:
        return None if self.current_price is None else self.quantity * self.current_price


@dataclass
class Flag:
    level: str
    icon: str
    text: str


@dataclass
class Analysis:
    currency: str
    as_of: Optional[str]
    holdings: List[Holding]
    lines: List[Dict[str, Any]]
    totals: Dict[str, Any]
    flags: List[Flag] = field(default_factory=list)

    def as_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, ensure_ascii=False)


_TICKER_RE = re.compile(r"^[A-Z0-9.\-]{2,20}$")
_NUM_RE = re.compile(r"-?[\d,]+(?:\.\d+)?")

_HEADER_ALIASES = {
    "ticker": "ticker",
    "symbol": "ticker",
    "stock": "ticker",
    "instrument": "ticker",
    "quantity": "quantity",
    "qty": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "purchase_price": "purchase_price",
    "buy_price": "purchase_price",
    "cost": "purchase_price",
    "cost_price": "purchase_price",
    "avg_price": "purchase_price",
    "purchase price": "purchase_price",
    "current_price": "current_price",
    "last_price": "current_price",
    "market_price": "current_price",
    "price": "current_price",
    "ltp": "current_price",
    "latest_price": "current_price",
}


def _to_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("₹", "").replace("$", "")
    if not s or s in {"-", "NA", "n/a", "N/A"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_holdings_csv(text: str) -> Tuple[List[Holding], Optional[str]]:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], None

    header = [h.strip().lower() for h in next(csv.reader(io.StringIO(lines[0])), [])]
    col: Dict[str, int] = {}
    for i, h in enumerate(header):
        canon = _HEADER_ALIASES.get(h)
        if canon and canon not in col:
            col[canon] = i

    if "ticker" not in col or "quantity" not in col:
        return _parse_unlabelled(lines), None

    holdings: List[Holding] = []
    for raw in csv.reader(io.StringIO("\n".join(lines[1:]))):
        if not raw or not any(c.strip() for c in raw):
            continue

        def g(name: str) -> str:
            idx = col.get(name)
            return raw[idx].strip() if idx is not None and idx < len(raw) else ""

        ticker = g("ticker").upper()
        if not ticker or not _TICKER_RE.match(ticker):
            continue
        qty = _to_float(g("quantity"))
        if qty is None or qty <= 0:
            continue

        h = Holding(
            ticker=ticker,
            quantity=qty,
            purchase_price=_to_float(g("purchase_price")),
            current_price=_to_float(g("current_price")),
            sector=sector_for(ticker),
        )
        holdings.append(h)
    return holdings, None


def _parse_unlabelled(rows: List[str]) -> List[Holding]:
    holdings: List[Holding] = []
    for ln in rows:
        parts = [p.strip() for p in ln.split(",")]
        if len(parts) < 2:
            continue
        ticker = parts[0].upper()
        if not _TICKER_RE.match(ticker):
            continue
        nums = [_to_float(p) for p in parts[1:]]
        nums = [n for n in nums if n is not None]
        if not nums or nums[0] <= 0:
            continue
        h = Holding(
            ticker=ticker,
            quantity=nums[0],
            purchase_price=nums[1] if len(nums) > 1 else None,
            current_price=nums[2] if len(nums) > 2 else None,
            sector=sector_for(ticker),
        )
        holdings.append(h)
    return holdings


def parse_statement_text(text: str) -> Tuple[List[Holding], Optional[str]]:
    as_of = None
    m = re.search(
        r"(?:as of|dated|date)[:\s]+("
        r"\d{1,2}\s+[A-Za-z]{3,9}\.?\s+\d{4}"
        r"|[A-Za-z]{3,9}\.?\s+\d{1,2}\s+\d{4}"
        r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
        r")",
        text,
        flags=re.I,
    )
    if m:
        as_of = m.group(1).strip()

    csv_like = [ln for ln in text.splitlines() if re.match(r"^\s*[A-Z0-9.\-]{2,20}\s*,", ln)]
    if len(csv_like) >= 2:
        h, _ = parse_holdings_csv("\n".join(csv_like))
        if h:
            return h, as_of

    holdings: List[Holding] = []
    for ln in text.splitlines():
        if re.search(r"---+|===+", ln):
            continue
        toks = ln.split()
        if len(toks) < 3:
            continue
        if not _TICKER_RE.match(toks[0].upper()):
            continue

        nums: List[float] = []
        for t in toks[1:]:
            if _NUM_RE.fullmatch(t):
                v = _to_float(t)
                if v is not None:
                    nums.append(v)
            else:
                break

        if len(nums) < 2 or nums[0] <= 0:
            continue

        h = Holding(
            ticker=toks[0].upper(),
            quantity=nums[0],
            purchase_price=nums[1] if len(nums) > 1 else None,
            current_price=nums[2] if len(nums) > 2 else None,
            sector=sector_for(toks[0].upper()),
        )
        holdings.append(h)
    return holdings, as_of


def load_holdings(path: Optional[str] = None, text: Optional[str] = None, name: str = "") -> Tuple[List[Holding], Optional[str]]:
    if text is None:
        assert path, "Either path or text must be provided"
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()

    name_l = (name or path or "").lower()
    lines = text.splitlines()
    if name_l.endswith(".csv") or (lines and "," in lines[0]):
        return parse_holdings_csv(text)
    return parse_statement_text(text)


def analyze(holdings: List[Holding], currency: str = CURRENCY_DEFAULT, as_of: Optional[str] = None) -> Analysis:
    if not holdings:
        return Analysis(
            currency=currency,
            as_of=as_of,
            holdings=[],
            lines=[],
            totals={"ok": False},
            flags=[Flag("warning", "⚠️", "No holdings could be parsed from this file.")],
        )

    lines: List[Dict[str, Any]] = []
    for h in holdings:
        cost = h.cost
        value = h.value
        pnl = (value - cost) if (cost is not None and value is not None) else None
        pnl_pct = (pnl / cost) if (pnl is not None and cost) else None
        lines.append(
            {
                "ticker": h.ticker,
                "sector": h.sector,
                "quantity": h.quantity,
                "purchase_price": h.purchase_price,
                "current_price": h.current_price,
                "cost": round(cost, 2) if cost is not None else None,
                "value": round(value, 2) if value is not None else None,
                "pnl": round(pnl, 2) if pnl is not None else None,
                "pnl_pct": round(pnl_pct, 4) if pnl_pct is not None else None,
            }
        )

    priced = [l for l in lines if l["value"] is not None]
    total_value = sum(l["value"] for l in priced) or 0.0
    total_cost = sum(l["cost"] for l in lines if l["cost"] is not None)
    total_pnl = (total_value - total_cost) if total_cost else None
    total_pnl_pct = (total_pnl / total_cost) if total_pnl is not None and total_cost else None

    for l in lines:
        l["weight"] = (l["value"] / total_value) if (l["value"] is not None and total_value) else None

    totals = {
        "ok": True,
        "holdings": len(lines),
        "priced_holdings": len(priced),
        "total_cost": round(total_cost, 2) if total_cost is not None else None,
        "total_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2) if total_pnl is not None else None,
        "total_pnl_pct": round(total_pnl_pct, 4) if total_pnl_pct is not None else None,
    }

    return Analysis(currency=currency, as_of=as_of, holdings=holdings, lines=lines, totals=totals, flags=_flag(lines, totals))


def _flag(lines: List[Dict[str, Any]], totals: Dict[str, Any]) -> List[Flag]:
    flags: List[Flag] = []

    missing = [l["ticker"] for l in lines if l["value"] is None]
    if missing:
        flags.append(
            Flag(
                "warning",
                "⚠️",
                f"Statement has no current price for: {', '.join(missing)} — these are excluded from value/P&L (ask for live prices to include them).",
            )
        )

    if not totals.get("priced_holdings"):
        return flags

    for l in sorted((l for l in lines if l.get("weight")), key=lambda x: -x["weight"]):
        if l["weight"] >= SINGLE_POSITION_ALERT:
            flags.append(
                Flag(
                    "warning",
                    "⚠️",
                    f"Concentration: {l['ticker']} is {l['weight']:.1%} of portfolio value (alert at ≥{SINGLE_POSITION_ALERT:.0%}).",
                )
            )

    top3 = [l["weight"] for l in sorted((l for l in lines if l.get("weight")), key=lambda x: -x["weight"])[:3]]
    if sum(top3) >= TOP3_CONCENTRATION_ALERT:
        flags.append(
            Flag(
                "info",
                "ℹ️",
                f"Top 3 positions hold {sum(top3):.1%} of the portfolio (watch line at {TOP3_CONCENTRATION_ALERT:.0%}).",
            )
        )

    losers = [l for l in lines if l.get("pnl_pct") is not None and l["pnl_pct"] < UNDERPERFORM_PNL_PCT]
    for l in sorted(losers, key=lambda x: x["pnl_pct"]):
        flags.append(
            Flag(
                "warning",
                "⚠️",
                f"Underperformer: {l['ticker']} is down {abs(l['pnl_pct']):.1%} vs cost ({format_inr(l['pnl'])}).",
            )
        )

    sectors = {l["sector"] for l in lines if l.get("weight")}
    if len(sectors) > 1:
        by_sector: Dict[str, float] = {}
        for l in lines:
            if l.get("weight"):
                by_sector[l["sector"]] = by_sector.get(l["sector"], 0.0) + float(l["weight"])
        for sec, w in sorted(by_sector.items(), key=lambda kv: -kv[1]):
            if w >= SECTOR_CONCENTRATION_ALERT:
                flags.append(Flag("info", "ℹ️", f"Sector tilt: {sec} is {w:.1%} of portfolio value."))
                break

    if totals.get("total_pnl_pct") is not None and totals["total_pnl_pct"] > 0:
        flags.append(Flag("positive", "✅", f"Portfolio is up {totals['total_pnl_pct']:.1%} vs cost overall."))

    return flags


def format_inr(x: Optional[float]) -> str:
    if x is None:
        return "—"
    sign = "-" if x < 0 else ""
    s = f"{abs(x):.2f}"
    head, _, dec = s.partition(".")
    if len(head) > 3:
        last3 = head[-3:]
        rest = head[:-3]
        grouped: List[str] = []
        while len(rest) > 2:
            grouped.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            grouped.insert(0, rest)
        head = ",".join(grouped) + "," + last3
    return f"{sign}₹{head}.{dec}"


def fmt_pct(x: Optional[float]) -> str:
    return "—" if x is None else f"{x:+.2%}"


def summary_lines(a: Analysis) -> List[str]:
    if not a.totals.get("ok"):
        return ["No holdings could be parsed from the uploaded file."]
    t = a.totals
    out = [f"Portfolio ({t['holdings']} holdings, currency {a.currency})" + (f" as of {a.as_of}" if a.as_of else "") + ":"]
    if t.get("total_value") is not None:
        out.append(f"- Total value: {format_inr(t['total_value'])}")
    if t.get("total_cost") is not None:
        out.append(f"- Total cost: {format_inr(t['total_cost'])}")
    if t.get("total_pnl") is not None:
        out.append(f"- Net P&L: {format_inr(t['total_pnl'])} ({fmt_pct(t.get('total_pnl_pct'))})")
    return out


# -----------------------------
# RAG fallback pipeline
# -----------------------------
class StatementDoc:
    def __init__(self, page_content: str, metadata: Optional[Dict[str, Any]] = None):
        self.page_content = page_content
        self.metadata = metadata or {}


class StatementRagPipeline:
    def __init__(self, raw_text: str = ""):
        self.raw_text = raw_text
        self.documents: List[StatementDoc] = [StatementDoc(raw_text, {"source": "direct_text"})] if raw_text else []
        self.chunks: List[StatementDoc] = []
        if self.documents:
            self.build_pipeline()

    def build_pipeline(self):
        self.chunks = []
        for doc in self.documents:
            text = doc.page_content or ""
            i = 0
            while i < len(text):
                self.chunks.append(StatementDoc(text[i : i + CHUNK_SIZE], metadata=doc.metadata))
                i += max(1, CHUNK_SIZE - CHUNK_OVERLAP)

    def retrieve(self, query: str, k: int = 3) -> List[StatementDoc]:
        if not self.chunks:
            return []
        q_tokens = set(re.findall(r"\w+", query.lower()))
        scored = []
        for idx, chunk in enumerate(self.chunks):
            c_lower = chunk.page_content.lower()
            score = sum(1 for tok in q_tokens if tok in c_lower)
            if score > 0:
                scored.append((score, idx, chunk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item[2] for item in scored[:k]]


# -----------------------------
# Market data
# -----------------------------
def currency_for(ticker: str) -> str:
    t = (ticker or "").upper()
    for suffix, cur in CURRENCY_BY_SUFFIX.items():
        if t.endswith(suffix):
            return cur
    return "UNKNOWN"


def _yahoo_json(url: str) -> Dict[str, Any]:
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (portfolio-agent-standalone)"})
    with urlopen(req, timeout=MARKET_TIMEOUT_S) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def get_live_price(ticker: str) -> Dict[str, Any]:
    ticker = (ticker or "").strip().upper()
    if not ticker:
        return {"ok": False, "ticker": ticker, "note": "No ticker given."}

    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}?range=1d&interval=1d"
        body = _yahoo_json(url)
        meta = body["chart"]["result"][0]["meta"]
        price = meta.get("regularMarketPrice") or meta.get("chartPreviousClose")
        if price:
            return {
                "ok": True,
                "ticker": ticker,
                "price": round(float(price), 2),
                "currency": meta.get("currency") or currency_for(ticker),
                "source": "yahoo-chart",
                "as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
    except Exception:
        pass

    return {
        "ok": False,
        "ticker": ticker,
        "note": f"Could not fetch a live price for {ticker} from any source. Do not guess a price — say it's unavailable.",
    }


def get_price_history(ticker: str, days: int = 30) -> Dict[str, Any]:
    ticker = (ticker or "").strip().upper()
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(ticker)}?range={days}d&interval=1d"
        body = _yahoo_json(url)
        res = body["chart"]["result"][0]
        closes = res["indicators"]["quote"][0].get("close", [])
        vals = [c for c in closes if c is not None]
        if not vals:
            return {"ok": False, "ticker": ticker, "note": "No history available."}
        return {
            "ok": True,
            "ticker": ticker,
            "currency": res["meta"].get("currency"),
            "high_30d": round(max(vals), 2),
            "low_30d": round(min(vals), 2),
            "change_30d_pct": round((vals[-1] / vals[0] - 1) * 100, 2) if vals[0] else None,
        }
    except Exception:
        return {"ok": False, "ticker": ticker, "note": "History unavailable."}


# -----------------------------
# Tools
# -----------------------------
def portfolio_analysis_tool() -> str:
    analysis = CURRENT_SESSION.get("analysis")
    if not analysis:
        return json.dumps({"ok": False, "error": "No statement ingested yet. Call ingest first."})
    return analysis.as_json()


def live_market_data_tool(ticker: str) -> str:
    clean_ticker = (ticker or "").strip().upper()
    quote = get_live_price(clean_ticker)
    hist = get_price_history(clean_ticker, days=30)
    if quote.get("ok") and hist.get("ok"):
        quote["range_30d"] = {
            "high": hist.get("high_30d"),
            "low": hist.get("low_30d"),
            "change_pct": hist.get("change_30d_pct"),
        }
    return json.dumps(quote, ensure_ascii=False)


def statement_retrieval_tool(query: str) -> str:
    rag = CURRENT_SESSION.get("rag")
    if not rag:
        return "No statement document has been loaded."
    results = rag.retrieve(query, k=RETRIEVAL_K)
    if not results:
        return f"No matches found in statement for query: '{query}'."
    return "\n\n---\n\n".join(f"[Chunk {i+1}]: {r.page_content}" for i, r in enumerate(results))


# -----------------------------
# Agent
# -----------------------------
class PortfolioReActAgent:
    def __init__(self):
        self.tools = [portfolio_analysis_tool, live_market_data_tool, statement_retrieval_tool]

    def ingest_statement(self, raw_text: str, file_name: str = "statement.csv") -> Dict[str, Any]:
        holdings, as_of = load_holdings(text=raw_text, name=file_name)
        analysis = analyze(holdings, currency=CURRENCY_DEFAULT, as_of=as_of)
        rag = StatementRagPipeline(raw_text=raw_text)

        CURRENT_SESSION["fileName"] = file_name
        CURRENT_SESSION["rawText"] = raw_text
        CURRENT_SESSION["holdings"] = holdings
        CURRENT_SESSION["analysis"] = analysis
        CURRENT_SESSION["rag"] = rag

        return {
            "ok": True,
            "fileName": file_name,
            "as_of": as_of,
            "holdings_count": len(holdings),
            "summary_text": "\n".join(summary_lines(analysis)),
            "flags": [{"icon": f.icon, "level": f.level, "text": f.text} for f in analysis.flags],
            "analysis": analysis,
        }

    def answer_question(self, question: str) -> str:
        q = (question or "").strip()
        analysis: Analysis = CURRENT_SESSION.get("analysis")
        if not analysis:
            return "⚠️ No statement is currently loaded. Please upload a brokerage statement or holdings sheet first."

        q_lower = q.lower()
        all_tickers = [l["ticker"] for l in analysis.lines]
        matched_ticker = next(
            (t for t in all_tickers if t.lower() in q_lower or t.split(".")[0].lower() in q_lower),
            None,
        )

        # Action 1: live price intent
        if any(w in q_lower for w in ["live", "price", "today", "current"]) and matched_ticker:
            quote = json.loads(live_market_data_tool(matched_ticker))
            stmt_holding = next((l for l in analysis.lines if l["ticker"] == matched_ticker), None)

            if quote.get("ok"):
                live_price = quote["price"]
                cur = quote.get("currency", "")
                as_of_time = quote.get("as_of", "Real-time")
                ans = [
                    f"### [🔴 live] Live Market Data for **{matched_ticker}**",
                    f"- **Live Price:** [🔴 live] **{format_inr(live_price)}** {cur} *(Source: {quote.get('source', 'Yahoo Finance')}, {as_of_time})*",
                ]
                if stmt_holding and stmt_holding.get("current_price"):
                    stmt_price = stmt_holding["current_price"]
                    delta_pct = (live_price - stmt_price) / stmt_price
                    ans.append(f"- **Statement Price:** [📄 statement] {format_inr(stmt_price)}")
                    ans.append(f"- **Delta Since Statement:** [🔴 live] **{fmt_pct(delta_pct)}** ({'higher' if delta_pct >= 0 else 'lower'})")

                if quote.get("range_30d"):
                    r = quote["range_30d"]
                    ans.append(f"- **30-Day Trading Range:** [🔴 live] Low {format_inr(r.get('low'))} / High {format_inr(r.get('high'))}")

                ans.append("\n*Educational analysis only — not personalised financial advice.*")
                return "\n".join(ans)

            return (
                f"### [🔴 live] Quote Lookup for **{matched_ticker}**\n\n"
                f"⚠️ {quote.get('note', 'Quote unavailable.')}\n"
                f"- In accordance with our honest data contract, we never fabricate or guess live prices.\n\n"
                f"*Educational analysis only — not personalised financial advice.*"
            )

        # Action 2: semantic/sector inquiry
        if any(w in q_lower for w in ["tech", "bank", "it", "pharma", "sector", "dividend", "performance", "risk", "weight"]):
            retrieval_text = statement_retrieval_tool(q)
            relevant_lines = [
                l
                for l in analysis.lines
                if any(tok in l["sector"].lower() or tok in l["ticker"].lower() for tok in q_lower.split())
            ]
            if relevant_lines:
                sec_name = relevant_lines[0]["sector"]
                sec_val = sum((l["value"] or 0) for l in relevant_lines)
                sec_pnl = sum((l["pnl"] or 0) for l in relevant_lines)
                total_val = analysis.totals.get("total_value") or 1
                sec_weight = sec_val / total_val

                ans = [
                    f"### [📄 statement] Position Breakdown: **{sec_name}**",
                    f"- **Total Sector Value:** [📄 statement] **{format_inr(sec_val)}** ({sec_weight:.1%} of portfolio)",
                    f"- **Net Unrealized P&L:** [📄 statement] **{format_inr(sec_pnl)}**",
                    "\n**Holdings in this group:**",
                ]
                for l in relevant_lines:
                    ans.append(
                        f"- **{l['ticker']}**: [📄 statement] {l['quantity']} units @ {format_inr(l['purchase_price'])} cost "
                        f"-> value {format_inr(l['value'])} ({fmt_pct(l['pnl_pct'])} P&L, {(l['weight'] or 0):.1%} portfolio weight)"
                    )
                ans.append("\n**Statement Retrieval Evidence:**")
                ans.append(retrieval_text)
                ans.append("\n*Educational analysis only — not personalised financial advice.*")
                return "\n".join(ans)

        # Default overview
        totals = analysis.totals
        overview = [
            f"### [📄 statement] Portfolio Summary ({totals.get('holdings')} Holdings)",
            f"- **Total Portfolio Value:** [📄 statement] **{format_inr(totals.get('total_value'))}**",
            f"- **Total Invested Cost:** [📄 statement] {format_inr(totals.get('total_cost'))}",
            f"- **Total Net P&L:** [📄 statement] **{format_inr(totals.get('total_pnl'))}** ({fmt_pct(totals.get('total_pnl_pct'))})",
            "\n**Active Risk Flags:**",
        ]
        for f in analysis.flags:
            overview.append(f"- {f.icon} [📄 statement] {f.text}")

        overview.append("\n*Educational analysis only — not personalised financial advice.*")
        return "\n".join(overview)


# -----------------------------
# CLI
# -----------------------------
def _read_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def run_cli():
    parser = argparse.ArgumentParser(description="Standalone Portfolio Agent")
    parser.add_argument("--file", help="Path to CSV/TXT statement")
    parser.add_argument("--ask", help="Single question after ingestion")
    parser.add_argument("--interactive", action="store_true", help="Interactive Q&A mode")
    parser.add_argument("--print-analysis-json", action="store_true", help="Print full analysis JSON")
    args = parser.parse_args()

    agent = PortfolioReActAgent()

    if args.file:
        try:
            raw = _read_file(args.file)
        except Exception as e:
            print(f"Failed to read file: {e}", file=sys.stderr)
            sys.exit(1)
        ing = agent.ingest_statement(raw, os.path.basename(args.file))
        print("\n" + ing["summary_text"])
        if ing["flags"]:
            print("\nRisk Flags:")
            for fl in ing["flags"]:
                print(f"- {fl['icon']} {fl['text']}")
        if args.print_analysis_json:
            print("\nAnalysis JSON:")
            print(ing["analysis"].as_json())
    else:
        print("No --file provided. You can still run --interactive after pasting a statement manually.")

    if args.ask:
        print("\n" + agent.answer_question(args.ask))

    if args.interactive:
        if not CURRENT_SESSION.get("analysis"):
            print("\nPaste statement text below. End with a line containing only: END")
            buf: List[str] = []
            while True:
                line = input()
                if line.strip() == "END":
                    break
                buf.append(line)
            raw_text = "\n".join(buf)
            agent.ingest_statement(raw_text, "pasted_statement.txt")

        print("\nInteractive mode. Type your questions. Type 'exit' to quit.")
        while True:
            q = input("\nYou: ").strip()
            if q.lower() in {"exit", "quit"}:
                break
            print("\nAgent:\n" + agent.answer_question(q))


if __name__ == "__main__":
    run_cli()
