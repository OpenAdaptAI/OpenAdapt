"""Bounded, local-only JSON artifacts for the operator console.

The normal console DTOs deliberately redact workflow and run values.  This
module powers the explicit "open local artifact" path: a bearer-authenticated
operator can inspect the exact JSON/JSONL bytes that already exist inside the
configured bundle or run root.  It never accepts a caller-provided path and it
never performs network I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

MAX_ARTIFACT_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_NODES = 20_000
MAX_DOCUMENT_DEPTH = 100
MAX_DISCOVERED_ARTIFACTS = 256
MAX_DISCOVERY_DEPTH = 6
MAX_DISCOVERY_ENTRIES = 4_096

ArtifactFormat = Literal["json", "jsonl"]

_RUN_ROOT_FILES = {
    "approval.json",
    "attended_decisions.json",
    "governed_policy.json",
    "pending_escalation.json",
    "report.json",
}
_BUNDLE_ROOT_FILES = {
    "annotations.json",
    "manifest.json",
    "param_proposals.json",
    "workflow.json",
}
_CHECKPOINT_PREFIXES = ("step_", "pstate_")
_HEAL_FILES = {"heal.json", "patch.json"}
_ROOT_LABELS = {
    "annotations.json": "Workflow annotations",
    "approval.json": "Operator approval",
    "attended_decisions.json": "Attended decisions",
    "governed_policy.json": "Governed policy",
    "manifest.json": "Bundle manifest",
    "param_proposals.json": "Flagged parameter proposals",
    "pending_escalation.json": "Pending escalation",
    "report.json": "Run report",
    "workflow.json": "Compiled workflow",
}


class JsonArtifactError(ValueError):
    """A local artifact cannot be admitted to the bounded viewer."""

    def __init__(self, message: str, *, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class JsonArtifact:
    """A path admitted from a fixed local artifact family."""

    id: str
    relative_path: str
    label: str
    root: Path
    path: Path
    format: ArtifactFormat
    size_bytes: int

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "format": self.format,
            "size_bytes": self.size_bytes,
            "download_name": f"openadapt-artifact-{self.id}.{self.format}",
        }


@dataclass(frozen=True)
class LoadedJsonArtifact:
    artifact: JsonArtifact
    raw: bytes
    document: Any
    node_count: int
    max_depth: int
    sha256: str

    def public(self) -> dict[str, Any]:
        out = self.artifact.public()
        out.update(
            {
                "document": self.document,
                "node_count": self.node_count,
                "max_depth": self.max_depth,
                "sha256": self.sha256,
            }
        )
        return out


def _artifact_id(relative_path: str) -> str:
    return hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:24]


def _public_label(relative: Path, artifact_id: str) -> str:
    if len(relative.parts) == 1:
        return _ROOT_LABELS.get(relative.name, "Local JSON artifact")
    if relative.parts[0] == "checkpoints":
        if relative.name == "_manifest.json":
            return "Checkpoint manifest"
        kind = (
            "Program checkpoint"
            if relative.name.startswith("pstate_")
            else "Run checkpoint"
        )
        return f"{kind} {artifact_id[:8]}"
    if relative.parts[0] == "heals":
        kind = "Repair patch" if relative.name == "patch.json" else "Repair event"
        return f"{kind} {artifact_id[:8]}"
    if relative.parts[0] == "qualification":
        return f"Qualification evidence {artifact_id[:8]}"
    return f"Run evidence {artifact_id[:8]}"


def _format(path: Path) -> Optional[ArtifactFormat]:
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix == ".jsonl":
        return "jsonl"
    return None


def _is_admitted_run_path(relative: Path) -> bool:
    parts = relative.parts
    if len(parts) == 1:
        return relative.name in _RUN_ROOT_FILES
    if parts[0] == "checkpoints" and len(parts) == 2:
        return relative.name == "_manifest.json" or (
            relative.name.endswith(".json")
            and relative.name.startswith(_CHECKPOINT_PREFIXES)
        )
    if parts[0] == "heals" and len(parts) == 3:
        return relative.name in _HEAL_FILES
    if parts[0] == "evidence":
        return _format(relative) is not None
    return False


def _is_admitted_bundle_path(relative: Path) -> bool:
    if len(relative.parts) == 1:
        return relative.name in _BUNDLE_ROOT_FILES
    if relative.parts[0] in {"evidence", "qualification"}:
        return _format(relative) is not None
    return False


def _walk_regular_files(root: Path) -> list[Path]:
    """Walk a shallow local tree without following any symlink."""
    out: list[Path] = []
    frontier: list[tuple[Path, int]] = [(root, 0)]
    entries_seen = 0
    while frontier:
        directory, depth = frontier.pop()
        if depth > MAX_DISCOVERY_DEPTH:
            continue
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            continue
        for entry in entries:
            entries_seen += 1
            if entries_seen > MAX_DISCOVERY_ENTRIES:
                return sorted(out)
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    frontier.append((Path(entry.path), depth + 1))
                elif entry.is_file(follow_symlinks=False) and _format(Path(entry.path)):
                    out.append(Path(entry.path))
                    if len(out) >= MAX_DISCOVERED_ARTIFACTS:
                        return sorted(out)
            except OSError:
                continue
    return sorted(out)


def _candidate_files(root: Path, *, scope: Literal["runs", "workflows"]) -> list[Path]:
    """Inspect only directories that can contain an admitted artifact family."""
    root_names = _RUN_ROOT_FILES if scope == "runs" else _BUNDLE_ROOT_FILES
    out = [root / name for name in sorted(root_names)]
    subdirectories = (
        ("checkpoints", "heals", "evidence")
        if scope == "runs"
        else ("evidence", "qualification")
    )
    for name in subdirectories:
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            continue
        out.extend(_walk_regular_files(directory))
        if len(out) >= MAX_DISCOVERED_ARTIFACTS:
            break
    ordered: list[Path] = []
    seen: set[Path] = set()
    for candidate in out:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered[:MAX_DISCOVERED_ARTIFACTS]


def _contained_regular_file(root: Path, candidate: Path) -> Optional[Path]:
    """Resolve a regular file beneath ``root`` with no symlink component."""
    lexical_root = root.absolute()
    lexical = candidate.absolute()
    if lexical_root.is_symlink():
        return None
    try:
        relative = lexical.relative_to(lexical_root)
    except ValueError:
        return None
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return None
    try:
        resolved_root = lexical_root.resolve(strict=True)
        resolved = lexical.resolve(strict=True)
        resolved.relative_to(resolved_root)
        info = resolved.stat(follow_symlinks=False)
    except (OSError, ValueError):
        return None
    return resolved if stat.S_ISREG(info.st_mode) else None


def list_artifacts(
    root: Path, *, scope: Literal["runs", "workflows"]
) -> list[JsonArtifact]:
    """List admitted JSON files in one already-resolved run or bundle dir."""
    predicate = _is_admitted_run_path if scope == "runs" else _is_admitted_bundle_path
    out: list[JsonArtifact] = []
    for candidate in _candidate_files(root, scope=scope):
        try:
            relative = candidate.relative_to(root)
        except ValueError:
            continue
        artifact_format = _format(relative)
        if artifact_format is None or not predicate(relative):
            continue
        safe = _contained_regular_file(root, candidate)
        if safe is None:
            continue
        try:
            size = safe.stat(follow_symlinks=False).st_size
        except OSError:
            continue
        relative_path = relative.as_posix()
        artifact_id = _artifact_id(relative_path)
        out.append(
            JsonArtifact(
                id=artifact_id,
                relative_path=relative_path,
                label=_public_label(relative, artifact_id),
                root=root,
                path=safe,
                format=artifact_format,
                size_bytes=size,
            )
        )
    return sorted(out, key=lambda artifact: artifact.relative_path)


def resolve_artifact(
    root: Path,
    artifact_id: str,
    *,
    scope: Literal["runs", "workflows"],
) -> Optional[JsonArtifact]:
    """Resolve only an opaque id returned by a fresh admitted listing."""
    return next(
        (
            artifact
            for artifact in list_artifacts(root, scope=scope)
            if artifact.id == artifact_id
        ),
        None,
    )


def _read_bounded(artifact: JsonArtifact) -> bytes:
    if artifact.size_bytes > MAX_ARTIFACT_BYTES:
        raise JsonArtifactError(
            f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte viewer limit",
            status_code=413,
        )
    safe = _contained_regular_file(artifact.root, artifact.path)
    if safe is None:
        raise JsonArtifactError("artifact is no longer available", status_code=404)
    try:
        before_open = safe.stat(follow_symlinks=False)
    except OSError as exc:
        raise JsonArtifactError(
            "artifact is no longer available", status_code=404
        ) from exc
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(safe, flags)
    except OSError as exc:
        raise JsonArtifactError(
            "artifact is no longer available", status_code=404
        ) from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise JsonArtifactError("artifact is not a regular file", status_code=404)
        if (opened.st_dev, opened.st_ino) != (before_open.st_dev, before_open.st_ino):
            raise JsonArtifactError("artifact changed while opening", status_code=404)
        if opened.st_size > MAX_ARTIFACT_BYTES:
            raise JsonArtifactError(
                f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte viewer limit",
                status_code=413,
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(64 * 1024, MAX_ARTIFACT_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_ARTIFACT_BYTES:
                raise JsonArtifactError(
                    f"artifact exceeds the {MAX_ARTIFACT_BYTES}-byte viewer limit",
                    status_code=413,
                )
        return b"".join(chunks)
    finally:
        os.close(fd)


def _reject_nonstandard_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant {value}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate JSON object key")
        out[key] = value
    return out


def _parse_json(text: str) -> Any:
    return json.loads(
        text,
        parse_constant=_reject_nonstandard_constant,
        object_pairs_hook=_unique_object,
    )


def _measure(document: Any) -> tuple[int, int]:
    """Bound the parsed tree before any UI recursively expands it."""
    nodes = 0
    max_depth = 0
    stack: list[tuple[Any, int]] = [(document, 0)]
    while stack:
        value, depth = stack.pop()
        nodes += 1
        max_depth = max(max_depth, depth)
        if nodes > MAX_DOCUMENT_NODES:
            raise JsonArtifactError(
                f"artifact exceeds the {MAX_DOCUMENT_NODES}-node viewer limit",
                status_code=413,
            )
        if depth > MAX_DOCUMENT_DEPTH:
            raise JsonArtifactError(
                f"artifact exceeds the {MAX_DOCUMENT_DEPTH}-level viewer limit",
                status_code=413,
            )
        if isinstance(value, dict):
            stack.extend((child, depth + 1) for child in value.values())
        elif isinstance(value, list):
            stack.extend((child, depth + 1) for child in value)
    return nodes, max_depth


def load_artifact(artifact: JsonArtifact) -> LoadedJsonArtifact:
    raw = _read_bounded(artifact)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise JsonArtifactError("artifact is not valid UTF-8") from exc
    try:
        measured: Optional[tuple[int, int]] = None
        if artifact.format == "json":
            document = _parse_json(text)
        else:
            document = []
            node_count = 1  # the JSONL document is presented as an array
            max_depth = 0
            for line_number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    item = _parse_json(line)
                except (json.JSONDecodeError, ValueError) as exc:
                    raise JsonArtifactError(
                        f"JSONL line {line_number} is invalid"
                    ) from exc
                item_nodes, item_depth = _measure(item)
                node_count += item_nodes
                max_depth = max(max_depth, item_depth + 1)
                if node_count > MAX_DOCUMENT_NODES:
                    raise JsonArtifactError(
                        f"artifact exceeds the {MAX_DOCUMENT_NODES}-node viewer limit",
                        status_code=413,
                    )
                if max_depth > MAX_DOCUMENT_DEPTH:
                    raise JsonArtifactError(
                        f"artifact exceeds the {MAX_DOCUMENT_DEPTH}-level viewer limit",
                        status_code=413,
                    )
                document.append(item)
            measured = node_count, max_depth
    except JsonArtifactError:
        raise
    except RecursionError as exc:
        raise JsonArtifactError(
            f"artifact exceeds the {MAX_DOCUMENT_DEPTH}-level viewer limit",
            status_code=413,
        ) from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise JsonArtifactError("artifact is not valid JSON") from exc
    node_count, max_depth = measured or _measure(document)
    return LoadedJsonArtifact(
        artifact=artifact,
        raw=raw,
        document=document,
        node_count=node_count,
        max_depth=max_depth,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
