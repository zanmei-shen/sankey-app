"""
sankey_data.py — Build Sankey chart models (nodes + links) from SEC EDGAR data.

Two modes:
    income    Revenue -> cost of revenue / gross profit -> opex / operating
              income -> other items -> pre-tax income -> tax / net income
    segments  Revenue segments -> total revenue (parsed from the filing's
              XBRL instance document, because companyfacts has no dimensions)

Periods: FY, Q1, Q2, Q3, Q4. Q4 is never filed on its own, so it is derived as
FY minus the Q3 year-to-date figure.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import sec_client

log = logging.getLogger(__name__)

PERIODS = ("FY", "Q1", "Q2", "Q3", "Q4")
MODES = ("income", "segments")

EPS = 1.0                    # dollars; amounts below this are treated as zero
FACTS_TTL_S = 3600
FACTS_CACHE_MAX = 16
INSTANCE_CACHE_MAX = 16

# Colours
C_REV = "#3b6fd6"
C_PROFIT = "#2f9e63"
C_GAIN = "#79c69a"
C_COST = "#e0605a"
C_COST2 = "#ec8f89"
C_LOSS = "#b3261e"
C_OTHER = "#9aa5b1"
SEGMENT_PALETTE = [
    "#4c78a8", "#f58518", "#54a24b", "#b279a2", "#e45756",
    "#72b7b2", "#eeca3b", "#9d755d", "#bab0ac",
]


class SankeyUnavailable(ValueError):
    """The filings do not contain the data needed to draw the requested chart."""


# ── Chart model ────────────────────────────────────────────────────────────────

@dataclass
class Node:
    name: str
    col: int
    color: str
    rank: float = 0.0        # vertical order within a column (low = top)


@dataclass
class Link:
    source: str
    target: str
    value: float
    color: str | None = None  # defaults to the target node colour when drawn


@dataclass
class Sankey:
    title: str
    subtitle: str
    nodes: list[Node]
    links: list[Link]
    base_value: float         # denominator for the "% of revenue" labels


class _Builder:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self.links: list[Link] = []

    def node(self, name: str, col: int, color: str, rank: float = 0.0) -> str:
        self.nodes.setdefault(name, Node(name, col, color, rank))
        return name

    def link(self, source: str, target: str, value: float, color: str | None = None) -> None:
        if value <= EPS:
            return
        if self.nodes[source].col >= self.nodes[target].col:
            raise AssertionError(f"link {source!r} -> {target!r} must go left to right")
        self.links.append(Link(source, target, value, color))


# ── Facts cache (slimmed to the concepts used here) ────────────────────────────

_REVENUE_TAGS = tuple(sec_client.REVENUE_CONCEPTS)
_COGS_TAGS = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")
_PRETAX_TAGS = (
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
    "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
)
_NET_TAGS = ("NetIncomeLoss", "ProfitLoss")
_RD_TAGS = (
    "ResearchAndDevelopmentExpense",
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
)
_SM_TAGS = ("SellingAndMarketingExpense", "MarketingExpense")

_WANTED = frozenset(
    _REVENUE_TAGS + _COGS_TAGS + _PRETAX_TAGS + _NET_TAGS + _RD_TAGS + _SM_TAGS + (
        "GrossProfit", "OperatingIncomeLoss", "CostsAndExpenses", "NonoperatingIncomeExpense",
        "IncomeTaxExpenseBenefit", "SellingGeneralAndAdministrativeExpense",
        "GeneralAndAdministrativeExpense",
    )
)

_facts_cache: dict[str, tuple[float, dict[str, list[dict[str, Any]]]]] = {}
_facts_lock = threading.Lock()


def _load_us(cik: str) -> dict[str, list[dict[str, Any]]]:
    """Return {concept: [USD duration facts]} for the CIK, cached for FACTS_TTL_S."""
    now = time.monotonic()
    with _facts_lock:
        hit = _facts_cache.get(cik)
        if hit and now - hit[0] < FACTS_TTL_S:
            return hit[1]

    facts = sec_client.fetch_facts(cik)
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    slim: dict[str, list[dict[str, Any]]] = {}
    for concept in _WANTED:
        rows = [
            f for f in us_gaap.get(concept, {}).get("units", {}).get("USD", [])
            if f.get("start") and f.get("end") and f.get("val") is not None
        ]
        if rows:
            slim[concept] = rows
    del facts

    with _facts_lock:
        _facts_cache[cik] = (now, slim)
        while len(_facts_cache) > FACTS_CACHE_MAX:
            _facts_cache.pop(min(_facts_cache, key=lambda k: _facts_cache[k][0]))
    return slim


# ── Period resolution ──────────────────────────────────────────────────────────

@dataclass
class _Window:
    concept: str             # revenue concept that anchors the period
    start: str
    end: str
    accn: str                # filing holding the period (FY filing for Q4)
    label: str
    fy_start: str | None = None   # Q4 only: fiscal-year start
    ytd_end: str | None = None    # Q4 only: end of the Q3 year-to-date window
    ytd_accn: str | None = None   # Q4 only: Q3 filing


def _days(f: dict[str, Any]) -> int:
    return (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days


def _fmt_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}, {d.year}"


def _pick(
    us: dict[str, list[dict[str, Any]]], concept: str, year: int, fp: str,
    lo: int, hi: int, start: str | None = None,
) -> dict[str, Any] | None:
    """Latest-ending fact for (fiscal year, fiscal period) whose duration is in [lo, hi] days."""
    best: dict[str, Any] | None = None
    for f in us.get(concept, ()):
        if f.get("fy") != year or f.get("fp") != fp:
            continue
        if not str(f.get("form", "")).startswith(("10-K", "10-Q")):
            continue
        if not lo <= _days(f) <= hi:
            continue
        if start is not None and f["start"] != start:
            continue
        if best is None or (f["end"], f.get("filed", "")) > (best["end"], best.get("filed", "")):
            best = f
    return best


def _resolve(us: dict[str, list[dict[str, Any]]], year: int, period: str) -> _Window:
    fp = "FY" if period in ("FY", "Q4") else period
    lo, hi = (340, 380) if fp == "FY" else (80, 100)

    for concept in _REVENUE_TAGS:
        f = _pick(us, concept, year, fp, lo, hi)
        if f is None:
            continue
        if period != "Q4":
            return _Window(
                concept, f["start"], f["end"], f["accn"],
                _period_label(period, year, f["start"], f["end"]),
            )
        ytd = _pick(us, concept, year, "Q3", 255, 290, start=f["start"])
        if ytd is None:
            raise SankeyUnavailable(
                f"Q4 {year} cannot be derived: the Q3 year-to-date figure is missing."
            )
        q4_start = (date.fromisoformat(ytd["end"]) + timedelta(days=1)).isoformat()
        return _Window(
            concept, q4_start, f["end"], f["accn"],
            _period_label(period, year, q4_start, f["end"]),
            fy_start=f["start"], ytd_end=ytd["end"], ytd_accn=ytd["accn"],
        )

    raise SankeyUnavailable(
        f"No {period} {year} revenue was found in the company's 10-K/10-Q filings."
    )


def _period_label(period: str, year: int, start: str, end: str) -> str:
    name = f"FY{year}" if period == "FY" else f"{period} FY{year}"
    return f"{name} ({_fmt_date(start)} – {_fmt_date(end)})"


class _Reader:
    """Look up a concept's value for the resolved window (Q4 = FY - Q3 YTD)."""

    def __init__(self, us: dict[str, list[dict[str, Any]]], w: _Window) -> None:
        self.us = us
        self.w = w

    def _exact(self, concept: str, start: str, end: str) -> float | None:
        best: dict[str, Any] | None = None
        for f in self.us.get(concept, ()):
            if f["start"] == start and f["end"] == end:
                if best is None or f.get("filed", "") >= best.get("filed", ""):
                    best = f
        return None if best is None else float(best["val"])

    def get(self, *concepts: str) -> float | None:
        w = self.w
        for concept in concepts:
            if w.ytd_end is None:
                v = self._exact(concept, w.start, w.end)
            else:
                full = self._exact(concept, w.fy_start or "", w.end)
                ytd = self._exact(concept, w.fy_start or "", w.ytd_end)
                v = None if full is None or ytd is None else full - ytd
            if v is not None:
                return v
        return None


