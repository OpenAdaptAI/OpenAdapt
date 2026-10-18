"""A structural, privacy-safe fingerprint of one application version.

Two tenants can share a structural patch only when they run the same version
of the same application. This module says what "the same version" means
without reading anything a person typed or any record the app shows:

* **application family and confirmed version.** The family is an
  operator-declared product id (``availity/payer-portal``). The version is the
  one the operator confirmed in Check it, as recorded by the existing
  qualification environment observation
  (:class:`~openadapt_flow.qualification_environment.QualificationEnvironmentObservation`,
  #498). Only its digest is kept. The observation's origin and session digest
  are deliberately left out: a self-hosted EHR's address names the tenant, and
  a session digest is per user.
* **DOM skeleton.** For a web page: element names, the ``type`` and ``role``
  attributes, and the sorted *names* of other attributes. Every text node and
  every other attribute value is dropped, including ``value``, ``id``,
  ``href``, ``title`` and ``aria-label``. A run of identical sibling subtrees
  (table rows, list items) collapses to one, so the skeleton does not change
  with the number of records on screen and does not reveal it.
* **Window class hierarchy.** For a native app: each node's control type and
  window class name only, never its name, value, or automation id.
* **Executable hash.** The SHA-256 of the application binary, where the
  caller can read it.

The DOM page-structure identity of #503 reads the opposite: a field group's
label text and the values of its other controls. Nothing that read produces
enters a fingerprint.
"""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Literal, Mapping, Optional, Sequence

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.qualification_environment import (
    QualificationEnvironmentObservation,
)

FINGERPRINT_SCHEMA: Literal["openadapt.fleet.app-fingerprint/v1"] = (
    "openadapt.fleet.app-fingerprint/v1"
)
_DOMAIN = b"openadapt.fleet.app-fingerprint/v1\x00"
_VERSION_DOMAIN = b"openadapt.fleet.app-version/v1\x00"
_DOM_DOMAIN = b"openadapt.fleet.dom-skeleton/v1\x00"
_WINDOW_DOMAIN = b"openadapt.fleet.window-hierarchy/v1\x00"

_SHA256 = r"^[a-f0-9]{64}$"
APP_FAMILY_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,63}/[a-z0-9][a-z0-9._-]{0,63}$"

TargetKind = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]

#: Attributes whose VALUE is part of the skeleton. Both are small closed
#: vocabularies defined by HTML and ARIA, not by the data on the page.
_VALUE_ATTRIBUTES = frozenset({"type", "role"})
_TOKEN_SAFE = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-_")
#: Elements whose content is never structure.
_SKIPPED_ELEMENTS = frozenset({"script", "style", "template", "noscript"})
_VOID_ELEMENTS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_MAX_DEPTH = 64
_MAX_NODES = 50_000


def _digest(domain: bytes, value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(domain + payload).hexdigest()


def version_sha256(app_family: str, confirmed_version: str) -> str:
    """Digest of one family's operator-confirmed version string."""
    return _digest(_VERSION_DOMAIN, [app_family, confirmed_version])


def _safe_token(value: str) -> Optional[str]:
    token = value.strip().lower()
    if not token or len(token) > 32 or any(ch not in _TOKEN_SAFE for ch in token):
        return None
    return token


# --------------------------------------------------------------------------- #
# DOM skeleton
# --------------------------------------------------------------------------- #


class _Node:
    __slots__ = ("label", "children")

    def __init__(self, label: str) -> None:
        self.label = label
        self.children: list[_Node] = []

    def render(self) -> Any:
        children = [child.render() for child in self.children]
        collapsed: list[Any] = []
        for child in children:
            # A run of identical siblings (rows, list items) is one entry.
            if collapsed and collapsed[-1] == child:
                continue
            collapsed.append(child)
        return [self.label, collapsed] if collapsed else [self.label]


def _element_label(tag: str, attrs: Sequence[tuple[str, Optional[str]]]) -> str:
    names: set[str] = set()
    kept: list[str] = []
    for raw_name, raw_value in attrs:
        name = raw_name.lower()
        if name in _VALUE_ATTRIBUTES and raw_value is not None:
            token = _safe_token(raw_value)
            kept.append(f"{name}={token}" if token else name)
        elif _safe_token(name) is not None or name.startswith("data-"):
            # Attribute NAMES are markup, never data. A data-* name can still
            # carry a record key in a hostile page (data-mrn-00123), so a
            # name with a digit run is reduced to its prefix.
            names.add(name if not any(ch.isdigit() for ch in name) else "data-*")
    return "|".join([tag.lower(), *sorted(kept), *sorted(names)])


class _SkeletonParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#document")
        self._stack: list[_Node] = [self.root]
        self._skip_depth = 0
        self._count = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if self._skip_depth:
            if tag not in _VOID_ELEMENTS:
                self._skip_depth += 1
            return
        if tag in _SKIPPED_ELEMENTS:
            self._skip_depth = 1
            return
        self._count += 1
        if self._count > _MAX_NODES:
            raise ValueError("page is too large to fingerprint")
        node = _Node(_element_label(tag, attrs))
        self._stack[-1].children.append(node)
        if tag not in _VOID_ELEMENTS and len(self._stack) < _MAX_DEPTH:
            self._stack.append(node)

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, Optional[str]]]
    ) -> None:
        if self._skip_depth:
            return
        self._count += 1
        if self._count > _MAX_NODES:
            raise ValueError("page is too large to fingerprint")
        self._stack[-1].children.append(_Node(_element_label(tag.lower(), attrs)))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._skip_depth:
            self._skip_depth -= 1
            return
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].label.split("|", 1)[0] == tag:
                del self._stack[index:]
                return

    # Text, comments and processing instructions carry no structure.
    def handle_data(self, data: str) -> None:  # noqa: D401 - parser hook
        return None


