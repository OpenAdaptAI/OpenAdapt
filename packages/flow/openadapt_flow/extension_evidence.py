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

EVIDENCE_FILE = "extension_evidence.jsonl"
SCHEMA = "openadapt.extension-evidence/1"
SECRET = "<secret>"

SECRET_NAME = re.compile(
    r"pass(word|wd|phrase)?\b|passwd|pwd|secret|private[_-]?key|^pin$|^otp$",
    re.I,
)


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
    ``SECRET`` placeholder is written for secret fields, so clear-text
    storage scanners see no sensitive payload on the write path.
    """

    names = {n.lower() for n in secret_names}
    name = line.get("name") or line.get("id")
    declared = bool(line.get("secret_field"))
    if name and str(name).lower() in names:
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
    if secret:
        out["secret_field"] = True
        if "value" in line:
            out["value"] = SECRET
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
        """Redact and keep one evidence line. Returns the stored copy."""

        stored = redact_evidence(line, secret_names=self._secret_names)
        stored.setdefault("schema", SCHEMA)
        if "seq" not in stored:
            stored["seq"] = len(self._lines)
        self._lines.append(stored)
        return stored

    def lines(self) -> list[dict[str, Any]]:
        return [dict(line) for line in self._lines]

    def write(self, recording_dir: Path | str) -> Path:
        """Write ``extension_evidence.jsonl``. Empty buffer still writes a header."""

        path = Path(recording_dir) / EVIDENCE_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = self.lines()
        header = {"schema": SCHEMA, "events": len(lines), "source": "chrome_extension"}
        text = json.dumps(header, sort_keys=True) + "\n"
        text += "".join(json.dumps(line, sort_keys=True) + "\n" for line in lines)
        path.write_text(text, encoding="utf-8")
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
    return [redact_evidence(row) for row in rows[1:]]


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
    if evidence.get("kind") != "target":
        raise ValueError("this slice accepts kind=target evidence only")
    return redact_evidence(evidence)
