"""Drop-in executors that hold commit-class actions for a person.

Wrap the executor your loop already has; the call signatures stay the same::

    computer = GatedComputer(ComputerTool())        # Anthropic computer tool
    computer = GatedComputerCall(LocalPlaywright()) # OpenAI computer_call loop

Before a click, a double click, an Enter or Space key, a save shortcut
(Ctrl/Cmd+S) or typed text that ends a line, the wrapper reads the target
under the pointer: first from the executor's own accessibility hook
(``inner.label_at(x, y)``; ``inner.focused_label()`` for keys) when it has
one, else from OCR of a fresh screenshot. OCR names the target only with a
text line whose box holds the click point (or overlaps the target's bounds,
when the executor has ``inner.target_bounds(x, y)``); a nearby line is not
the target's label.

The gate fails closed. A click passes without a person only when its target
is known to be safe: a role that only focuses or reveals (a text field, a
combo box, a tab; given by the accessibility hook as the label's first word,
``"textbox Note"``), or a whole label on the short :data:`SAFE_LABELS` list
of navigation and reading controls (Cancel, Close, Back, Search, Open,
View...). Every other click is commit-class and goes to
:class:`~openadapt_flow.flight.gate.PreCommitGate`: Save, OK, Next, Done,
Delete, a label in another language, an icon with no text, a target that
can't be read. The same list decides Enter and Space on the focused control.
Unless the gate says ``allow``, the executor is never called and the agent is
told the action was held for a person. ``safe_labels`` (or
``OPENADAPT_FLIGHT_SAFE_LABELS``, a JSON list, which ``openadapt-flow flight
--safe-label`` sets) adds labels for one deployment; a label holding one of
the :data:`COMMIT_VERBS` is refused there.

Run under ``openadapt-flow flight``, the wrappers find the recorder and the
gate through ``OPENADAPT_FLIGHT_GATE_URL`` (a loopback URL) by themselves;
in your own process, pass ``gate=`` and ``recorder=``.

Actions are normalised with the JIT translators
(:func:`openadapt_flow.jit.adapters.translate_anthropic` and
:func:`~openadapt_flow.jit.adapters.translate_openai`). An action they refuse
(a triple click, a separate mouse-down) is commit-class too: it can't be
classified.
"""

from __future__ import annotations

import base64
import inspect
import io
import json
import os
import re
import sys
import threading
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional, Protocol

from openadapt_flow.flight.gate import (
    HELD_MESSAGE,
    Decision,
    GateAction,
    PreCommitGate,
    TerminalGate,
)

__all__ = [
    "COMMIT_VERBS",
    "GATE_URL_ENV",
    "SAFE_LABELS",
    "SAFE_LABELS_ENV",
    "SAFE_ROLES",
    "ActionHeld",
    "Classification",
    "GatedComputer",
    "GatedComputerCall",
    "HeldResult",
    "Recorder",
    "RemoteGate",
    "RemoteRecorder",
    "classify",
    "label_is_commit",
    "label_is_safe",
    "parse_safe_labels",
    "split_role",
]

