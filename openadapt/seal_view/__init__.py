"""Write a sealed run's Seal as one offline HTML page: ``RUN_DIR/seal.html``.

The page is ``viewer.html`` (built from ``packages/seal-view``; Preact, the
program map and the Seal inlined) with the run's own files embedded in it:
the exact bytes of ``report.json``, the exact text of ``seal.json`` (and of
``seal.anchor.json``, ``seal.tsa.json`` and ``seal.tsr`` when the run has them), the Seal
keys the reader trusts, and the program graph of the run's bundle. In the
browser it checks the Ed25519 signature over the embedded report, draws the
Seal from it, replays the run over the step table and the program map, and
lets the reader tamper with a copy. It makes no network requests (its
Content-Security-Policy allows none), so it works from ``file://`` offline,
and nothing is uploaded.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import webbrowser
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

#: The file the page is written to, next to the run's ``report.json``.
PAGE_NAME = "seal.html"
#: Where the run's files go in the viewer.
DATA_SLOT = "<!--SEAL-DATA-->"
#: The run's files a reader can open from the page, when present.
RUN_FILES = (
    "report.json",
    "seal.json",
    "seal.anchor.json",
    "seal.tsa.json",
    "seal.tsr",
)


class SealViewError(RuntimeError):
    """The run can't be shown: no Seal, no report, or no viewer."""


def viewer_template() -> str:
    """The viewer as shipped in this package."""

    try:
        return (
            resources.files("openadapt.seal_view")
            .joinpath("viewer.html")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, ModuleNotFoundError) as exc:  # pragma: no cover
        raise SealViewError("this install has no Seal viewer (viewer.html)") from exc


def _text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _b64(path: Path) -> Optional[str]:
    try:
        return base64.b64encode(path.read_bytes()).decode("ascii")
    except FileNotFoundError:
        return None


def _digest_of(spec: Mapping[str, Any]) -> Optional[str]:
    provenance = (spec.get("bundle") or {}).get("provenance") or {}
    return provenance.get("content_digest")


def bundle_graph(bundle_dir: Path) -> dict[str, Any]:
    """The program graph of a compiled bundle, as the program map reads it.

    The remote-safe projection: step kinds, checks and stop rules, with no
    recorded text or images. The page names each step from the run's own
    report instead.
    """

    from openadapt_flow.ir import Workflow
    from openadapt_flow.visualize import (
        PresentationProfile,
        build_program_graph,
        project_program_graph,
    )

    full = build_program_graph(Workflow.load(Path(bundle_dir)))
    spec = json.loads(
        project_program_graph(
            full, PresentationProfile("remote-safe")
        ).model_dump_json()
    )
    # The projection drops the bundle's content digest; the page needs it to
    # match the program to the report, which carries the same digest.
    bundle = spec.setdefault("bundle", {})
    provenance = bundle.get("provenance") or {}
    provenance["content_digest"] = full.bundle.provenance.content_digest
    bundle["provenance"] = provenance
    return spec


def _bundle_candidates(run_dir: Path) -> list[Path]:
    # A run folder of the quickstart or the tutorial sits next to its bundle.
    return [run_dir / "bundle", run_dir.parent / "bundle"]


def find_program(
    run_dir: Path, report: Mapping[str, Any], bundle_dir: Optional[Path] = None
) -> dict[str, Any]:
    """The run's program graph: from ``bundle_dir``, else a bundle next to the run.

    Only a bundle whose content digest is the report's counts. Without one,
    the program is the run's own steps in order, read from the report.
    """

    digest = report.get("bundle_content_digest")
    candidates = [Path(bundle_dir)] if bundle_dir else _bundle_candidates(run_dir)
    for candidate in candidates:
        if not (candidate / "workflow.json").is_file():
            continue
        try:
            spec = bundle_graph(candidate)
        except Exception:  # noqa: BLE001 - another candidate, or the report's steps
            continue
        if _digest_of(spec) == digest:
            return spec
        if bundle_dir:
            raise SealViewError(
                f"{candidate} is not the program this run ran "
                f"(bundle digest {_digest_of(spec)}, the report says {digest})"
            )
    return report_graph(report)


def report_graph(report: Mapping[str, Any]) -> dict[str, Any]:
    """A program graph of the run's own steps, in order, when no bundle is at hand."""

    results = [r for r in report.get("results") or [] if isinstance(r, Mapping)]
    nodes: list[dict[str, Any]] = []
    for index, result in enumerate(results):
        intent = str(result.get("intent") or "")
        nodes.append(
            {
                "id": str(result.get("step_id") or f"step_{index:03d}"),
                "index": index,
                "kind": "action",
                "title": intent or f"Step {index + 1}",
                "action": intent.split(" ", 1)[0] if intent else "click",
                "identity": None,
                "effects": [],
                "postconditions": [],
                "halts": [],
                "badges": [],
            }
        )
    nodes.append(
        {
            "id": "__end__",
            "index": len(nodes),
            "kind": "terminal",
            "title": "End of the run's steps",
            "outcome": "success",
            "effects": [],
            "postconditions": [],
            "halts": [],
            "badges": [],
        }
    )
    edges = [
        {"source": a["id"], "target": b["id"], "kind": "sequence", "label": ""}
        for a, b in zip(nodes, nodes[1:])
    ]
    return {
        "spec_version": 2,
        "bundle": {
            "name": str(report.get("workflow_name") or "run"),
            "provenance": {"content_digest": report.get("bundle_content_digest")},
        },
        "nodes": nodes,
        "edges": edges,
    }


