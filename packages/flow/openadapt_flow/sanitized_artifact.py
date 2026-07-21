"""Create, inspect, review, and approve sanitized artifact derivatives.

Sanitization is an egress transformation, not a claim that the source never
contained PHI.  The source is immutable; every supported file is transformed
on a copy, scrubbed a second time to verify a stable result, and recorded in a
manifest.  Approval binds the complete derivative content and manifest hashes.

The built-in local reviewer binds only to loopback and serves no remote assets.
It is intentionally small: deployments can replace it with a richer desktop UI
while retaining the manifest and approval contracts in this module.
"""

from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import html
import importlib.metadata
import json
import mimetypes
import os
import re
import secrets
import shutil
import tempfile
import webbrowser
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs

try:  # Python 3.10 compatibility
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on 3.10 CI
    import tomli as tomllib

from openadapt_flow import privacy

MANIFEST_NAME = ".openadapt-sanitization.json"
APPROVAL_NAME = ".openadapt-approval.json"
SCHEMA_VERSION = 1
POLICY_VERSION = "outbound-phi-v1"
POLICY_KEY_ID_ENV = "OPENADAPT_SANITIZATION_POLICY_KEY_ID"
POLICY_KEY_ENV = "OPENADAPT_SANITIZATION_POLICY_KEY"

_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp"})
_TEXT_SUFFIXES = frozenset(
    {
        ".json",
        ".jsonl",
        ".txt",
        ".md",
        ".csv",
        ".yaml",
        ".yml",
        ".toml",
        ".html",
        ".htm",
        ".xml",
        ".log",
        ".py",
    }
)
_SYSTEM_FILES = frozenset({MANIFEST_NAME, APPROVAL_NAME})


