"""
sec_client.py — Reusable SEC EDGAR XBRL API client.

Supports any publicly-traded US issuer by ticker symbol.
Handles ticker-to-CIK resolution, companyfacts retrieval,
consolidated revenue extraction, and best-effort segment detection.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Any

import requests

log = logging.getLogger(__name__)

# ── Public endpoints ───────────────────────────────────────────────────────────
SEC_BASE = "https://data.sec.gov"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"

# ── Revenue concept priority list ─────────────────────────────────────────────
REVENUE_CONCEPTS: list[str] = [
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "NetSales",
]

# ── Segment keyword mapping ────────────────────────────────────────────────────
# (concept_substring_lower, display_name) — both a segment keyword AND a
# revenue keyword must appear in the concept name (case-insensitive).
SEGMENT_KEYWORDS: list[tuple[str, str]] = [
    ("northamerica", "North America"),
    ("international", "International"),
    ("amazonwebservices", "AWS"),
    ("awssegment", "AWS"),
    ("cloud", "Cloud"),
    ("enterprise", "Enterprise"),
    ("consumer", "Consumer"),
    ("commercial", "Commercial"),
    ("government", "Government"),
    ("domestic", "Domestic"),
    ("foreign", "Foreign"),
]
REVENUE_KEYWORDS: frozenset[str] = frozenset({"revenue", "sales", "netsale"})

# ── HTTP settings ──────────────────────────────────────────────────────────────
TIMEOUT_S = 30
MAX_RETRIES = 3
BACKOFF_BASE = 2.0          # wait = BACKOFF_BASE ** attempt  (seconds)
REQUEST_INTERVAL_S = 0.6    # minimum gap between SEC requests (fair-use)

# ── Output schema ──────────────────────────────────────────────────────────────
OUTPUT_COLUMNS: list[str] = [
    "company", "ticker", "cik", "metric", "segment",
    "period_end", "fiscal_year", "fiscal_period",
    "value", "unit", "form", "accession", "filed",
]

# ── Module-level state ─────────────────────────────────────────────────────────
_last_req_ts: float = 0.0
_ticker_cache: dict[str, Any] | None = None
_ticker_index: list[tuple[str, str, str, list[str], str]] | None = None
_ticker_lock = threading.Lock()


# ── HTTP Client ────────────────────────────────────────────────────────────────

def get_user_agent() -> str:
    """Return SEC_USER_AGENT env var; raise EnvironmentError if unset/empty."""
    ua = os.getenv("SEC_USER_AGENT", "").strip()
    if not ua:
        raise EnvironmentError(
            "SEC_USER_AGENT is not set. "
            'Set it to "Your Name your@email.com" in .env or as an environment variable.'
        )
    return ua


def sec_get(url: str, raw: bool = False) -> Any:
    """
    GET a resource from SEC EDGAR with rate-limiting and retry/backoff.
    Returns parsed JSON, or the raw response bytes when raw=True.
    Raises RuntimeError after MAX_RETRIES failed attempts.
    """
    global _last_req_ts

    headers = {
        "User-Agent": get_user_agent(),
        "Accept-Encoding": "gzip, deflate",
    }

    for attempt in range(MAX_RETRIES):
        elapsed = time.monotonic() - _last_req_ts
        if elapsed < REQUEST_INTERVAL_S:
            time.sleep(REQUEST_INTERVAL_S - elapsed)

        try:
            log.info("GET %s", url)
            resp = requests.get(url, headers=headers, timeout=TIMEOUT_S)
            _last_req_ts = time.monotonic()

            if resp.status_code == 200:
                return resp.content if raw else resp.json()

            if resp.status_code in (429, 503):
                wait = BACKOFF_BASE ** (attempt + 1)
                log.warning(
                    "HTTP %s — waiting %.1fs before retry %d/%d.",
                    resp.status_code, wait, attempt + 2, MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            resp.raise_for_status()

        except requests.RequestException as exc:
            if attempt < MAX_RETRIES - 1:
                wait = BACKOFF_BASE ** (attempt + 1)
                log.warning("Request error: %s — retrying in %.1fs.", exc, wait)
                time.sleep(wait)
            else:
                raise RuntimeError(
                    f"Failed to fetch {url} after {MAX_RETRIES} attempts: {exc}"
                ) from exc

    raise RuntimeError(f"Failed to fetch {url} after {MAX_RETRIES} attempts.")


# ── Ticker resolution ──────────────────────────────────────────────────────────

def load_tickers() -> dict[str, Any]:
    """Return the SEC ticker master list, fetching it once and caching it in memory."""
    global _ticker_cache, _ticker_index
    with _ticker_lock:
        if _ticker_cache is None:
            log.info("Fetching SEC EDGAR ticker master list...")
            data = sec_get(TICKERS_URL)
            index = []
            # The SEC lists issuers in descending market-cap order; dict order keeps that rank.
            for entry in data.values():
                tk = str(entry.get("ticker", "")).upper()
                if not tk:
                    continue
                title = str(entry.get("title", tk))
                norm = _norm_name(title)
                index.append((tk, title, norm, norm.split(), str(entry["cik_str"]).zfill(10)))
            _ticker_cache, _ticker_index = data, index
        return _ticker_cache


def _norm_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9& ]+", " ", s.lower())).strip()


def _pretty_name(title: str) -> str:
    if not title.isupper():
        return title
    return " ".join(w if "&" in w and len(w) > 1 else w.title() for w in title.split(" "))


# Well-known brands whose names differ from the registrant's legal name.
_ALIASES: dict[str, tuple[str, ...]] = {
    "google": ("GOOGL", "GOOG"), "youtube": ("GOOGL",), "android": ("GOOGL",),
    "facebook": ("META",), "instagram": ("META",), "whatsapp": ("META",),
    "chase": ("JPM",), "xbox": ("MSFT",), "linkedin": ("MSFT",),
    "iphone": ("AAPL",), "aws": ("AMZN",), "whole foods": ("AMZN",),
}
# Preferred shares, warrants, units and rights (e.g. JPM-PC, XYZ-WT) rank after common stock.
_SECONDARY_RE = re.compile(r"-(P[A-Z]{0,2}|W[A-Z]{0,2}|U|R)$")


def search_tickers(query: str, limit: int = 8) -> list[dict[str, str]]:
    """
    Rank issuers against a partial ticker or company name.

    Order: exact ticker, ticker prefix, name (or brand alias) starts with the
    query, every query word starts a word of the name, query inside the name.
    Ties put common stock before preferred/warrants, then keep the SEC's
    market-cap order, so "micr" gives Microsoft before smaller issuers.
    """
    q_raw = query.strip().lower().replace(".", "-")      # SEC writes class shares as BRK-B
    q = _norm_name(query)
    if not q:
        return []
    load_tickers()
    assert _ticker_index is not None
    tokens = q.split()
    compact = q.replace(" ", "")

    aliased: set[str] = set()
    if len(q) >= 3:
        for key, tks in _ALIASES.items():
            if key.startswith(q):
                aliased.update(tks)

    scored: list[tuple[int, int, int]] = []        # (score, is_secondary, index position)
    for pos, (tk, _title, norm, words, _cik) in enumerate(_ticker_index):
        tk_l = tk.lower()
        norm_c = norm.replace(" ", "")
        if tk_l == q_raw:
            score = 0
        elif tk_l.startswith(q_raw):
            score = 1
        elif norm.startswith(q) or norm_c.startswith(compact):
            score = 2
        elif all(any(w.startswith(t) for w in words) for t in tokens):
            score = 3
        elif len(q) >= 3 and (q in norm or (len(compact) >= 4 and compact in norm_c)):
            score = 4
        else:
            score = 9
        if tk in aliased:
            score = min(score, 2)
        if score == 9:
            continue
        scored.append((score, 1 if _SECONDARY_RE.search(tk) else 0, pos))

    scored.sort()
    out: list[dict[str, str]] = []
    for _, _, pos in scored[:limit]:
        tk, title, _norm, _words, cik = _ticker_index[pos]
        out.append({"ticker": tk, "name": _pretty_name(title), "cik": cik})
    return out


def resolve_ticker(ticker: str) -> tuple[str, str]:
    """
    Map a ticker symbol to (padded_cik, entity_name).
    The full ticker master list is cached in memory after the first call.
    Raises ValueError if the ticker is not found in SEC EDGAR.
    """
    cache = load_tickers()

    ticker_upper = ticker.upper().strip().replace(".", "-")
    for entry in cache.values():
        if entry.get("ticker", "").upper() == ticker_upper:
            cik_padded = str(entry["cik_str"]).zfill(10)
            name = entry.get("title", ticker_upper)
            return cik_padded, name

    raise ValueError(
        f"Ticker '{ticker}' was not found in the SEC EDGAR issuer list. "
        "Verify the symbol is listed on a US exchange and files with the SEC."
    )


def fetch_facts(cik_padded: str) -> dict[str, Any]:
    """Fetch the full XBRL companyfacts JSON for a given CIK."""
    url = f"{SEC_BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json"
    return sec_get(url)


# ── Fact normalisation ─────────────────────────────────────────────────────────

def _annual_rows(
    units_usd: list[dict[str, Any]],
    concept: str,
    segment: str,
    ticker: str,
    cik_padded: str,
    entity_name: str,
) -> list[dict[str, Any]]:
    """
    Filter raw USD fact dicts to annual 10-K/10-K/A records, deduplicate by
    fiscal year (latest-filed entry supersedes earlier ones, handles amendments),
    and return normalised row dicts.
    """
    annual = [
        f for f in units_usd
        if f.get("fp") == "FY"
        and f.get("form") in ("10-K", "10-K/A")
        and f.get("fy") is not None
    ]

    # A filing's `fy` also tags its prior-year comparatives, so prefer the
    # latest period end, then the latest filing (amendments supersede originals).
    by_fy: dict[int, dict[str, Any]] = {}
    for f in annual:
        fy: int = f["fy"]
        if fy not in by_fy or (f.get("end", ""), f.get("filed", "")) > (
            by_fy[fy].get("end", ""), by_fy[fy].get("filed", "")
        ):
            by_fy[fy] = f

    return [
        {
            "company": entity_name,
            "ticker": ticker.upper(),
            "cik": cik_padded,
            "metric": concept,
            "segment": segment,
            "period_end": f.get("end"),
            "fiscal_year": fy,
            "fiscal_period": f.get("fp"),
            "value": f.get("val"),
            "unit": "USD",
            "form": f.get("form"),
            "accession": f.get("accn"),
            "filed": f.get("filed"),
        }
        for fy, f in sorted(by_fy.items())
    ]


# ── Revenue & segment extraction ───────────────────────────────────────────────

def extract_revenue(
    facts: dict[str, Any],
    ticker: str,
    cik_padded: str,
    entity_name: str,
    start_year: int,
    end_year: int,
) -> tuple[list[dict[str, Any]], str]:
    """
    Try REVENUE_CONCEPTS in priority order; return (rows, concept_used) for
    the first concept that has data in the requested fiscal year range.
    Returns ([], '') if no concept yields data.
    """
    us_gaap: dict = facts.get("facts", {}).get("us-gaap", {})
    target_fys = set(range(start_year, end_year + 1))

    for concept in REVENUE_CONCEPTS:
        units_usd = us_gaap.get(concept, {}).get("units", {}).get("USD", [])
        rows = _annual_rows(units_usd, concept, "Consolidated", ticker, cik_padded, entity_name)
        rows = [r for r in rows if r["fiscal_year"] in target_fys]
        if rows:
            log.info("Revenue concept: %s | %d records", concept, len(rows))
            return rows, concept

    return [], ""


def extract_segments(
    facts: dict[str, Any],
    ticker: str,
    cik_padded: str,
    entity_name: str,
    target_fys: set[int],
) -> tuple[list[dict[str, Any]], list[str]]:
    """
    Best-effort segment extraction: scan all us-gaap concept names for
    keyword combinations indicating a segment-level revenue fact.

    Note: SEC companyfacts surfaces entity-level facts. Full dimensional
    segment breakdowns require parsing the XBRL instance document (v2 scope).
    This function captures any segment facts tagged as standalone concepts.
    """
    us_gaap: dict = facts.get("facts", {}).get("us-gaap", {})
    rows: list[dict[str, Any]] = []
    found_segments: list[str] = []
    seen: set[str] = set()

    for concept, concept_data in us_gaap.items():
        if concept in REVENUE_CONCEPTS:
            continue

        concept_lower = concept.lower()
        matched: str | None = None
        for kw, seg_name in SEGMENT_KEYWORDS:
            if kw in concept_lower and any(rk in concept_lower for rk in REVENUE_KEYWORDS):
                matched = seg_name
                break
        if not matched:
            continue

        units_usd = concept_data.get("units", {}).get("USD", [])
        seg_rows = _annual_rows(units_usd, concept, matched, ticker, cik_padded, entity_name)
        seg_rows = [r for r in seg_rows if r["fiscal_year"] in target_fys]

        if seg_rows:
            rows.extend(seg_rows)
            if matched not in seen:
                found_segments.append(matched)
                seen.add(matched)
            log.info("Segment concept: %s -> %s (%d records)", concept, matched, len(seg_rows))

    return rows, found_segments


# ── Main public entry point ────────────────────────────────────────────────────

def fetch_financials(
    ticker: str,
    start_year: int,
    end_year: int,
) -> dict[str, Any]:
    """
    Fetch SEC EDGAR annual revenue for a given ticker and fiscal year range.

    Returns a dict:
        company       — entity name
        ticker        — normalised ticker symbol
        cik           — zero-padded CIK string
        concept_used  — XBRL revenue concept that yielded data
        segments_found — list of segment display names (may be empty)
        rows          — list of dicts matching OUTPUT_COLUMNS schema
    """
    cik_padded, entity_name = resolve_ticker(ticker)
    log.info("Resolved %s → CIK %s (%s)", ticker.upper(), cik_padded, entity_name)

    facts = fetch_facts(cik_padded)

    consolidated, concept_used = extract_revenue(
        facts, ticker, cik_padded, entity_name, start_year, end_year
    )
    if not consolidated:
        raise ValueError(
            f"No annual 10-K revenue data found for {ticker.upper()} "
            f"({start_year}–{end_year}). "
            "The issuer may not file 10-K reports or may use a non-standard revenue concept."
        )

    target_fys = {r["fiscal_year"] for r in consolidated}
    segment_rows, found_segments = extract_segments(
        facts, ticker, cik_padded, entity_name, target_fys
    )

    return {
        "company": entity_name,
        "ticker": ticker.upper(),
        "cik": cik_padded,
        "concept_used": concept_used,
        "segments_found": found_segments,
        "rows": consolidated + segment_rows,
    }