# ── Money formatting (subtitle) ────────────────────────────────────────────────

def fmt_money(v: float) -> str:
    a = abs(v)
    sign = "-" if v < 0 else ""
    if a >= 1e9:
        return f"{sign}${a / 1e9:,.1f}B" if a >= 1e10 else f"{sign}${a / 1e9:,.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:,.0f}M" if a >= 1e8 else f"{sign}${a / 1e6:,.1f}M"
    return f"{sign}${a:,.0f}"


def _display_name(name: str, ticker: str) -> str:
    return f"{name.title() if name.isupper() else name} ({ticker.upper()})"


# ── Income statement ───────────────────────────────────────────────────────────

def _opex_items(r: _Reader, opex: float) -> list[tuple[str, float]]:
    """Named operating-expense lines that fit inside opex, plus an 'Other' remainder."""
    items: list[tuple[str, float]] = []
    rd = r.get(*_RD_TAGS)
    sga = r.get("SellingGeneralAndAdministrativeExpense")
    if rd and rd > EPS:
        items.append(("Research & development", rd))
    if sga and sga > EPS:
        items.append(("Selling, general & admin", sga))
    else:
        sm = r.get(*_SM_TAGS)
        ga = r.get("GeneralAndAdministrativeExpense")
        if sm and sm > EPS:
            items.append(("Sales & marketing", sm))
        if ga and ga > EPS:
            items.append(("General & admin", ga))

    named = sum(v for _, v in items)
    if not items or named > opex * 1.001:
        return []            # overlapping tags: fall back to a single opex node
    rest = opex - named
    if rest > opex * 0.002:
        items.append(("Other operating expenses", rest))
    return items