class SanitizationError(RuntimeError):
    """The derivative cannot be proven complete or internally consistent."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_hash(rows: list[tuple[str, str]]) -> str:
    payload = json.dumps(sorted(rows), separators=(",", ":"), ensure_ascii=True)
    return _sha256_bytes(payload.encode("utf-8"))


def _manifest_hash(manifest: dict[str, Any]) -> str:
    payload = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return _sha256_bytes(payload.encode("utf-8"))


def _scrubber_version() -> str:
    try:
        return importlib.metadata.version("openadapt-privacy")
    except importlib.metadata.PackageNotFoundError:
        scrubber = privacy.get_scrubber()
        return f"injected:{type(scrubber).__module__}.{type(scrubber).__name__}"


def _source_files(src: Path) -> list[Path]:
    if not src.is_dir():
        raise SanitizationError(f"Artifact is not a directory: {src}")
    files: list[Path] = []
    for path in sorted(src.rglob("*")):
        if path.is_symlink():
            raise SanitizationError(
                f"Symlink is not reviewable: {path.relative_to(src)}"
            )
        if path.is_file():
            files.append(path)
    if not files:
        raise SanitizationError(f"Artifact has no files: {src}")
    return files


def source_tree_sha256(source: Path) -> str:
    """Hash an exact source inventory without exposing its names in a manifest."""
    source = Path(source)
    rows = [
        (str(path.relative_to(source)), _sha256_file(path))
        for path in _source_files(source)
    ]
    return _canonical_hash(rows)


def _verify_original_source(source: Path, manifest: dict[str, Any]) -> None:
    source = Path(source)
    if source_tree_sha256(source) != manifest.get("source_tree_sha256"):
        raise SanitizationError(
            "Original source changed after sanitization; create a new derivative"
        )
    if _sha256_bytes(source.name.encode("utf-8")) != manifest.get("source_name_sha256"):
        raise SanitizationError("Original source name changed after sanitization")


def _safe_component(
    component: str, scrubber: Any, *, preserve_extension: bool = False
) -> tuple[str, bool]:
    stem, ext = os.path.splitext(component) if preserve_extension else (component, "")
    scrubbed = scrubber.scrub_text(stem)
    if scrubber.scrub_text(scrubbed) != scrubbed:
        raise SanitizationError(
            "A sanitized filename still changes on a second scrub pass"
        )
    if scrubbed == stem:
        return component, False
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", scrubbed).strip(" ._")
    if not safe:
        safe = "redacted"
    suffix = hashlib.sha256(component.encode("utf-8")).hexdigest()[:8]
    return f"{safe}-{suffix}{ext}", True


def _sanitized_relpath(rel: Path, scrubber: Any) -> tuple[Path, bool]:
    parts: list[str] = []
    changed = False
    last = len(rel.parts) - 1
    for index, component in enumerate(rel.parts):
        safe, part_changed = _safe_component(
            component, scrubber, preserve_extension=index == last
        )
        parts.append(safe)
        changed = changed or part_changed
    return Path(*parts), changed


def _load_redactions(path: Optional[Path]) -> dict[str, list[dict[str, Any]]]:
    if path is None:
        return {"text": [], "images": []}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SanitizationError(f"Cannot read manual redactions {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise SanitizationError("Manual redactions must be a JSON object")
    text = data.get("text", [])
    images = data.get("images", [])
    if not isinstance(text, list) or not isinstance(images, list):
        raise SanitizationError("Manual redaction 'text' and 'images' must be lists")
    return {"text": text, "images": images}


def _matching_redactions(
    redactions: list[dict[str, Any]], rel: str
) -> list[dict[str, Any]]:
    return [r for r in redactions if r.get("file", "*") in ("*", rel)]


def _apply_text_redactions(
    text: str, redactions: list[dict[str, Any]], rel: str
) -> tuple[str, list[dict[str, Any]]]:
    applied: list[dict[str, Any]] = []
    for item in _matching_redactions(redactions, rel):
        literal = item.get("find")
        replacement = str(item.get("replacement", "[REDACTED]"))
        if not isinstance(literal, str) or not literal:
            raise SanitizationError(
                f"Text redaction for {rel} needs a non-empty 'find'"
            )
        count = text.count(literal)
        text = text.replace(literal, replacement)
        applied.append(
            {
                "type": "text-literal",
                "literal_sha256": _sha256_bytes(literal.encode("utf-8")),
                "replacement_sha256": _sha256_bytes(replacement.encode("utf-8")),
                "matches": count,
            }
        )
    return text, applied


_OPAQUE_MACHINE_ID = re.compile(
    r"(?:[0-9a-f]{16,}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}|"
    r"(?:step|state|s)::?[A-Za-z0-9_.:-]+|step_[0-9]+)",
    re.IGNORECASE,
)
_RECORDING_STRUCTURAL_JSON_KEYS = frozenset(
    {
        "app_url",
        "automation_id",
        "created_at",
        "dom_fields_after",
        "dom_fields_before",
        "dx",
        "dy",
        "i",
        "id",
        "key",
        "kind",
        "name",
        "pages_after",
        "pages_before",
        "param",
        "params",
        "role",
        "secret",
        "secret_params",
        "selector",
        "sor_after",
        "sor_before",
        "source",
        "structural",
        "structured_identity",
        "t",
        "task_description",
        "text",
        "title_after",
        "title_before",
        "url_after",
        "url_before",
        "viewport",
        "x",
        "y",
    }
)


@lru_cache(maxsize=1)
def _bundle_structural_json_keys() -> frozenset[str]:
    """Collect syntax keys declared by the executable bundle schema."""

    from openadapt_flow.ir import Workflow

    found: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                found.update(str(key) for key in properties)
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(Workflow.model_json_schema())
    return frozenset(found)


def _json_key_is_structural(kind: str, key: str) -> bool:
    if kind == "recording":
        return key in _RECORDING_STRUCTURAL_JSON_KEYS
    if kind == "bundle":
        return key in _bundle_structural_json_keys()
    return False


def _bundle_string_is_structural(path: tuple[str | int, ...], value: str) -> bool:
    """Return whether a bundle string is generated operational metadata.

    Unknown fields default to scrubbing. The allowlist covers values whose
    mutation would corrupt the executable schema without removing user data;
    user-derived fields such as names, intents, labels, parameter examples,
    postcondition text, effect values, and identity text are deliberately absent.
    """

    key = path[-1] if path and isinstance(path[-1], str) else ""
    if key.endswith(("_sha256", "_digest", "_hash", "_version")):
        return True
    if key.endswith("_at") and value:
        return True
    if key in {"recording_id", "id"} and _OPAQUE_MACHINE_ID.fullmatch(value):
        return True
    if key in {"c", "r", "phash", "salt", "structured"} and re.fullmatch(
        r"[0-9a-f]{16,}", value
    ):
        return True
    if (
        len(path) >= 2
        and path[-2] == "file_hashes"
        and re.fullmatch(r"[0-9a-f]{64}", value)
    ):
        return True
    return False


def _merge_applied_redactions(
    target: dict[tuple[str, str, str], dict[str, Any]],
    applied: list[dict[str, Any]],
) -> None:
    for item in applied:
        key = (
            item["type"],
            item["literal_sha256"],
            item["replacement_sha256"],
        )
        current = target.setdefault(key, {**item, "matches": 0})
        current["matches"] += item["matches"]


def _scrub_json_value(
    value: Any,
    *,
    scrubber: Any,
    redactions: list[dict[str, Any]],
    rel: str,
    kind: str,
    path_map: dict[str, str],
    known_paths: set[str],
    path: tuple[str | int, ...] = (),
) -> tuple[Any, list[dict[str, Any]]]:
    """Scrub JSON string values without exposing keys or non-string scalars.

    Artifact-relative paths are transformed by the filename policy and then
    treated as already scrubbed. This prevents Presidio URL recognizers from
    corrupting safe ``*.png`` references while keeping path and file content
    transformations bound to the same derivative inventory.
    """

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
    if isinstance(value, dict):
        transformed: dict[str, Any] = {}
        for raw_key, child in value.items():
            if not isinstance(raw_key, str):
                raise SanitizationError(f"JSON object key is not a string in {rel}")
            key = raw_key
            safe_key = path_map.get(key, key)
            if safe_key not in known_paths and not _json_key_is_structural(
                kind, safe_key
            ):
                safe_key = scrubber.scrub_text(safe_key)
                safe_key, key_applied = _apply_text_redactions(
                    safe_key, redactions, rel
                )
                _merge_applied_redactions(aggregate, key_applied)
            if safe_key in transformed:
                raise SanitizationError(
                    f"Scrubbing produced a duplicate JSON key in {rel}: {safe_key!r}"
                )
            updated, applied = _scrub_json_value(
                child,
                scrubber=scrubber,
                redactions=redactions,
                rel=rel,
                kind=kind,
                path_map=path_map,
                known_paths=known_paths,
                path=(*path, safe_key),
            )
            transformed[safe_key] = updated
            _merge_applied_redactions(aggregate, applied)
        return transformed, list(aggregate.values())
    if isinstance(value, list):
        transformed_list: list[Any] = []
        for index, child in enumerate(value):
            updated, applied = _scrub_json_value(
                child,
                scrubber=scrubber,
                redactions=redactions,
                rel=rel,
                kind=kind,
                path_map=path_map,
                known_paths=known_paths,
                path=(*path, index),
            )
            transformed_list.append(updated)
            _merge_applied_redactions(aggregate, applied)
        return transformed_list, list(aggregate.values())
    if not isinstance(value, str):
        return value, []

    mapped = path_map.get(value, value)
    if mapped in known_paths or (
        kind == "bundle" and _bundle_string_is_structural(path, mapped)
    ):
        scrubbed = mapped
    else:
        scrubbed = scrubber.scrub_text(mapped)
    scrubbed, applied = _apply_text_redactions(scrubbed, redactions, rel)
    return scrubbed, applied


def _scrub_json_text(
    raw: str,
    *,
    suffix: str,
    scrubber: Any,
    redactions: list[dict[str, Any]],
    rel: str,
    kind: str,
    path_map: dict[str, str],
    known_paths: set[str],
) -> tuple[str, list[dict[str, Any]]]:
    """Parse, transform, and re-serialize JSON/JSONL without type corruption."""

    if suffix == ".json":
        parsed = json.loads(raw)
        transformed, applied = _scrub_json_value(
            parsed,
            scrubber=scrubber,
            redactions=redactions,
            rel=rel,
            kind=kind,
            path_map=path_map,
            known_paths=known_paths,
        )
        if transformed == parsed:
            return raw, applied
        newline = "\n" if raw.endswith("\n") else ""
        return json.dumps(transformed, indent=2, ensure_ascii=False) + newline, applied

    aggregate: dict[tuple[str, str, str], dict[str, Any]] = {}
    output: list[str] = []
    for raw_line in raw.splitlines(keepends=True):
        content = raw_line.rstrip("\r\n")
        ending = raw_line[len(content) :]
        if not content.strip():
            output.append(raw_line)
            continue
        parsed = json.loads(content)
        transformed, applied = _scrub_json_value(
            parsed,
            scrubber=scrubber,
            redactions=redactions,
            rel=rel,
            kind=kind,
            path_map=path_map,
            known_paths=known_paths,
        )
        output.append(
            content
            if transformed == parsed
            else json.dumps(transformed, ensure_ascii=False, separators=(",", ":"))
        )
        output.append(ending)
        _merge_applied_redactions(aggregate, applied)
    return "".join(output), list(aggregate.values())


def _redact_image_rectangles(
    data: bytes, redactions: list[dict[str, Any]], rel: str
) -> tuple[bytes, list[dict[str, Any]]]:
    matches = _matching_redactions(redactions, rel)
    if not matches:
        return data, []
    from io import BytesIO

    from PIL import Image, ImageDraw

    image = Image.open(BytesIO(data)).convert("RGB")
    draw = ImageDraw.Draw(image)
    applied: list[dict[str, Any]] = []
    for item in matches:
        try:
            x = int(item["x"])
            y = int(item["y"])
            width = int(item["width"])
            height = int(item["height"])
        except (KeyError, TypeError, ValueError) as exc:
            raise SanitizationError(
                f"Image redaction for {rel} needs integer x/y/width/height"
            ) from exc
        if x < 0 or y < 0 or width <= 0 or height <= 0:
            raise SanitizationError(f"Invalid image rectangle for {rel}")
        if x + width > image.width or y + height > image.height:
            raise SanitizationError(f"Image rectangle is outside {rel}'s bounds")
        draw.rectangle((x, y, x + width, y + height), fill="black")
        applied.append(
            {
                "type": "image-rectangle",
                "x": x,
                "y": y,
                "width": width,
                "height": height,
            }
        )
    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue(), applied


def _scrub_text_file(
    source: Path,
    rel: str,
    scrubber: Any,
    redactions: list[dict[str, Any]],
    *,
    kind: str,
    path_map: dict[str, str],
    known_paths: set[str],
) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        raw = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise SanitizationError(f"Text artifact is not valid UTF-8: {rel}") from exc
    if "\x00" in raw:
        raise SanitizationError(f"Text artifact contains NUL/binary data: {rel}")
    suffix = source.suffix.lower()
    _validate_structured_text(suffix, raw, rel)
    if suffix in {".json", ".jsonl"}:
        scrubbed, applied = _scrub_json_text(
            raw,
            suffix=suffix,
            scrubber=scrubber,
            redactions=redactions,
            rel=rel,
            kind=kind,
            path_map=path_map,
            known_paths=known_paths,
        )
        rescanned, _ = _scrub_json_text(
            scrubbed,
            suffix=suffix,
            scrubber=scrubber,
            redactions=redactions,
            rel=rel,
            kind=kind,
            path_map={},
            known_paths=known_paths,
        )
    else:
        scrubbed = scrubber.scrub_text(raw)
        scrubbed, applied = _apply_text_redactions(scrubbed, redactions, rel)
        rescanned = scrubber.scrub_text(scrubbed)
        rescanned, _ = _apply_text_redactions(rescanned, redactions, rel)
    if rescanned != scrubbed:
        raise SanitizationError(
            f"Second scrub pass still changed {rel}; residual identifiers may remain"
        )
    _validate_structured_text(suffix, scrubbed, rel)
    return scrubbed.encode("utf-8"), applied


def _validate_structured_text(suffix: str, text: str, rel: str) -> None:
    """Ensure scrubbing did not corrupt a structured artifact's syntax."""
    try:
        if suffix == ".json":
            json.loads(text)
        elif suffix == ".jsonl":
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        elif suffix == ".toml":
            tomllib.loads(text)
        elif suffix in {".yaml", ".yml"}:
            import yaml

            yaml.safe_load(text)
        elif suffix == ".csv":
            list(csv.reader(text.splitlines()))
        elif suffix == ".xml":
            import xml.etree.ElementTree as ET

            ET.fromstring(text)
    except Exception as exc:  # noqa: BLE001 - normalize parser-specific failures
        raise SanitizationError(
            f"Scrubbing made structured artifact invalid: {rel} ({exc})"
        ) from exc


