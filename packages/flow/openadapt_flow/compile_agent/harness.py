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
    "CommitHint",
    "GateRefused",
    "GateRequest",
    "Harness",
    "HarnessStats",
    "SaveGate",
    "StaleRef",
]


class GateRefused(RuntimeError):
    """A write was not committed: the wrong record was on screen, or the hold
    said no. Nothing in the refused action was performed."""


class StaleRef(ValueError):
    """The ref is not in the latest observation, or the page changed under it."""


@dataclass(frozen=True)
class CommitHint:
    """What a structure-aware tool knows about the control it acts on.

    The harness reads the top-level page itself, which can't see into an
    iframe, and a desktop or remote surface has no page at all. A tool that
    resolved its target from the frame's DOM, an accessibility tree or OCR
    passes the control's ``name`` and the ``scope`` text around it (the row,
    dialog or form). The harness gates the action when either its own reading
    or the hint says it commits a write; a hint never turns a gate off.
    """

    name: str
    scope: str = ""
    #: An Enter or Space on this control (the focused one), not a click.
    key: Optional[str] = None
    #: A text field: a click on it only focuses it, so it commits nothing.
    editable: bool = False


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
    def has_page(self) -> bool:
        return self.computer.page is not None

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

    # -- every action -------------------------------------------------------------

    def act(
        self, native: Mapping[str, Any], *, hint: Optional[CommitHint] = None
    ) -> Observation:
        """Perform one native action in the computer's dialect, behind the gate.

        ``hint`` names the control a structure-aware tool resolved (see
        :class:`CommitHint`). It can only add a gate.

        Raises:
            GateRefused: the action commits a write and the gate refused it.
            UnsupportedAction: the recording can't hold the action. Nothing
                in it was performed.
        """

        commit = self._commit_target(native) if self.has_page else None
        if commit is None:
            commit = self._hinted_commit(native, hint)
        if commit is not None:
            self._check_gate(*commit)
        obs = self.computer.execute(native)
        self.stats.actions += 1
        if commit is not None and self.has_page:
            # Let the app react before the agent looks at the result.
            _settle(self.page)
        return obs

    def _hinted_commit(
        self, native: Mapping[str, Any], hint: Optional[CommitHint]
    ) -> Optional[tuple[str, Optional[str]]]:
        """The commit a hint names, and on a surface with no page, every
        action the harness can't rule out (no hint, or an unread target)."""

        for op in translate(self.dialect, native):
            if op.kind in ("click", "double_click", "right_click"):
                if hint is not None and hint.key is None:
                    if not hint.editable and _observe.commit_label(hint.name):
                        return (hint.name, hint.scope)
                elif not self.has_page:
                    return ("a click on an element the harness could not read", None)
            elif op.kind == "type" and any(ch in (op.text or "") for ch in "\r\n"):
                if not self.has_page:
                    return ("a typed line break", hint.scope if hint else None)
            elif op.kind == "hotkey" and not self.has_page:
                shortcut = "+".join([*op.modifiers, op.key or ""])
                return (f"the shortcut {shortcut}", hint.scope if hint else None)
            elif op.kind == "press" and op.key in ("Enter", "Space"):
                if hint is not None and hint.key == op.key:
                    if op.key == "Enter" or _observe.commit_label(hint.name):
                        return (f"{op.key} on {hint.name}", hint.scope)
                elif not self.has_page:
                    return (f"{op.key} on an element the harness could not read", None)
        return None

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
                if point is None or point[0] is None:
                    continue
                at = _observe.element_at(
                    self.page,
                    point[0],
                    point[1],
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
            "url": str(getattr(self.computer.page, "url", "")),
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
