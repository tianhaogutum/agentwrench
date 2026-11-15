"""Render the `agentwrench slim` benchmark as before/after bar charts (light and dark).

Reads benchmarks/mcp-servers/results.json (tiktoken o200k_base counts) and writes
assets/slim-benchmark-light.svg and assets/slim-benchmark-dark.svg. Re-run after
the benchmark is re-run:

    uv run python assets/render_benchmark.py      # from the repository root
"""

from __future__ import annotations

import json
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "benchmarks" / "mcp-servers" / "results.json"
TOKENIZER = "o200k_base"

# (results.json id or "stack", label). Order: the combined stack, then by size.
ROWS = [
    ("stack", "Typical stack*"),
    ("github", "GitHub"),
    ("chrome-devtools", "Chrome DevTools"),
    ("playwright", "Playwright"),
    ("filesystem", "Filesystem"),
    ("context7", "Context7"),
    ("notion", "Notion"),
]
SERIES = [("before", "Before"), ("strip", "Strip-only"), ("plan50", "slim plan @50%")]

THEMES = {
    "light": {"surface": "#ffffff", "border": "#d1d9e0", "text": "#1f2328", "muted": "#59636e",
              "grid": "#e6e9ec", "series": ["#7d7c77", "#86b6ef", "#1c5cab"]},
    "dark": {"surface": "#151b23", "border": "#3d444d", "text": "#f0f3f6", "muted": "#9198a1",
             "grid": "#262c36", "series": ["#8a8983", "#1c5cab", "#6da7ec"]},
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif"


def load() -> list[dict]:
    data = json.loads(RESULTS.read_text(encoding="utf-8"))
    servers = {s["id"]: s for s in data["servers"]}
    rows = []
    for key, label in ROWS:
        if key == "stack":
            entry = data["stack"]
            tools = entry["before"]["tools"]
            values = {"before": entry["before"][TOKENIZER]}
            saved = {}
            for variant in ("strip", "plan50"):
                values[variant] = entry[variant][TOKENIZER]
                saved[variant] = entry[variant][f"savedPercent_{TOKENIZER}"]
        else:
            entry = servers[key]
            tools = entry["tools"]
            values = {"before": entry["before"][TOKENIZER]}
            saved = {}
            for variant in ("strip", "plan50"):
                values[variant] = entry["variants"][variant][TOKENIZER]
                saved[variant] = entry["variants"][variant][f"savedPercent_{TOKENIZER}"]
        rows.append({"label": label, "tools": tools, "values": values, "saved": saved})
    return rows, data


def bar(x: float, y: float, w: float, h: float, r: float, fill: str) -> str:
    """Bar anchored square at the baseline (left), rounded at the data end (right)."""
    r = min(r, w, h / 2)
    return (f'<path d="M{x:.1f} {y:.1f} H{x + w - r:.1f} a{r} {r} 0 0 1 {r} {r} V{y + h - r:.1f} '
            f'a{r} {r} 0 0 1 -{r} {r} H{x:.1f} Z" fill="{fill}"/>')


def render(theme: dict, rows: list[dict], data: dict) -> str:
    width, left, plot_w = 860, 196, 520
    bar_h, bar_gap, group_gap, top = 12, 3, 20, 92
    group_h = len(SERIES) * bar_h + (len(SERIES) - 1) * bar_gap
    height = top + len(rows) * (group_h + group_gap) + 88
    vmax = max(r["values"]["before"] for r in rows)
    scale = plot_w / vmax
    t = theme
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">',
        "<title id=\"t\">MCP tool-definition tokens per turn, before and after agentwrench slim</title>",
        "<desc id=\"d\">Bar chart from benchmarks/mcp-servers/results.json, tiktoken o200k_base. "
        + escape("; ".join(
            f"{r['label'].rstrip('*')}: {r['values']['before']:,} before, {r['values']['strip']:,} strip-only, "
            f"{r['values']['plan50']:,} with slim plan" for r in rows))
        + ". No tool is removed in any column.</desc>",
        f"<style>text{{font-family:{FONT};fill:{t['text']}}}.m{{fill:{t['muted']}}}"
        ".h{font-size:17px;font-weight:600}.s{font-size:12.5px}.l{font-size:13.5px;font-weight:600}"
        ".v{font-size:12px;font-variant-numeric:tabular-nums}</style>",
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="10" '
        f'fill="{t["surface"]}" stroke="{t["border"]}"/>',
        '<text class="h" x="24" y="36">Tokens your MCP tools cost on every turn</text>',
        f'<text class="s m" x="24" y="57">Tool definitions (name, description, input schema) counted with '
        f'tiktoken {TOKENIZER}. No tool is removed in any column.</text>',
    ]
    # Legend.
    lx = 24
    for i, (_, name) in enumerate(SERIES):
        out.append(f'<rect x="{lx}" y="68" width="12" height="12" rx="2" fill="{t["series"][i]}"/>')
        out.append(f'<text class="s" x="{lx + 18}" y="78.5">{escape(name)}</text>')
        lx += 18 + len(name) * 7.2 + 22
    # Gridlines at round values.
    step = 5000
    for v in range(0, vmax + 1, step):
        x = left + v * scale
        out.append(f'<line x1="{x:.1f}" y1="{top - 6}" x2="{x:.1f}" '
                   f'y2="{top + len(rows) * (group_h + group_gap) - group_gap + 6}" stroke="{t["grid"]}"/>')
        out.append(f'<text class="v m" x="{x:.1f}" y="{top + len(rows) * (group_h + group_gap) - group_gap + 22}" '
                   f'text-anchor="middle">{v // 1000}k</text>')
    for n, row in enumerate(rows):
        y0 = top + n * (group_h + group_gap)
        out.append(f'<text class="l" x="24" y="{y0 + group_h / 2 - 2}">{escape(row["label"])}</text>')
        out.append(f'<text class="s m" x="24" y="{y0 + group_h / 2 + 14}">{row["tools"]} tools</text>')
        for i, (key, _) in enumerate(SERIES):
            y = y0 + i * (bar_h + bar_gap)
            value = row["values"][key]
            w = max(value * scale, 2)
            out.append(bar(left, y, w, bar_h, 4, t["series"][i]))
            label = f"{value:,}"
            if key != "before":
                label += f" (−{row['saved'][key]:.0f}%)"
            out.append(f'<text class="v" x="{left + w + 6:.1f}" y="{y + bar_h - 2}">{label}</text>')
    stack = " + ".join(s.replace("-", " ").title() if s != "github" else "GitHub" for s in data["stack"]["servers"])
    foot = top + len(rows) * (group_h + group_gap) - group_gap + 44
    notes = [
        f"* {stack}, {data['stack']['before']['tools']} tools together.",
        "Strip-only keeps tool descriptions verbatim and drops per-parameter help text; plan also caps "
        "description length.",
        f"Source: benchmarks/mcp-servers (agentwrench {data['agentwrench']}, {data['date']}). "
        "Notion barely shrinks: its schemas are structure, not prose.",
    ]
    for i, note in enumerate(notes):
        out.append(f'<text class="s m" x="24" y="{foot + i * 18}">{escape(note)}</text>')
    out.append("</svg>")
    return "\n".join(out) + "\n"


def main() -> None:
    rows, data = load()
    for name, theme in THEMES.items():
        path = ROOT / "assets" / f"slim-benchmark-{name}.svg"
        svg = render(theme, rows, data)
        path.write_text(svg, encoding="utf-8")
        print(f"wrote {path} ({len(svg.encode()):,} bytes)")


if __name__ == "__main__":
    main()