def _forbidden_binary_type(path: Path) -> Optional[str]:
    with path.open("rb") as fh:
        head = fh.read(512)
    signatures = (
        (b"SQLite format 3\x00", "SQLite database"),
        (b"PK\x03\x04", "ZIP/archive"),
        (b"\x1f\x8b", "gzip/archive"),
        (b"%PDF", "PDF"),
        (b"\x7fELF", "executable"),
        (b"MZ", "executable"),
        (b"OggS", "audio/video"),
        (b"ID3", "audio"),
    )
    for signature, label in signatures:
        if head.startswith(signature):
            return label
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "MP4/QuickTime video"
    if head.startswith(b"RIFF") and head[8:12] in {b"WAVE", b"AVI "}:
        return "audio/video"
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "TAR/archive"
    return None


def _scrub_image_file(
    source: Path,
    rel: str,
    redactions: list[dict[str, Any]],
    *,
    preserve_stable_bytes: bool = False,
) -> tuple[bytes, list[dict[str, Any]]]:
    original = source.read_bytes()
    first = privacy.scrub_image_bytes(original, force=True)
    first, applied = _redact_image_rectangles(first, redactions, rel)
    second = privacy.scrub_image_bytes(first, force=True)
    second, _ = _redact_image_rectangles(second, redactions, rel)
    if not _image_pixels_equal(second, first):
        raise SanitizationError(
            f"Second image scrub pass still changed {rel}; residual identifiers may remain"
        )
    if (
        preserve_stable_bytes
        and _image_pixels_equal(original, first)
        and _image_metadata_free(original)
    ):
        # Compiled templates are load-bearing evidence. A privacy provider may
        # re-encode an unchanged PNG, but encoding drift is not a redaction and
        # must not invalidate an otherwise exact executable bundle.
        return original, applied
    return first, applied