def local_keys(home: Optional[Path] = None) -> list[dict[str, str]]:
    """This computer's Seal key, as a one-key registry."""

    from openadapt_flow.offline_seal import public_key_entry, seal_home

    return [public_key_entry(home or seal_home())]


def page_data(
    run_dir: Path,
    *,
    keys: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    program: Mapping[str, Any],
    about: Optional[Mapping[str, Any]] = None,
    host: Optional[Mapping[str, Any]] = None,
    commands: Optional[str] = None,
) -> dict[str, Any]:
    """What the viewer reads: the run's files exactly as they are on disk."""

    run_dir = Path(run_dir)
    report = (run_dir / "report.json").read_bytes()
    seal = _text(run_dir / "seal.json")
    if seal is None:
        raise SealViewError(f"{run_dir} has no seal.json")
    present = [name for name in RUN_FILES if (run_dir / name).is_file()]
    return {
        "report_b64": base64.b64encode(report).decode("ascii"),
        "seal_text": seal,
        "anchor_text": _text(run_dir / "seal.anchor.json"),
        "tsa_text": _text(run_dir / "seal.tsa.json"),
        "tsr_b64": _b64(run_dir / "seal.tsr"),
        "keys": keys,
        "program": {"spec": dict(program)},
        "about": dict(about or {}),
        "host": {"files": present, **dict(host or {})},
        "commands": commands,
    }


def render_page(data: Mapping[str, Any], template: Optional[str] = None) -> str:
    """The viewer with ``data`` in its slot."""

    template = template if template is not None else viewer_template()
    if DATA_SLOT not in template:
        raise SealViewError("the Seal viewer has no slot for the run's files")
    # JSON in a script element: escape "<" so no "</script>" can end it early.
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    payload = payload.replace("<", "\\u003c")
    block = f'<script type="application/json" id="seal-data">{payload}</script>'
    return template.replace(DATA_SLOT, block, 1)


def write_seal_page(
    run_dir: Path,
    *,
    keys: Optional[Sequence[Mapping[str, Any]] | Mapping[str, Any]] = None,
    bundle_dir: Optional[Path] = None,
    program: Optional[Mapping[str, Any]] = None,
    about: Optional[Mapping[str, Any]] = None,
    host: Optional[Mapping[str, Any]] = None,
    commands: Optional[str] = None,
    out: Optional[Path] = None,
) -> Path:
    """Write ``run_dir/seal.html`` (or ``out``) and return its path.

    ``keys`` is the registry the page trusts; by default this computer's Seal
    key, which is the key the quickstart and local runs sign with. ``program``
    is the run's program graph when the caller has it (:func:`bundle_graph`);
    otherwise it is found from ``bundle_dir`` or next to the run.
    """

    run_dir = Path(run_dir)
    report_path = run_dir / "report.json"
    if not (run_dir / "seal.json").is_file():
        raise SealViewError(f"{run_dir} is not a sealed run (no seal.json)")
    try:
        report = json.loads(report_path.read_bytes())
    except (OSError, ValueError) as exc:
        raise SealViewError(f"{report_path} can't be read") from exc
    if keys is None:
        keys = local_keys()
        # A page forwarded to someone else stays a local check, not third-party proof.
        host = {
            "trustWords": "checked against your computer's Seal key",
            **dict(host or {}),
        }
    if program is None or _digest_of(program) != report.get("bundle_content_digest"):
        program = find_program(run_dir, report, bundle_dir)
    data = page_data(
        run_dir,
        keys=keys,
        program=program,
        about=about,
        host={"unchanged": "The files on disk are unchanged.", **dict(host or {})},
        commands=commands,
    )
    target = Path(out) if out else run_dir / PAGE_NAME
    target.write_text(render_page(data), encoding="utf-8")
    return target


def open_page(page: Path) -> bool:
    """Open ``page`` in the default browser; False when nothing could open it."""

    uri = Path(page).resolve().as_uri()
    try:
        if sys.platform == "darwin":
            return (
                subprocess.run(["open", uri], check=False, timeout=10).returncode == 0
            )
        if os.name == "nt":  # pragma: no cover - Windows
            os.startfile(uri)  # type: ignore[attr-defined]  # noqa: S606
            return True
        return bool(webbrowser.open(uri))
    except Exception:  # noqa: BLE001 - the path is printed either way
        return False


__all__ = [
    "PAGE_NAME",
    "SealViewError",
    "bundle_graph",
    "find_program",
    "local_keys",
    "open_page",
    "page_data",
    "render_page",
    "report_graph",
    "viewer_template",
    "write_seal_page",
]
