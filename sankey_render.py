"""
sankey_render.py — Render a Sankey model (see sankey_data.py) as a standalone SVG.

Pure Python, no third-party dependencies and no headless browser.
"""
from __future__ import annotations

from xml.sax.saxutils import escape

from sankey_data import Sankey, fmt_money

WIDTH = 1280
HEIGHT = 720
MARGIN_LEFT = 250
MARGIN_RIGHT = 210
TEXT_X = 30
PLOT_TOP = 112
PLOT_BOTTOM = HEIGHT - 44
NODE_W = 16
MIN_NODE_H = 2.0
NODE_GAP = 36            # room for a two-line label between neighbouring nodes
LINK_OPACITY = 0.42
RELAX_ITERATIONS = 10
FONT = "'Segoe UI', Roboto, Helvetica, Arial, sans-serif"


class _N:
    __slots__ = ("name", "col", "color", "rank", "value", "h", "x", "y", "ins", "outs")

    def __init__(self, name: str, col: int, color: str, rank: float) -> None:
        self.name, self.col, self.color, self.rank = name, col, color, rank
        self.value = 0.0
        self.h = 0.0
        self.x = 0.0
        self.y = 0.0
        self.ins: list[_L] = []
        self.outs: list[_L] = []

    @property
    def cy(self) -> float:
        return self.y + self.h / 2


class _L:
    __slots__ = ("src", "dst", "value", "color", "follow", "w", "y0", "y1")

    def __init__(self, src: _N, dst: _N, value: float, color: str, follow: str) -> None:
        self.src, self.dst, self.value, self.color, self.follow = src, dst, value, color, follow
        self.w = 0.0
        self.y0 = 0.0
        self.y1 = 0.0


def _layout(model: Sankey) -> tuple[list[_N], list[_L]]:
    nodes = {n.name: _N(n.name, n.col, n.color, n.rank) for n in model.nodes}
    links: list[_L] = []
    for lk in model.links:
        s, d = nodes[lk.source], nodes[lk.target]
        # An explicit link colour is the source node's colour; otherwise it follows the target.
        link = _L(s, d, lk.value, lk.color or d.color, "s" if lk.color else "t")
        s.outs.append(link)
        d.ins.append(link)
        links.append(link)

    used = [n for n in nodes.values() if n.ins or n.outs]
    for n in used:
        n.value = max(sum(l.value for l in n.ins), sum(l.value for l in n.outs))

    # Compact column numbers (drops empty columns).
    cols_sorted = sorted({n.col for n in used})
    remap = {c: i for i, c in enumerate(cols_sorted)}
    for n in used:
        n.col = remap[n.col]
    columns: list[list[_N]] = [[] for _ in cols_sorted]
    for n in sorted(used, key=lambda n: (n.rank, n.name)):
        columns[n.col].append(n)

    plot_h = PLOT_BOTTOM - PLOT_TOP
    scale = min(
        (plot_h - NODE_GAP * (len(c) - 1)) / sum(n.value for n in c) for c in columns
    )
    # Tiny nodes are floored at MIN_NODE_H; shrink the scale until every column still fits.
    for _ in range(12):
        for n in used:
            n.h = max(n.value * scale, MIN_NODE_H)
        over = max((sum(n.h for n in c) + NODE_GAP * (len(c) - 1)) / plot_h for c in columns)
        if over <= 1.0:
            break
        scale *= 0.995 / over

    span = WIDTH - MARGIN_LEFT - MARGIN_RIGHT - NODE_W
    step = span / max(len(columns) - 1, 1)
    for c, col in enumerate(columns):
        total = sum(n.h for n in col) + NODE_GAP * (len(col) - 1)
        y = PLOT_TOP + (plot_h - total) / 2
        for n in col:
            n.x = MARGIN_LEFT + c * step
            n.y = y
            y += n.h + NODE_GAP

    def resolve(col: list[_N]) -> None:
        for i, n in enumerate(col):
            floor = PLOT_TOP if i == 0 else col[i - 1].y + col[i - 1].h + NODE_GAP
            n.y = max(n.y, floor)
        overflow = col[-1].y + col[-1].h - PLOT_BOTTOM
        if overflow > 0:
            col[-1].y -= overflow
            for i in range(len(col) - 2, -1, -1):
                col[i].y = min(col[i].y, col[i + 1].y - NODE_GAP - col[i].h)
        for i, n in enumerate(col):                     # top clamp after the upward shift
            floor = PLOT_TOP if i == 0 else col[i - 1].y + col[i - 1].h + NODE_GAP
            n.y = max(n.y, floor)

    def pull(n: _N, ends: list[_L], attr: str) -> None:
        total = sum(l.value for l in ends)
        if total > 0:
            n.y = sum(getattr(l, attr).cy * l.value for l in ends) / total - n.h / 2

    for _ in range(RELAX_ITERATIONS):
        for col in columns[1:]:
            for n in col:
                pull(n, n.ins, "src")
            resolve(col)
        for col in reversed(columns[:-1]):
            for n in col:
                pull(n, n.outs, "dst")
            resolve(col)

    for n in used:
        out_scale = n.h / n.value if n.value else 0.0
        y = n.y
        for l in sorted(n.outs, key=lambda l: (l.dst.cy, l.dst.rank)):
            l.w = l.value * out_scale
            l.y0 = y
            y += l.w
        y = n.y
        for l in sorted(n.ins, key=lambda l: (l.src.cy, l.src.rank)):
            l.y1 = y
            y += l.value * out_scale
    return used, links


