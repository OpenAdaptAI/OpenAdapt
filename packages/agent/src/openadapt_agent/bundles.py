"""Discover compiled openadapt-flow workflow bundles and derive tool schemas.

A *bundle* is a directory written by ``openadapt-flow compile`` (or
``induce``) containing ``workflow.json`` — or ``workflow.json.enc`` when
encrypted at rest. Loading always goes through
:meth:`openadapt_flow.ir.Workflow.load`, so structural validation,
integrity verification, and decryption (via ``OPENADAPT_BUNDLE_KEY``)
behave exactly as they do for the flow CLI; this module never parses
``workflow.json`` itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "WorkflowInfo",
    "discover_bundles",
    "is_bundle_dir",
    "load_workflow_info",
    "slugify",
    "tool_input_schema",
]

_BUNDLE_MARKERS = ("workflow.json", "workflow.json.enc")


def is_bundle_dir(path: Path) -> bool:
    """True when *path* looks like a compiled workflow bundle directory."""
    return any((path / marker).is_file() for marker in _BUNDLE_MARKERS)


def slugify(name: str) -> str:
    """Lowercase, underscore-separated identifier for tool names.

    Mirrors flow's skill-slug convention but uses underscores so the
    result is also a valid ``run_<slug>`` MCP tool-name fragment.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "workflow"


@dataclass
class WorkflowInfo:
    """Metadata for one discovered bundle (or the error loading it)."""

    slug: str
    bundle_dir: Path
    name: str = ""
    params: dict[str, str] = field(default_factory=dict)
    step_intents: list[str] = field(default_factory=list)
    schema_version: int | None = None
    encrypted: bool = False
    load_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.load_error is None


def load_workflow_info(bundle_dir: Path, slug: str | None = None) -> WorkflowInfo:
    """Load bundle metadata via ``Workflow.load`` (never hand-parsed).

    A bundle that cannot be loaded (missing decryption key, tampered
    manifest, malformed structure) yields a ``WorkflowInfo`` with
    ``load_error`` set instead of raising, so one bad bundle does not take
    down the whole server.
    """
    bundle_dir = Path(bundle_dir)
    encrypted = (bundle_dir / "workflow.json.enc").is_file()
    try:
        from openadapt_flow.ir import Workflow

        wf = Workflow.load(bundle_dir)
    except Exception as exc:  # fail-soft per bundle, loud in the listing
        return WorkflowInfo(
            slug=slug or slugify(bundle_dir.name),
            bundle_dir=bundle_dir,
            encrypted=encrypted,
            load_error=f"{type(exc).__name__}: {exc}",
        )
    return WorkflowInfo(
        slug=slug or slugify(wf.name),
        bundle_dir=bundle_dir,
        name=wf.name,
        params=dict(wf.params),
        step_intents=[step.intent for step in wf.steps],
        schema_version=wf.schema_version,
        encrypted=encrypted,
    )


def discover_bundles(root: Path) -> list[WorkflowInfo]:
    """Discover bundles under *root*.

    ``root`` may itself be a single bundle, or a directory whose immediate
    subdirectories are bundles. Slugs are made unique by suffixing the
    directory name on collision.
    """
    root = Path(root)
    if not root.is_dir():
        raise FileNotFoundError(f"bundles directory does not exist: {root}")

    bundle_dirs: list[Path]
    if is_bundle_dir(root):
        bundle_dirs = [root]
    else:
        bundle_dirs = sorted(
            (p for p in root.iterdir() if p.is_dir() and is_bundle_dir(p)),
            key=lambda p: p.name,
        )

    infos: list[WorkflowInfo] = []
    seen: set[str] = set()
    for bundle_dir in bundle_dirs:
        info = load_workflow_info(bundle_dir)
        if info.slug in seen:
            info.slug = slugify(f"{info.slug}_{bundle_dir.name}")
        seen.add(info.slug)
        infos.append(info)
    return infos


def tool_input_schema(info: WorkflowInfo, *, allow_url_override: bool = False) -> dict:
    """JSON schema for a workflow's run tool, derived from declared params.

    Every workflow parameter becomes an optional string property whose
    default is the recorded example value (the same fallback the flow CLI
    applies when a ``--param`` is omitted). ``url`` is only present when
    the operator started the server with ``--allow-url-override``.
    """
    properties: dict[str, dict] = {}
    for name, example in info.params.items():
        properties[name] = {
            "type": "string",
            "description": (
                f"Value for the {name!r} workflow parameter "
                f"(default: the recorded example value {example!r})."
            ),
            "default": example,
        }
    if allow_url_override:
        properties["url"] = {
            "type": "string",
            "description": (
                "Optional target app URL override. Only honoured because the "
                "operator started the server with --allow-url-override."
            ),
        }
    return {
        "type": "object",
        "properties": properties,
        "required": [],
        "additionalProperties": False,
    }
