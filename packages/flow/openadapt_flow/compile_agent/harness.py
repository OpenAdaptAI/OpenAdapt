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

Read-back after every save. When the harness has a ``readback`` (the pipeline
always gives it one, built by :func:`record_readback` from the task's
``verify.yaml`` reader), every action that went through the gate is followed
by a read of the system of record, compared with the read taken when run 1
started:

* a change on any record other than the one the task names stops run 1 at
  once (:class:`ReadBackRefused`); no draft is made;
* no change at all is reported to the agent ("nothing was saved"), so a screen
  that says "saved" can't stand in for a save;
* a change on the named record is reported to the agent with its field names.
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
    "ReadBack",
    "ReadBackRefused",
    "GateRequest",
    "Harness",
    "HarnessStats",
    "SaveGate",
    "StaleRef",
    "SurfaceControl",
    "RecordReadBack",
    "record_readback",
    "SurfaceHarness",
    "SurfaceObservation",
]


class GateRefused(RuntimeError):
    """A write was not committed: the wrong record was on screen, or the hold
    said no. Nothing in the refused action was performed."""


class ReadBackRefused(GateRefused):
    """A save went through, and the system of record shows a change on a record
    the task does not name. Run 1 stops; no draft is made."""


@dataclass(frozen=True)
class ReadBack:
    """What the system of record showed after one save.

    ``outcome`` is ``landed`` (only the named record changed), ``no_change``,
    ``wrong_record`` (another record changed) or ``unreadable``.
    """

    outcome: str
    detail: str
    #: Field names of the named record's change (values stay out of the log).
    fields: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "detail": self.detail,
            "fields": list(self.fields),
        }


class RecordReadBack:
    """A read-back for the harness (see :func:`record_readback`).

    :meth:`mark` takes a read just before a save; calling the object then
    reads again. A record that changed or disappeared since run 1 started and
    is not the named record is the wrong record. Whether this save landed is
    judged against the read :meth:`mark` took, so a second save that writes
    nothing is told so.
    """

    def __init__(
        self,
        read: Callable[[], Optional[list[dict[str, Any]]]],
        expect: Mapping[str, Any],
        baseline: Callable[[], Optional[list[dict[str, Any]]]],
    ) -> None:
        self.read = read
        self.baseline = baseline
        self.wanted = {str(k): str(v) for k, v in expect.items()}
        self._before: Optional[list[dict[str, Any]]] = None

    def named(self, record: Mapping[str, Any]) -> bool:
        return all(str(record.get(k)) == v for k, v in self.wanted.items())

    def mark(self) -> None:
        try:
            self._before = self.read()
        except Exception:  # noqa: BLE001 - judged against the run's start then
            self._before = None

    def _others(self, diff: Any) -> list[str]:
        """Changes on records the task does not name."""

        bad: list[str] = []
        named_created = [r for r in diff.created if self.named(r)]
        for record in diff.created:
            if not self.named(record):
                bad.append("a record the task does not name was created")
        for change in diff.changed:
            # The named record before and after: overwriting another record
            # until it matches the task is not the named record.
            if not (self.named(change.before) and self.named(change.after)):
                bad.append("a record the task does not name changed")
        for record in diff.removed:
            # A store without record ids shows an edit as removed + created.
            if not (self.named(record) and named_created):
                bad.append("a record disappeared")
        return bad

    def __call__(self) -> ReadBack:
        from openadapt_flow.record_sor import diff_snapshots

        start, after = self.baseline(), self.read()
        if start is None or after is None:
            return ReadBack("unreadable", "the system of record could not be read")
        bad = self._others(diff_snapshots(start, after))
        if bad:
            return ReadBack(
                "wrong_record", f"{len(bad)} change(s) the task does not name: {bad[0]}"
            )
        before = self._before if self._before is not None else start
        self._before = None
        step = diff_snapshots(before, after)
        touched = [*step.created, *(c.after for c in step.changed)]
        if not touched:
            return ReadBack("no_change", "nothing was saved in the system of record")
        fields = tuple(sorted({k for r in touched for k in r}))
        return ReadBack(
            "landed",
            f"{len(touched)} record(s) with "
            + ", ".join(f"{k}={v}" for k, v in self.wanted.items())
            + " saved",
            fields,
        )


