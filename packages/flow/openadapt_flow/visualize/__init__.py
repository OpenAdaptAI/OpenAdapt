"""Compiled-program visualization: a shared, serializable *program graph* spec
emitted from a compiled bundle, plus renderers (self-contained HTML, Mermaid).

The engine is the single source of truth: :func:`build_program_graph` reads a
compiled :class:`~openadapt_flow.ir.Workflow` and emits a
:class:`ProgramGraphSpec` -- a backend-neutral, JSON-serializable description of
what the demonstration compiled INTO. :func:`build_composition_graph` does the
same for a composed parent directory without lifting children into one
ProgramGraph. Every surface (this CLI, the cloud view, the desktop view)
renders the SAME spec; none of them re-parse the bundle IR.

See :mod:`openadapt_flow.visualize.spec` for the spec model,
:mod:`openadapt_flow.visualize.builder` for the bundle -> spec projection, and
:mod:`openadapt_flow.visualize.render` for the HTML / Mermaid renderers.
"""

from __future__ import annotations

from openadapt_flow.visualize.builder import build_program_graph
from openadapt_flow.visualize.composition import build_composition_graph
from openadapt_flow.visualize.projection import (
    PresentationProfile,
    project_program_graph,
)
from openadapt_flow.visualize.render import render_html, render_mermaid
from openadapt_flow.visualize.spec import (
    DECLARED_STEPS_END_TITLE,
    SPEC_VERSION,
    BundleMeta,
    EdgeKind,
    EffectInfo,
    GraphEdge,
    GraphNode,
    IdentityInfo,
    NodeKind,
    ParamInfo,
    ProgramGraphSpec,
    ProvenanceInfo,
    ResolutionInfo,
    ResolutionRung,
)

__all__ = [
    "DECLARED_STEPS_END_TITLE",
    "SPEC_VERSION",
    "BundleMeta",
    "EdgeKind",
    "EffectInfo",
    "GraphEdge",
    "GraphNode",
    "IdentityInfo",
    "NodeKind",
    "ParamInfo",
    "ProgramGraphSpec",
    "PresentationProfile",
    "ProvenanceInfo",
    "ResolutionInfo",
    "ResolutionRung",
    "build_composition_graph",
    "build_program_graph",
    "project_program_graph",
    "render_html",
    "render_mermaid",
]
