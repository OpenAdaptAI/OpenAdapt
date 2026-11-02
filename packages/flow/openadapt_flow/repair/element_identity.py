"""Which control a repair's new click hits, on any backend, and when to admit it.

A repair's bridge adds new clicks to a workflow: dismiss an update notice,
close a new dialog. Each new click needs something that says it is the
control the fix meant. The browser reads it from the DOM: the click must sit
inside the record the recording verified. Native apps expose an
accessibility tree instead (Windows UI Automation, macOS AX, Linux AT-SPI),
and remote displays (RDP, Citrix) expose only pixels.

This module gives every backend one rule set (docs/REPAIR_ON_EVERY_BACKEND.md):

- :class:`ElementIdentity`: an element's role, name, ancestry and window, from
  whatever tree the backend reads. A backend implements
  ``element_identity_at(x, y)`` and returns a mapping in this shape.
- :func:`backend_kind`: ``dom``, ``accessibility`` or ``pixel``, from what the
  backend can do.
- :func:`record_check_cover`: whether a system-of-record check covers the
  workflow's saves, so a click the screen can't identify is still safe.
- :func:`admit_new_click`: the decision for one new click.

The browser rule is unchanged. Everything here fails closed: a read that
fails, a second matching element, or a missing record check refuses the
click, and the run stays stopped for a person.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Mapping, Optional

__all__ = [
    "BASIS_ELEMENT_AND_RECORD_CHECK",
    "BASIS_RECORD_CHECK",
    "BASIS_RECORD_IDENTITY",
    "ClickAdmission",
    "ClickRefused",
    "ElementIdentity",
    "admit_new_click",
    "backend_kind",
    "read_element_identity",
    "record_check_cover",
]

BackendKind = Literal["dom", "accessibility", "pixel"]

#: The click sits in a record the recording verified (the browser's rule).
BASIS_RECORD_IDENTITY = "record_identity"
#: A unique tree element in the pinned window, and a record check on the save.
BASIS_ELEMENT_AND_RECORD_CHECK = "element_and_record_check"
#: Pixels only: nothing on screen identifies the click; the record check does.
BASIS_RECORD_CHECK = "record_check"
#: The bases that rest on the record check rather than on screen identity. A
#: fix admitted on one always needs a person's approval.
NEEDS_PERSON = frozenset({BASIS_ELEMENT_AND_RECORD_CHECK, BASIS_RECORD_CHECK})

SOURCES = frozenset({"dom", "uia", "ax", "atspi"})
#: Ancestor roles that mark a dialog, sheet or alert, across trees.
DIALOG_ROLES = frozenset(
    {
        "dialog",
        "alertdialog",
        "AXSheet",
        "AXDialog",
        "AXSystemDialog",
        "AXFloatingWindow",
    }
)
_PRODUCTION_PROFILES = frozenset({"standard", "regulated"})
_MAX_ANCESTRY = 32
#: Accessibility reads a backend may have without element identity.
_TREE_READS = ("structural_locator_at", "structured_text_at", "locate_structural")


class ClickRefused(Exception):
    """A new click is not admitted. Nothing was clicked because of it."""


def _text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text or None


@dataclass(frozen=True)
class ElementIdentity:
    """One element of an accessibility or DOM tree, as a repair records it.

    ``ancestry`` is (role, name) pairs, parent first, ending at the top-level
    window; a role the tree doesn't map is ``""``. ``window`` is that
    top-level window (for an owned dialog, its owner; the dialog is in
    ``ancestry``). ``dialog_region`` is the pixel box of the nearest dialog-like
    ancestor, in the backend's click coordinates, when the backend reads it.
    """

    source: str
    role: Optional[str]
    name: Optional[str]
    automation_id: Optional[str]
    ancestry: tuple[tuple[str, Optional[str]], ...]
    window: str
    process: Optional[str] = None
    dialog_region: Optional[tuple[int, int, int, int]] = field(
        default=None, compare=False
    )

    @classmethod
    def from_mapping(cls, value: Any) -> Optional["ElementIdentity"]:
        """Validate a backend's answer; None for anything malformed."""

        if isinstance(value, ElementIdentity):
            return value
        if not isinstance(value, Mapping):
            return None
        source = value.get("source")
        role = _text(value.get("role"))
        window = _text(value.get("window"))
        raw_ancestry = value.get("ancestry")
        automation_id = _text(value.get("automation_id"))
        if source not in SOURCES or window is None:
            return None
        if role is None and automation_id is None:
            return None
        if not isinstance(raw_ancestry, (list, tuple)) or not raw_ancestry:
            return None
        if len(raw_ancestry) > _MAX_ANCESTRY:
            return None
        ancestry: list[tuple[str, Optional[str]]] = []
        for entry in raw_ancestry:
            if not isinstance(entry, (list, tuple)) or len(entry) != 2:
                return None
            # A tree may not map every ancestor's role (UIA control types
            # without an ARIA role), so an entry needs a role or a name.
            entry_role, entry_name = _text(entry[0]), _text(entry[1])
            if entry_role is None and entry_name is None:
                return None
            ancestry.append((entry_role or "", entry_name))
        region = value.get("dialog_region")
        dialog_region = None
        if region is not None:
            if (
                not isinstance(region, (list, tuple))
                or len(region) != 4
                or not all(
                    isinstance(v, int) and not isinstance(v, bool) for v in region
                )
                or region[2] <= 0
                or region[3] <= 0
            ):
                return None
            dialog_region = (region[0], region[1], region[2], region[3])
        return cls(
            source=str(source),
            role=role,
            name=_text(value.get("name")),
            automation_id=automation_id,
            ancestry=tuple(ancestry),
            window=window,
            process=_text(value.get("process")),
            dialog_region=dialog_region,
        )

    def locatable(self) -> bool:
        """An id, or a role with a name: something replay can find again."""

        return bool(self.automation_id or (self.role and self.name))

    def ends_at_window(self) -> bool:
        """The ancestry reaches the pinned window by its exact name."""

        return bool(self.ancestry) and self.ancestry[-1][1] == self.window

    def dialog(self) -> Optional[tuple[str, Optional[str]]]:
        """The nearest dialog-like ancestor, or None."""

        return next((a for a in self.ancestry if a[0] in DIALOG_ROLES), None)

    def locator(self) -> Any:
        """The structural locator the new step stores for replay."""

        from openadapt_flow.ir import StructuralLocator

        return StructuralLocator(
            automation_id=self.automation_id,
            role=self.role,
            name=self.name,
            window_name=self.window,
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "role": self.role,
            "name": self.name,
            "automation_id": self.automation_id,
            "ancestry": [list(a) for a in self.ancestry],
            "window": self.window,
            "process": self.process,
        }

    def digest(self) -> str:
        canonical = json.dumps(self.evidence(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()


def backend_kind(backend: Any) -> BackendKind:
    """``dom``, ``accessibility`` or ``pixel``, from what ``backend`` can do.

    Raises :class:`ClickRefused` for a backend that reads an accessibility
    tree but not element identity: a broken tree read must not fall to the
    pixel rule.
    """

    if getattr(backend, "page", None) is not None:
        return "dom"
    if callable(getattr(backend, "element_identity_at", None)):
        return "accessibility"
    if any(callable(getattr(backend, name, None)) for name in _TREE_READS):
        raise ClickRefused(
            f"{type(backend).__name__} reads an accessibility tree but can't "
            "read element identity yet, so a new click can't be identified"
        )
    return "pixel"


def read_element_identity(backend: Any, point: tuple[int, int]) -> ElementIdentity:
    """The element identity under ``point``, or :class:`ClickRefused`."""

    reader = getattr(backend, "element_identity_at", None)
    if not callable(reader):
        raise ClickRefused("the backend can't read element identity")
    try:
        raw = reader(int(point[0]), int(point[1]))
    except Exception:  # noqa: BLE001 - an unreadable element is refused
        raw = None
    identity = ElementIdentity.from_mapping(raw)
    if identity is None:
        raise ClickRefused("no element identity could be read under the click")
    if not identity.locatable():
        raise ClickRefused(
            f"the element under the click ({identity.role}) has no name or id, "
            "so replay could not find it again"
        )
    if not identity.ends_at_window():
        raise ClickRefused(
            "the element under the click is not inside the pinned window "
            f"{identity.window!r}"
        )
    return identity


def _replay_locator(
    backend: Any, identity: ElementIdentity, point: tuple[int, int]
) -> Any:
    """The locator replay will use: the backend's own for this point.

    A backend scopes its locator its own way (Windows names the innermost
    window, the identity the outermost), so the backend's locator is checked
    and stored. It must name the same element as the identity. A backend
    without one uses the identity's.
    """

    reader = getattr(backend, "structural_locator_at", None)
    if not callable(reader):
        return identity.locator()
    try:
        locator = reader(int(point[0]), int(point[1]))
    except Exception:  # noqa: BLE001 - no locator is refused below
        locator = None
    if locator is None:
        raise ClickRefused("the backend gives no locator for the element")
    if (
        (locator.role or None) != identity.role
        or (locator.name or None) != identity.name
        or (locator.automation_id or None) != identity.automation_id
    ):
        raise ClickRefused(
            "the backend's locator for the click names a different element "
            "than its element identity"
        )
    return locator


def _unique_here(backend: Any, locator: Any, point: tuple[int, int]):
    """The structural handle for ``locator``; refuses a miss or a second match."""

    from openadapt_flow.backend import StructuralResolutionRefused

    locate = getattr(backend, "locate_structural", None)
    if not callable(locate):
        raise ClickRefused("the backend can't check that the element is unique")
    try:
        handle = locate(locator)
    except StructuralResolutionRefused as exc:
        raise ClickRefused(f"the element is not unique in its window: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - an unreadable tree is refused
        raise ClickRefused(f"the element could not be found again: {exc}") from exc
    if handle is None or getattr(handle, "candidate_count", 1) != 1:
        raise ClickRefused("the element could not be found again by its locator")
    region = getattr(handle, "region", None)
    if region is not None:
        x, y, w, h = region
        if not (x <= point[0] < x + w and y <= point[1] < y + h):
            raise ClickRefused(
                "the element found by the locator is not the one under the click"
            )
    return handle


def record_check_cover(workflow: Any, target: Any) -> Optional[str]:
    """Why no record check covers ``workflow`` on ``target``, or None if one does.

    Covered means: the run has a system-of-record verifier; its profile makes
    the verifier mandatory; there is at least one save; and every save
    declares confirmed system-of-record effects on each path and has its own
    identity check armed.
    """

    from openadapt_flow.policy import (
        all_effect_paths_covered,
        has_unconfirmed_effect_binding,
        is_identity_armed,
    )
    from openadapt_flow.run_gate import is_consequential

    if getattr(target, "effect_verifier", None) is None:
        return "the run has no system-of-record verifier"
    profile = getattr(target, "profile", None)
    if profile not in _PRODUCTION_PROFILES:
        return (
            f"the run's profile is {profile!r}; a record check is enforced only "
            "under standard or regulated"
        )
    saves = [s for s in workflow.steps if is_consequential(s, workflow)]
    if not saves:
        return "the workflow has no save for a record check to cover"
    for step in saves:
        if not all_effect_paths_covered(step) or has_unconfirmed_effect_binding(step):
            return f"{step.id} has no confirmed system-of-record effect contract"
        if not is_identity_armed(step):
            return f"{step.id} has no armed identity check of its own"
    return None


@dataclass(frozen=True)
class ClickAdmission:
    """Why a new click was admitted, and what the new step carries."""

    basis: str
    identity_template: Any = None
    element: Optional[ElementIdentity] = None
    locator: Any = None

    @property
    def needs_person(self) -> bool:
        return self.basis in NEEDS_PERSON

    def structural(self, fallback: Any) -> Any:
        """The locator the new step stores: the checked one, else ``fallback``."""

        return self.locator if self.locator is not None else fallback

    def describe(self) -> str:
        if self.element is None:
            return self.basis
        return f"{self.basis} ({self.element.role} {self.element.name!r})"


def admit_new_click(
    backend: Any,
    point: tuple[int, int],
    *,
    record_identity: Callable[[], Any],
    cover: Callable[[], Optional[str]],
) -> ClickAdmission:
    """Admit one new bridge click, or raise :class:`ClickRefused`.

    ``record_identity`` returns the click's record identity template or
    raises (today's rule). When it succeeds the click is admitted on that
    basis on any backend. Otherwise a DOM backend refuses, as before; an
    accessibility backend needs a readable, unique element identity and a
    record check; a pixel backend needs a record check.
    """

    try:
        return ClickAdmission(
            BASIS_RECORD_IDENTITY, identity_template=record_identity()
        )
    except Exception as exc:  # noqa: BLE001 - the reason is carried below
        unverified = str(exc) or type(exc).__name__
    kind = backend_kind(backend)
    if kind == "dom":
        raise ClickRefused(unverified)
    why_not = cover()
    if why_not is not None:
        article = "an" if kind == "accessibility" else "a"
        raise ClickRefused(
            f"{unverified}; on {article} {kind} backend a new click needs a record check "
            f"instead, and none covers this workflow: {why_not}"
        )
    if kind == "accessibility":
        element = read_element_identity(backend, point)
        locator = _replay_locator(backend, element, point)
        _unique_here(backend, locator, point)
        return ClickAdmission(
            BASIS_ELEMENT_AND_RECORD_CHECK, element=element, locator=locator
        )
    return ClickAdmission(BASIS_RECORD_CHECK)
