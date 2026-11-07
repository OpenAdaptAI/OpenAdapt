"""Optional Chrome-extension evidence for a recording (``extension_evidence.jsonl``).

Everyday Chrome is recorded through the OS accessibility tree without an
install. Some detail accessibility cannot see: open shadow DOM structure,
cross-origin iframes, and network saves. An optional Manifest V3 extension
(``packages/flow/chrome_extension/``) can supply that detail while a
recording is active.

This module is the engine-side ingest. The extension is one more evidence
source for a web surface and is never required: an absent file is a valid
empty capture, and recording still works through accessibility alone.

Schema ``openadapt.extension-evidence/1``. Secret field values are withheld
(``<secret>``) before a line is written. No remote endpoints.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, MutableMapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.extension_network import NETWORK_SAVE_KIND, redact_network_save

EVIDENCE_FILE = "extension_evidence.jsonl"
SCHEMA = "openadapt.extension-evidence/1"
REDACTED_VALUE = "<secret>"

SECRET_NAME = re.compile(
    r"pass(word|wd|phrase)?\b|passwd|pwd|secret|private[_-]?key|^pin$|^otp$",
    re.I,
)


EVIDENCE_KINDS = frozenset({"target", "frame_focus"})
URL_FIELDS = ("frame_url", "frame_src")
_FRAME_INT_KEYS = ("tab_id", "frame_id")
_FRAME_STR_KEYS = ("document_id", "origin")


def strip_url(url: Any) -> Optional[str]:
    """A URL without credentials, query or fragment (those often carry tokens).

    ``http``/``https`` keep scheme, host and path. ``about:`` keeps its path
    (``about:srcdoc``). Every other scheme (``data:``, ``blob:``, ...) keeps
    only the scheme, since the rest can be page content.
    """

    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        host = parts.hostname or ""
        if ":" in host:
            # IPv6 literal: keep the brackets, or the result is not a URL and
            # the next pass (ingest re-redacts on load) can't parse it.
            host = f"[{host}]"
        try:
            port = parts.port
        except ValueError:
            return None
        if port is not None:
            host = f"{host}:{port}"
        return f"{scheme}://{host}{parts.path}"
    if scheme == "about":
        return f"about:{parts.path}"
    return f"{scheme}:" if scheme else None


def _clean_frame(frame: Any) -> Optional[dict[str, Any]]:
    """Keep only the typed frame identity keys the extension background stamps."""

    if not isinstance(frame, Mapping):
        return None
    out: dict[str, Any] = {}
    for key in _FRAME_INT_KEYS:
        value = frame.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    for key in _FRAME_STR_KEYS:
        value = frame.get(key)
        if isinstance(value, str) and value:
            out[key] = strip_url(value) if key == "origin" else value
    return out or None


def is_secret_field(
    *,
    tag: str | None = None,
    name: str | None = None,
    field_id: str | None = None,
    secret_field: bool = False,
    input_type: str | None = None,
) -> bool:
    """True when a target must never carry a live value."""

    if secret_field:
        return True
    if (input_type or "").lower() == "password":
        return True
    for candidate in (name, field_id):
        if candidate and SECRET_NAME.search(str(candidate)):
            return True
    _ = tag  # reserved for future tag-based rules
    return False


def redact_evidence(
    line: Mapping[str, Any],
    *,
    secret_names: Iterable[str] = (),
) -> dict[str, Any]:
    """Return a copy with secret values withheld.

    Defense in depth for lines that already left the content script.
    A live secret value is never copied into the result: only the constant
    ``REDACTED_VALUE`` placeholder (`"<secret>"`) is written for secret fields, so clear-text
    storage scanners see no sensitive payload on the write path.
    """

    names = {n.lower() for n in secret_names}
    declared = bool(line.get("secret_field"))
    # Check name and id independently: a benign name must not hide a secret id.
    for candidate in (line.get("name"), line.get("id")):
        if candidate and str(candidate).lower() in names:
            declared = True
    secret = is_secret_field(
        tag=line.get("tag"),
        name=line.get("name"),
        field_id=line.get("id"),
        secret_field=declared,
        input_type=line.get("input_type"),
    )
    # Rebuild without carrying a live secret value forward.
    out: dict[str, Any] = {k: v for k, v in line.items() if k != "value"}
    # Frame URLs lose query/fragment/credentials; frame identity keeps only
    # typed keys. Defense in depth: the extension already strips both.
    for key in URL_FIELDS:
        if key in out:
            out[key] = strip_url(out[key])
    if "frame" in out:
        frame = _clean_frame(out["frame"])
        if frame is None:
            del out["frame"]
        else:
            out["frame"] = frame
    if secret:
        out["secret_field"] = True
        if "value" in line:
            out["value"] = REDACTED_VALUE
    elif "value" in line:
        out["value"] = line["value"]
    return out


class ExtensionEvidenceBuffer:
    """In-memory buffer of extension evidence lines for one recording.

    Start empty. Append lines the native host (or a test) delivers. Write
    beside the recording's other artifacts. An absent file on load means the
    extension was not used; that is normal.
    """

    def __init__(self, *, secret_names: Iterable[str] = ()) -> None:
        self._secret_names = frozenset(secret_names)
        self._lines: list[dict[str, Any]] = []

    @property
    def count(self) -> int:
        return len(self._lines)

    def append(self, line: Mapping[str, Any]) -> dict[str, Any]:
        """Redact and keep one evidence line. Returns the stored copy.

        A ``network_save`` line goes through the fail-closed network redactor;
        one it cannot redact is refused (``ValueError``) and nothing is kept.
        """

        if line.get("kind") == NETWORK_SAVE_KIND:
            network = redact_network_save(line)
            if network is None:
                raise ValueError("network_save evidence could not be redacted")
            stored = network
        else:
            stored = redact_evidence(line, secret_names=self._secret_names)
        stored.setdefault("schema", SCHEMA)
        if "seq" not in stored:
            stored["seq"] = len(self._lines)
        self._lines.append(stored)
        return stored

    def lines(self) -> list[dict[str, Any]]:
        return [dict(line) for line in self._lines]

    def write(self, recording_dir: Path | str) -> Path:
        """Write ``extension_evidence.jsonl``. Empty buffer still writes a header.

        Secret field values are forced to the plain ``"<secret>"`` literal at
        serialize time so the on-disk payload never carries a live value.
        """

        path = Path(recording_dir) / EVIDENCE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = self.lines()
        header = {"schema": SCHEMA, "events": len(lines), "source": "chrome_extension"}
        chunks = [json.dumps(header, sort_keys=True)]
        for line in lines:
            row = dict(line)
            if row.get("kind") == NETWORK_SAVE_KIND:
                network = redact_network_save(row)
                if network is None:
                    continue
                row = network
            if row.get("secret_field") and "value" in row:
                row["value"] = "<secret>"
            chunks.append(json.dumps(row, sort_keys=True))
        path.write_text("\n".join(chunks) + "\n", encoding="utf-8")
        return path


def load_extension_evidence(recording_dir: Path | str) -> list[dict[str, Any]]:
    """Load evidence lines. Missing file → empty list (extension optional)."""

    path = Path(recording_dir) / EVIDENCE_FILE
    if not path.is_file():
        return []
    rows = [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not rows:
        return []
    header = rows[0]
    if header.get("schema") != SCHEMA:
        raise ValueError(f"{path} is not a {SCHEMA} capture")
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        if row.get("kind") == NETWORK_SAVE_KIND:
            # Re-redact on load; a line that cannot be redacted is dropped.
            network = redact_network_save(row)
            if network is not None:
                out.append(network)
            continue
        out.append(redact_evidence(row))
    return out


def shadow_targets(lines: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Evidence lines that crossed an open shadow root."""

    return [
        dict(line)
        for line in lines
        if line.get("kind") == "target" and line.get("in_shadow")
    ]


