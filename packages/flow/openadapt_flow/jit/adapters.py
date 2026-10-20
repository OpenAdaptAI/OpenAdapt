"""Turn a computer-use agent's actions into a Flow demonstration.

A computer-use agent (CUA) runs a loop: the model asks for an action, the
caller performs it and sends back a screenshot. :class:`CaptureComputer` is
that caller. It performs each action through
:class:`~openadapt_flow.authoring.AuthoringSession`, so the agent's run is
recorded by the same :class:`~openadapt_flow.recorder.Recorder` a person's
demonstration uses: a before and after frame for every step, the clicked
element's locator, and the system-of-record delta when a reader is attached.

Four action formats are built in, and :func:`register_dialect` adds more:

``anthropic``
    The ``input`` of a ``tool_use`` block for Anthropic's ``computer`` tool
    (``{"action": "left_click", "coordinate": [x, y]}``).
``openai``
    An OpenAI Responses ``computer_call`` item, with a batched ``actions``
    list or a single ``action``, or one bare action.
``playwright_mcp``
    A Playwright MCP tool call, ``{"name": "browser_click", "arguments":
    {...}}``. Element targets must be unique CSS selectors. Snapshot refs
    (``e12``) are refused: only the Playwright MCP server's own page can
    resolve them.
``generic``
    Flow's own step vocabulary as a plain dict, for a loop that already has
    its own action format and maps it itself
    (``{"kind": "click", "x": 10, "y": 20}``). See :func:`translate_generic`.

An action is refused with :class:`UnsupportedAction` when the recording
format can't hold it without a guess: a triple-click, a held modifier, a
middle or back button, a multi-point drag path, a scroll away from the
pointer, a fill that would replace existing text. A refused action is not
performed, and the run produces no candidate.

A pointer move is not performed. The recording has no pointer-move step, so
performing it could change the app (a hover menu) in a way the program would
not repeat. The agent gets a fresh screenshot and a note instead.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.benchmark.agent_baseline import SCROLL_PX_PER_UNIT

__all__ = [
    "DIALECTS",
    "CaptureComputer",
    "Observation",
    "Op",
    "UnsupportedAction",
    "dialects",
    "register_dialect",
    "translate",
    "translate_anthropic",
    "translate_generic",
    "translate_openai",
    "translate_playwright_mcp",
]

#: The built-in action formats. :func:`dialects` also lists registered ones.
DIALECTS = ("anthropic", "openai", "playwright_mcp", "generic")

#: Seconds a ``wait`` may pause during capture.
MAX_WAIT_S = 2.0

_MODIFIERS = {
    "ctrl": "Control",
    "control": "Control",
    "alt": "Alt",
    "option": "Alt",
    "shift": "Shift",
    "super": "Meta",
    "meta": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "win": "Meta",
}
#: Named keys, keyed by lowercase name, across xdotool (Anthropic), OpenAI
#: and Playwright spellings.
_NAMED_KEYS = {
    "return": "Enter",
    "enter": "Enter",
    "kp_enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "space": "Space",
    "home": "Home",
    "end": "End",
    "page_up": "PageUp",
    "pageup": "PageUp",
    "prior": "PageUp",
    "page_down": "PageDown",
    "pagedown": "PageDown",
    "next": "PageDown",
    "up": "ArrowUp",
    "arrowup": "ArrowUp",
    "down": "ArrowDown",
    "arrowdown": "ArrowDown",
    "left": "ArrowLeft",
    "arrowleft": "ArrowLeft",
    "right": "ArrowRight",
    "arrowright": "ArrowRight",
    **{f"f{n}": f"F{n}" for n in range(1, 13)},
}
_SNAPSHOT_REF_RE = re.compile(r"^(?:f\d+)?e\d+$")


class UnsupportedAction(ValueError):
    """The action has no faithful mapping into the recording format."""

    def __init__(self, dialect: str, action: str, reason: str) -> None:
        super().__init__(f"{dialect} action {action!r} is not supported: {reason}")
        self.dialect = dialect
        self.action = action
        self.reason = reason


@dataclass(frozen=True)
class Op:
    """One recordable step, or an observation that records nothing.

    ``kind`` is one of ``click``, ``double_click``, ``right_click``, ``drag``,
    ``type``, ``press``, ``hotkey``, ``scroll``, ``wait`` or ``observe``.
    A ``target`` is a CSS selector the computer resolves to a point; with
    ``fill`` the target must be empty before typing.
    """

    kind: str
    x: Optional[int] = None
    y: Optional[int] = None
    end_x: Optional[int] = None
    end_y: Optional[int] = None
    text: Optional[str] = None
    key: Optional[str] = None
    modifiers: tuple[str, ...] = ()
    dx: int = 0
    dy: int = 0
    seconds: float = 0.0
    target: Optional[str] = None
    fill: bool = False
    note: Optional[str] = None


@dataclass
class Observation:
    """What the agent gets back: the frame, and notes on what was not done."""

    png: bytes
    text: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    def png_base64(self) -> str:
        import base64

        return base64.standard_b64encode(self.png).decode("ascii")


def _point(dialect: str, action: str, value: Any) -> tuple[int, int]:
    if not isinstance(value, Sequence) or len(value) != 2:
        raise UnsupportedAction(dialect, action, f"coordinate {value!r} is not [x, y]")
    return int(value[0]), int(value[1])


def _chord(dialect: str, action: str, tokens: Sequence[str]) -> Op:
    mods: list[str] = []
    keys: list[str] = []
    for raw in tokens:
        token = str(raw).strip()
        lower = token.lower()
        if lower in _MODIFIERS:
            mods.append(_MODIFIERS[lower])
        elif lower in _NAMED_KEYS:
            keys.append(_NAMED_KEYS[lower])
        elif len(token) == 1:
            keys.append(token)
        else:
            raise UnsupportedAction(dialect, action, f"unknown key name {token!r}")
    if len(keys) != 1:
        raise UnsupportedAction(
            dialect, action, f"expected one key plus modifiers, got {list(tokens)!r}"
        )
    if mods:
        return Op("hotkey", key=keys[0], modifiers=tuple(mods))
    return Op("press", key=keys[0])


def _no_modifiers(dialect: str, action: str, keys: Any) -> None:
    if keys:
        raise UnsupportedAction(
            dialect, action, "a held modifier has no step in the recording format"
        )


# -- Anthropic computer tool ------------------------------------------------

_ANTHROPIC_OBSERVE = {"screenshot", "cursor_position", "zoom"}
_ANTHROPIC_REFUSED = {
    "triple_click": "a triple-click has no step in the recording format",
    "middle_click": "a middle click has no step in the recording format",
    "left_mouse_down": "a separate button press has no step in the recording format",
    "left_mouse_up": "a separate button release has no step in the recording format",
    "hold_key": "a held key has no step in the recording format",
}


def translate_anthropic(action_input: Mapping[str, Any]) -> list[Op]:
    """Map one ``computer`` tool ``input`` to steps."""

    d = "anthropic"
    action = str(action_input.get("action", ""))
    if action in _ANTHROPIC_OBSERVE:
        return [Op("observe")]
    if action in _ANTHROPIC_REFUSED:
        raise UnsupportedAction(d, action, _ANTHROPIC_REFUSED[action])
    if action == "wait":
        return [Op("wait", seconds=float(action_input.get("duration", 1.0)))]
    if action == "mouse_move":
        return [Op("observe", note="pointer move not performed")]
    if action in ("left_click", "right_click", "double_click"):
        _no_modifiers(d, action, action_input.get("text"))
        kind = {"left_click": "click"}.get(action, action)
        if "coordinate" not in action_input or action_input["coordinate"] is None:
            return [Op(kind)]  # at the pointer; the computer checks it is known
        x, y = _point(d, action, action_input["coordinate"])
        return [Op(kind, x=x, y=y)]
    if action == "left_click_drag":
        _no_modifiers(d, action, action_input.get("text"))
        x, y = _point(d, action, action_input.get("start_coordinate"))
        ex, ey = _point(d, action, action_input.get("coordinate"))
        return [Op("drag", x=x, y=y, end_x=ex, end_y=ey)]
    if action == "type":
        return [Op("type", text=str(action_input.get("text", "")))]
    if action == "key":
        # xdotool syntax: "ctrl+s"; a space separates keys pressed in turn.
        sequence = str(action_input.get("text", "")).split()
        if not sequence:
            raise UnsupportedAction(d, action, "no key given")
        return [_chord(d, action, chord.split("+")) for chord in sequence]
    if action == "scroll":
        _no_modifiers(d, action, action_input.get("text"))
        amount = int(action_input.get("scroll_amount", 3)) * SCROLL_PX_PER_UNIT
        direction = str(action_input.get("scroll_direction", ""))
        deltas = {
            "down": (0, amount),
            "up": (0, -amount),
            "right": (amount, 0),
            "left": (-amount, 0),
        }
        if direction not in deltas:
            raise UnsupportedAction(d, action, f"scroll_direction {direction!r}")
        dx, dy = deltas[direction]
        if action_input.get("coordinate") is None:
            return [Op("scroll", dx=dx, dy=dy)]
        x, y = _point(d, action, action_input["coordinate"])
        return [Op("scroll", x=x, y=y, dx=dx, dy=dy)]
    raise UnsupportedAction(d, action, "unknown action")


# -- OpenAI Responses computer_call ------------------------------------------


def _openai_action(action: Mapping[str, Any]) -> list[Op]:
    d = "openai"
    kind = str(action.get("type", ""))
    if kind == "screenshot":
        return [Op("observe")]
    if kind == "wait":
        return [Op("wait", seconds=1.0)]
    if kind == "move":
        _no_modifiers(d, kind, action.get("keys"))
        return [Op("observe", note="pointer move not performed")]
    if kind == "click":
        _no_modifiers(d, kind, action.get("keys"))
        button = str(action.get("button", "left"))
        if button not in ("left", "right"):
            raise UnsupportedAction(
                d, kind, f"button {button!r} has no step in the recording format"
            )
        op = "click" if button == "left" else "right_click"
        return [Op(op, x=int(action["x"]), y=int(action["y"]))]
    if kind == "double_click":
        _no_modifiers(d, kind, action.get("keys"))
        return [Op("double_click", x=int(action["x"]), y=int(action["y"]))]
    if kind == "drag":
        _no_modifiers(d, kind, action.get("keys"))
        path = list(action.get("path") or [])
        if len(path) != 2:
            raise UnsupportedAction(
                d, kind, f"a {len(path)}-point path; the recording keeps two endpoints"
            )
        (a, b) = path
        return [
            Op(
                "drag",
                x=int(a["x"]),
                y=int(a["y"]),
                end_x=int(b["x"]),
                end_y=int(b["y"]),
            )
        ]
    if kind == "keypress":
        return [_chord(d, kind, list(action.get("keys") or []))]
    if kind == "type":
        return [Op("type", text=str(action.get("text", "")))]
    if kind == "scroll":
        _no_modifiers(d, kind, action.get("keys"))
        return [
            Op(
                "scroll",
                x=int(action["x"]),
                y=int(action["y"]),
                dx=int(action.get("scroll_x", 0)),
                dy=int(action.get("scroll_y", 0)),
            )
        ]
    raise UnsupportedAction(d, kind, "unknown action")


def translate_openai(call: Mapping[str, Any]) -> list[Op]:
    """Map a ``computer_call`` (``actions`` or ``action``) or one action."""

    if isinstance(call.get("actions"), list):
        actions = list(call["actions"])
    elif isinstance(call.get("action"), Mapping):
        actions = [call["action"]]
    else:
        actions = [call]
    ops: list[Op] = []
    for action in actions:
        ops.extend(_openai_action(action))
    return ops


# -- Playwright MCP ----------------------------------------------------------

_PW_OBSERVE = {
    "browser_snapshot",
    "browser_take_screenshot",
    "browser_console_messages",
    "browser_network_requests",
    "browser_verify_element_visible",
    "browser_verify_list_visible",
    "browser_verify_text_visible",
    "browser_verify_value",
    "browser_wait_for",
}


def _pw_target(tool: str, args: Mapping[str, Any]) -> str:
    target = args.get("target", args.get("ref"))
    if not isinstance(target, str) or not target.strip():
        raise UnsupportedAction("playwright_mcp", tool, "no element target")
    if _SNAPSHOT_REF_RE.match(target.strip()):
        raise UnsupportedAction(
            "playwright_mcp",
            tool,
            f"snapshot ref {target!r}: only the Playwright MCP server's page can "
            "resolve it; target the element with a unique CSS selector",
        )
    return target.strip()


def translate_playwright_mcp(call: Mapping[str, Any]) -> list[Op]:
    """Map one Playwright MCP tool call (``name`` and ``arguments``)."""

    d = "playwright_mcp"
    tool = str(call.get("name", call.get("tool", "")))
    args: Mapping[str, Any] = call.get("arguments", call.get("input")) or {}
    if tool in _PW_OBSERVE:
        if tool == "browser_wait_for" and args.get("time") is not None:
            return [Op("wait", seconds=float(args["time"]))]
        return [Op("observe")]
    if tool == "browser_navigate":
        return [Op("observe", target=str(args.get("url", "")), note="navigate")]
    if tool == "browser_click":
        _no_modifiers(d, tool, args.get("modifiers"))
        target = _pw_target(tool, args)
        button = str(args.get("button") or "left")
        if button not in ("left", "right"):
            raise UnsupportedAction(d, tool, f"button {button!r}")
        if args.get("doubleClick"):
            if button != "left":
                raise UnsupportedAction(d, tool, "a right double-click")
            return [Op("double_click", target=target)]
        return [Op("click" if button == "left" else "right_click", target=target)]
    if tool == "browser_type":
        target = _pw_target(tool, args)
        ops = [
            Op("click", target=target),
            Op("type", text=str(args.get("text", "")), target=target, fill=True),
        ]
        if args.get("submit"):
            ops.append(Op("press", key="Enter"))
        return ops
    if tool == "browser_press_key":
        return [_chord(d, tool, str(args.get("key", "")).split("+"))]
    if tool == "browser_mouse_click_xy":
        button = str(args.get("button") or "left")
        count = int(args.get("clickCount") or 1)
        if args.get("delay"):
            raise UnsupportedAction(d, tool, "a held click (delay) has no step")
        if button not in ("left", "right") or count not in (1, 2):
            raise UnsupportedAction(d, tool, f"button {button!r} x{count}")
        if count == 2:
            if button != "left":
                raise UnsupportedAction(d, tool, "a right double-click")
            return [Op("double_click", x=int(args["x"]), y=int(args["y"]))]
        kind = "click" if button == "left" else "right_click"
        return [Op(kind, x=int(args["x"]), y=int(args["y"]))]
    if tool == "browser_mouse_drag_xy":
        return [
            Op(
                "drag",
                x=int(args["startX"]),
                y=int(args["startY"]),
                end_x=int(args["endX"]),
                end_y=int(args["endY"]),
            )
        ]
    if tool == "browser_mouse_move_xy":
        return [Op("observe", note="pointer move not performed")]
    if tool == "browser_mouse_wheel":
        return [
            Op("scroll", dx=int(args.get("deltaX", 0)), dy=int(args.get("deltaY", 0)))
        ]
    raise UnsupportedAction(d, tool, "no step in the recording format")


# -- generic: Flow's own step vocabulary -------------------------------------

#: For each ``kind``: the fields it requires, and the fields it may carry.
_GENERIC_FIELDS: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "click": (frozenset(), frozenset({"x", "y", "target"})),
    "double_click": (frozenset(), frozenset({"x", "y", "target"})),
    "right_click": (frozenset(), frozenset({"x", "y", "target"})),
    "drag": (frozenset({"x", "y", "end_x", "end_y"}), frozenset()),
    "type": (frozenset({"text"}), frozenset()),
    "press": (frozenset({"key"}), frozenset()),
    "hotkey": (frozenset({"key", "modifiers"}), frozenset()),
    "scroll": (frozenset(), frozenset({"x", "y", "dx", "dy"})),
    "wait": (frozenset(), frozenset({"seconds"})),
    "observe": (frozenset(), frozenset()),
}
_INT_FIELDS = ("x", "y", "end_x", "end_y", "dx", "dy")


def translate_generic(action: Mapping[str, Any]) -> list[Op]:
    """Map one generic action to a step.

    A generic action is a dict with a ``kind`` and that kind's fields:

    * ``click``, ``double_click``, ``right_click``: ``x`` and ``y``, or a CSS
      ``target``, or neither to click at the pointer.
    * ``drag``: ``x``, ``y``, ``end_x``, ``end_y``.
    * ``type``: ``text``. ``press``: ``key``. ``hotkey``: ``key`` and a
      ``modifiers`` list. Key names use the same spellings as the other
      formats (``Return``, ``ctrl``).
    * ``scroll``: ``dx`` and ``dy`` in pixels, at the pointer.
    * ``wait``: ``seconds``. ``observe``: nothing.

    An unknown kind or an unknown field is refused, so a typo can't become a
    silently dropped argument.
    """

    d = "generic"
    kind = str(action.get("kind", ""))
    if kind not in _GENERIC_FIELDS:
        raise UnsupportedAction(
            d, kind, f"unknown kind (expected one of {sorted(_GENERIC_FIELDS)})"
        )
    required, optional = _GENERIC_FIELDS[kind]
    fields = set(action) - {"kind"}
    missing = required - fields
    extra = fields - required - optional
    if missing:
        raise UnsupportedAction(d, kind, f"missing {sorted(missing)}")
    if extra:
        raise UnsupportedAction(d, kind, f"unknown fields {sorted(extra)}")
    values: dict[str, Any] = {}
    for name in _INT_FIELDS:
        if name in action:
            values[name] = int(action[name])
    if ("x" in values) != ("y" in values):
        raise UnsupportedAction(d, kind, "x and y go together")
    if "target" in action:
        if "x" in values:
            raise UnsupportedAction(d, kind, "give x and y, or a target, not both")
        values["target"] = str(action["target"])
    if kind == "type":
        return [Op("type", text=str(action["text"]))]
    if kind in ("press", "hotkey"):
        mods = list(action.get("modifiers") or [])
        if kind == "hotkey" and not mods:
            raise UnsupportedAction(d, kind, "a hotkey needs at least one modifier")
        op = _chord(d, kind, [*mods, str(action["key"])])
        if (op.kind == "hotkey") != (kind == "hotkey"):
            raise UnsupportedAction(d, kind, f"{action['key']!r} is a modifier")
        return [op]
    if kind == "wait":
        return [Op("wait", seconds=float(action.get("seconds", 1.0)))]
    return [Op(kind, **values)]


_TRANSLATORS: dict[str, Callable[[Mapping[str, Any]], list[Op]]] = {
    "anthropic": translate_anthropic,
    "openai": translate_openai,
    "playwright_mcp": translate_playwright_mcp,
    "generic": translate_generic,
}


def dialects() -> tuple[str, ...]:
    """Every action format :class:`CaptureComputer` accepts now."""

    return tuple(_TRANSLATORS)


def register_dialect(
    name: str, translator: Callable[[Mapping[str, Any]], list[Op]]
) -> None:
    """Add an action format.

    ``translator`` maps one native action to a list of :class:`Op`, or raises
    :class:`UnsupportedAction`. It must not perform anything. A built-in
    format can't be replaced.
    """

    if not name or not isinstance(name, str):
        raise ValueError("a dialect needs a name")
    if name in DIALECTS:
        raise ValueError(f"{name!r} is built in and can't be replaced")
    _TRANSLATORS[name] = translator


def translate(dialect: str, native: Mapping[str, Any]) -> list[Op]:
    """Map one native action in ``dialect`` to steps, or raise UnsupportedAction."""

    if dialect not in _TRANSLATORS:
        raise ValueError(f"unknown dialect {dialect!r} (expected one of {dialects()})")
    return _TRANSLATORS[dialect](native)


# -- the computer the agent drives -------------------------------------------


class CaptureComputer:
    """Perform a CUA's actions through an authoring session, and record them.

    Args:
        session: A started :class:`~openadapt_flow.authoring.AuthoringSession`.
        dialect: The agent's action format (see :func:`dialects`).
        page: The Playwright page behind the session, for CSS-selector targets
            and the text snapshot a Playwright MCP agent reads. Optional for
            the coordinate formats.
        sleep: Injected for tests.
        guard: A :class:`~openadapt_flow.teach_scope.TeachGuard`. Each action
            is checked against the task's declared scope before it runs, and
            the page after it; a violation raises
            :class:`~openadapt_flow.teach_scope.ScopeViolation`.

    Every performed step is also kept in :attr:`log` (native action and the
    steps it became) for the candidate's review.
    """

    def __init__(
        self,
        session: Any,
        *,
        dialect: str,
        page: Any = None,
        sleep: Callable[[float], None] = time.sleep,
        guard: Any = None,
    ) -> None:
        if dialect not in _TRANSLATORS:
            raise ValueError(
                f"unknown dialect {dialect!r} (expected one of {dialects()})"
            )
        self._session = session
        self.dialect = dialect
        self._page = page
        self._sleep = sleep
        self._guard = guard
        self._pointer: Optional[tuple[int, int]] = None
        self.log: list[dict[str, Any]] = []
        self.actions = 0

    @property
    def page(self) -> Any:
        """The Playwright page behind the session, or None."""

        return self._page

    @property
    def pointer(self) -> Optional[tuple[int, int]]:
        """Where the last click or drag left the pointer, or None."""

        return self._pointer

    def screenshot(self) -> bytes:
        """The current frame, without performing or recording an action."""

        return self._session.screenshot()

    @property
    def session(self) -> Any:
        """The authoring session the actions are recorded through."""

        return self._session

    def execute(self, native: Mapping[str, Any]) -> Observation:
        """Perform one native action and return the new frame.

        Raises:
            UnsupportedAction: the action has no faithful step. Nothing in it
                was performed.
        """

        ops = [self._resolve(op) for op in translate(self.dialect, native)]
        self._check(ops)
        if self._guard is not None:
            self._guard.check_ops(ops, page=self._page, pointer=self._pointer)
        notes: list[str] = []
        for op in ops:
            note = self._perform(op)
            if note:
                notes.append(note)
        self.actions += 1
        self.log.append(
            {
                "native": dict(native),
                "steps": [op.kind for op in ops if op.kind not in ("observe", "wait")],
                "notes": notes,
            }
        )
        if self._guard is not None:
            self._guard.after_action(self._page)
        return Observation(
            png=self._session.screenshot(), text=self._snapshot(), notes=notes
        )

    # -- internals -----------------------------------------------------------

    def _resolve(self, op: Op) -> Op:
        if op.note == "navigate":
            current = getattr(self._page, "url", None)
            if (
                op.target
                and current
                and op.target.rstrip("/") == str(current).rstrip("/")
            ):
                return Op("observe")
            raise UnsupportedAction(
                self.dialect,
                "browser_navigate",
                "navigating away has no step in the recording format",
            )
        if op.target is None or op.kind == "observe":
            return op
        if self._page is None:
            raise UnsupportedAction(
                self.dialect, op.kind, "no page to resolve a selector"
            )
        locator = self._page.locator(op.target)
        count = locator.count()
        if count != 1:
            raise UnsupportedAction(
                self.dialect,
                op.kind,
                f"selector {op.target!r} matches {count} elements",
            )
        if op.fill:
            try:
                existing = locator.input_value()
            except Exception:
                existing = None
            if existing is None or existing != "":
                raise UnsupportedAction(
                    self.dialect,
                    "browser_type",
                    "the field is not empty or not readable; a fill would replace "
                    "text that typing appends to",
                )
            return Op("type", text=op.text)
        box = locator.bounding_box()
        if box is None:
            raise UnsupportedAction(
                self.dialect, op.kind, f"{op.target!r} is not visible"
            )
        x = int(box["x"] + box["width"] / 2)
        y = int(box["y"] + box["height"] / 2)
        return Op(op.kind, x=x, y=y)

    def _check(self, ops: list[Op]) -> None:
        """Refuse the whole action before any of it is performed."""

        pointer = self._pointer
        for op in ops:
            if op.kind in ("click", "double_click", "right_click"):
                if op.x is None or op.y is None:
                    if pointer is None:
                        raise UnsupportedAction(
                            self.dialect, op.kind, "no known pointer position"
                        )
                else:
                    pointer = (op.x, op.y)
            elif op.kind == "drag":
                assert op.end_x is not None and op.end_y is not None
                pointer = (op.end_x, op.end_y)
            elif op.kind == "scroll":
                at = (op.x, op.y) if op.x is not None and op.y is not None else pointer
                if pointer is None or at != pointer:
                    raise UnsupportedAction(
                        self.dialect,
                        "scroll",
                        f"scroll at {at} but the pointer is at {pointer}; the "
                        "recording scrolls at the pointer and has no pointer-move "
                        "step",
                    )

    def _perform(self, op: Op) -> Optional[str]:
        s = self._session
        if op.kind == "observe":
            return op.note
        if op.kind == "wait":
            self._sleep(min(max(op.seconds, 0.0), MAX_WAIT_S))
            return None
        if op.kind in ("click", "double_click", "right_click"):
            x, y = (op.x, op.y) if op.x is not None else self._pointer  # type: ignore[misc]
            assert x is not None and y is not None
            {
                "click": s.click,
                "double_click": s.double_click,
                "right_click": s.right_click,
            }[op.kind](int(x), int(y))
            self._pointer = (int(x), int(y))
        elif op.kind == "drag":
            assert op.x is not None and op.y is not None
            assert op.end_x is not None and op.end_y is not None
            s.drag(op.x, op.y, op.end_x, op.end_y)
            self._pointer = (op.end_x, op.end_y)
        elif op.kind == "type":
            s.type_text(op.text or "")
        elif op.kind == "press":
            s.press(op.key or "")
        elif op.kind == "hotkey":
            s.hotkey(list(op.modifiers), op.key or "")
        elif op.kind == "scroll":
            s.scroll(op.dx, op.dy)
        else:  # pragma: no cover - translate() emits only the kinds above
            raise UnsupportedAction(self.dialect, op.kind, "unknown step")
        return None

    def _snapshot(self) -> Optional[str]:
        if self.dialect != "playwright_mcp" or self._page is None:
            return None
        try:
            return str(self._page.locator("body").aria_snapshot())
        except Exception:
            return None
