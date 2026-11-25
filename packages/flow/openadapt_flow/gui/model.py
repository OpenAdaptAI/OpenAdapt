"""A neutral view of one screen, and how a pack names a control on it.

Every backend reads a screen differently: a browser has a DOM, a desktop app
an accessibility tree, a spreadsheet a grid of cells. The mechanisms in this
package read only what they all share:

- :class:`Control`: one thing a person can act on or read, with a normalised
  ``role`` (:data:`ROLES`), its accessible ``name``, its value or checked
  state, and the chain of :class:`Container` objects it sits in (a fieldset,
  a card, a table row, a dialog), outermost first.
- :class:`Dialog`: an open dialog, and the controls inside it.
- :class:`Screen`: the controls, dialogs and containers seen at one moment.
  ``complete`` is False when the backend capped or could not finish the read;
  a partial screen can refute a check, never hold one.

A :class:`ControlSpec` is pack data that names exactly one control by role,
name, containers and (for a link) target. :func:`match_one` resolves it; zero
or several matches are different failures, and neither picks a control.

:class:`DemoStep` is one action of a demonstration with the screen before and
after it and the number of writes the backend counted in between.
:class:`Actuator` is what a mechanism needs from a live backend.

Nothing here names an application or holds a word list.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Literal, Optional, Protocol, runtime_checkable

__all__ = [
    "ACTIVATABLE",
    "EDITABLE",
    "ROLES",
    "Actuator",
    "AmbiguousControl",
    "Container",
    "Control",
    "ControlSpec",
    "DemoStep",
    "Dialog",
    "MissingControl",
    "Recorder",
    "Screen",
    "match_all",
    "match_one",
    "norm",
    "screen_signature",
    "spec_for",
    "write_outcomes_of",
]

#: The roles a backend maps its controls to. Anything else is ``other``.
ROLES = frozenset(
    {
        "textbox",
        "checkbox",
        "switch",
        "radio",
        "combobox",
        "listbox",
        "button",
        "link",
        "tab",
        "menuitem",
        "cell",
        "text",
        "other",
    }
)
#: Roles a person activates to move on, open something or commit.
ACTIVATABLE = frozenset({"button", "link", "tab", "menuitem"})
#: Roles that hold a value a person enters.
EDITABLE = frozenset(
    {"textbox", "checkbox", "switch", "radio", "combobox", "listbox", "cell"}
)


def norm(text: object) -> str:
    """Whitespace collapsed and case folded: how names are compared."""

    return " ".join(str(text or "").split()).casefold()


@dataclass(frozen=True)
class Container:
    """A labelled or structural region around controls.

    ``handle`` is the backend's opaque id for it, stable within one screen
    read and across reads of the same element. ``kind`` is what it is
    structurally (``group``, ``row``, ``dialog``, ``region``, ``item``,
    ``form``); ``name`` its own label or heading, possibly empty; ``text`` its
    visible text, capped by the backend."""

    handle: str
    kind: str = "group"
    name: str = ""
    text: str = ""


@dataclass(frozen=True)
class Control:
    """One control. ``handle`` is opaque to every mechanism: only the backend
    that made it acts on it."""

    handle: str
    role: str
    name: str = ""
    value: str = ""
    checked: Optional[bool] = None
    disabled: bool = False
    readonly: bool = False
    required: bool = False
    #: Containers, outermost first. Dialogs appear here too.
    containers: tuple[Container, ...] = ()
    #: Handle of the open dialog that holds the control, or "".
    dialog: str = ""
    #: Where a link goes, as the backend reports it (opaque), or "".
    target: str = ""
    #: The control's own error message, if the backend shows one.
    error: str = ""

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")

    @property
    def editable(self) -> bool:
        return self.role in EDITABLE and not self.disabled and not self.readonly

    @property
    def activatable(self) -> bool:
        return self.role in ACTIVATABLE and not self.disabled

    def container_names(self) -> tuple[str, ...]:
        return tuple(norm(c.name) for c in self.containers if c.name)


@dataclass(frozen=True)
class Dialog:
    handle: str
    name: str = ""
    text: str = ""
    #: ``modal`` when the backend says the dialog blocks the rest of the screen.
    modal: bool = True


@dataclass(frozen=True)
class Screen:
    controls: tuple[Control, ...] = ()
    dialogs: tuple[Dialog, ...] = ()
    #: Where the screen is (a URL path, a window title, a sheet name): opaque.
    location: str = ""
    title: str = ""
    text: str = ""
    #: False when the backend capped the read or could not finish it.
    complete: bool = True
    #: Banner or summary errors the backend shows outside any one control.
    errors: tuple[str, ...] = ()

    def control(self, handle: str) -> Optional[Control]:
        for ctrl in self.controls:
            if ctrl.handle == handle:
                return ctrl
        return None

    def containers(self) -> dict[str, Container]:
        out: dict[str, Container] = {}
        for ctrl in self.controls:
            for box in ctrl.containers:
                out.setdefault(box.handle, box)
        return out

    def in_dialog(self, dialog: str) -> tuple[Control, ...]:
        return tuple(c for c in self.controls if c.dialog == dialog)


def screen_signature(screen: Screen) -> tuple[object, ...]:
    """What has to change for a step to count as having moved the screen:
    the location, the title, the open dialogs and the editable controls (by
    role, name and handle). A value typed into a field does not change it."""

    return (
        screen.location,
        screen.title,
        tuple(sorted(d.handle for d in screen.dialogs)),
        tuple((c.role, norm(c.name), c.handle) for c in screen.controls if c.editable),
    )


# -- naming a control -------------------------------------------------------------


class MissingControl(LookupError):
    """No control on the screen matches the spec."""


class AmbiguousControl(LookupError):
    """More than one control matches the spec."""


@dataclass(frozen=True)
class ControlSpec:
    """Pack data naming one control.

    ``role`` is one of :data:`ROLES`, or ``"activatable"`` for any of
    :data:`ACTIVATABLE` (a step that one version of an app draws as a button
    and another as a link). ``name`` is compared whole after :func:`norm`, never
    as a substring. ``containers`` must appear in the control's container names
    in this order (other containers may sit between them). ``target``, when
    set, must equal the link's target exactly. ``dialog`` says whether the
    control must be inside an open dialog (True), outside every dialog
    (False) or either (None)."""

    role: str
    name: str
    containers: tuple[str, ...] = ()
    target: Optional[str] = None
    dialog: Optional[bool] = None

    def __post_init__(self) -> None:
        if self.role != "activatable" and self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")
        if not norm(self.name):
            raise ValueError("a control spec needs a name")

    def matches(self, ctrl: Control) -> bool:
        if self.role == "activatable":
            if ctrl.role not in ACTIVATABLE:
                return False
        elif ctrl.role != self.role:
            return False
        if norm(ctrl.name) != norm(self.name):
            return False
        if self.target is not None and ctrl.target != self.target:
            return False
        if self.dialog is not None and bool(ctrl.dialog) != self.dialog:
            return False
        return _in_order(
            tuple(norm(c) for c in self.containers), ctrl.container_names()
        )


def _in_order(wanted: tuple[str, ...], have: tuple[str, ...]) -> bool:
    at = 0
    for name in have:
        if at < len(wanted) and name == wanted[at]:
            at += 1
    return at == len(wanted)


def match_all(spec: ControlSpec, screen: Screen) -> tuple[Control, ...]:
    return tuple(c for c in screen.controls if spec.matches(c))


def match_one(spec: ControlSpec, screen: Screen) -> Control:
    """The one control ``spec`` names. Raises :class:`MissingControl` or
    :class:`AmbiguousControl`; it never picks the first of several."""

    found = match_all(spec, screen)
    if not found:
        raise MissingControl(f"no {spec.role} named {spec.name!r}")
    if len(found) > 1:
        raise AmbiguousControl(f"{len(found)} controls match {spec.role} {spec.name!r}")
    return found[0]


def spec_for(
    ctrl: Control,
    screen: Screen,
    *,
    role: Optional[str] = None,
) -> Optional[ControlSpec]:
    """The narrowest spec that names ``ctrl`` alone on ``screen``: its role and
    name, then its labelled containers and its link target as needed. None
    when even all of them still match another control (two identical rows
    with no label between them): the pack author has to say which."""

    base = ControlSpec(
        role=role or ctrl.role,
        name=ctrl.name,
        dialog=bool(ctrl.dialog),
    )
    names = tuple(c.name for c in ctrl.containers if c.name)
    tries = [base]
    for n in range(1, len(names) + 1):
        tries.append(ControlSpec(base.role, base.name, names[-n:], dialog=base.dialog))
    if ctrl.target:
        tries += [
            ControlSpec(t.role, t.name, t.containers, ctrl.target, t.dialog)
            for t in tries
        ]
    for spec in tries:
        if match_all(spec, screen) == (ctrl,):
            return spec
    return None


# -- demonstrations and live backends ---------------------------------------------

ActionKind = Literal["click", "type", "check", "uncheck", "select"]


@dataclass(frozen=True)
class DemoStep:
    """One action of a demonstration.

    ``target`` is the control as seen in ``before``. ``writes`` is how many
    writes the backend counted between ``before`` and ``after`` (requests that
    change state, a file save, a cell committed), or None when it could not
    count them. ``write_outcomes`` is what each of those writes returned
    (``"2xx"``, ``"redirect /done"``), or None when the backend does not say."""

    action: ActionKind
    target: Control
    before: Screen
    after: Screen
    writes: Optional[int]
    value: str = ""
    write_outcomes: Optional[tuple[str, ...]] = None


@runtime_checkable
class Actuator(Protocol):
    """What a mechanism needs from a live backend. Every action waits for the
    screen to settle before it returns."""

    def observe(self) -> Screen: ...

    def click(self, handle: str) -> None: ...

    def set_text(self, handle: str, value: str) -> None: ...

    def set_checked(self, handle: str, checked: bool) -> None: ...

    def write_count(self) -> Optional[int]:
        """Writes counted since the backend started, or None if it cannot
        count them. A backend that cannot count can never show a commit."""
        ...


def write_outcomes_of(actuator: object) -> Optional[tuple[str, ...]]:
    """What each write so far returned, from a backend's optional
    ``write_outcomes()``, or None when it does not report them."""
    fn = getattr(actuator, "write_outcomes", None)
    got = fn() if callable(fn) else None
    return tuple(got) if isinstance(got, (tuple, list)) else None


@dataclass
class Recorder:
    """Wrap an :class:`Actuator` and keep every action as a :class:`DemoStep`.

    Used to record a demonstration through the same backend a run uses."""

    actuator: Actuator
    steps: list[DemoStep] = field(default_factory=list)

    def _do(
        self,
        action: ActionKind,
        handle: str,
        run: Callable[[], None],
        value: str = "",
    ) -> None:
        before = self.actuator.observe()
        target = before.control(handle)
        if target is None:
            raise MissingControl(handle)
        o0 = write_outcomes_of(self.actuator)
        w0 = self.actuator.write_count()
        run()
        after = self.actuator.observe()
        # Read after the screen settles, as a run does, so a late write is
        # counted in the demo too.
        w1 = self.actuator.write_count()
        writes = None if w0 is None or w1 is None else w1 - w0
        o1 = write_outcomes_of(self.actuator)
        outcomes = None if o0 is None or o1 is None else o1[len(o0) :]
        self.steps.append(
            DemoStep(action, target, before, after, writes, value, outcomes)
        )

    def click(self, handle: str) -> None:
        self._do("click", handle, lambda: self.actuator.click(handle))

    def set_text(self, handle: str, value: str) -> None:
        self._do("type", handle, lambda: self.actuator.set_text(handle, value), value)

    def set_checked(self, handle: str, checked: bool) -> None:
        self._do(
            "check" if checked else "uncheck",
            handle,
            lambda: self.actuator.set_checked(handle, checked),
        )