def merge_target_path(
    ax_path: Optional[str],
    extension_path: Optional[str],
) -> dict[str, Any]:
    """Combine an accessibility path with optional extension shadow detail.

    Backend-agnostic: callers on any web surface may use this. The extension
    path wins for shadow detail when present; absence leaves the AX path alone.
    """

    result: dict[str, Any] = {
        "ax_path": ax_path,
        "extension_path": extension_path,
        "path": extension_path or ax_path,
        "from_extension": bool(extension_path),
    }
    return result


def validate_native_message(message: Mapping[str, Any]) -> MutableMapping[str, Any]:
    """Accept a native-messaging envelope from the extension; refuse otherwise."""

    if message.get("type") != "EXTENSION_EVIDENCE":
        raise ValueError("native message type must be EXTENSION_EVIDENCE")
    if message.get("schema") not in (None, SCHEMA):
        raise ValueError(f"schema must be {SCHEMA}")
    evidence = message.get("evidence")
    if not isinstance(evidence, Mapping):
        raise ValueError("evidence object required")
    if evidence.get("kind") == NETWORK_SAVE_KIND:
        network = redact_network_save(evidence)
        if network is None:
            raise ValueError("network_save evidence could not be redacted")
        return network
    if evidence.get("kind") not in EVIDENCE_KINDS:
        raise ValueError(
            "evidence kind must be one of: " + ", ".join(sorted(EVIDENCE_KINDS))
        )
    return redact_evidence(evidence)


def frame_key(line: Mapping[str, Any]) -> tuple[Any, ...]:
    """Stable key for the frame a line came from.

    Prefer Chrome's (tab, frame, document) identity stamped by the extension
    background; fall back to the stripped frame URL for older lines.
    """

    frame = line.get("frame")
    if isinstance(frame, Mapping) and "frame_id" in frame:
        return (
            "frame",
            frame.get("tab_id"),
            frame.get("frame_id"),
            frame.get("document_id"),
        )
    return ("url", strip_url(line.get("frame_url")))


def group_by_frame(
    lines: Iterable[Mapping[str, Any]],
) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
    """Evidence lines grouped by source frame, in arrival order."""

    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
    for line in lines:
        groups.setdefault(frame_key(line), []).append(dict(line))
    return groups


def cross_origin_frame_targets(
    lines: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Target lines from a child frame whose parent is a different origin.

    Accessibility sees these frames as opaque web areas; the extension's copy
    of the content script running inside the frame supplies the DOM detail.
    """

    return [
        dict(line)
        for line in lines
        if line.get("kind") == "target"
        and line.get("is_iframe")
        and line.get("cross_origin_parent")
    ]


def link_frame_targets(
    lines: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Attach the parent-side ``<iframe>`` path to child-frame targets.

    A parent frame emits ``frame_focus`` when focus enters a child frame
    (``frame_src`` = stripped child URL). Each later child target whose
    stripped ``frame_url`` matches the most recent ``frame_focus`` gets
    ``parent_frame_path``. Unmatched targets are returned unchanged: the link
    is extra evidence, never required.
    """

    rows = [dict(line) for line in lines]
    last_focus: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.get("kind") == "frame_focus":
            src = strip_url(row.get("frame_src"))
            if src:
                last_focus[src] = row
            continue
        if row.get("kind") != "target" or not row.get("is_iframe"):
            continue
        focus = last_focus.get(strip_url(row.get("frame_url")) or "")
        if focus is not None:
            row["parent_frame_path"] = focus.get("path")
            row["parent_frame_url"] = strip_url(focus.get("frame_url"))
    return rows
