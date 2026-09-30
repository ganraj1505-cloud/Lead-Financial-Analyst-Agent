from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request, send_file

BASE_DIR = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)

# Prevent browser caching of static files and API responses
@app.after_request
def add_cache_control_headers(response):
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


# In-memory session store
CURRENT_SESSION: Dict[str, Any] = {
    "file_name": "",
    "raw_text": "",
    "holdings": [],
    "analysis": None,
}

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
}

SECTOR_KEYWORDS = [
    ("PHARMA", "Pharma"),
    ("BANK", "Banking"),
    ("IT", "IT Services"),
    ("TECH", "IT Services"),
    ("AUTO", "Automobile"),
    ("MOTORS", "Automobile"),
    ("GOLD", "Gold ETF"),
    ("ETF", "ETF/Index"),
    ("RETAIL", "Retail"),
    ("OIL", "Energy"),
    ("GAS", "Energy"),
]

HEADER_ALIASES = {
    "ticker": "ticker",
    "symbol": "ticker",
    "quantity": "quantity",
    "qty": "quantity",
    "shares": "quantity",
    "units": "quantity",
    "purchase_price": "purchase_price",
    "buy_price": "purchase_price",
    "cost": "purchase_price",
    "avg_price": "purchase_price",
    "current_price": "current_price",
    "last_price": "current_price",
    "market_price": "current_price",
    "price": "current_price",
    "ltp": "current_price",
    "sector": "sector",
}

TICKER_RE = re.compile(r"^[A-Z0-9.\-]{2,20}$")


@dataclass
class Holding:
    ticker: str
    quantity: float
    purchase_price: Optional[float]
    current_price: Optional[float]
    sector: str


def sector_for(ticker: str, extracted_sector: Optional[str] = None) -> str:
    if extracted_sector and extracted_sector.strip() and extracted_sector.lower() not in {"other", "none", "-"}:
        return extracted_sector.strip()
    t = (ticker or "").upper()
    if t in SECTOR_MAP:
        return SECTOR_MAP[t]
    for kw, sec in SECTOR_KEYWORDS:
        if kw in t:
            return sec
    return "Other"