#: The closed, general list of verbs a commit control carries.
COMMIT_VERBS: tuple[str, ...] = (
    "save",
    "submit",
    "send",
    "sign",
    "approve",
    "confirm",
    "post",
    "pay",
    "file",
    "finalize",
)
#: Whole labels (normalised: lower case, single spaces, no surrounding
#: arrows or punctuation) a click may pass on without a person. Navigation
#: and reading only. "Next" is not here: on a wizard's last step it submits.
SAFE_LABELS: frozenset[str] = frozenset(
    {
        "back",
        "go back",
        "cancel",
        "close",
        "search",
        "find",
        "open",
        "view",
        "view details",
        "details",
        "show more",
        "show less",
        "more",
        "expand",
        "collapse",
        "menu",
        "previous page",
        "next page",
        "home",
        "help",
    }
)
#: Accessibility roles whose click only focuses a field or reveals a view.
SAFE_ROLES: frozenset[str] = frozenset(
    {"textbox", "searchbox", "textarea", "multiline", "combobox", "tab"}
)
#: Roles an accessibility hook may put before the name (``"button Save"``).
KNOWN_ROLES: frozenset[str] = SAFE_ROLES | {
    "button",
    "link",
    "menuitem",
    "menuitemcheckbox",
    "menuitemradio",
    "checkbox",
    "radio",
    "switch",
    "option",
    "listbox",
    "treeitem",
    "cell",
    "row",
    "heading",
    "img",
    "image",
    "statictext",
    "text",
    "generic",
    "group",
}
GATE_URL_ENV = "OPENADAPT_FLIGHT_GATE_URL"
SAFE_LABELS_ENV = "OPENADAPT_FLIGHT_SAFE_LABELS"
#: Padding (pixels) around an OCR line's box when testing the click point.
OCR_BOX_PAD_PX = 3
_EDGE_MARKS = " \t\n\r.,:;!?'\"()[]{}<>«»‹›←→↑↓⟵⟶…*·•|-–—_/\\"
_GATE_KIND = {"allow": "allowed", "hold": "held", "refuse": "refused"}
_COMMIT_KEYS = {"Enter", "Space"}
_SAVE_MODIFIERS = {"Control", "Meta"}


def label_is_commit(label: Optional[str]) -> Optional[str]:
    """The commit verb ``label`` holds, or None."""
    if not label:
        return None
    for token in re.findall(r"[a-z]+", label.lower()):
        if token in COMMIT_VERBS:
            return token
    return None


def split_role(label: Optional[str]) -> tuple[Optional[str], str]:
    """(role, name) of an accessibility label (``"button Save"``); the role
    is None when the first word is not a known role."""
    text = (label or "").strip()
    head, _, rest = text.partition(" ")
    if head.lower() in KNOWN_ROLES:
        return head.lower(), rest.strip()
    return None, text


def normalise_label(name: str) -> str:
    """Lower case, single spaces, no surrounding arrows or punctuation."""
    return " ".join(name.strip(_EDGE_MARKS).lower().split())


def parse_safe_labels(values: Optional[Any]) -> frozenset[str]:
    """Deployment-specific safe labels; raises ValueError for one that holds
    a commit verb (a Save can't be declared safe)."""
    out = set()
    for value in values or ():
        label = normalise_label(str(value))
        if not label:
            continue
        verb = label_is_commit(label)
        if verb:
            raise ValueError(f"a safe label can't hold a commit verb: {value!r}")
        out.add(label)
    return frozenset(out)


def _env_safe_labels() -> frozenset[str]:
    raw = os.environ.get(SAFE_LABELS_ENV)
    if not raw:
        return frozenset()
    try:
        values = json.loads(raw)
    except ValueError:
        return frozenset()  # an unreadable list adds nothing (fail closed)
    if not isinstance(values, list):
        return frozenset()
    try:
        return parse_safe_labels(values)
    except ValueError:
        return frozenset()


def label_is_safe(
    label: Optional[str], extra: frozenset[str] = frozenset()
) -> Optional[str]:
    """Why a click on ``label`` may pass without a person, or None (held)."""
    if not label:
        return None
    role, name = split_role(label)
    if role in SAFE_ROLES:
        return f"a {role} (focus or view only)"
    if label_is_commit(name):
        return None
    key = normalise_label(name)
    if not key.isascii():
        return None
    if key in SAFE_LABELS or key in extra:
        return f"{key!r} is a known-safe control"
    return None


class Recorder(Protocol):
    """Where the wrapper logs actions (a Timeline or the flight process)."""

    def executor_action(self, **fields: Any) -> None: ...

    def gate_event(self, kind: str, reason: str, **fields: Any) -> Any: ...


@dataclass(frozen=True)
class Classification:
    """Whether one action is commit-class, and why."""

    commit: bool
    kind: str
    x: Optional[int]
    y: Optional[int]
    label: Optional[str]
    label_source: str
    reason: str
    ops: tuple[dict[str, Any], ...] = ()


