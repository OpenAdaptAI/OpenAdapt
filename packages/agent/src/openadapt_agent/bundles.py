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

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "WorkflowInfo",
    "discover_bundles",
    "is_bundle_dir",
    "load_workflow_info",
    "slugify",
    "tool_input_schema",
    "workflow_public_id",
]

_BUNDLE_MARKERS = ("workflow.json", "workflow.json.enc")


def is_bundle_dir(path: Path) -> bool:
    """True when *path* looks like a compiled workflow bundle directory."""
    return any((path / marker).is_file() for marker in _BUNDLE_MARKERS)


def slugify(name: str) -> str:
    """Lowercase, underscore-separated identifier for tool names.

    Mirrors flow's skill-slug convention. The slug remains local metadata
    unless protected export is explicitly enabled; default MCP tool names
    use :func:`workflow_public_id`.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return slug or "workflow"


@dataclass
class WorkflowInfo:
    """Metadata for one discovered bundle (or the error loading it)."""

    slug: str
    public_id: str
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
            public_id=workflow_public_id(bundle_dir),
            bundle_dir=bundle_dir,
            encrypted=encrypted,
            load_error=f"{type(exc).__name__}: {exc}",
        )
    return WorkflowInfo(
        slug=slug or slugify(wf.name),
        public_id=workflow_public_id(bundle_dir, workflow=wf),
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


def workflow_public_id(
    bundle_dir: Path,
    *,
    workflow: Any = None,
) -> str:
    """Stable opaque MCP identifier without exporting bundle labels or paths."""
    digest = None
    manifest = getattr(workflow, "manifest", None)
    if manifest is not None:
        digest = getattr(manifest, "content_digest", None)
    if isinstance(digest, str) and digest:
        material = digest.encode("utf-8")
    elif workflow is not None:
        material = workflow.model_dump_json().encode("utf-8")
    else:
        hasher = hashlib.sha256()
        for marker in _BUNDLE_MARKERS:
            path = Path(bundle_dir) / marker
            if not path.is_file() or path.is_symlink():
                continue
            hasher.update(marker.encode("ascii"))
            try:
                hasher.update(path.read_bytes())
            except OSError:
                continue
        material = hasher.digest()
    opaque = hashlib.sha256(b"openadapt-agent-workflow-id-v1\0" + material).hexdigest()[:24]
    return f"workflow_{opaque}"


def tool_input_schema(
    info: WorkflowInfo,
    *,
    allow_url_override: bool = False,
    allow_recorded_defaults: bool = False,
) -> dict:
    """JSON schema for a workflow's run tool, derived from declared params.

    Recorded demonstration values never enter the schema. Parameters are
    required unless the operator explicitly enabled synthetic recorded-default
    reuse when starting the server. ``url`` is present only under the separate
    URL-override opt-in.
    """
    properties: dict[str, dict] = {}
    for name in info.params:
        properties[name] = {
            "type": "string",
            "description": f"Value for the declared {name!r} workflow parameter.",
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
        "required": [] if allow_recorded_defaults else sorted(info.params),
        "additionalProperties": False,
    }