def dom_skeleton(html: str) -> list[Any]:
    """Normalized, text-free skeleton of an HTML document (nested lists)."""
    parser = _SkeletonParser()
    parser.feed(html)
    parser.close()
    return parser.root.render()


def dom_skeleton_sha256(html: str) -> str:
    return _digest(_DOM_DOMAIN, dom_skeleton(html))


# --------------------------------------------------------------------------- #
# Native window class hierarchy
# --------------------------------------------------------------------------- #


def window_hierarchy(tree: Mapping[str, Any], *, _depth: int = 0) -> list[Any]:
    """Control type and class name of each node; names and ids are dropped.

    ``tree`` uses the node shape the native authoring path already builds
    (``control_type``, ``class_name``, ``children``). Any other key, such as
    ``name`` or ``automation_id``, is ignored.
    """
    if _depth > _MAX_DEPTH:
        raise ValueError("window tree is too deep to fingerprint")
    control = str(tree.get("control_type") or "")[:64]
    class_name = str(tree.get("class_name") or "")[:64]
    children = tree.get("children") or []
    rendered: list[Any] = []
    for child in children:
        if not isinstance(child, Mapping):
            continue
        item = window_hierarchy(child, _depth=_depth + 1)
        if rendered and rendered[-1] == item:
            continue
        rendered.append(item)
    label = f"{control}|{class_name}"
    return [label, rendered] if rendered else [label]


def window_hierarchy_sha256(tree: Mapping[str, Any]) -> str:
    return _digest(_WINDOW_DOMAIN, window_hierarchy(tree))


def executable_sha256(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# The fingerprint
# --------------------------------------------------------------------------- #


class AppVersionFingerprint(BaseModel):
    """Structural identity of one application version. Digests only."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.fleet.app-fingerprint/v1"] = FINGERPRINT_SCHEMA
    app_family: str = Field(pattern=APP_FAMILY_PATTERN)
    target_kind: TargetKind
    version_sha256: str = Field(pattern=_SHA256)
    dom_skeleton_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    window_hierarchy_sha256: Optional[str] = Field(default=None, pattern=_SHA256)
    executable_sha256: Optional[str] = Field(default=None, pattern=_SHA256)

    def fingerprint_sha256(self) -> str:
        return _digest(_DOMAIN, self.model_dump(mode="json"))

    def structural_components(self) -> dict[str, str]:
        return {
            name: value
            for name, value in (
                ("dom_skeleton_sha256", self.dom_skeleton_sha256),
                ("window_hierarchy_sha256", self.window_hierarchy_sha256),
                ("executable_sha256", self.executable_sha256),
            )
            if value is not None
        }

    def mismatch(self, other: "AppVersionFingerprint") -> Optional[str]:
        """Why ``other`` is not the same app version, or None when it is.

        Family, target kind, and confirmed version must be equal. Every
        structural component that both sides measured must be equal, and at
        least one must be measured by both: a version string alone is the
        operator's word, not an observation.
        """
        if self.app_family != other.app_family:
            return "different application family"
        if self.target_kind != other.target_kind:
            return "different target kind"
        if self.version_sha256 != other.version_sha256:
            return "different confirmed application version"
        mine = self.structural_components()
        theirs = other.structural_components()
        shared = set(mine) & set(theirs)
        if not shared:
            return "no structural component measured on both sides"
        for name in sorted(shared):
            if mine[name] != theirs[name]:
                return f"different {name.removesuffix('_sha256').replace('_', ' ')}"
        return None


def fingerprint_from_observation(
    observation: QualificationEnvironmentObservation,
    *,
    app_family: str,
    html: Optional[str] = None,
    window_tree: Optional[Mapping[str, Any]] = None,
    executable: Optional[Path | str] = None,
) -> AppVersionFingerprint:
    """Build a fingerprint from a Check-it environment observation.

    Uses the observation's target kind and confirmed version. Its application
    identity (a web origin) and session digest are not used; see the module
    docstring.
    """
    return AppVersionFingerprint(
        app_family=app_family,
        target_kind=observation.target_kind,
        version_sha256=version_sha256(app_family, observation.application_version),
        dom_skeleton_sha256=dom_skeleton_sha256(html) if html is not None else None,
        window_hierarchy_sha256=(
            window_hierarchy_sha256(window_tree) if window_tree is not None else None
        ),
        executable_sha256=(
            executable_sha256(executable) if executable is not None else None
        ),
    )


__all__ = [
    "AppVersionFingerprint",
    "dom_skeleton",
    "dom_skeleton_sha256",
    "executable_sha256",
    "fingerprint_from_observation",
    "version_sha256",
    "window_hierarchy",
    "window_hierarchy_sha256",
]