def _ops(dialect: str, raw: dict[str, Any]) -> tuple[list[Any], Optional[str]]:
    from openadapt_flow.jit.adapters import (
        UnsupportedAction,
        translate_anthropic,
        translate_openai,
    )

    try:
        if dialect == "anthropic":
            return translate_anthropic(raw), None
        return translate_openai(raw), None
    except (UnsupportedAction, KeyError, TypeError, ValueError) as exc:
        return [], str(exc)


def _ocr_label(
    png: bytes,
    x: int,
    y: int,
    bounds: Optional[tuple[int, int, int, int]] = None,
) -> Optional[str]:
    """The target's own OCR text, or None.

    Without ``bounds``: the OCR line whose box (padded by OCR_BOX_PAD_PX)
    holds the click point. With ``bounds`` (the target's x, y, w, h): every
    line whose box overlaps it, in reading order. A line next to the target
    (a field label beside an icon-only button) is never its label.
    """
    from openadapt_flow.vision.ocr import ocr

    pad = OCR_BOX_PAD_PX
    hits: list[tuple[int, int, str]] = []
    for line in ocr(png):
        lx, ly, lw, lh = line.region
        text = line.text.strip()
        if not text:
            continue
        if bounds is not None:
            bx, by, bw, bh = bounds
            inside = lx < bx + bw and bx < lx + lw and ly < by + bh and by < ly + lh
        else:
            inside = lx - pad <= x <= lx + lw + pad and ly - pad <= y <= ly + lh + pad
        if inside:
            hits.append((ly, lx, text))
    if not hits:
        return None
    return " ".join(text for _, _, text in sorted(hits))