def _image_pixels_equal(left: bytes, right: bytes) -> bool:
    """Compare decoded pixels, ignoring harmless encoder byte differences."""
    from io import BytesIO

    from PIL import Image, ImageChops

    try:
        with (
            Image.open(BytesIO(left)) as left_image,
            Image.open(BytesIO(right)) as right_image,
        ):
            if left_image.size != right_image.size:
                return False
            difference = ImageChops.difference(
                left_image.convert("RGBA"), right_image.convert("RGBA")
            )
            return difference.getbbox() is None
    except Exception as exc:  # noqa: BLE001 - normalize decoder-specific failures
        raise SanitizationError(
            f"Cannot compare sanitized image pixels: {exc}"
        ) from exc


def _image_metadata_free(data: bytes) -> bool:
    """Only preserve exact bundle bytes when no ancillary metadata can carry PHI."""
    from io import BytesIO

    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as image:
            return not image.info and not image.getexif()
    except Exception as exc:  # noqa: BLE001 - normalize decoder-specific failures
        raise SanitizationError(f"Cannot inspect image metadata: {exc}") from exc


def _write_manifest(dest: Path, manifest: dict[str, Any]) -> None:
    (dest / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _tree_rows(dest: Path) -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(dest)), _sha256_file(path))
        for path in sorted(dest.rglob("*"))
        if path.is_file() and path.name not in _SYSTEM_FILES
    ]


def _reseal_sanitized_bundle(bundle: Path) -> None:
    """Rebuild generated bundle surfaces after a load-bearing transformation."""

    from openadapt_flow.compiler.codegen import render_workflow_py
    from openadapt_flow.ir import Workflow

    workflow = Workflow.load(bundle, verify_integrity=False)
    if workflow.manifest is not None:
        provenance = workflow.manifest.provenance
        provenance.policy_name = None
        provenance.certified = False
        provenance.certification_status = None
        provenance.certified_at = None
        provenance.expires_at = None
    workflow.save(bundle)
    workflow_py = bundle / "workflow.py"
    if workflow_py.is_file():
        workflow_py.write_text(render_workflow_py(workflow), encoding="utf-8")


_RECORDING_FRAME_PATH = re.compile(
    r"frames/[0-9]{4,}_(?:before|after)\.png", re.IGNORECASE
)
_BUNDLE_GENERATED_TEMPLATE_PATH = re.compile(
    r"templates/step_[0-9]+(?:_expect)?\.png(?:\.enc)?", re.IGNORECASE
)


def _is_generated_artifact_path(kind: str, rel: Path) -> bool:
    """Protect exact compiler/recorder schema paths from NLP false positives."""

    value = rel.as_posix()
    if kind == "recording":
        return value in {"meta.json", "events.jsonl"} or bool(
            _RECORDING_FRAME_PATH.fullmatch(value)
        )
    if kind == "bundle":
        return value in {
            "workflow.json",
            "workflow.json.enc",
            "workflow.py",
            "manifest.json",
        } or bool(_BUNDLE_GENERATED_TEMPLATE_PATH.fullmatch(value))
    return False