def record_readback(
    read: Callable[[], Optional[list[dict[str, Any]]]],
    expect: Mapping[str, Any],
    baseline: Callable[[], Optional[list[dict[str, Any]]]],
) -> RecordReadBack:
    """A read-back for the harness: ``read`` the system of record after each
    save and compare it with ``baseline()`` (the read when run 1 started) and
    with the read just before the save. A record is the named one when it has
    every ``expect`` field with that value."""

    return RecordReadBack(read, expect, baseline)


def _expect_write(harness: Any, wait_s: Optional[float] = None) -> None:
    """Tell the recorder the next action may write, so the event's after-read
    of the system of record waits for the change (and the compiler binds the
    write to this step). ``wait_s=0`` takes it back."""

    session = getattr(harness.computer, "session", None)
    recorder = getattr(session, "_recorder", None)
    expect = getattr(recorder, "expect_write", None)
    if callable(expect):
        expect(harness.readback_wait_s if wait_s is None else wait_s)


def _before_write(harness: Any) -> None:
    """Just before a save the gate let through: the read-back's before-read,
    and the recorder's wait for the write."""

    mark = getattr(harness.readback, "mark", None)
    if callable(mark):
        mark()
    _expect_write(harness)


def _read_back(harness: Any, control: str) -> Optional[ReadBack]:
    """Read the system of record after a save went through the gate; poll a
    little for a write that lands late. Raises ReadBackRefused on another
    record's change."""

    check = getattr(harness, "readback", None)
    if check is None:
        return None
    deadline = time.monotonic() + max(float(harness.readback_wait_s), 0.0)
    while True:
        try:
            got = check()
        except Exception as exc:  # noqa: BLE001 - an unreadable read is reported
            got = ReadBack("unreadable", f"{type(exc).__name__}: {exc}"[:200])
        if got.outcome != "no_change" or time.monotonic() >= deadline:
            break
        time.sleep(0.25)
    harness.stats.readbacks += 1
    harness.readback_log.append(
        {"control": control, **got.to_dict(), "at": time.time()}
    )
    if got.outcome == "wrong_record":
        harness.stats.gate_refusals += 1
        harness.refusal = (
            f"after {control!r}, the read-back shows {got.detail}; run 1 stopped"
        )
        raise ReadBackRefused(harness.refusal)
    return got


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
    #: The tool knows the action commits (a drop-down whose change submits
    #: the form): always gated.
    commits: bool = False


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
    #: System-of-record reads after a save (see :func:`record_readback`).
    readbacks: int = 0

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
    #: Read after every save that went through the gate (see the module doc).
    readback: Optional[Callable[[], ReadBack]] = None
    readback_wait_s: float = 3.0
    readback_log: list[dict[str, Any]] = field(default_factory=list)
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

    def native_for(
        self, action: Mapping[str, Any]
    ) -> tuple[dict[str, Any], Optional[str]]:
        """The generic action for a ref-based ``click`` and the ref it named."""

        ref = action.get("ref")
        if ref is None:
            return dict(action), None
        control = self._control(str(ref))
        return {"kind": "click", "x": control.x, "y": control.y}, control.ref

    def commit_target(
        self,
        native: Mapping[str, Any],
        ref: Optional[str] = None,
        *,
        hint: Optional[CommitHint] = None,
    ) -> Optional[str]:
        """What may commit a write in ``native``, or None. Nothing is performed.

        ``ref`` is the text field a ``type`` will be typed into (it is clicked
        first), so the check looks at that field rather than today's focus.
        ``hint`` is what a structure-aware tool resolved (see :meth:`act`).
        """

        if ref is not None and native.get("kind") == "type":
            control = (self._last.controls if self._last else {}).get(ref)
            text = str(native.get("text") or "")
            if control is not None and _editable(control):
                return "a typed line break" if any(c in text for c in "\r\n") else None
        hit = self._commit_target(native) if self.has_page else None
        if hit is None:
            hit = self._hinted_commit(native, hint)
        return hit[0] if hit is not None else None

    # -- every action -------------------------------------------------------------

    def act(
        self,
        native: Mapping[str, Any],
        ref: Optional[str] = None,
        *,
        hint: Optional[CommitHint] = None,
    ) -> Observation:
        """Perform one native action in the computer's dialect, behind the gate.

        ``ref`` is accepted for the surface harness's signature; the web
        harness reads the page at the point itself. ``hint`` names the control
        a structure-aware tool resolved (see :class:`CommitHint`). It can only
        add a gate.

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
            _before_write(self)
        try:
            obs = self.computer.execute(native)
        except BaseException:
            if commit is not None:
                _expect_write(self, 0.0)
            raise
        self.stats.actions += 1
        if commit is not None and self.has_page:
            # Let the app react before the agent looks at the result.
            _settle(self.page)
        if commit is not None:
            _read_back(self, commit[0])
        return obs

    def _hinted_commit(
        self, native: Mapping[str, Any], hint: Optional[CommitHint]
    ) -> Optional[tuple[str, Optional[str]]]:
        """The commit a hint names, and on a surface with no page, every
        action the harness can't rule out (no hint, or an unread target)."""

        for op in translate(self.dialect, native):
            if op.kind in ("click", "double_click", "right_click"):
                if hint is not None and hint.key is None:
                    # Without a page, the tool's resolved element is the only
                    # reading there is; with one, the harness read it too.
                    if hint.commits or (
                        not hint.editable and _observe.commit_label(hint.name)
                    ):
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
                if not self.has_page:
                    # Nothing can rule a key on an unread element out.
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
            elif op.kind == "type" and " " in (op.text or ""):
                # A typed space on a focused button presses it.
                focused = _observe.focused_element(self.page) or {}
                # Typing on a native drop-down picks an option, unless its
                # change submits the form.
                on_select = focused.get("tag") == "select" and not focused.get(
                    "submits"
                )
                if not focused.get("editable") and not on_select:
                    label = str(focused.get("name") or "the focused element")
                    return (f"typing a space on {label}", focused.get("scope"))
            elif op.kind == "hotkey":
                focused = _observe.focused_element(self.page) or {}
                shortcut = "+".join([*op.modifiers, op.key or ""])
                return (f"the shortcut {shortcut}", focused.get("scope"))
            elif op.kind == "press" and op.key in ("Enter", "Space"):
                focused = _observe.focused_element(self.page) or {}
                if op.key == "Space" and focused.get("editable"):
                    continue
                # Enter on a native drop-down picks an option; it does not
                # submit the form. (A text area stays gated: chat-style
                # boxes send on Enter.)
                if (
                    op.key == "Enter"
                    and focused.get("tag") == "select"
                    and not focused.get("submits")
                ):
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