def _build_financial_statement(
    ticker: str,
    name: str,
    cik: str,
    w: _Window,
    r: _Reader,
    rev: float,
    categories: bool,
) -> Sankey:
    """Build a bank-style income flow when operating income is not reported."""
    pretax = r.get(*_PRETAX_TAGS)
    if pretax is None or pretax <= EPS:
        raise SankeyUnavailable(
            "This company does not report standard operating income or positive pre-tax income "
            "for the selected period, so an income-statement chart is unavailable."
        )

    net = r.get(*_NET_TAGS)
    tax = r.get("IncomeTaxExpenseBenefit")
    if tax is None:
        tax = (pretax - net) if net is not None else 0.0
    if net is None:
        net = pretax - tax

    b = _Builder()
    b.node("Net revenues", 0, C_REV, 0)
    b.node("Pre-tax income", 1, C_PROFIT, 0)
    if pretax <= rev:
        expenses = rev - pretax
        b.link("Net revenues", "Pre-tax income", pretax)
        if expenses > EPS:
            b.node("Expenses & other items", 1, C_COST, 1)
            b.link("Net revenues", "Expenses & other items", expenses)
    else:
        b.link("Net revenues", "Pre-tax income", rev)
        b.node("Other income", 0, C_GAIN, 1)
        b.link("Other income", "Pre-tax income", pretax - rev)

    adj = net - (pretax - tax)
    if abs(adj) < 0.0002 * rev:
        adj = 0.0
    tax_out = min(max(tax, 0.0), pretax)
    adj_out = min(max(-adj, 0.0), pretax - tax_out)
    net_flow = pretax - tax_out - adj_out
    tax_in = max(-tax, 0.0)
    adj_in = max(adj, 0.0)

    if tax_out > EPS:
        b.node("Income tax", 2, C_COST, 1)
        b.link("Pre-tax income", "Income tax", tax_out)
    if adj_out > EPS:
        b.node("Minority interest & other", 2, C_COST, 2)
        b.link("Pre-tax income", "Minority interest & other", adj_out)
    if net_flow + tax_in + adj_in > EPS:
        b.node("Net income", 2, C_PROFIT, 0)
        b.link("Pre-tax income", "Net income", net_flow)
        if tax_in > EPS:
            b.node("Tax benefit", 1, C_GAIN, 2)
            b.link("Tax benefit", "Net income", tax_in)
        if adj_in > EPS:
            b.node("Other gains", 1, C_GAIN, 3)
            b.link("Other gains", "Net income", adj_in)

    parts = [f"Net revenues {fmt_money(rev)}", f"Pre-tax income {fmt_money(pretax)}"]
    if net is not None:
        parts.append(f"Net income {fmt_money(net)}")
    parts.append("bank-style income statement (operating income not reported)")

    if categories:
        try:
            cats = _revenue_categories(cik, w, rev)
        except (ValueError, RuntimeError, ET.ParseError) as exc:
            log.warning("Revenue categories unavailable for %s: %s", ticker.upper(), exc)
            parts.append("revenue categories not available")
        else:
            for node in b.nodes.values():
                node.col += 1
            for i, (label, value, color) in enumerate(cats):
                if label in b.nodes:
                    label = f"{label} (revenue)"
                b.node(label, 0, color, i)
                b.link(label, "Net revenues", value, color)

    return Sankey(
        title=f"{_display_name(name, ticker)} — Income statement, {w.label}",
        subtitle="  ·  ".join(parts),
        nodes=list(b.nodes.values()),
        links=b.links,
        base_value=rev,
    )