def to_float(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("₹", "").replace("$", "").replace("£", "")
    if s in {"", "-", "NA", "n/a", "N/A", "null"}:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def format_money(x: Optional[float], currency: str = "INR") -> str:
    if x is None:
        return "Data not available"
    if currency == "INR":
        sign = "-" if x < 0 else ""
        s = f"{abs(x):.2f}"
        head, dec = s.split(".")
        if len(head) > 3:
            last3 = head[-3:]
            rest = head[:-3]
            groups = []
            while len(rest) > 2:
                groups.insert(0, rest[-2:])
                rest = rest[:-2]
            if rest:
                groups.insert(0, rest)
            head = ",".join(groups) + "," + last3
        return f"{sign}₹{head}.{dec}"
    return f"{x:,.2f} {currency}"


def load_holdings(text: str) -> List[Holding]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []

    # Filter out divider lines (e.g., '---', '===')
    clean_lines = [ln for ln in lines if not re.match(r"^[\s\-\=\*\_]+$", ln)]
    if not clean_lines:
        return []

    # Auto-detect delimiter
    sample = "\n".join(clean_lines[:5])
    if "|" in sample:
        delimiter = "|"
    elif "\t" in sample:
        delimiter = "\t"
    else:
        delimiter = ","

    rows: List[List[str]] = []
    for ln in clean_lines:
        parts = [p.strip() for p in ln.split(delimiter)]
        if len(parts) > 1 or (len(parts) == 1 and parts[0]):
            rows.append(parts)

    if not rows:
        return []

    # Map column headers if present
    header = [h.lower() for h in rows[0]]
    col: Dict[str, int] = {}
    for i, h in enumerate(header):
        for alias_key, canon_val in HEADER_ALIASES.items():
            if alias_key in h and canon_val not in col:
                col[canon_val] = i

    out: List[Holding] = []

    # Parsing Strategy 1: Header-matched columns
    if "ticker" in col and "quantity" in col:
        for raw in rows[1:]:
            if not raw:
                continue
            ticker = raw[col["ticker"]].strip().upper() if col["ticker"] < len(raw) else ""
            if not ticker or not TICKER_RE.match(ticker):
                continue
            qty = to_float(raw[col["quantity"]]) if col["quantity"] < len(raw) else None
            if qty is None or qty <= 0:
                continue

            purchase = to_float(raw[col["purchase_price"]]) if "purchase_price" in col and col["purchase_price"] < len(raw) else None
            current = to_float(raw[col["current_price"]]) if "current_price" in col and col["current_price"] < len(raw) else None
            extracted_sec = raw[col["sector"]].strip() if "sector" in col and col["sector"] < len(raw) else None

            out.append(Holding(ticker, qty, purchase, current, sector_for(ticker, extracted_sec)))

        if out:
            return out

    # Parsing Strategy 2: Position-based fallback
    for parts in rows:
        if not parts:
            continue
        ticker = parts[0].strip().upper()
        if not TICKER_RE.match(ticker):
            continue

        extracted_sec = None
        nums = []
        for p in parts[1:]:
            val = to_float(p)
            if val is not None:
                nums.append(val)
            elif not extracted_sec and re.match(r"^[A-Za-z\s&]+$", p):
                extracted_sec = p.strip()

        if not nums or nums[0] <= 0:
            continue

        qty = nums[0]
        purchase = nums[1] if len(nums) > 1 else None
        current = nums[2] if len(nums) > 2 else None

        out.append(Holding(ticker, qty, purchase, current, sector_for(ticker, extracted_sec)))

    return out


def analyze(holdings: List[Holding]) -> Dict[str, Any]:
    lines: List[Dict[str, Any]] = []
    for h in holdings:
        cost = h.quantity * h.purchase_price if h.purchase_price is not None else None
        value = h.quantity * h.current_price if h.current_price is not None else None
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
    total_value = round(sum(l["value"] for l in priced), 2) if priced else 0.0
    total_cost = round(sum(l["cost"] for l in lines if l["cost"] is not None), 2) if lines else 0.0
    total_pnl = round(total_value - total_cost, 2) if total_cost else None
    total_pnl_pct = round(total_pnl / total_cost, 4) if total_pnl is not None and total_cost else None

    for l in lines:
        l["weight"] = (l["value"] / total_value) if (l["value"] is not None and total_value) else None

    return {
        "currency": "INR",
        "lines": lines,
        "totals": {
            "holdings": len(lines),
            "priced_holdings": len(priced),
            "total_cost": total_cost,
            "total_value": total_value,
            "total_pnl": total_pnl,
            "total_pnl_pct": total_pnl_pct,
        },
    }


def extract_ticker(question: str, analysis: Dict[str, Any]) -> Optional[str]:
    q = (question or "").lower()
    for line in analysis.get("lines", []):
        t = line["ticker"]
        if t.lower() in q or t.split(".")[0].lower() in q:
            return t
    m = re.search(r"\b[A-Z]{2,10}(?:\.[A-Z]{1,8})?\b", question or "")
    return m.group(0).upper() if m else None


def _get_json(url: str) -> Dict[str, Any]:
    req = Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    )
    with urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def get_live_market_data(ticker: str) -> Dict[str, Any]:
    clean = (ticker or "").strip().upper()
    if not clean:
        return {"ok": False, "note": "Ticker missing."}

    out: Dict[str, Any] = {"ok": False, "ticker": clean}
    try:
        q1 = _get_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(clean)}?range=5d&interval=1d")
        res = q1["chart"]["result"][0]
        meta = res.get("meta", {})
        closes = [x for x in res["indicators"]["quote"][0].get("close", []) if x is not None]
        price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
        change_week = None
        if len(closes) >= 2 and closes[0]:
            change_week = round((closes[-1] / closes[0] - 1) * 100, 2)

        out.update(
            {
                "ok": True,
                "price": round(float(price), 2) if price is not None else None,
                "currency": meta.get("currency") or "UNKNOWN",
                "trend_week_pct": change_week,
                "as_of": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            }
        )
    except Exception:
        out["note"] = f"Could not fetch live price for {clean}."
        return out

    out["news_summary"] = "Data not available in the provided documents."
    return out