def classify(
    dialect: str,
    raw: dict[str, Any],
    *,
    screenshot: Callable[[], Optional[bytes]],
    label_at: Optional[Callable[[int, int], Optional[str]]] = None,
    focused_label: Optional[Callable[[], Optional[str]]] = None,
    ocr: Callable[..., Optional[str]] = _ocr_label,
    pointer: Optional[tuple[int, int]] = None,
    target_bounds: Optional[
        Callable[[int, int], Optional[tuple[int, int, int, int]]]
    ] = None,
    safe_labels: frozenset[str] = frozenset(),
) -> Classification:
    """Classify one action. A click or key passes only on a known-safe
    target; any failure to read the target is commit-class."""
    ops, refused = _ops(dialect, raw)
    kind = str(raw.get("action") or raw.get("type") or "")
    if refused is not None:
        return Classification(
            True,
            kind,
            None,
            None,
            None,
            "none",
            f"the action can't be classified ({refused})",
        )
    op_dicts = tuple(
        {k: v for k, v in asdict(op).items() if v is not None and v != ()} for op in ops
    )
    pointer_op = None
    key_reason: Optional[str] = None
    enter_like = False
    for op in ops:
        if op.kind in ("click", "double_click"):
            pointer_op = op
        elif op.kind in ("press", "hotkey") and op.key in _COMMIT_KEYS:
            key_reason = f"the {op.key} key can commit the focused control"
            enter_like = enter_like or op.key == "Enter"
        elif (
            op.kind == "hotkey"
            and str(op.key).lower() == "s"
            and _SAVE_MODIFIERS & set(op.modifiers)
        ):
            return Classification(
                True,
                kind,
                None,
                None,
                None,
                "none",
                "a save shortcut",
                op_dicts,
            )
        elif op.kind == "type" and op.text and ("\n" in op.text or "\r" in op.text):
            key_reason = "typed text that ends a line can commit the focused control"
            enter_like = True
    if pointer_op is None and key_reason is None:
        return Classification(False, kind, None, None, None, "none", "", op_dicts)
    if pointer_op is not None:
        x, y = pointer_op.x, pointer_op.y
        if x is None or y is None:
            x, y = pointer if pointer is not None else (None, None)
        if x is None or y is None:
            return Classification(
                True,
                kind,
                None,
                None,
                None,
                "none",
                "the pointer position is unknown (unknown counts as commit)",
                op_dicts,
            )
        label, source = None, "none"
        if label_at is not None:
            try:
                label = label_at(int(x), int(y)) or None
            except Exception:  # noqa: BLE001 - an unreadable target is unknown
                label = None
            source = "accessibility" if label else "none"
        if label is None:
            try:
                png = screenshot()
                bounds = None
                if target_bounds is not None:
                    bounds = target_bounds(int(x), int(y))
                if not png:
                    label = None
                elif bounds is not None:
                    label = ocr(png, int(x), int(y), tuple(bounds))
                else:
                    label = ocr(png, int(x), int(y))
            except Exception:  # noqa: BLE001 - an unreadable target is unknown
                label = None
            source = "ocr" if label else "none"
        verb = label_is_commit(label)
        if label is None:
            reason = (
                "the target under the pointer can't be read (unknown counts as commit)"
            )
        elif verb:
            reason = f"the target reads {label!r} ({verb})"
        elif label_is_safe(label, safe_labels):
            return Classification(False, kind, x, y, label, source, "", op_dicts)
        else:
            reason = (
                f"the target reads {label!r}, which is not a known-safe control "
                "(unknown counts as commit)"
            )
        return Classification(True, kind, x, y, label, source, reason, op_dicts)
    label = None
    source = "none"
    if focused_label is not None:
        try:
            label = focused_label() or None
        except Exception:  # noqa: BLE001
            label = None
        source = "accessibility" if label else "none"
    verb = label_is_commit(label)
    if label is not None and not verb:
        role, _name = split_role(label)
        if enter_like:
            # Enter adds a line in a multi-line text area and runs a search
            # in a search box; on a known-safe control it activates it.
            # Anywhere else it can submit the form.
            if _focused_is_text_area(label) or role == "searchbox":
                return Classification(
                    False, kind, None, None, label, source, "", op_dicts
                )
        elif role in ("textbox", "searchbox", "textarea", "multiline", "combobox"):
            # Space types a space in a field.
            return Classification(False, kind, None, None, label, source, "", op_dicts)
        if role not in SAFE_ROLES and label_is_safe(label, safe_labels):
            return Classification(False, kind, None, None, label, source, "", op_dicts)
    reason = key_reason or ""
    if verb:
        reason = f"{reason}; the focused control reads {label!r} ({verb})"
    elif label is None:
        reason = f"{reason} (the focused control can't be read)"
    else:
        reason = f"{reason} (focused {label!r}, not a known-safe control)"
    return Classification(True, kind, None, None, label, source, reason, op_dicts)


def _focused_is_text_area(label: str) -> bool:
    """A multi-line text area: Enter adds a line there and commits nothing.

    The accessibility hook names the role first (``"textarea Note"``)."""
    return label.lower().split(" ", 1)[0] in ("textarea", "multiline")


# -- remote recorder and gate (the flight process) ---------------------------


def _post(url: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=data, method="POST", headers={"Content-Type": "application/json"}
    )
    # Loopback only: never through a proxy from the environment.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def _loopback(url: str) -> bool:
    from urllib.parse import urlsplit

    host = urlsplit(url).hostname or ""
    return host in ("127.0.0.1", "localhost", "::1")


class RemoteGate:
    """Ask the flight process's gate over its loopback URL. Errors refuse."""

    def __init__(self, url: str, *, timeout_s: float = 4 * 3600.0 + 60) -> None:
        if not _loopback(url):
            raise ValueError(f"{GATE_URL_ENV} must be a loopback URL")
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s

    def decide(self, action: GateAction, screenshot: Optional[bytes]) -> Decision:
        try:
            answer = _post(
                self.url + "/decide",
                {
                    "action": action.to_dict(),
                    "screenshot_b64": (
                        base64.b64encode(screenshot).decode("ascii")
                        if screenshot
                        else None
                    ),
                },
                self.timeout_s,
            )
        except Exception:  # noqa: BLE001 - an unreachable gate refuses
            return "refuse"
        decision = answer.get("decision")
        return decision if decision in ("allow", "hold", "refuse") else "refuse"