def build_income_statement(
    ticker: str, year: int, period: str, categories: bool = True
) -> Sankey:
    cik, name = sec_client.resolve_ticker(ticker)
    us = _load_us(cik)
    w = _resolve(us, year, period)
    r = _Reader(us, w)

    rev = r.get(w.concept)
    if rev is None or rev <= EPS:
        raise SankeyUnavailable(f"Revenue is not reported for {w.label}.")

    gp = r.get("GrossProfit")
    if gp is None:
        cogs_rep = r.get(*_COGS_TAGS)
        gp = None if cogs_rep is None else rev - cogs_rep
    op = r.get("OperatingIncomeLoss")
    if op is None:
        costs = r.get("CostsAndExpenses")
        if costs is not None:
            op = rev - costs
    if op is None:
        return _build_financial_statement(ticker, name, cik, w, r, rev, categories)
    if gp is not None:
        gp = min(gp, rev)
        if gp < 0 < op:
            raise SankeyUnavailable("Inconsistent gross profit and operating income in the filing.")

    b = _Builder()
    b.node("Revenue", 0, C_REV, 0)

    # Stage 1 — cost of revenue vs. gross profit
    top: str | None = "Revenue"
    top_avail = rev
    if gp is not None:
        cogs = rev - gp
        b.node("Cost of revenue", 1, C_COST, 1)
        b.link("Revenue", "Cost of revenue", min(cogs, rev))
        if gp > EPS:
            b.node("Gross profit", 1, C_PROFIT, 0)
            b.link("Revenue", "Gross profit", gp)
            top, top_avail = "Gross profit", gp
        else:
            b.node("Gross loss", 0, C_LOSS, 1)
            b.link("Gross loss", "Cost of revenue", -gp)
            top, top_avail = None, 0.0

    # Stage 2 — operating expenses vs. operating income
    gross = gp if gp is not None else rev
    opex = gross - op
    other_op_inc = 0.0
    if opex < 0:
        if op > EPS:
            other_op_inc, opex = -opex, 0.0
        else:
            opex = 0.0

    if op > EPS:
        b.node("Operating income", 2, C_PROFIT, 0)
    if other_op_inc > EPS:
        b.node("Other operating income", 1, C_GAIN, -1)
        b.link("Other operating income", "Operating income", other_op_inc)

    if opex > EPS:
        b.node("Operating expenses", 2, C_COST, 1)
        used = min(top_avail, opex) if top else 0.0
        if top:
            b.link(top, "Operating expenses", used)
        if opex - used > EPS:
            b.node("Operating loss", 1, C_LOSS, 3)
            b.link("Operating loss", "Operating expenses", opex - used)
        for i, (label, val) in enumerate(_opex_items(r, opex)):
            b.node(label, 3, C_COST2, 10 + i)
            b.link("Operating expenses", label, val)
    if op > EPS and top:
        b.link(top, "Operating income", op - other_op_inc)

    # Stage 3 — non-operating items, pre-tax income, tax, net income
    net = r.get(*_NET_TAGS)
    pretax: float | None = None
    if op > EPS:
        pretax_rep = r.get(*_PRETAX_TAGS)
        nonop = r.get("NonoperatingIncomeExpense")
        pretax = pretax_rep if pretax_rep is not None else op + (nonop or 0.0)
        oth = pretax - op

        if pretax > EPS:
            b.node("Pre-tax income", 3, C_PROFIT, 0)
        if oth > EPS:
            b.node("Other income", 2, C_GAIN, -1)
            b.link("Other income", "Pre-tax income", oth)
            b.link("Operating income", "Pre-tax income", op)
        elif oth < -EPS:
            loss = -oth
            from_op = min(op, loss)
            b.node("Other expense", 3, C_COST, 1)
            b.link("Operating income", "Other expense", from_op)
            if loss - from_op > EPS:
                b.node("Pre-tax loss", 2, C_LOSS, 2)
                b.link("Pre-tax loss", "Other expense", loss - from_op)
            b.link("Operating income", "Pre-tax income", op - from_op)
        else:
            b.link("Operating income", "Pre-tax income", op)

        if pretax > EPS:
            tax = r.get("IncomeTaxExpenseBenefit")
            if tax is None:
                tax = (pretax - net) if net is not None else 0.0
            if net is None:
                net = pretax - tax
            adj = net - (pretax - tax)
            if abs(adj) < 0.0002 * rev:      # rounding noise only; larger gaps get their own node
                adj = 0.0
            tax_out = min(max(tax, 0.0), pretax)
            adj_out = min(max(-adj, 0.0), pretax - tax_out)
            net_flow = pretax - tax_out - adj_out
            tax_in = max(-tax, 0.0)
            adj_in = max(adj, 0.0)

            if tax_out > EPS:
                b.node("Income tax", 4, C_COST, 1)
                b.link("Pre-tax income", "Income tax", tax_out)
            if adj_out > EPS:
                b.node("Minority interest & other", 4, C_COST, 2)
                b.link("Pre-tax income", "Minority interest & other", adj_out)
            if net_flow + tax_in + adj_in > EPS:
                b.node("Net income", 4, C_PROFIT, 0)
                b.link("Pre-tax income", "Net income", net_flow)
                if tax_in > EPS:
                    b.node("Tax benefit", 3, C_GAIN, 5)
                    b.link("Tax benefit", "Net income", tax_in)
                if adj_in > EPS:
                    b.node("Other gains", 3, C_GAIN, 6)
                    b.link("Other gains", "Net income", adj_in)

    parts = [f"Revenue {fmt_money(rev)}"]
    parts.append(f"Operating {'income' if op >= 0 else 'loss'} {fmt_money(abs(op))}")
    if net is not None:
        parts.append(f"Net {'income' if net >= 0 else 'loss'} {fmt_money(abs(net))}")
    if net is not None and op <= EPS:
        parts.append("chart ends at the operating stage")

    if categories:
        try:
            cats = _revenue_categories(cik, w, rev)
        except (ValueError, RuntimeError, ET.ParseError) as exc:
            log.warning("Revenue categories unavailable for %s: %s", ticker.upper(), exc)
            parts.append("revenue categories not available")
        else:
            for n in b.nodes.values():
                n.col += 1
            for i, (label, value, color) in enumerate(cats):
                if label in b.nodes:
                    label = f"{label} (revenue)"
                b.node(label, 0, color, i)
                b.link(label, "Revenue", value, color)

    return Sankey(
        title=f"{_display_name(name, ticker)} — Income statement, {w.label}",
        subtitle="  ·  ".join(parts),
        nodes=list(b.nodes.values()),
        links=b.links,
        base_value=rev,
    )