def sanitize_artifact(
    source: Path,
    destination: Path,
    *,
    kind: str,
    redactions_file: Optional[Path] = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Create a verified sanitized derivative without modifying ``source``."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if kind not in ("recording", "bundle"):
        raise SanitizationError(f"Unsupported artifact kind: {kind!r}")
    if source == destination or source in destination.parents:
        raise SanitizationError("Destination must not be the source or inside it")
    files = _source_files(source)
    if kind == "bundle" and (source / "workflow.py").is_file():
        try:
            from openadapt_flow.compiler.codegen import render_workflow_py
            from openadapt_flow.ir import Workflow

            expected = render_workflow_py(Workflow.load(source))
            actual = (source / "workflow.py").read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - normalize bundle validation
            raise SanitizationError(
                f"Cannot validate generated workflow.py: {exc}"
            ) from exc
        if actual != expected:
            raise SanitizationError(
                "workflow.py is not the deterministic rendering of workflow.json"
            )
    canonical_bundle = kind == "bundle" and all(
        (source / name).is_file()
        for name in ("workflow.json", "workflow.py", "manifest.json")
    )
    scrubber = privacy.get_scrubber()
    if scrubber is None:
        raise SanitizationError(
            "No PHI scrubber is available. Install 'openadapt-flow[privacy]' "
            "before creating an outbound derivative."
        )
    if destination.exists():
        if not overwrite:
            raise SanitizationError(f"Destination already exists: {destination}")
        shutil.rmtree(destination)
    redactions = _load_redactions(redactions_file)
    staging = Path(tempfile.mkdtemp(prefix="openadapt-sanitize-")) / "artifact"
    staging.mkdir(parents=True)
    entries: list[dict[str, Any]] = []
    semantics = "preserved"
    try:
        path_plan: list[tuple[Path, Path, Path, bool]] = []
        path_map: dict[str, str] = {}
        seen_paths: set[str] = set()
        for path in files:
            rel = path.relative_to(source)
            if _is_generated_artifact_path(kind, rel):
                safe_rel, renamed = rel, False
            else:
                safe_rel, renamed = _sanitized_relpath(rel, scrubber)
            if (
                path.suffix.lower() in _IMAGE_SUFFIXES
                and safe_rel.suffix.lower() != ".png"
            ):
                safe_rel = safe_rel.with_suffix(".png")
                renamed = True
            safe_rel_str = safe_rel.as_posix()
            if safe_rel_str in seen_paths:
                raise SanitizationError(f"Sanitized filename collision: {safe_rel_str}")
            seen_paths.add(safe_rel_str)
            path_map[rel.as_posix()] = safe_rel_str
            path_plan.append((path, rel, safe_rel, renamed))

        known_paths = set(path_map.values())
        for index, (path, rel, safe_rel, renamed) in enumerate(path_plan):
            safe_rel_str = safe_rel.as_posix()
            suffix = path.suffix.lower()
            manual: list[dict[str, Any]]
            forbidden = _forbidden_binary_type(path)
            if forbidden is not None and suffix not in _IMAGE_SUFFIXES:
                raise SanitizationError(
                    f"Unsupported {forbidden} content in {rel}; extension cannot bypass policy"
                )
            if canonical_bundle and rel.as_posix() in {"workflow.py", "manifest.json"}:
                # Both files are deterministic derivatives of workflow.json.
                # Preserve them byte-for-byte when workflow data is unchanged;
                # a load-bearing transform reseals/regenerates them below.
                output, manual = path.read_bytes(), []
                handler = "utf8-text"
            elif suffix in _TEXT_SUFFIXES:
                output, manual = _scrub_text_file(
                    path,
                    rel.as_posix(),
                    scrubber,
                    redactions["text"],
                    kind=kind,
                    path_map=path_map,
                    known_paths=known_paths,
                )
                handler = "utf8-text"
            elif suffix in _IMAGE_SUFFIXES:
                output, manual = _scrub_image_file(
                    path,
                    rel.as_posix(),
                    redactions["images"],
                    preserve_stable_bytes=kind == "bundle",
                )
                handler = "image-ocr-redaction"
            else:
                raise SanitizationError(
                    f"Unsupported artifact type {rel}. Database, video, audio, "
                    "archive, encrypted, executable, and unknown files require a "
                    "deployment-specific sanitizer and remain local."
                )
            target = staging / safe_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(output)
            source_hash = _sha256_file(path)
            derivative_hash = _sha256_bytes(output)
            changed = renamed or source_hash != derivative_hash
            # A bundle is executable input. Unless a future versioned bundle
            # schema explicitly classifies non-runtime metadata, every file is
            # load-bearing and any byte/path change invalidates semantics.
            if kind == "bundle" and changed:
                semantics = "not-preserved"
            elif kind == "recording" and changed and semantics == "preserved":
                semantics = "requires-parameterization-validation"
            entries.append(
                {
                    "index": index,
                    "source_path_sha256": _sha256_bytes(rel.as_posix().encode("utf-8")),
                    "path": safe_rel_str,
                    "handler": handler,
                    "source_sha256": source_hash,
                    "derivative_sha256": derivative_hash,
                    "changed": changed,
                    "renamed": renamed,
                    "manual_redactions": manual,
                    "verification": "stable-second-pass",
                }
            )
        if canonical_bundle and semantics == "not-preserved":
            _reseal_sanitized_bundle(staging)
            for entry in entries:
                derivative = staging / entry["path"]
                entry["derivative_sha256"] = _sha256_file(derivative)
                entry["changed"] = entry["renamed"] or (
                    entry["source_sha256"] != entry["derivative_sha256"]
                )
        source_rows = [
            (str(path.relative_to(source)), _sha256_file(path)) for path in files
        ]
        derivative_rows = _tree_rows(staging)
        manifest: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "scrubber_version": _scrubber_version(),
            "created_at": _utc_now(),
            "source_name_sha256": _sha256_bytes(source.name.encode("utf-8")),
            "kind": kind,
            "source_file_count": len(files),
            "processed_file_count": len(entries),
            "coverage_complete": len(files) == len(entries),
            "source_tree_sha256": _canonical_hash(source_rows),
            "derivative_tree_sha256": _canonical_hash(derivative_rows),
            "execution_semantics": semantics,
            # Privacy transformation stability is not runtime validation. A
            # separate challenge-bound operator attestation carries lint,
            # certification, and successful replay evidence for hosted runs.
            "runtime_semantics_validated": False,
            "trusted_boundary_required_at_runtime": True,
            "review_required": True,
            "residual_findings": [],
            "files": entries,
            "limitations": [
                "Automated OCR/NER scrubbing can miss contextual or non-textual PHI.",
                "Runtime observations can reintroduce PHI and remain subject to the execution boundary.",
                "Database, video, audio, archive, encrypted, executable, and unknown artifacts are unsupported.",
            ],
        }
        _write_manifest(staging, manifest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staging), str(destination))
        return manifest
    except Exception:
        shutil.rmtree(staging.parent, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(staging.parent, ignore_errors=True)


def load_and_verify_derivative(destination: Path) -> dict[str, Any]:
    """Verify that every derivative file is inventoried and hash-stable."""
    destination = Path(destination)
    for path in destination.rglob("*"):
        if path.is_symlink():
            raise SanitizationError(
                f"Derivative contains an unreviewed symlink: {path.relative_to(destination)}"
            )
    manifest_path = destination / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SanitizationError(f"No {MANIFEST_NAME} in {destination}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SanitizationError(f"Invalid sanitization manifest: {exc}") from exc
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise SanitizationError("Unsupported sanitization manifest version")
    entries = manifest.get("files")
    if not isinstance(entries, list) or not manifest.get("coverage_complete"):
        raise SanitizationError("Sanitization coverage is incomplete")
    expected = {(entry["path"], entry["derivative_sha256"]) for entry in entries}
    actual = set(_tree_rows(destination))
    if actual != expected:
        raise SanitizationError("Derivative files changed or are not fully inventoried")
    if _canonical_hash(list(actual)) != manifest.get("derivative_tree_sha256"):
        raise SanitizationError("Derivative tree hash does not match its manifest")
    return manifest


def approval_path(destination: Path) -> Path:
    return Path(destination) / APPROVAL_NAME


def _rescan_derivative(destination: Path) -> dict[str, Any]:
    """Re-run every handler immediately before approval.

    This catches identifiers introduced through a reviewer-supplied replacement
    and proves that manual edits did not skip the automatic second-pass gate.
    """
    manifest = load_and_verify_derivative(destination)
    scrubber = privacy.get_scrubber()
    if scrubber is None:
        raise SanitizationError(
            "Cannot approve without the manifest's scrubbing capability"
        )
    known_paths = {entry["path"] for entry in manifest["files"]}
    bundle_workflow: Any = None
    for entry in manifest["files"]:
        path = destination / entry["path"]
        if entry["handler"] == "utf8-text":
            current_text = path.read_text(encoding="utf-8")
            if manifest["kind"] == "bundle" and entry["path"] in {
                "workflow.py",
                "manifest.json",
            }:
                from openadapt_flow.compiler.codegen import render_workflow_py
                from openadapt_flow.ir import Workflow

                if bundle_workflow is None:
                    bundle_workflow = Workflow.load(destination)
                if entry["path"] == "workflow.py":
                    expected = render_workflow_py(bundle_workflow)
                    if current_text != expected:
                        raise SanitizationError(
                            "Approval rescan found workflow.py is not the "
                            "deterministic rendering of workflow.json"
                        )
                else:
                    expected_manifest = bundle_workflow.manifest.model_dump(mode="json")
                    if json.loads(current_text) != expected_manifest:
                        raise SanitizationError(
                            "Approval rescan found manifest.json does not match "
                            "the sealed workflow manifest"
                        )
                continue
            suffix = path.suffix.lower()
            _validate_structured_text(suffix, current_text, entry["path"])
            if suffix in {".json", ".jsonl"}:
                rescanned_text, _ = _scrub_json_text(
                    current_text,
                    suffix=suffix,
                    scrubber=scrubber,
                    redactions=[],
                    rel=entry["path"],
                    kind=manifest["kind"],
                    path_map={},
                    known_paths=known_paths,
                )
            else:
                rescanned_text = scrubber.scrub_text(current_text)
            if rescanned_text != current_text:
                raise SanitizationError(
                    f"Approval rescan found a remaining identifier in {entry['path']}"
                )
        elif entry["handler"] == "image-ocr-redaction":
            current_bytes = path.read_bytes()
            rescanned_bytes = privacy.scrub_image_bytes(current_bytes, force=True)
            if not _image_pixels_equal(rescanned_bytes, current_bytes):
                raise SanitizationError(
                    f"Approval rescan found remaining image text in {entry['path']}"
                )
        else:
            raise SanitizationError(
                f"Approval has no verification handler for {entry['path']}"
            )
    return manifest


def approved_archive_path(destination: Path) -> Path:
    destination = Path(destination)
    return destination.parent / f"{destination.name}.approved.zip"


def _write_deterministic_archive(destination: Path) -> Path:
    """Freeze the reviewed bytes with stable names, order, metadata, and time."""
    archive = approved_archive_path(destination)
    tmp = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(
        tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zf:
        for path in sorted(destination.rglob("*")):
            if not path.is_file() or path.name == APPROVAL_NAME:
                continue
            rel = path.relative_to(destination).as_posix()
            info = zipfile.ZipInfo(rel, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100600 << 16
            info.create_system = 3
            zf.writestr(
                info,
                path.read_bytes(),
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    os.replace(tmp, archive)
    return archive


def approve_derivative(
    destination: Path,
    *,
    source: Path,
    reviewer: str,
    automatic: bool = False,
) -> dict[str, Any]:
    """Approve the currently verified derivative and exact manifest hashes."""
    if not reviewer.strip():
        raise SanitizationError("Reviewer identity must not be empty")
    destination = Path(destination)
    manifest = _rescan_derivative(destination)
    _verify_original_source(Path(source), manifest)
    if automatic and not manifest.get("coverage_complete"):
        raise SanitizationError("Automatic approval requires complete type coverage")
    existing_verification = manifest.get("approval_verification")
    try:
        load_valid_approval(destination)
        prior_approval_valid = True
    except SanitizationError:
        prior_approval_valid = False
    if not (
        prior_approval_valid
        and isinstance(existing_verification, dict)
        and existing_verification.get("method") == "full-stable-rescan"
        and existing_verification.get("file_count") == len(manifest["files"])
        and existing_verification.get("unresolved") == 0
        and isinstance(existing_verification.get("verified_at"), str)
    ):
        existing_verification = {
            "verified_at": _utc_now(),
            "method": "full-stable-rescan",
            "file_count": len(manifest["files"]),
            "unresolved": 0,
        }
        manifest["approval_verification"] = existing_verification
        _write_manifest(destination, manifest)
    archive = _write_deterministic_archive(destination)
    archive_hash = _sha256_file(archive)
    approval = {
        "schema_version": SCHEMA_VERSION,
        "approved_at": _utc_now(),
        "reviewer": reviewer.strip(),
        "automatic": automatic,
        "policy_version": manifest["policy_version"],
        "manifest_sha256": _manifest_hash(manifest),
        "derivative_tree_sha256": manifest["derivative_tree_sha256"],
        "approved_derivative_sha256": archive_hash,
        "approved_archive_size_bytes": archive.stat().st_size,
        "verification": {
            "verified_at": existing_verification["verified_at"],
            "method": "full-stable-rescan",
            "source_tree_verified": True,
            "file_count": len(manifest["files"]),
            "unresolved": 0,
        },
    }
    approval_path(destination).write_text(
        json.dumps(approval, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return approval


def load_valid_approval(destination: Path) -> dict[str, Any]:
    destination = Path(destination)
    manifest = load_and_verify_derivative(destination)
    path = approval_path(destination)
    if not path.is_file():
        raise SanitizationError(
            "Sanitized artifact has not been approved. Review it locally, then approve it."
        )
    try:
        approval = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SanitizationError(f"Invalid approval: {exc}") from exc
    if approval.get("manifest_sha256") != _manifest_hash(manifest):
        raise SanitizationError("Approval is stale: the sanitization manifest changed")
    if approval.get("derivative_tree_sha256") != manifest.get("derivative_tree_sha256"):
        raise SanitizationError("Approval is stale: the derivative content changed")
    archive = approved_archive_path(destination)
    if not archive.is_file():
        raise SanitizationError("Approved immutable archive is missing")
    if approval.get("approved_derivative_sha256") != _sha256_file(archive):
        raise SanitizationError("Approved immutable archive changed")
    if approval.get("approved_archive_size_bytes") != archive.stat().st_size:
        raise SanitizationError("Approved immutable archive size changed")
    return approval


def build_ingest_manifest(destination: Path) -> dict[str, Any]:
    """Build the public ``openadapt.sanitization/v1`` cloud ingest envelope."""
    destination = Path(destination)
    local = load_and_verify_derivative(destination)
    approval = load_valid_approval(destination)
    media_types = sorted(
        {
            "image" if entry["handler"] == "image-ocr-redaction" else "text"
            for entry in local["files"]
        }
    )
    envelope: dict[str, Any] = {
        "schema": "openadapt.sanitization/v1",
        "artifact": {
            "kind": local["kind"],
            "sha256": approval["approved_derivative_sha256"],
            "size_bytes": approval["approved_archive_size_bytes"],
            "execution_semantics": local["execution_semantics"],
            "runtime_semantics_validated": local["runtime_semantics_validated"],
            "trusted_boundary_required_at_runtime": local[
                "trusted_boundary_required_at_runtime"
            ],
        },
        "scrubber": {
            "name": "openadapt-privacy",
            "version": local["scrubber_version"],
            "policy": local["policy_version"],
        },
        "coverage": {
            "complete": bool(local["coverage_complete"]),
            "media_types": media_types,
        },
        "findings": {"unresolved": len(local.get("residual_findings", []))},
        "approval": {
            "status": "approved",
            "method": "policy" if approval["automatic"] else "human",
            "artifact_sha256": approval["approved_derivative_sha256"],
            "approved_at": approval["approved_at"],
            "approved_by": approval["reviewer"],
        },
    }
    if approval["automatic"]:
        key_id = os.environ.get(POLICY_KEY_ID_ENV, "").strip()
        encoded_key = os.environ.get(POLICY_KEY_ENV, "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,99}", key_id):
            raise SanitizationError(
                f"Automatic approval requires a valid {POLICY_KEY_ID_ENV}"
            )
        try:
            key = base64.b64decode(encoded_key, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise SanitizationError(
                f"Automatic approval requires a base64 {POLICY_KEY_ENV}"
            ) from exc
        if len(key) < 32:
            raise SanitizationError(
                f"Automatic approval requires {POLICY_KEY_ENV} to decode to at least 32 bytes"
            )
        envelope["approval"]["policy_key_id"] = key_id
        message = json.dumps(
            [
                "openadapt.sanitization-policy/v1",
                envelope["artifact"]["kind"],
                envelope["artifact"]["sha256"],
                envelope["artifact"]["size_bytes"],
                envelope["artifact"]["execution_semantics"],
                envelope["artifact"]["runtime_semantics_validated"],
                envelope["artifact"]["trusted_boundary_required_at_runtime"],
                envelope["scrubber"]["name"],
                envelope["scrubber"]["version"],
                envelope["scrubber"]["policy"],
                sorted(envelope["coverage"]["media_types"]),
                envelope["approval"]["approved_at"],
                envelope["approval"]["approved_by"],
                key_id,
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        envelope["approval"]["policy_signature"] = hmac.new(
            key, message.encode("utf-8"), hashlib.sha256
        ).hexdigest()
    return envelope


def _update_entry(destination: Path, rel: str, redaction: dict[str, Any]) -> None:
    manifest_path = destination / MANIFEST_NAME
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SanitizationError(f"Cannot update sanitization manifest: {exc}") from exc
    entry = next((item for item in manifest["files"] if item["path"] == rel), None)
    if entry is None:
        raise SanitizationError(f"File is not in the derivative manifest: {rel}")
    path = destination / rel
    entry["derivative_sha256"] = _sha256_file(path)
    entry["changed"] = True
    entry.setdefault("manual_redactions", []).append(redaction)
    manifest["derivative_tree_sha256"] = _canonical_hash(_tree_rows(destination))
    manifest["review_required"] = True
    manifest["updated_at"] = _utc_now()
    if manifest["kind"] == "bundle":
        manifest["execution_semantics"] = "not-preserved"
    elif manifest["kind"] == "recording":
        manifest["execution_semantics"] = "requires-parameterization-validation"
    manifest["runtime_semantics_validated"] = False
    manifest["trusted_boundary_required_at_runtime"] = True
    manifest.pop("approval_verification", None)
    approval_path(destination).unlink(missing_ok=True)
    approved_archive_path(destination).unlink(missing_ok=True)
    _write_manifest(destination, manifest)


def _authorized_review_path(
    destination: Path, rel: str, *, handler: str
) -> tuple[Path, dict[str, Any]]:
    """Authorize a reviewer-supplied path against the verified manifest first."""
    destination = Path(destination).resolve()
    manifest = load_and_verify_derivative(destination)
    entry = next(
        (
            item
            for item in manifest["files"]
            if item["path"] == rel and item["handler"] == handler
        ),
        None,
    )
    if entry is None:
        raise SanitizationError(f"File is not an authorized {handler} artifact: {rel}")
    path = (destination / entry["path"]).resolve()
    if destination not in path.parents or not path.is_file():
        raise SanitizationError(f"Review path escapes the derivative: {rel}")
    return path, entry


def add_manual_text_redaction(
    destination: Path, rel: str, literal: str, replacement: str = "[REDACTED]"
) -> None:
    if not literal:
        raise SanitizationError("Manual text redaction cannot be empty")
    destination = Path(destination)
    path, _ = _authorized_review_path(destination, rel, handler="utf8-text")
    text = path.read_text(encoding="utf-8")
    count = text.count(literal)
    if count == 0:
        raise SanitizationError(f"Manual redaction text was not found in {rel}")
    path.write_text(text.replace(literal, replacement), encoding="utf-8")
    _update_entry(
        destination,
        rel,
        {
            "type": "text-literal",
            "literal_sha256": _sha256_bytes(literal.encode("utf-8")),
            "replacement_sha256": _sha256_bytes(replacement.encode("utf-8")),
            "matches": count,
            "reviewer_added": True,
        },
    )


def add_manual_image_redaction(
    destination: Path, rel: str, *, x: int, y: int, width: int, height: int
) -> None:
    destination = Path(destination)
    path, _ = _authorized_review_path(destination, rel, handler="image-ocr-redaction")
    output, applied = _redact_image_rectangles(
        path.read_bytes(),
        [{"file": rel, "x": x, "y": y, "width": width, "height": height}],
        rel,
    )
    path.write_bytes(output)
    redaction = dict(applied[0])
    redaction["reviewer_added"] = True
    _update_entry(destination, rel, redaction)


def render_review_html(source: Path, destination: Path, csrf_token: str) -> str:
    """Render a self-contained original-vs-sanitized review page."""
    source = Path(source)
    destination = Path(destination)
    manifest = load_and_verify_derivative(destination)
    originals = _source_files(source)
    source_rows = [
        (str(path.relative_to(source)), _sha256_file(path)) for path in originals
    ]
    if _canonical_hash(source_rows) != manifest.get("source_tree_sha256"):
        raise SanitizationError(
            "Original source changed after sanitization; create a new derivative before review"
        )
    sections: list[str] = []
    for entry in manifest["files"]:
        original = originals[int(entry["index"])]
        sanitized = destination / entry["path"]
        rel = html.escape(entry["path"])
        if entry["handler"] == "utf8-text":
            before = html.escape(original.read_text(encoding="utf-8")[:200_000])
            after = html.escape(sanitized.read_text(encoding="utf-8")[:200_000])
            preview = f"<div class='pair'><pre>{before}</pre><pre>{after}</pre></div>"
            form = f"""
              <form method='post' action='/redact-text'>
                <input type='hidden' name='csrf' value='{csrf_token}'>
                <input type='hidden' name='path' value='{rel}'>
                <input name='literal' required placeholder='Additional text to redact'>
                <input name='replacement' value='[REDACTED]'>
                <button>Add text redaction</button>
              </form>"""
        else:
            mime = mimetypes.guess_type(sanitized.name)[0] or "image/png"
            before = base64.b64encode(original.read_bytes()).decode("ascii")
            after = base64.b64encode(sanitized.read_bytes()).decode("ascii")
            preview = (
                "<div class='pair'>"
                f"<img src='data:{mime};base64,{before}' alt='original {rel}'>"
                f"<img src='data:image/png;base64,{after}' alt='sanitized {rel}'>"
                "</div>"
            )
            form = f"""
              <form method='post' action='/redact-image'>
                <input type='hidden' name='csrf' value='{csrf_token}'>
                <input type='hidden' name='path' value='{rel}'>
                <input name='x' type='number' min='0' required placeholder='x'>
                <input name='y' type='number' min='0' required placeholder='y'>
                <input name='width' type='number' min='1' required placeholder='width'>
                <input name='height' type='number' min='1' required placeholder='height'>
                <button>Add rectangle</button>
              </form>"""
        sections.append(f"<section><h2>{rel}</h2>{preview}{form}</section>")
    semantic = html.escape(str(manifest["execution_semantics"]))
    return f"""<!doctype html>
<html><head><meta charset='utf-8'><meta name='referrer' content='no-referrer'>
<meta http-equiv='Content-Security-Policy' content="default-src 'none'; img-src data:; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'">
<title>OpenAdapt local sanitization review</title><style>
body{{font:16px Georgia,serif;margin:2rem;background:#f4efe6;color:#18201d}}.pair{{display:grid;grid-template-columns:1fr 1fr;gap:1rem}}pre,img{{box-sizing:border-box;width:100%;max-height:28rem;overflow:auto;background:white;border:1px solid #9a8f7a;padding:1rem}}section{{margin:2rem 0;border-top:2px solid #18201d}}input,button{{margin:.4rem;padding:.55rem}}.warning{{background:#fff1c7;padding:1rem}}@media(max-width:800px){{.pair{{grid-template-columns:1fr}}}}
</style></head><body><h1>Local sanitized-artifact review</h1>
<p class='warning'>This page is served only on 127.0.0.1 and loads no remote assets. Automated scrubbing can miss contextual or non-textual PHI. Review every file. Execution semantics: <strong>{semantic}</strong>.</p>
<div class='pair'><strong>Original</strong><strong>Sanitized derivative</strong></div>
{"".join(sections)}
<form method='post' action='/approve'><input type='hidden' name='csrf' value='{csrf_token}'><input name='reviewer' required placeholder='Reviewer identity'><button>Approve exact derivative hash</button></form>
</body></html>"""


def _valid_review_host(value: Optional[str], port: int) -> bool:
    return value in {f"127.0.0.1:{port}", f"localhost:{port}"}


def _review_content_length(value: Optional[str]) -> int:
    if value is None:
        raise SanitizationError("Missing Content-Length")
    try:
        length = int(value)
    except ValueError as exc:
        raise SanitizationError("Invalid Content-Length") from exc
    if length < 0 or length > 1_000_000:
        raise SanitizationError("Review request length must be between 0 and 1000000")
    return length


def serve_review(
    source: Path,
    destination: Path,
    *,
    port: int = 0,
    open_browser: bool = True,
) -> str:
    """Serve the local reviewer until interrupted; return its loopback URL."""
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    token = secrets.token_urlsafe(24)

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: str) -> None:
            encoded = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:  # noqa: N802
            if not _valid_review_host(
                self.headers.get("Host"), int(getattr(self.server, "server_port"))
            ):
                self._send(421, "Invalid loopback Host header")
                return
            if self.path != "/":
                self._send(404, "Not found")
                return
            self._send(200, render_review_html(source, destination, token))

        def do_POST(self) -> None:  # noqa: N802
            try:
                if not _valid_review_host(
                    self.headers.get("Host"), int(getattr(self.server, "server_port"))
                ):
                    self._send(421, "Invalid loopback Host header")
                    return
                length = _review_content_length(self.headers.get("Content-Length"))
                form = {
                    key: values[-1]
                    for key, values in parse_qs(
                        self.rfile.read(length).decode("utf-8")
                    ).items()
                }
                if not secrets.compare_digest(form.get("csrf", ""), token):
                    self._send(403, "Invalid review token")
                    return
                if self.path == "/redact-text":
                    add_manual_text_redaction(
                        destination,
                        form["path"],
                        form["literal"],
                        form.get("replacement", "[REDACTED]"),
                    )
                elif self.path == "/redact-image":
                    add_manual_image_redaction(
                        destination,
                        form["path"],
                        x=int(form["x"]),
                        y=int(form["y"]),
                        width=int(form["width"]),
                        height=int(form["height"]),
                    )
                elif self.path == "/approve":
                    approve_derivative(
                        destination, source=source, reviewer=form["reviewer"]
                    )
                else:
                    self._send(404, "Not found")
                    return
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
            except (KeyError, ValueError, SanitizationError) as exc:
                self._send(400, html.escape(str(exc)))

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"Local review viewer: {url}")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return url