class RemoteRecorder:
    """Send executor actions to the flight process's timeline.

    Gate events are recorded by the flight process when it decides, so
    :meth:`gate_event` does nothing here.
    """

    def __init__(self, url: str, *, timeout_s: float = 10.0) -> None:
        if not _loopback(url):
            raise ValueError(f"{GATE_URL_ENV} must be a loopback URL")
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self._warned = False

    def executor_action(self, **fields: Any) -> None:
        frame = fields.pop("frame_png", None)
        payload = dict(fields)
        if frame:
            payload["frame_b64"] = base64.b64encode(frame).decode("ascii")
        try:
            _post(self.url + "/event", payload, self.timeout_s)
        except Exception as exc:  # noqa: BLE001 - logging never blocks the run
            if not self._warned:
                self._warned = True
                print(
                    f"[openadapt flight] could not record an action: {exc}",
                    file=sys.stderr,
                )

    def gate_event(self, kind: str, reason: str, **fields: Any) -> None:
        return None


def _env_gate_and_recorder(
    gate: Optional[PreCommitGate], recorder: Optional[Recorder]
) -> tuple[PreCommitGate, Optional[Recorder]]:
    url = os.environ.get(GATE_URL_ENV)
    if url:
        if gate is None:
            gate = RemoteGate(url)
        if recorder is None:
            recorder = RemoteRecorder(url)
    return (gate if gate is not None else TerminalGate()), recorder


# -- the shared gatekeeper -------------------------------------------------


class ActionHeld(RuntimeError):
    """Raised instead of returning when ``on_held='raise'``."""

    def __init__(self, message: str, decision: Decision) -> None:
        super().__init__(message)
        self.decision = decision


@dataclass
class HeldResult:
    """What the agent gets back for an action that was not performed.

    Shaped like the computer-use reference loop's ``ToolResult``
    (``output``, ``error``, ``base64_image``, ``system``), so it goes into
    the same ``tool_result`` code path. :meth:`content` gives raw blocks.
    """

    error: str
    output: Optional[str] = None
    base64_image: Optional[str] = None
    system: Optional[str] = None

    def __bool__(self) -> bool:
        return True

    def content(self) -> list[dict[str, Any]]:
        return [{"type": "text", "text": self.error}]