def _editable(control: Any) -> bool:
    """A web control that takes typed text."""

    return control.tag in ("input", "textarea") or control.role in (
        "textbox",
        "combobox",
    )


#: Roles a click only focuses. Typing into them commits nothing.
_EDITABLE_ROLES = frozenset({"text_input", "combobox"})
#: Roles whose click only changes what the form shows or holds.
_SAFE_CLICK_ROLES = _EDITABLE_ROLES | frozenset({"checkbox", "radio", "tab"})
#: First words of labels that navigate or open something, and never write.
_NAVIGATION_WORDS = frozenset(
    {
        "open",
        "new",
        "next",
        "back",
        "previous",
        "cancel",
        "close",
        "search",
        "find",
        "view",
        "show",
        "edit",
        "select",
        "start",
        "go",
        "home",
        "menu",
        "more",
        "expand",
        "collapse",
    }
)
#: Keys that move focus or dismiss, which never write by themselves.
_FOCUS_KEYS = frozenset(
    {
        "Tab",
        "Escape",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "Home",
        "End",
        "PageUp",
        "PageDown",
    }
)


@dataclass
class SurfaceHarness:
    """The harness for a native window or a remote session.

    It has the same methods as :class:`Harness`, so an agent drives either.
    Observations come from the authoring session's PHI-safe tree (no field
    values, no window titles). Every action goes through the same
    :class:`CaptureComputer`, so the recording is the one any other capture
    makes.

    The save gate is stricter than on the web, because the harness can't read
    which form or row a control sits in and so can't do the right-record check
    itself. A click is let through only on a control named by its ref whose
    role only edits the form (a text field, a check box, a tab) or whose label
    plainly navigates (Open, New, Cancel); every other click, including any
    click at x,y, is gated. So are Enter, Space and any typing outside a text
    field, a typed line break, a single-letter key (a dialog's accelerator),
    and every shortcut. A person approves every gated action (the hold).
    Without a hold, a gated action is refused.
    """

    computer: CaptureComputer
    gate: SaveGate
    keep: Sequence[str] = ()
    evidence_dir: Optional[Path] = None
    stats: HarnessStats = field(default_factory=HarnessStats)
    gate_log: list[dict[str, Any]] = field(default_factory=list)
    refusal: Optional[str] = None
    readback: Optional[Callable[[], ReadBack]] = None
    readback_wait_s: float = 3.0
    readback_log: list[dict[str, Any]] = field(default_factory=list)
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
        return self.act({"kind": "click", "x": control.x, "y": control.y}, ref=ref)

    def type_text(self, text: str, ref: Optional[str] = None) -> Observation:
        if ref is not None:
            self.click(ref)
        return self.act({"kind": "type", "text": text}, ref=ref)

    def press(self, key: str) -> Observation:
        return self.act({"kind": "press", "key": key})

    def act(self, native: Mapping[str, Any], ref: Optional[str] = None) -> Observation:
        """Perform one generic action, behind the gate (see the class doc).

        ``ref`` names the control a click is on, or the text field a ``type``
        goes into. A click with no ref is a click at x,y.

        Raises:
            GateRefused: the action may commit a write and no person approved it.
            UnsupportedAction: the recording can't hold the action.
        """

        commit = self.commit_target(native, ref)
        if commit is not None:
            self._check_gate(commit)
            _before_write(self)
        try:
            obs = self.computer.execute(native)
        except BaseException:
            if commit is not None:
                _expect_write(self, 0.0)
            raise
        self.stats.actions += 1
        if commit is not None:
            _read_back(self, commit)
        kind = native.get("kind")
        if kind in ("click", "double_click", "right_click"):
            control = self._ref_control(ref)
            self._focused_role = control.role if control is not None else None
        elif kind == "hotkey" or (
            kind == "press" and str(native.get("key")) in _FOCUS_KEYS | {"Enter"}
        ):
            self._focused_role = None
        return obs

    def _ref_control(self, ref: Optional[str]) -> Optional[SurfaceControl]:
        if ref is None or self._last is None:
            return None
        return self._last.controls.get(ref)

    def commit_target(
        self, native: Mapping[str, Any], ref: Optional[str] = None
    ) -> Optional[str]:
        """What may commit a write in ``native``, or None (see the class doc)."""

        control = self._ref_control(ref)
        focus = control.role if control is not None else self._focused_role
        editable = focus in _EDITABLE_ROLES
        for op in translate(self.dialect, native):
            if op.kind in ("click", "double_click", "right_click"):
                if control is None:
                    return "a click at a point, not on a named control"
                if _observe.commit_label(control.name):
                    return control.name
                words = control.name.lower().split()
                if control.role in _SAFE_CLICK_ROLES or (
                    words and words[0] in _NAVIGATION_WORDS
                ):
                    continue
                return control.name or f"a {control.role} with no name"
            elif op.kind == "type":
                text = op.text or ""
                if any(ch in text for ch in "\r\n"):
                    return "a typed line break"
                if text and not editable:
                    return "typing outside a text field"
            elif op.kind == "hotkey":
                return "the shortcut " + "+".join([*op.modifiers, op.key or ""])
            elif op.kind == "press":
                key = op.key or ""
                if key in ("Enter", "Return"):
                    return "Enter on the focused control"
                if key == "Space" and not editable:
                    return "Space on the focused control"
                if len(key) == 1 and not editable:
                    return f"the key {key!r} outside a text field"
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