def search_financial_documents(query: str, ticker: str) -> Dict[str, Any]:
    analysis = CURRENT_SESSION.get("analysis") or {}
    raw_text = (CURRENT_SESSION.get("raw_text") or "").lower()

    line = next((l for l in analysis.get("lines", []) if l.get("ticker") == ticker), None)
    totals = analysis.get("totals", {})

    metrics = {
        "position_value": line.get("value") if line else None,
        "position_pnl_pct": line.get("pnl_pct") if line else None,
        "portfolio_weight": line.get("weight") if line else None,
    }

    q_tokens = set(re.findall(r"\w+", query.lower()))
    chunks: List[str] = []
    text = CURRENT_SESSION.get("raw_text", "")
    i = 0
    while i < len(text):
        chunks.append(text[i : i + 600])
        i += 520

    scored: List[Tuple[int, str]] = []
    for chunk in chunks:
        lc = chunk.lower()
        score = sum(1 for tok in q_tokens if tok in lc)
        if score:
            scored.append((score, chunk))
    scored.sort(key=lambda x: x[0], reverse=True)

    return {
        "ticker_line": line,
        "totals": totals,
        "metrics": metrics,
        "evidence": [c for _, c in scored[:3]] or ["Data not available in the provided documents."],
        "raw_contains_risk": ("risk" in raw_text),
    }


def _pct_to_words(x: Optional[float]) -> str:
    if x is None:
        return "Data not available in the provided documents."
    return f"{x*100:.2f}%"


def compose_response(question: str) -> str:
    analysis = CURRENT_SESSION.get("analysis")
    if not analysis or not analysis.get("lines"):
        return "Please ingest a valid statement first."

    q_lower = (question or "").lower()
    totals = analysis.get("totals", {})
    portfolio_intent = any(
        kw in q_lower
        for kw in [
            "net profit",
            "profit",
            "p&l",
            "pnl",
            "overall",
            "portfolio",
            "total",
            "return",
            "gain",
            "loss",
        ]
    )

    ticker = extract_ticker(question, analysis)
    if not ticker and analysis.get("lines") and not portfolio_intent:
        ticker = analysis["lines"][0]["ticker"]

    market = get_live_market_data(ticker or "") if ticker else {"ok": False, "note": "No single ticker selected."}
    docs = search_financial_documents(question, ticker or "")

    line_obj = docs.get("ticker_line")
    metrics = docs.get("metrics", {})

    position_value = metrics.get("position_value")
    position_pnl_pct = metrics.get("position_pnl_pct")
    portfolio_weight = metrics.get("portfolio_weight")

    if portfolio_intent:
        position_value = totals.get("total_value")
        total_cost = totals.get("total_cost")
        total_pnl = totals.get("total_pnl")
        if total_cost not in (None, 0) and total_pnl is not None:
            position_pnl_pct = total_pnl / total_cost
        portfolio_weight = 1.0

    if ticker and market.get("ok"):
        trend = market.get("trend_week_pct")
        trend_text = "Data not available"
        if trend is not None:
            trend_text = f"{'Up' if trend >= 0 else 'Down'} {abs(trend):.2f}% this week"
        price_text = format_money(market.get("price"), market.get("currency", "INR"))
        line1 = f"- Current price for {ticker}: {price_text} ({trend_text})."
    elif ticker and not market.get("ok"):
        line1 = f"- Live market price for {ticker} is currently unavailable from the data source."
    else:
        line1 = "- This question is portfolio-level, so there is no single stock price to report."

    news_line = market.get("news_summary") or "Data not available in the provided documents."

    metric_1 = (
        f"- **Value Snapshot:** {'This portfolio' if portfolio_intent else 'This holding'} is currently valued at {format_money(position_value)}. "
        f"In simple terms, this is the latest worth based on your uploaded numbers."
        if position_value is not None
        else "- **Value Snapshot:** Data not available in the provided documents."
    )

    if portfolio_intent and totals.get("total_pnl") is not None:
        metric_2 = (
            f"- **Profit Snapshot:** Your net portfolio profit/loss is {format_money(totals.get('total_pnl'))} "
            f"({_pct_to_words(position_pnl_pct)} versus cost). In simple terms, this is your overall gain or loss right now."
        )
    else:
        metric_2 = (
            f"- **Profit Snapshot:** {'Your net portfolio result' if portfolio_intent else 'This holding'} is at {_pct_to_words(position_pnl_pct)} versus cost. "
            f"In simple terms, this shows whether the investment is above or below your buy value."
            if position_pnl_pct is not None
            else "- **Profit Snapshot:** Data not available in the provided documents."
        )

    metric_3 = (
        f"- **Concentration Snapshot:** {'Portfolio coverage is 100%' if portfolio_intent else f'This stock is {_pct_to_words(portfolio_weight)} of your portfolio'}. "
        f"In simple terms, concentration tells you how much one position can impact total returns."
        if portfolio_weight is not None
        else "- **Concentration Snapshot:** Data not available in the provided documents."
    )

    if portfolio_intent:
        risk = "concentration risk" if any((ln.get("weight") or 0) >= 0.30 for ln in analysis.get("lines", [])) else "market volatility"
        verdict = (
            "- Your uploaded statement does provide enough data to compute net portfolio performance, and the current totals should be treated as the primary truth for this question. "
            f"The main risk to watch is {risk}, especially if large positions move sharply in a short period."
        )
    elif line_obj is None:
        verdict = (
            "- I could not find enough company-specific metrics in your uploaded documents to fully test whether the current stock price is justified. "
            "The main risk to watch is missing data quality, because incomplete statements can hide concentration or loss risks."
        )
    else:
        justified = "partly justified" if (market.get("ok") and position_pnl_pct is not None) else "not fully testable"
        risk = "concentration risk" if (portfolio_weight or 0) >= 0.30 else "market volatility"
        verdict = (
            f"- Based on current market data and your uploaded statement, the stock valuation looks {justified} by your position-level performance. "
            f"The main risk to watch now is {risk}, especially if short-term price swings continue."
        )

    return "\n".join(
        [
            "### 1. Market Reality Check",
            line1,
            f"- Market sentiment summary: {news_line}",
            "",
            "### 2. Behind the Numbers (Document Analysis)",
            metric_1,
            metric_2,
            metric_3,
            "",
            "### 3. The Bottom Line",
            verdict,
            "",
            "*Educational analysis only, not a recommendation to buy or sell.*",
        ]
    )