class _Gatekeeper:
    def __init__(
        self,
        dialect: str,
        gate: Optional[PreCommitGate],
        recorder: Optional[Recorder],
        *,
        label_at: Optional[Callable[[int, int], Optional[str]]],
        focused_label: Optional[Callable[[], Optional[str]]],
        ocr: Optional[Callable[..., Optional[str]]],
        target_bounds: Optional[
            Callable[[int, int], Optional[tuple[int, int, int, int]]]
        ] = None,
        safe_labels: Optional[Any] = None,
    ) -> None:
        self.dialect = dialect
        self.gate, self.recorder = _env_gate_and_recorder(gate, recorder)
        self.label_at = label_at
        self.focused_label = focused_label
        self.ocr = ocr or _ocr_label
        self.target_bounds = target_bounds
        self.safe_labels = parse_safe_labels(safe_labels) | _env_safe_labels()
        self.pointer: Optional[tuple[int, int]] = None
        self._index = 0
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []

    def note_pointer(self, raw: dict[str, Any]) -> None:
        coordinate = raw.get("coordinate")
        if isinstance(coordinate, (list, tuple)) and len(coordinate) == 2:
            try:
                self.pointer = (int(coordinate[0]), int(coordinate[1]))
            except (TypeError, ValueError):
                pass
        elif "x" in raw and "y" in raw:
            try:
                self.pointer = (int(raw["x"]), int(raw["y"]))
            except (TypeError, ValueError):
                pass

    def check(
        self, raw: dict[str, Any], screenshot: Callable[[], Optional[bytes]]
    ) -> tuple[Decision, Optional[GateAction], Optional[str]]:
        """Return (decision, action, label) for one action; never raises."""
        with self._lock:
            self._index += 1
            index = self._index
        frame: dict[str, Optional[bytes]] = {}

        def shot() -> Optional[bytes]:
            if "png" not in frame:
                frame["png"] = screenshot()
            return frame["png"]

        try:
            result = classify(
                self.dialect,
                raw,
                screenshot=shot,
                label_at=self.label_at,
                focused_label=self.focused_label,
                ocr=self.ocr,
                pointer=self.pointer,
                target_bounds=self.target_bounds,
                safe_labels=self.safe_labels,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed
            result = Classification(
                True,
                str(raw.get("action") or raw.get("type") or ""),
                None,
                None,
                None,
                "none",
                f"classification failed ({type(exc).__name__})",
            )
        if not result.commit:
            if result.kind not in ("screenshot", "cursor_position"):
                self._record(raw, result, "performed", None)
            return "allow", None, result.label
        action = GateAction(
            dialect=self.dialect,
            raw=dict(raw),
            kind=result.kind,
            x=result.x,
            y=result.y,
            label=result.label,
            label_source=result.label_source,
            reason=result.reason,
            index=index,
            ops=result.ops,
        )
        try:
            png = shot()
        except Exception:  # noqa: BLE001
            png = None
        try:
            decision = self.gate.decide(action, png)
        except Exception:  # noqa: BLE001 - a failed gate refuses
            decision = "refuse"
        if decision not in ("allow", "hold", "refuse"):
            decision = "refuse"
        self._record(raw, result, _GATE_KIND[decision], png)
        if self.recorder is not None:
            try:
                self.recorder.gate_event(
                    _GATE_KIND[decision],
                    result.reason,
                    label=result.label,
                    frame_png=png,
                )
            except Exception:  # noqa: BLE001 - logging never allows an action
                pass
        self.events.append({"decision": decision, "label": result.label})
        return decision, action, result.label

    def _record(
        self,
        raw: dict[str, Any],
        result: Classification,
        decision: str,
        png: Optional[bytes],
    ) -> None:
        if self.recorder is None:
            return
        try:
            self.recorder.executor_action(
                dialect=self.dialect,
                action=raw,
                commit_class=result.commit,
                label=result.label,
                label_source=result.label_source,
                decision=decision,
                frame_png=png,
            )
        except Exception:  # noqa: BLE001 - logging never blocks the run
            pass


def _png_from(result: Any) -> Optional[bytes]:
    """Screenshot bytes from an executor's result, whatever its shape."""
    if result is None:
        return None
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    if isinstance(result, str):
        text = result.split(",", 1)[1] if result.startswith("data:") else result
        try:
            return base64.b64decode(text, validate=False)
        except ValueError:
            return None
    for key in ("base64_image", "image", "screenshot", "data"):
        value = (
            result.get(key) if isinstance(result, dict) else getattr(result, key, None)
        )
        if value:
            return _png_from(value)
    return None


def _held_text(label: Optional[str]) -> str:
    return HELD_MESSAGE.format(label=repr(label) if label else "unreadable")


def _is_async(inner: Any) -> bool:
    if inspect.iscoroutinefunction(inner):
        return True
    call = getattr(type(inner), "__call__", None)
    return call is not None and inspect.iscoroutinefunction(call)


class GatedComputer:
    """The Anthropic computer tool, with commit-class actions gated.

    ``inner`` is your executor, called with the tool's input as keyword
    arguments (``inner(action="left_click", coordinate=[x, y])``), sync or
    async; the wrapper is the same kind. Other attributes (``name``,
    ``to_params``...) pass through. With the ``computer_toolset_20260801``
    toolset the tool name is the action: call ``computer(action=name,
    **input)``.

    ``held`` builds the result for an action that was not performed
    (default :class:`HeldResult`); ``on_held='raise'`` raises
    :class:`ActionHeld` instead. ``screenshot`` overrides how the wrapper
    takes a fresh frame (default: ``inner(action="screenshot")``).
    """

    def __init__(
        self,
        inner: Any,
        gate: Optional[PreCommitGate] = None,
        recorder: Optional[Recorder] = None,
        *,
        held: Optional[Callable[[str], Any]] = None,
        on_held: str = "return",
        screenshot: Optional[Callable[[], Any]] = None,
        label_at: Optional[Callable[[int, int], Optional[str]]] = None,
        focused_label: Optional[Callable[[], Optional[str]]] = None,
        ocr: Optional[Callable[..., Optional[str]]] = None,
        target_bounds: Optional[
            Callable[[int, int], Optional[tuple[int, int, int, int]]]
        ] = None,
        safe_labels: Optional[Any] = None,
    ) -> None:
        self._inner = inner
        self._held = held or (lambda message: HeldResult(error=message))
        if on_held not in ("return", "raise"):
            raise ValueError("on_held is 'return' or 'raise'")
        self._on_held = on_held
        self._screenshot = screenshot
        self._async = _is_async(inner)
        self._keeper = _Gatekeeper(
            "anthropic",
            gate,
            recorder,
            label_at=label_at or getattr(inner, "label_at", None),
            focused_label=focused_label or getattr(inner, "focused_label", None),
            ocr=ocr,
            target_bounds=target_bounds or getattr(inner, "target_bounds", None),
            safe_labels=safe_labels,
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def gate_events(self) -> list[dict[str, Any]]:
        return list(self._keeper.events)

    def _held_result(self, decision: Decision, label: Optional[str]) -> Any:
        message = _held_text(label)
        if self._on_held == "raise":
            raise ActionHeld(message, decision)
        return self._held(message)

    def __call__(self, **kwargs: Any) -> Any:
        if self._async:
            return self._acall(**kwargs)
        return self._call(**kwargs)

    def _shot_sync(self) -> Optional[bytes]:
        if self._screenshot is not None:
            return _png_from(self._screenshot())
        return _png_from(self._inner(action="screenshot"))

    def _call(self, **kwargs: Any) -> Any:
        decision, _, label = self._keeper.check(dict(kwargs), self._shot_sync)
        if decision != "allow":
            return self._held_result(decision, label)
        self._keeper.note_pointer(kwargs)
        return self._inner(**kwargs)

    async def _acall(self, **kwargs: Any) -> Any:
        import asyncio

        loop = asyncio.get_running_loop()

        async def wait_for(awaitable: Any) -> Any:
            return await awaitable

        def shot() -> Optional[bytes]:
            if self._screenshot is not None:
                value: Any = self._screenshot()
                if inspect.isawaitable(value):
                    value = asyncio.run_coroutine_threadsafe(
                        wait_for(value), loop
                    ).result()
                return _png_from(value)
            future = asyncio.run_coroutine_threadsafe(
                self._inner(action="screenshot"), loop
            )
            return _png_from(future.result())

        decision, _, label = await asyncio.to_thread(
            self._keeper.check, dict(kwargs), shot
        )
        if decision != "allow":
            return self._held_result(decision, label)
        self._keeper.note_pointer(kwargs)
        return await self._inner(**kwargs)


_BANNER = "OpenAdapt: the last action was held for a person and not performed"


def _with_banner(png: bytes) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.open(io.BytesIO(png)).convert("RGB")
    draw = ImageDraw.Draw(image)
    height = max(18, image.height // 30)
    draw.rectangle([0, 0, image.width, height], fill=(179, 38, 30))
    draw.text((6, max(1, height // 4)), _BANNER, fill=(255, 255, 255))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class GatedComputerCall:
    """An OpenAI ``computer_call`` executor, with commit-class actions gated.

    ``inner`` is your computer object (the sample loop's ``click(x, y,
    button)``, ``double_click(x, y)``, ``keypress(keys)``, ``type(text)``,
    ``screenshot()``...); every method passes through. A held action is not
    performed, and the next ``screenshot()`` carries a red banner saying so,
    because a ``computer_call_output`` returns only a screenshot.
    ``on_held='raise'`` raises :class:`ActionHeld` instead.
    """

    _ACTIONS = ("click", "double_click", "keypress", "type")

    def __init__(
        self,
        inner: Any,
        gate: Optional[PreCommitGate] = None,
        recorder: Optional[Recorder] = None,
        *,
        on_held: str = "banner",
        label_at: Optional[Callable[[int, int], Optional[str]]] = None,
        focused_label: Optional[Callable[[], Optional[str]]] = None,
        ocr: Optional[Callable[..., Optional[str]]] = None,
        target_bounds: Optional[
            Callable[[int, int], Optional[tuple[int, int, int, int]]]
        ] = None,
        safe_labels: Optional[Any] = None,
    ) -> None:
        if on_held not in ("banner", "raise"):
            raise ValueError("on_held is 'banner' or 'raise'")
        self._inner = inner
        self._on_held = on_held
        self._pending_banner = False
        self.last_held: Optional[str] = None
        self._keeper = _Gatekeeper(
            "openai",
            gate,
            recorder,
            label_at=label_at or getattr(inner, "label_at", None),
            focused_label=focused_label or getattr(inner, "focused_label", None),
            ocr=ocr,
            target_bounds=target_bounds or getattr(inner, "target_bounds", None),
            safe_labels=safe_labels,
        )

    @property
    def gate_events(self) -> list[dict[str, Any]]:
        return list(self._keeper.events)

    def _raw_shot(self) -> Optional[bytes]:
        return _png_from(self._inner.screenshot())

    def screenshot(self, *args: Any, **kwargs: Any) -> Any:
        value = self._inner.screenshot(*args, **kwargs)
        if not self._pending_banner:
            return value
        self._pending_banner = False
        png = _png_from(value)
        if not png:
            return value
        try:
            marked = _with_banner(png)
        except Exception:  # noqa: BLE001 - the banner is a courtesy
            return value
        if isinstance(value, str):
            return base64.b64encode(marked).decode("ascii")
        return marked

    def _gated(self, name: str, raw: dict[str, Any], *args: Any, **kwargs: Any) -> Any:
        decision, _, label = self._keeper.check(raw, self._raw_shot)
        if decision != "allow":
            message = _held_text(label)
            self.last_held = message
            if self._on_held == "raise":
                raise ActionHeld(message, decision)
            self._pending_banner = True
            return None
        self._keeper.note_pointer(raw)
        return getattr(self._inner, name)(*args, **kwargs)

    def click(
        self, x: int, y: int, button: str = "left", *args: Any, **kwargs: Any
    ) -> Any:
        raw = {"type": "click", "x": x, "y": y, "button": button}
        if button not in ("left",):
            # A right, wheel or back click opens or navigates; it saves nothing.
            self._keeper._record(
                raw,
                Classification(False, "click", x, y, None, "none", ""),
                "performed",
                None,
            )
            return self._inner.click(x, y, button, *args, **kwargs)
        return self._gated("click", raw, x, y, button, *args, **kwargs)

    def double_click(self, x: int, y: int, *args: Any, **kwargs: Any) -> Any:
        return self._gated(
            "double_click",
            {"type": "double_click", "x": x, "y": y},
            x,
            y,
            *args,
            **kwargs,
        )

    def keypress(self, keys: Any, *args: Any, **kwargs: Any) -> Any:
        return self._gated(
            "keypress", {"type": "keypress", "keys": list(keys)}, keys, *args, **kwargs
        )

    def type(self, text: str, *args: Any, **kwargs: Any) -> Any:
        return self._gated(
            "type", {"type": "type", "text": text}, text, *args, **kwargs
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
