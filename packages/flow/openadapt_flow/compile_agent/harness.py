"""The harness a computer-use agent works in during run 1.

The agent reads the page's structure first (:mod:`.observe`) and acts on refs.
A screenshot goes into its context only when it asks for one, or at a gate.
Every action still goes through
:class:`~openadapt_flow.jit.adapters.CaptureComputer`, so the run is recorded
and compiled exactly as any other JIT capture.

The save gate. Before any action that commits a write (a click on a control
labelled Save, Submit, Sign, Approve, Confirm, Send or Post, or Enter in a
form field), the harness:

1. checks that every identity string the operator named for this task (for
   example ``MRN P1``) is on the screen. If one is missing, the action is not
   performed and the run ends with :class:`GateRefused`: no draft is made.
2. takes a screenshot as evidence of what was on screen when the write was
   committed;
3. asks the hold, when one is given (a person at a terminal, or an approval
   callback). A hold that says no refuses the action the same way.

This is the right-record check and the hold before saving from the guarded
save design. It runs in the agent's run only; the compiled program has its own
identity checks and reads every save back from the system of record.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.compile_agent import observe as _observe
from openadapt_flow.jit.adapters import CaptureComputer, Observation, translate

__all__ = [
    "GateRefused",
    "GateRequest",
    "Harness",
    "HarnessStats",
    "SaveGate",
    "StaleRef",
    "SurfaceControl",
    "SurfaceHarness",
    "SurfaceObservation",
]


class GateRefused(RuntimeError):
    """A write was not committed: the wrong record was on screen, or the hold
    said no. Nothing in the refused action was performed."""


class StaleRef(ValueError):
    """The ref is not in the latest observation, or the page changed under it."""


@dataclass(frozen=True)
class GateRequest:
    """What the hold is asked to approve."""

    control: str
    identity: tuple[str, ...]
    url: str
    screenshot: Optional[Path]


@dataclass
class SaveGate:
    """The right-record check and the hold, for every commit action.

    Args:
        on_screen: Strings that must all be visible when a write is committed
            (the record's identity: a name, an MRN). Required: a save with
            nothing to check it against is refused.
        hold: Called before the write with a :class:`GateRequest`; returns
            True to let it through. None means no hold.
    """

    on_screen: Sequence[str]
    hold: Optional[Callable[[GateRequest], bool]] = None

    def __post_init__(self) -> None:
        self.on_screen = tuple(str(s) for s in self.on_screen if str(s).strip())


_SCREENSHOT_COUNTERS = {
    "requested": "screenshots_requested",
    "gate": "screenshots_at_gate",
    "every_turn": "screenshots_every_turn",
}


@dataclass
class HarnessStats:
    """What the agent looked at. Screenshots sent to the model are counted by
    why they were taken: the agent asked, a gate, or the every-turn baseline."""

    observations: int = 0
    screenshots_requested: int = 0
    screenshots_at_gate: int = 0
    screenshots_every_turn: int = 0
    actions: int = 0
    gate_checks: int = 0
    #: Kept as evidence with the run; not sent to the model.
    gate_evidence_screenshots: int = 0
    gate_refusals: int = 0

    def to_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class Harness:
    """One run's harness. Built by the compile-agent pipeline around the capture
    computer; agents call its methods."""

    computer: CaptureComputer
    gate: SaveGate
    keep: Sequence[str] = ()
    evidence_dir: Optional[Path] = None
    stats: HarnessStats = field(default_factory=HarnessStats)
    #: One entry per gate decision, kept with the run.
    gate_log: list[dict[str, Any]] = field(default_factory=list)
    refusal: Optional[str] = None
    _last: Optional[_observe.StructuralObservation] = None

    @property
    def page(self) -> Any:
        page = self.computer.page
        if page is None:
            raise RuntimeError("the compile-agent harness needs a browser page")
        return page

    @property
    def dialect(self) -> str:
        return self.computer.dialect

    # -- observation -------------------------------------------------------------

    def observe(self) -> _observe.StructuralObservation:
        """The page's structure, scrubbed except for the task's own values."""

        self._last = _observe.observe(self.page, keep=self.keep)
        self.stats.observations += 1
        return self._last

    def screenshot(self, why: str = "requested") -> bytes:
        """A screenshot for the agent's context. ``why`` is ``requested``,
        ``gate`` or ``every_turn``; each is counted."""

        from openadapt_flow.privacy import scrub_image_bytes

        counter = _SCREENSHOT_COUNTERS[why]
        png = self.computer.screenshot()
        setattr(self.stats, counter, getattr(self.stats, counter) + 1)
        return scrub_image_bytes(png)

    # -- actions on refs (selective vision) -----------------------------------------

    def _control(self, ref: str) -> _observe.Control:
        last = self._last
        if last is None or ref not in last.controls:
            raise StaleRef(f"{ref!r} is not in the latest observation; observe again")
        control = last.controls[ref]
        at = _observe.element_at(self.page, control.x, control.y) or {}
        # Same name, and the same row: a re-sorted list must not turn a ref
        # into another row's control with the same label.
        if (
            str(at.get("name", "")).lower() != control.name.lower()
            or str(at.get("context", "")) != control.context
        ):
            raise StaleRef(
                f"{ref!r} ({control.name!r}) is no longer at its place; observe again"
            )
        return control

    def click(self, ref: str) -> Observation:
        control = self._control(ref)
        return self.act({"kind": "click", "x": control.x, "y": control.y})

    def type_text(self, text: str, ref: Optional[str] = None) -> Observation:
        if ref is not None:
            self.click(ref)
        return self.act({"kind": "type", "text": text})

    def press(self, key: str) -> Observation:
        return self.act({"kind": "press", "key": key})

    def native_for(
        self, action: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Optional[str]]:
        """The generic action for a ref-based ``click`` and the ref it named."""

        ref = action.get("ref")
        if ref is None:
            return dict(action), None
        control = self._control(str(ref))
        return {"kind": "click", "x": control.x, "y": control.y}, control.ref

    def commit_target(self, native: Mapping[str, Any]) -> Optional[str]:
        """What may commit a write in ``native``, or None. Nothing is performed."""

        hit = self._commit_target(native)
        return hit[0] if hit is not None else None

    # -- every action -------------------------------------------------------------

    def act(self, native: Mapping[str, Any]) -> Observation:
        """Perform one native action in the computer's dialect, behind the gate.

        Raises:
            GateRefused: the action commits a write and the gate refused it.
            UnsupportedAction: the recording can't hold the action. Nothing
                in it was performed.
        """

        commit = self._commit_target(native)
        if commit is not None:
            self._check_gate(*commit)
        obs = self.computer.execute(native)
        self.stats.actions += 1
        if commit is not None:
            # Let the app react before the agent looks at the result.
            _settle(self.page)
        return obs

    def _commit_target(
        self, native: Mapping[str, Any]
    ) -> Optional[tuple[str, Optional[str]]]:
        """``(what commits, the text around it)`` when the action may commit a
        write, else None.

        Every action that can change the app is gated unless it is plainly not
        a commit: a click on a control whose label names no write, typing
        without a line break, or a navigation key. A click on something the
        harness can't read, Enter, Space on a control, a typed line break, and
        any shortcut are all gated.
        """

        for op in translate(self.dialect, native):
            if op.kind in ("click", "double_click", "right_click"):
                # A click with no point lands where the pointer is.
                point = (op.x, op.y) if op.x is not None else self.computer.pointer
                if point is None:
                    continue
                x, y = point
                if x is None or y is None:
                    continue
                at = _observe.element_at(
                    self.page,
                    x,
                    y,
                    backend=getattr(
                        getattr(self.computer, "session", None), "backend", None
                    ),
                )
                if at is None:
                    return ("a click on an element the harness could not read", None)
                name = str(at.get("name") or "")
                # A click on a text field only focuses it ("Postal code"
                # holds the word "post" but commits nothing).
                if (
                    at.get("interactive")
                    and not at.get("editable")
                    and _observe.commit_label(name)
                ):
                    return (name, at.get("scope"))
            elif op.kind == "type" and any(ch in (op.text or "") for ch in "\r\n"):
                focused = _observe.focused_element(self.page) or {}
                return ("a typed line break", focused.get("scope"))
            elif op.kind == "hotkey":
                focused = _observe.focused_element(self.page) or {}
                shortcut = "+".join([*op.modifiers, op.key or ""])
                return (f"the shortcut {shortcut}", focused.get("scope"))
            elif op.kind == "press" and op.key in ("Enter", "Space"):
                focused = _observe.focused_element(self.page) or {}
                if op.key == "Space" and focused.get("editable"):
                    continue
                label = str(focused.get("name") or "the focused element")
                return (f"{op.key} on {label}", focused.get("scope"))
        return None

    def _check_gate(self, control: str, scope: Optional[str]) -> None:
        """The right-record check: the identity must be shown, as whole words,
        in the row, dialog, form or section the commit happens in."""

        self.stats.gate_checks += 1
        missing = [s for s in self.gate.on_screen if not _observe.shows(scope or "", s)]
        if not self.gate.on_screen:
            missing = ["(the task names no record identity to check)"]
        shot: Optional[Path] = None
        png = self.computer.screenshot()
        self.stats.gate_evidence_screenshots += 1
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            shot = self.evidence_dir / f"gate-{len(self.gate_log) + 1:02d}.png"
            shot.write_bytes(png)
        entry: dict[str, Any] = {
            "control": control,
            "identity": list(self.gate.on_screen),
            "missing": missing,
            "url": str(getattr(self.page, "url", "")),
            "screenshot": str(shot) if shot else None,
            "at": time.time(),
        }
        if missing:
            entry["decision"] = "refused_wrong_record"
            self.gate_log.append(entry)
            self._refuse(
                f"{control!r} may commit a write, but the form, row or section it "
                f"is in does not show {missing}; the right-record check refused it"
            )
        if self.gate.hold is not None:
            approved = bool(
                self.gate.hold(
                    GateRequest(
                        control=control,
                        identity=tuple(self.gate.on_screen),
                        url=entry["url"],
                        screenshot=shot,
                    )
                )
            )
            if not approved:
                entry["decision"] = "refused_by_hold"
                self.gate_log.append(entry)
                self._refuse(f"the hold did not approve {control!r}")
            entry["decision"] = "approved_by_hold"
        else:
            entry["decision"] = "passed_no_hold"
        self.gate_log.append(entry)

    def _refuse(self, reason: str) -> None:
        self.stats.gate_refusals += 1
        self.refusal = reason
        raise GateRefused(reason)


def _settle(page: Any, timeout_ms: int = 1500) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:  # noqa: BLE001 - a page that never idles is still usable
        pass


# -- every other surface ----------------------------------------------------------


@dataclass(frozen=True)
class SurfaceControl:
    """One control in a surface observation. ``ref`` is the authoring node id."""

    ref: str
    role: str
    name: str
    x: int
    y: int


@dataclass
class SurfaceObservation:
    """What the agent reads on a native or remote app.

    ``text`` has one line per control the accessibility tree shows (UI
    Automation, Accessibility, AT-SPI): ``[n_1a2b3c4d] button "Save"``. A
    remote session (RDP, Citrix) has no tree: ``screen_only`` is True and the
    agent works from the screen and pixel clicks.
    """

    backend: str
    screen_only: bool
    text: str
    controls: dict[str, SurfaceControl] = field(default_factory=dict)

    def find(
        self, name: str, *, near: Optional[str] = None
    ) -> Optional[SurfaceControl]:
        """The one control named ``name`` (case-insensitive), or None."""

        wanted = name.strip().lower()
        hits = [c for c in self.controls.values() if c.name.lower() == wanted]
        return hits[0] if len(hits) == 1 else None


#: Roles a click only focuses. Typing into them commits nothing.
_EDITABLE_ROLES = frozenset({"text_input", "combobox"})


@dataclass
class SurfaceHarness:
    """The harness for a native window or a remote session.

    It has the same methods as :class:`Harness`, so an agent drives either.
    Observations come from the authoring session's PHI-safe tree (no field
    values, no window titles). Every action goes through the same
    :class:`CaptureComputer`, so the recording is the one any other capture
    makes.

    The save gate gates the same actions as on the web: a click on a control
    whose label names a write, a click on a point no control covers, Enter,
    Space, a typed line break, and any shortcut. On these surfaces the harness
    can't read which form or row a control sits in, so it can't do the
    right-record check itself. A person approves every gated action instead
    (the hold). Without a hold, a gated action is refused.
    """

    computer: CaptureComputer
    gate: SaveGate
    keep: Sequence[str] = ()
    evidence_dir: Optional[Path] = None
    stats: HarnessStats = field(default_factory=HarnessStats)
    gate_log: list[dict[str, Any]] = field(default_factory=list)
    refusal: Optional[str] = None
    _last: Optional[SurfaceObservation] = None
    _focused_role: Optional[str] = None

    page: Any = None

    @property
    def dialect(self) -> str:
        return self.computer.dialect

    @property
    def session(self) -> Any:
        return self.computer.session

    # -- observation -------------------------------------------------------------

    def observe(self) -> SurfaceObservation:
        raw = self.session.observe()
        controls: dict[str, SurfaceControl] = {}
        lines: list[str] = []
        for node in raw.get("tree") or []:
            ref = str(node.get("node_id") or "")
            center = self.session.node_center(ref) if ref else None
            if center is None:
                continue
            name = str(node.get("name") or "")
            role = str(node.get("role") or "unknown")
            controls[ref] = SurfaceControl(ref, role, name, center[0], center[1])
            lines.append(f"[{ref}] {role} " + (f'"{name}"' if name else "(no name)"))
        screen_only = bool(getattr(self.session, "screen_only", False))
        if screen_only:
            lines.append(
                "(remote session: no control tree; read the screen and click x,y)"
            )
        elif not controls:
            lines.append(
                "(this app's backend lists no controls; read the screen and click x,y)"
            )
        self._last = SurfaceObservation(
            backend=str(raw.get("backend") or ""),
            screen_only=screen_only,
            text="\n".join(lines),
            controls=controls,
        )
        self.stats.observations += 1
        return self._last

    def screenshot(self, why: str = "requested") -> bytes:
        from openadapt_flow.privacy import scrub_image_bytes

        counter = _SCREENSHOT_COUNTERS[why]
        png = self.computer.screenshot()
        setattr(self.stats, counter, getattr(self.stats, counter) + 1)
        return scrub_image_bytes(png)

    # -- actions --------------------------------------------------------------------

    def _control(self, ref: str) -> SurfaceControl:
        last = self._last
        if last is None or ref not in last.controls:
            raise StaleRef(f"{ref!r} is not in the latest observation; observe again")
        control = last.controls[ref]
        # The node id hashes the role, automation id and bounds: if the
        # control moved or changed, it is gone from a fresh tree.
        fresh = {
            str(n.get("node_id")): n for n in self.session.observe().get("tree") or []
        }
        node = fresh.get(ref)
        if node is None or str(node.get("name") or "") != control.name:
            raise StaleRef(
                f"{ref!r} ({control.name!r}) is no longer at its place; observe again"
            )
        return control

    def native_for(
        self, action: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Optional[str]]:
        """The generic action for a ref-based ``click`` and the control's name."""

        ref = action.get("ref")
        if ref is None:
            return dict(action), None
        control = self._control(str(ref))
        return {"kind": "click", "x": control.x, "y": control.y}, control.ref

    def click(self, ref: str) -> Observation:
        control = self._control(ref)
        return self.act({"kind": "click", "x": control.x, "y": control.y})

    def type_text(self, text: str, ref: Optional[str] = None) -> Observation:
        if ref is not None:
            self.click(ref)
        return self.act({"kind": "type", "text": text})

    def press(self, key: str) -> Observation:
        return self.act({"kind": "press", "key": key})

    def act(self, native: Mapping[str, Any]) -> Observation:
        """Perform one generic action, behind the gate (see the class doc).

        Raises:
            GateRefused: the action may commit a write and no person approved it.
            UnsupportedAction: the recording can't hold the action.
        """

        commit = self.commit_target(native)
        if commit is not None:
            self._check_gate(commit)
        obs = self.computer.execute(native)
        self.stats.actions += 1
        clicked = self._control_at(native)
        if clicked is not None or native.get("kind") in ("click", "double_click"):
            self._focused_role = clicked.role if clicked is not None else None
        return obs

    def _control_at(self, native: Mapping[str, Any]) -> Optional[SurfaceControl]:
        if native.get("kind") not in ("click", "double_click", "right_click"):
            return None
        x, y = native.get("x"), native.get("y")
        if x is None or y is None or self._last is None:
            return None
        for control in self._last.controls.values():
            if control.x == x and control.y == y:
                return control
        return None

    def commit_target(self, native: Mapping[str, Any]) -> Optional[str]:
        """What may commit a write in ``native``, or None (see the class doc)."""

        for op in translate(self.dialect, native):
            if op.kind in ("click", "double_click", "right_click"):
                control = self._control_at(native)
                if control is None:
                    return "a click on a point the harness can't name"
                if control.role in _EDITABLE_ROLES:
                    continue
                if _observe.commit_label(control.name):
                    return control.name
            elif op.kind == "type" and any(ch in (op.text or "") for ch in "\r\n"):
                return "a typed line break"
            elif op.kind == "hotkey":
                return "the shortcut " + "+".join([*op.modifiers, op.key or ""])
            elif op.kind == "press" and op.key in ("Enter", "Space"):
                if op.key == "Space" and self._focused_role in _EDITABLE_ROLES:
                    continue
                return f"{op.key} on the focused control"
        return None

    def _check_gate(self, control: str) -> None:
        self.stats.gate_checks += 1
        shot: Optional[Path] = None
        png = self.computer.screenshot()
        self.stats.gate_evidence_screenshots += 1
        if self.evidence_dir is not None:
            self.evidence_dir.mkdir(parents=True, exist_ok=True)
            shot = self.evidence_dir / f"gate-{len(self.gate_log) + 1:02d}.png"
            shot.write_bytes(png)
        entry: dict[str, Any] = {
            "control": control,
            "identity": list(self.gate.on_screen),
            "missing": [],
            "url": "",
            "screenshot": str(shot) if shot else None,
            "at": time.time(),
            "identity_check": "person",
        }
        hold = self.gate.hold
        if hold is None:
            entry["decision"] = "refused_no_hold"
            self.gate_log.append(entry)
            self._refuse(
                f"{control!r} may commit a write. On this surface the harness "
                "can't read which record it is on, so a person must approve it "
                "(a hold), and there is none"
            )
            return
        approved = bool(
            hold(
                GateRequest(
                    control=control,
                    identity=tuple(self.gate.on_screen),
                    url="",
                    screenshot=shot,
                )
            )
        )
        if not approved:
            entry["decision"] = "refused_by_hold"
            self.gate_log.append(entry)
            self._refuse(f"the hold did not approve {control!r}")
        entry["decision"] = "approved_by_hold"
        self.gate_log.append(entry)

    def _refuse(self, reason: str) -> None:
        self.stats.gate_refusals += 1
        self.refusal = reason
        raise GateRefused(reason)