def _ribbon(l: _L) -> str:
    x0 = l.src.x + NODE_W
    x1 = l.dst.x
    xm = (x0 + x1) / 2
    w = l.w
    return (
        f"M{x0:.1f},{l.y0:.1f} C{xm:.1f},{l.y0:.1f} {xm:.1f},{l.y1:.1f} {x1:.1f},{l.y1:.1f} "
        f"L{x1:.1f},{l.y1 + w:.1f} C{xm:.1f},{l.y1 + w:.1f} {xm:.1f},{l.y0 + w:.1f} {x0:.1f},{l.y0 + w:.1f} Z"
    )


def render_svg(model: Sankey) -> str:
    nodes, links = _layout(model)
    last_col = max(n.col for n in nodes)
    base = model.base_value
    index = {n.name: i for i, n in enumerate(nodes)}
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}" '
        f'viewBox="0 0 {WIDTH} {HEIGHT}" font-family="{escape(FONT)}">',
        f'<rect width="{WIDTH}" height="{HEIGHT}" fill="#ffffff"/>',
        f'<text x="{TEXT_X}" y="42" font-size="24" font-weight="700" fill="#0f172a">'
        f"{escape(model.title)}</text>",
        f'<text x="{TEXT_X}" y="68" font-size="15" fill="#475569">'
        f"{escape(model.subtitle)}</text>",
    ]

    parts.append(f'<g fill-opacity="{LINK_OPACITY}">')
    for l in links:
        tip = f"{l.src.name} → {l.dst.name}: {fmt_money(l.value)}"
        parts.append(
            f'<path class="sk-link" d="{_ribbon(l)}" fill="{escape(l.color)}" '
            f'data-s="{index[l.src.name]}" data-t="{index[l.dst.name]}" data-f="{l.follow}" '
            f'data-y0="{l.y0:.1f}" data-y1="{l.y1:.1f}" data-w="{l.w:.1f}">'
            f"<title>{escape(tip)}</title></path>"
        )
    parts.append("</g>")

    for n in nodes:
        parts.append(
            f'<rect class="sk-node" data-i="{index[n.name]}" data-n="{escape(n.name, {chr(34): "&quot;"})}" '
            f'data-x="{n.x:.1f}" data-y="{n.y:.1f}" '
            f'data-h="{n.h:.1f}" x="{n.x:.1f}" y="{n.y:.1f}" width="{NODE_W}" height="{n.h:.1f}" '
            f'fill="{escape(n.color)}" rx="2" stroke="transparent" stroke-width="12" '
            f'style="cursor:grab;touch-action:none"><title>{escape(n.name)}: {fmt_money(n.value)}</title></rect>'
        )

    halo = 'stroke="#ffffff" stroke-width="4" stroke-linejoin="round" paint-order="stroke"'
    for n in nodes:
        pct = f" · {n.value / base * 100:.0f}%" if base > 0 and n.value / base >= 0.005 else ""
        value_line = f"{fmt_money(n.value)}{pct}"
        if n.col == 0 and last_col > 0:
            x, anchor = n.x - 8, "end"
        else:
            x, anchor = n.x + NODE_W + 8, "start"
        cy = n.cy
        parts.append(
            f'<g class="sk-lbl" data-i="{index[n.name]}" pointer-events="none">'
            f'<text x="{x:.1f}" y="{cy - 2:.1f}" text-anchor="{anchor}" font-size="14" '
            f'font-weight="600" fill="#0f172a" {halo}>{escape(n.name)}</text>'
            f'<text x="{x:.1f}" y="{cy + 14:.1f}" text-anchor="{anchor}" font-size="12.5" '
            f'fill="#475569" {halo}>{escape(value_line)}</text></g>'
        )

    parts.append(
        f'<text x="{TEXT_X}" y="{HEIGHT - 14}" font-size="11.5" fill="#94a3b8">'
        "Source: SEC EDGAR XBRL filings (10-K / 10-Q). Percentages are of revenue. "
        "Amounts are as reported, in US dollars.</text>"
    )
    parts.append("</svg>")
    return "\n".join(parts)
