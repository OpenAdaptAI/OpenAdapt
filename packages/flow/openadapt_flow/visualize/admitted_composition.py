"""Visualizer for a ProcessContract parent. Own spec, not a ProgramGraph.

When ``openadapt-flow visualize`` is pointed at a process-contract directory,
emit HTML / Mermaid / JSON of the PARENT: one node per admitted child, DAG
or list-order edges, handoff edges labelled with effect-bound param names,
and a terminal titled "End of declared steps" (never Success).

Do not merge two capabilities into one ProgramGraph. The compose visualizer
is a different module and a different agent.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.admitted_composition import (
    ProcessContract,
    ProcessContractError,
    is_process_contract_artifact,
    topological_order,
)

PROCESS_GRAPH_SPEC_VERSION: Literal["openadapt.process-contract-graph/v0"] = (
    "openadapt.process-contract-graph/v0"
)
END_OF_DECLARED_STEPS: Literal["End of declared steps"] = "End of declared steps"
TERMINAL_ID = "end_declared_steps"


class ProcessNode(BaseModel):
    """One admitted child, or the parent terminal."""

    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["admitted_capability", "terminal"]
    title: str
    admission_id: Optional[str] = None
    admission_id_short: Optional[str] = None
    digest: Optional[str] = None
    digest_short: Optional[str] = None
    surface: Optional[str] = None


class ProcessEdge(BaseModel):
    """DAG / list-order edge, or a handoff of an effect-bound param."""

    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    kind: Literal["order", "handoff"]
    label: str = ""


class ProcessContractSpec(BaseModel):
    """Parent-only graph. Not program-graph-v1."""

    model_config = ConfigDict(extra="forbid")

    spec_version: Literal["openadapt.process-contract-graph/v0"] = (
        PROCESS_GRAPH_SPEC_VERSION
    )
    name: str
    terminal_title: Literal["End of declared steps"] = END_OF_DECLARED_STEPS
    nodes: list[ProcessNode] = Field(default_factory=list)
    edges: list[ProcessEdge] = Field(default_factory=list)


def _short(value: str, n: int = 8) -> str:
    if len(value) <= n:
        return value
    return value[:n]


def build_process_graph(parent_dir: Path | str) -> ProcessContractSpec:
    """Project a process-contract directory onto the parent graph spec."""

    path = Path(parent_dir)
    if not is_process_contract_artifact(path):
        raise ProcessContractError(
            f"{path} is not a process-contract artifact (no process-contract.json)"
        )
    contract = ProcessContract.load(path)
    order = topological_order(contract)
    nodes = [
        ProcessNode(
            id=spec.name,
            kind="admitted_capability",
            title=spec.name,
            admission_id=spec.admission_id,
            admission_id_short=_short(spec.admission_id),
            digest=spec.bundle_content_digest,
            digest_short=_short(spec.bundle_content_digest),
            surface=spec.surface,
        )
        for spec in (contract.child(name) for name in order)
    ]
    nodes.append(
        ProcessNode(
            id=TERMINAL_ID,
            kind="terminal",
            title=END_OF_DECLARED_STEPS,
        )
    )
    edges: list[ProcessEdge] = []
    for index, name in enumerate(order):
        nxt = order[index + 1] if index + 1 < len(order) else TERMINAL_ID
        edges.append(ProcessEdge(source=name, target=nxt, kind="order", label=""))
    for handoff in contract.handoffs:
        edges.append(
            ProcessEdge(
                source=handoff.from_child,
                target=handoff.to_child,
                kind="handoff",
                label=handoff.source,
            )
        )
    return ProcessContractSpec(name=contract.name, nodes=nodes, edges=edges)


def render_process_mermaid(spec: ProcessContractSpec) -> str:
    """Render the parent as a Mermaid flowchart."""

    lines = ["flowchart TD"]
    for node in spec.nodes:
        nid = _mm_id(node.id)
        if node.kind == "terminal":
            lines.append(f'  {nid}["{END_OF_DECLARED_STEPS}"]')
            continue
        bits = [node.title]
        if node.admission_id_short:
            bits.append(f"adm {node.admission_id_short}")
        if node.digest_short:
            bits.append(f"digest {node.digest_short}")
        if node.surface:
            bits.append(node.surface)
        label = "<br/>".join(_mm(bit) for bit in bits)
        lines.append(f'  {nid}["{label}"]')
    for edge in spec.edges:
        src = _mm_id(edge.source)
        dst = _mm_id(edge.target)
        if edge.kind == "handoff":
            label = _mm(edge.label or "handoff")
            lines.append(f"  {src} -.->|{label}| {dst}")
        else:
            lines.append(f"  {src} --> {dst}")
    lines.append("  classDef admitted fill:#e8f0fe,stroke:#3b6ea5,color:#111;")
    lines.append("  classDef terminal fill:#f3f4f6,stroke:#6b7280,color:#111;")
    admitted = [_mm_id(n.id) for n in spec.nodes if n.kind == "admitted_capability"]
    if admitted:
        lines.append("  class " + ",".join(admitted) + " admitted;")
    lines.append(f"  class {_mm_id(TERMINAL_ID)} terminal;")
    return "\n".join(lines)


def render_process_html(spec: ProcessContractSpec, *, title: str | None = None) -> str:
    """Self-contained HTML of the parent process graph. No network, no ProgramGraph JS."""

    page_title = title or f"Process contract: {spec.name}"
    mermaid = render_process_mermaid(spec)
    spec_json = spec.model_dump_json(indent=2).replace("</", "<\\/")
    cards = []
    for node in spec.nodes:
        if node.kind == "terminal":
            cards.append(
                f'<article class="node terminal"><h2>{html.escape(node.title)}</h2></article>'
            )
            continue
        meta = []
        if node.admission_id_short:
            meta.append(f"<li>admission {html.escape(node.admission_id_short)}</li>")
        if node.digest_short:
            meta.append(f"<li>digest {html.escape(node.digest_short)}</li>")
        if node.surface:
            meta.append(f"<li>surface {html.escape(node.surface)}</li>")
        cards.append(
            '<article class="node admitted">'
            f"<h2>{html.escape(node.title)}</h2>"
            f"<ul>{''.join(meta)}</ul>"
            "</article>"
        )
    handoff_items = []
    for edge in spec.edges:
        if edge.kind != "handoff":
            continue
        handoff_items.append(
            "<li>"
            f"{html.escape(edge.source)}.{html.escape(edge.label)} → "
            f"{html.escape(edge.target)}"
            "</li>"
        )
    handoffs_html = (
        "<ul>" + "".join(handoff_items) + "</ul>" if handoff_items else "<p>None.</p>"
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(page_title)}</title>
<style>
body {{ margin: 0; padding: 24px; max-width: 960px; margin-inline: auto;
       font-family: ui-sans-serif, system-ui, sans-serif; background: Canvas;
       color: CanvasText; }}
h1 {{ font-size: 1.4rem; margin-bottom: 0.25rem; }}
.sub {{ color: #4b5563; margin-top: 0; }}
.row {{ display: flex; flex-wrap: wrap; gap: 12px; align-items: stretch; }}
.node {{ border: 1px solid #3b6ea5; border-radius: 10px; padding: 12px 16px;
        min-width: 160px; background: #e8f0fe; }}
.node.terminal {{ background: #f3f4f6; border-color: #6b7280; }}
.node h2 {{ margin: 0 0 8px; font-size: 1rem; }}
.node ul {{ margin: 0; padding-left: 1.1rem; }}
pre {{ overflow: auto; background: #111827; color: #f9fafb; padding: 12px;
      border-radius: 8px; }}
</style>
</head>
<body>
<h1>{html.escape(spec.name)}</h1>
<p class="sub">Process contract of independently admitted capabilities. Terminal: {html.escape(END_OF_DECLARED_STEPS)}.</p>
<div class="row">
{"".join(cards)}
</div>
<h2>Handoffs</h2>
{handoffs_html}
<h2>Mermaid</h2>
<pre>{html.escape(mermaid)}</pre>
<script type="application/json" id="process-contract-spec">{spec_json}</script>
</body>
</html>
"""


def _mm_id(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch == "_" else "_" for ch in value)
    if not cleaned or cleaned[0].isdigit():
        cleaned = "n_" + cleaned
    return cleaned


def _mm(text: str, limit: int = 46) -> str:
    text = (
        (text or "")
        .replace('"', "'")
        .replace("\n", " ")
        .replace("[", "(")
        .replace("]", ")")
    )
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def dumps_process_graph(spec: ProcessContractSpec) -> str:
    return json.dumps(spec.model_dump(mode="json"), indent=2)