# ── Revenue by segment (XBRL instance document) ────────────────────────────────

_XBRLI = "{http://www.xbrl.org/2003/instance}"
_XBRLDI = "{http://xbrl.org/2006/xbrldi}"
_AXIS_PREFERENCE = (
    "StatementBusinessSegmentsAxis", "ProductOrServiceAxis", "StatementGeographicalAxis",
)
_SKIP_AXES = frozenset({"ConsolidationItemsAxis", "ConsolidatedEntitiesAxis"})
_SKIP_MEMBER = re.compile(r"total|consolidat|eliminat|reconcil", re.I)
_REVENUE_SET = frozenset(_REVENUE_TAGS)

# One parsed instance: (concept, start, end, axis, member, value)
_Fact = tuple[str, str, str, str, str, float]
_instance_cache: dict[str, list[_Fact]] = {}
_instance_lock = threading.Lock()


def _find_instance_name(base: str) -> str:
    index = sec_client.sec_get(base + "index.json")
    names = [i.get("name", "") for i in index.get("directory", {}).get("item", [])]
    for pattern in (r"_htm\.xml$", r"^[A-Za-z0-9]+-\d{8}\.xml$"):
        for n in names:
            if re.search(pattern, n):
                return n
    raise SankeyUnavailable("The filing has no XBRL instance document to read segments from.")