@app.get("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.get("/styles.css")
def styles():
    return send_file(BASE_DIR / "styles.css")


@app.get("/app.js")
def script():
    return send_file(BASE_DIR / "app.js")


@app.post("/api/ingest")
def ingest():
    body = request.get_json(silent=True) or {}
    raw_text = (body.get("rawText") or "").strip()
    file_name = (body.get("fileName") or "statement.csv").strip() or "statement.csv"

    if not raw_text:
        return jsonify({"ok": False, "error": "rawText is required."}), 400

    holdings = load_holdings(raw_text)
    if not holdings:
        return jsonify({
            "ok": False,
            "error": "Could not parse any holdings from the text. Ensure valid tickers & quantities are present."
        }), 400

    analysis = analyze(holdings)

    # Completely flush and reset session dictionary
    CURRENT_SESSION.clear()
    CURRENT_SESSION["file_name"] = file_name
    CURRENT_SESSION["raw_text"] = raw_text
    CURRENT_SESSION["holdings"] = [h.__dict__ for h in holdings]
    CURRENT_SESSION["analysis"] = analysis

    flags = []
    for line in analysis["lines"]:
        if line.get("weight") is not None and line["weight"] >= 0.30:
            flags.append({"icon": "⚠️", "text": f"{line['ticker']} is concentrated at {line['weight']*100:.1f}% of portfolio."})
        if line.get("pnl_pct") is not None and line["pnl_pct"] < -0.10:
            flags.append({"icon": "⚠️", "text": f"{line['ticker']} is down {abs(line['pnl_pct']*100):.1f}% versus cost."})

    summary = [
        f"Portfolio ({analysis['totals']['holdings']} holdings, currency INR)",
        f"- Total value: {format_money(analysis['totals']['total_value'])}",
        f"- Total cost: {format_money(analysis['totals']['total_cost'])}",
        f"- Net P&L: {format_money(analysis['totals']['total_pnl'])}",
    ]

    return jsonify(
        {
            "ok": True,
            "fileName": file_name,
            "holdings_count": len(holdings),
            "summary_text": "\n".join(summary),
            "flags": flags,
            "analysis": analysis,
        }
    )


@app.post("/api/ask")
def ask():
    body = request.get_json(silent=True) or {}
    question = (body.get("question") or "").strip()
    if not question:
        return jsonify({"ok": False, "error": "question is required."}), 400

    answer = compose_response(question)
    return jsonify({"ok": True, "answer": answer})


@app.get("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=7860, debug=True)