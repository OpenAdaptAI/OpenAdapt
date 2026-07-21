"""Renderers for the shared :class:`ProgramGraphSpec`.

- :func:`render_html` -> a SELF-CONTAINED HTML page (no external assets, no
  network): the shared CSS + JS (``static/program_graph.{css,js}``) are inlined
  and the spec is embedded as JSON, so it opens offline and renders under a
  strict CSP. This is the SAME CSS/JS the desktop (Tauri) view vendors and the
  same spec the cloud React view consumes -- one renderer, three surfaces.
- :func:`render_mermaid` -> a Mermaid ``flowchart`` source string, for pasting
  into Markdown / docs / PR descriptions that render Mermaid natively.

Rendering-approach note (documented for the PR): the engine emits the spec and
the surfaces render it (rather than each surface re-parsing the bundle). The
default CLI rendering is a lightweight CUSTOM layout (inline CSS + a ~250-line
vanilla-JS builder, no graph library) because (a) it must be self-contained and
CSP-safe, (b) the compiled program is a vertical sequence with room for
branches -- a full graph lib (d3/cytoscape/reactflow) is heavy overkill, and
(c) rich per-node annotations (resolution ladder, identity gate, effect check,
halt points) are far clearer as node CARDS than as Mermaid labels. Mermaid is
offered as a portable secondary format; JSON is offered for tooling.
"""

from __future__ import annotations

import html
from pathlib import Path

from openadapt_flow.visualize.spec import ProgramGraphSpec

_STATIC = Path(__file__).parent / "static"


def _asset(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def render_html(spec: ProgramGraphSpec, *, title: str | None = None) -> str:
    """Render ``spec`` to a self-contained HTML document string."""
    css = _asset("program_graph.css")
    js = _asset("program_graph.js")
    page_title = title or f"Compiled program — {spec.bundle.name}"
    # Embed the spec as JSON in a <script type="application/json"> block. Escape
    # ``</`` so the payload can never terminate the script element early.
    spec_json = spec.model_dump_json()
    spec_json = spec_json.replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page_title)}</title>
<style>
body {{ margin: 0; padding: 24px; max-width: 900px; margin-inline: auto;
       background: Canvas; color: CanvasText; }}
{css}
</style>
</head>
<body>
<div id="program-graph"></div>
<script type="application/json" id="program-graph-spec">{spec_json}</script>
<script>
{js}
</script>
<script>
(function () {{
  var raw = document.getElementById("program-graph-spec").textContent;
  var spec = JSON.parse(raw);
  OpenAdaptProgramGraph.render(spec, document.getElementById("program-graph"));
}})();
</script>
</body>
</html>
"""


_MERMAID_ESCAPE = str.maketrans({'"': "'", "\n": " ", "[": "(", "]": ")"})


def _mm(text: str, limit: int = 46) -> str:
    text = (text or "").translate(_MERMAID_ESCAPE).strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def render_mermaid(spec: ProgramGraphSpec) -> str:
    """Render ``spec`` to a Mermaid ``flowchart TD`` source string.

    Node shape encodes kind (rounded = action, diamond = branch/terminal), and
    ``classDef`` styling marks irreversible steps and halt points; edges carry
    guard/branch labels. This is a compact secondary view -- the HTML render
    carries the full per-node annotations.
    """
    lines: list[str] = ["flowchart TD"]
    irrev: list[str] = []
    halt: list[str] = []
    id_map: dict[str, str] = {}
    for i, node in enumerate(spec.nodes):
        nid = f"n{i}"
        id_map[node.id] = nid
        label = _mm(node.title)
        if node.kind.value == "action":
            badges = []
            if node.identity and node.identity.armed is True:
                badges.append("identity")
            if node.effects:
                badges.append("effect")
            if node.risk == "irreversible":
                badges.append("irreversible")
                irrev.append(nid)
            suffix = (
                f"<br/><small>{_mm(' · '.join(badges), 40)}</small>" if badges else ""
            )
            lines.append(f'  {nid}("{label}{suffix}")')
        elif node.kind.value == "terminal":
            lines.append(f'  {nid}{{{{"{label}"}}}}')
            if node.outcome in ("halt", "escalate"):
                halt.append(nid)
        else:
            lines.append(f'  {nid}{{"{label}"}}')
        if node.halts:
            halt.append(nid)
    for edge in spec.edges:
        src = id_map.get(edge.source)
        tgt = id_map.get(edge.target)
        if not src or not tgt:
            continue
        label = _mm(edge.label, 30)
        if label:
            lines.append(f"  {src} -->|{label}| {tgt}")
        else:
            lines.append(f"  {src} --> {tgt}")
    lines.append("  classDef irreversible stroke:#b4530a,stroke-width:2px;")
    lines.append("  classDef halt stroke:#b21f2d,stroke-width:2px;")
    if irrev:
        lines.append(f"  class {','.join(sorted(set(irrev)))} irreversible;")
    if halt:
        lines.append(f"  class {','.join(sorted(set(halt)))} halt;")
    return "\n".join(lines)