def _parse_instance(cik: str, accn: str) -> list[_Fact]:
    with _instance_lock:
        if accn in _instance_cache:
            return _instance_cache[accn]

    base = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/"
    raw = sec_client.sec_get(base + _find_instance_name(base), raw=True)
    root = ET.fromstring(raw)

    contexts: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {}
    for c in root.iter(_XBRLI + "context"):
        period = c.find(_XBRLI + "period")
        if period is None or c.find(f".//{_XBRLDI}typedMember") is not None:
            continue
        s, e = period.find(_XBRLI + "startDate"), period.find(_XBRLI + "endDate")
        if s is None or e is None or not s.text or not e.text:
            continue
        dims = tuple(
            (m.get("dimension", "").split(":")[-1], (m.text or "").strip().split(":")[-1])
            for m in c.iter(_XBRLDI + "explicitMember")
        )
        contexts[c.get("id", "")] = (s.text.strip(), e.text.strip(), dims)

    facts: list[_Fact] = []
    for el in root:
        if not isinstance(el.tag, str):
            continue
        local = el.tag.rsplit("}", 1)[-1]
        if local not in _REVENUE_SET or "us-gaap" not in el.tag:
            continue
        ctx = contexts.get(el.get("contextRef", ""))
        if ctx is None or len(ctx[2]) != 1 or not el.text:
            continue
        try:
            value = float(el.text)
        except ValueError:
            continue
        facts.append((local, ctx[0], ctx[1], ctx[2][0][0], ctx[2][0][1], value))
    del root

    with _instance_lock:
        _instance_cache[accn] = facts
        while len(_instance_cache) > INSTANCE_CACHE_MAX:
            _instance_cache.pop(next(iter(_instance_cache)))
    return facts


def _axis_groups(cik: str, accn: str, start: str, end: str) -> dict[str, dict[str, float]]:
    """{axis: {member: revenue}} for single-dimension revenue facts in the exact window."""
    groups: dict[str, dict[str, float]] = {}
    facts = [f for f in _parse_instance(cik, accn) if f[1] == start and f[2] == end]
    for concept in _REVENUE_TAGS:                     # priority order, first value wins
        for c, _, _, axis, member, value in facts:
            if c == concept and axis not in _SKIP_AXES and not _SKIP_MEMBER.search(member):
                groups.setdefault(axis, {}).setdefault(member, value)
    return groups


_LABEL_FIXES = (
    ("Linked In", "LinkedIn"), ("Homeand", "Home and"), ("Three Six Five", "365"),
    (" And ", " & "),
)
_LABEL_ALIASES = {"Product": "Products", "Service": "Services"}


def _member_label(member: str) -> str:
    m = re.sub(r"(Segment)?Member$", "", member)
    m = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", m)
    m = re.sub(r"(?<=[A-Z]{2})(?=[A-Z][a-z])", " ", m)     # keeps IPhone / IPad intact
    for old, new in _LABEL_FIXES:
        m = m.replace(old, new)
    m = re.sub(r" Revenues?$", "", m) or m
    m = _LABEL_ALIASES.get(m, m)
    if len(m) <= 34:
        return m
    cut = m[:33]
    return (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"


def _member_key(member: str) -> str:
    """Normalised member name, so a renamed line (Advertising -> AdvertisingServices) still matches."""
    k = re.sub(r"Member$", "", member).lower()
    return re.sub(r"(services?|revenues?|sales)$", "", k)


def _subtract(full: dict[str, float], ytd: dict[str, float]) -> dict[str, float]:
    by_key: dict[str, list[str]] = {}
    for m in ytd:
        by_key.setdefault(_member_key(m), []).append(m)
    out: dict[str, float] = {}
    for m, v in full.items():
        if m in ytd:
            out[m] = v - ytd[m]
        else:
            alt = by_key.get(_member_key(m), [])
            if len(alt) == 1:
                out[m] = v - ytd[alt[0]]
    return out


def _groups_for(cik: str, w: _Window) -> dict[str, dict[str, float]]:
    """{axis: {member: revenue}} for the resolved window (Q4 = FY - Q3 year-to-date)."""
    if w.ytd_end is None:
        return _axis_groups(cik, w.accn, w.start, w.end)
    full = _axis_groups(cik, w.accn, w.fy_start or "", w.end)
    ytd = _axis_groups(cik, w.ytd_accn or "", w.fy_start or "", w.ytd_end)
    return {axis: _subtract(members, ytd.get(axis, {})) for axis, members in full.items()}


# ── Revenue categories (left column of the income statement chart) ─────────────────────

_CATEGORY_AXES = ("ProductOrServiceAxis", "StatementBusinessSegmentsAxis")
MAX_CATEGORIES = 8
_PARTITION_MAX_MEMBERS = 14      # 2**14 subsets at most
_PARTITION_TOL = 0.002
_PARTITION_EXACT = 0.0005


def _partition(members: dict[str, float], rev: float) -> dict[str, float] | None:
    """
    Companies tag overlapping lines (e.g. Product, Service AND each product line).
    Find the non-overlapping subset that adds up to revenue; prefer exact sums,
    then the finest breakdown (most members).
    """
    items = sorted(
        ((m, v) for m, v in members.items() if v > rev * 0.001), key=lambda kv: -kv[1]
    )[:_PARTITION_MAX_MEMBERS]
    n = len(items)
    if n < 2:
        return None

    sums = [0.0] * (1 << n)
    counts = [0] * (1 << n)
    cands: list[tuple[int, float, int]] = []          # (members, error, mask)
    for mask in range(1, 1 << n):
        low = mask & -mask
        rest = mask ^ low
        sums[mask] = sums[rest] + items[low.bit_length() - 1][1]
        counts[mask] = counts[rest] + 1
        err = abs(sums[mask] - rev) / rev
        if counts[mask] >= 2 and err <= _PARTITION_TOL:
            cands.append((counts[mask], err, mask))
    if not cands:
        return None

    exact = [c for c in cands if c[1] <= _PARTITION_EXACT]
    pool = exact or cands
    _, _, mask = max(pool, key=lambda c: (c[0], -c[1]))
    return {items[i][0]: items[i][1] for i in range(n) if mask >> i & 1}


def _revenue_categories(cik: str, w: _Window, rev: float) -> list[tuple[str, float, str]]:
    """[(label, value, colour)] for the left column; raises SankeyUnavailable if none fit."""
    groups = _groups_for(cik, w)
    chosen: dict[str, float] | None = None
    for axis in _CATEGORY_AXES:
        if axis in groups:
            chosen = _partition(groups[axis], rev)
            if chosen:
                break
    if not chosen:
        raise SankeyUnavailable(f"No revenue categories that add up to revenue for {w.label}.")

    ordered = sorted(chosen.items(), key=lambda kv: -kv[1])
    if len(ordered) > MAX_CATEGORIES:
        head, tail = ordered[: MAX_CATEGORIES - 1], ordered[MAX_CATEGORIES - 1:]
        ordered = head + [("Other", sum(v for _, v in tail))]

    out: list[tuple[str, float, str]] = []
    for i, (member, value) in enumerate(ordered):
        label = member if member == "Other" else _member_label(member)
        out.append((label, value, SEGMENT_PALETTE[i % len(SEGMENT_PALETTE)]))
    rest = rev - sum(v for _, v, _ in out)
    if rest > rev * 0.005:
        out.append(("Other / unallocated", rest, C_OTHER))
    return out


def _choose_axis(groups: dict[str, dict[str, float]], rev: float) -> tuple[str, dict[str, float]] | None:
    ordered = [a for a in _AXIS_PREFERENCE if a in groups] + sorted(
        a for a in groups if a not in _AXIS_PREFERENCE
    )
    for axis in ordered:
        members = {m: v for m, v in groups[axis].items() if v > EPS}
        total = sum(members.values())
        if len(members) >= 2 and 0.5 * rev <= total <= 1.03 * rev:
            return axis, members
    return None


def build_segments(ticker: str, year: int, period: str) -> Sankey:
    cik, name = sec_client.resolve_ticker(ticker)
    us = _load_us(cik)
    w = _resolve(us, year, period)
    rev = _Reader(us, w).get(w.concept)
    if rev is None or rev <= EPS:
        raise SankeyUnavailable(f"Revenue is not reported for {w.label}.")

    groups = _groups_for(cik, w)

    chosen = _choose_axis(groups, rev)
    if chosen is None:
        raise SankeyUnavailable(
            f"No revenue-by-segment breakdown that adds up to total revenue was found for {w.label}."
        )
    axis, members = chosen

    b = _Builder()
    b.node("Total revenue", 1, C_REV, 0)
    ordered = sorted(members.items(), key=lambda kv: -kv[1])
    for i, (member, value) in enumerate(ordered):
        color = SEGMENT_PALETTE[i % len(SEGMENT_PALETTE)]
        label = _member_label(member)
        b.node(label, 0, color, i)
        b.link(label, "Total revenue", value, color)
    rest = rev - sum(members.values())
    if rest > rev * 0.01:
        b.node("Other / unallocated", 0, C_OTHER, len(ordered))
        b.link("Other / unallocated", "Total revenue", rest, C_OTHER)

    axis_label = re.sub(r"Axis$", "", axis)
    axis_label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", axis_label)
    return Sankey(
        title=f"{_display_name(name, ticker)} — Revenue by segment, {w.label}",
        subtitle=f"Total revenue {fmt_money(rev)}  ·  grouped by {axis_label.lower()}",
        nodes=list(b.nodes.values()),
        links=b.links,
        base_value=rev,
    )


# ── Public entry point ─────────────────────────────────────────────────────────

def build(ticker: str, year: int, period: str, mode: str, categories: bool = True) -> Sankey:
    if period not in PERIODS:
        raise ValueError(f"period must be one of {', '.join(PERIODS)}.")
    if mode == "income":
        return build_income_statement(ticker, year, period, categories)
    if mode == "segments":
        return build_segments(ticker, year, period)
    raise ValueError(f"mode must be one of {', '.join(MODES)}.")
