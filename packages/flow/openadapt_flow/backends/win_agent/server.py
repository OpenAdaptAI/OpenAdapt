"""In-guest Windows agent server (WAA-contract HTTP shim, session 1).

Runs INSIDE the Windows VM's *interactive* desktop session and exposes exactly
the endpoints ``openadapt_flow.backends.windows_backend.WindowsBackend`` calls,
matching the Windows Agent Arena Flask contract (the WAADirect pattern):

    GET  /screenshot        -> raw PNG bytes of the desktop (Content-Type
                               image/png; NOT base64 JSON)
    POST /context/identity  -> PHI-free foreground-app + logon-session identity
    POST /input             -> bounded physical input operations (typed JSON)
    POST /input/guarded     -> frame/context/focus check + bounded input in one
                               serialized request
    POST /uia/locator-at    -> stable UIA locator at a demonstrated point
    POST /uia/element-identity-at -> role, name, AutomationId, window path and
                               process of the element at a point
    POST /uia/text-at-point -> structured row text for identity verification
    POST /uia/identity-candidates-at -> rows that read the same as that row
    POST /uia/find          -> zero / unique / ambiguous exact candidates
    POST /uia/act           -> unique native UIA action + delivery receipt
    POST /uia/read          -> read-only text of one unique control (value or
                               name) and, on request, the text of its rows
    POST /execute_windows   -> legacy WAA-shaped input commands, DISABLED by
                               default (``--allow-legacy-commands``). Commands
                               are parsed into an allow-listed set of
                               ``pyautogui`` input calls with literal
                               arguments; nothing is ever exec()'d.
    GET  /health            -> ``{"status": "ok", ...}`` liveness + which
                               desktop session the process is attached to.

Why a separate in-session server at all (the session-0 problem)
---------------------------------------------------------------
``prlctl exec`` (and any Windows service) runs as ``NT AUTHORITY\\SYSTEM`` in
session 0, which is isolated from the logged-on user's desktop. An mss/BitBlt
screenshot there captures a blank/non-existent desktop and pyautogui SendInput
goes nowhere -- the automation silently drives the wrong desktop. This server
MUST therefore run in the interactive console session (session 1). The
canonical way to start it from SYSTEM is the ``session1_launch.py`` launcher
(WTSQueryUserToken -> CreateProcessAsUserW with ``lpDesktop=winsta0\\default``);
for an unattended VM the ``run_agent.bat`` + logon scheduled-task recipe in this
package's ``README.md`` starts it in-session at user logon.

Hardening (vs the original ``scripts/desktop/waa_shim.py``)
-----------------------------------------------------------
* **Typed actions by default, and no remote code execution at all.**
  Production observation and actuation use a bounded JSON schema. The legacy
  ``/execute_windows`` compatibility route is off unless an operator enables
  it, and even then it only parses the WAA command shape into allow-listed
  ``pyautogui`` input calls (see ``parse_legacy_command``); arbitrary Python is
  refused with HTTP 400.
* **Loopback by default.** The default bind is ``127.0.0.1``. Exposing the
  agent on the guest LAN interface is an explicit opt-in.
* **Optional bearer token.** The PHI at-rest audit flagged this shim as
  unauthenticated. When a token is configured (``--token`` or the
  ``OAFLOW_AGENT_TOKEN`` env var) every ``/screenshot`` and ``/execute_windows``
  request must carry ``Authorization: Bearer <token>`` or is rejected 401. The
  comparison is constant-time. ``/health`` stays unauthenticated (liveness only,
  no desktop bytes, no input).
* **TLS in transit (encryption + pinned server identity).** The channel carries
  PHI (screenshots of the patient chart, the commands that read/write it), so
  the 2026 HIPAA Security Rule requires it be encrypted. When a cert/key pair is
  configured (``--certfile`` / ``--keyfile``, provisioned per run by the control
  plane) the listener serves **HTTPS**; the client pins the certificate's
  SHA-256 fingerprint (see ``tls.py`` for the trust model). Encryption and
  token-auth are independent factors -- ``--token`` is still required to expose
  the channel off loopback. Cert minting lives on the control plane
  (``cryptography``); the guest needs only stdlib ``ssl`` to wrap its socket.

Self-contained by construction
------------------------------
Only the Python standard library is imported at module load (no Flask), so the
guest needs no third-party web framework and CI on macOS/Linux imports this
module freely. The heavy, Windows-only pieces (mss/Pillow for the screenshot,
pyautogui/uiautomation used by the input handlers) import LAZILY inside the
request handlers, and the desktop grabber is injectable so tests exercise the
full HTTP roundtrip with a fake frame.
"""

from __future__ import annotations

import argparse
import ast
import base64
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Optional

# PNG magic used to validate/return frames.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Env var the token is read from when ``--token`` is not passed (keeps the
# secret off the process command line / argv where feasible).
TOKEN_ENV_VAR = "OAFLOW_AGENT_TOKEN"

# Env vars the TLS cert/key paths are read from when the flags are not passed.
CERTFILE_ENV_VAR = "OAFLOW_AGENT_CERTFILE"
KEYFILE_ENV_VAR = "OAFLOW_AGENT_KEYFILE"

GrabFn = Callable[[], bytes]
InputFn = Callable[[dict[str, Any]], dict[str, Any]]
UiaFn = Callable[[str, dict[str, Any]], dict[str, Any]]
ContextFn = Callable[[], dict[str, Any]]

_MAX_BODY_BYTES = 1_048_576
_MAX_TEXT_CHARS = 65_536
_MAX_UIA_NODES = 5_000
_MAX_UIA_CANDIDATES = 16
_MAX_UIA_DEPTH = 24
# An Invoke that has not returned after this long (a click handler showing a
# modal dialog, or an app that is not answering) is an uncertain delivery.
_INVOKE_RETURN_S = 2.0
_INVOKE_START_S = 15.0

_ROLE_TO_CONTROL = {
    "button": "ButtonControl",
    "link": "HyperlinkControl",
    "menuitem": "MenuItemControl",
    "tab": "TabItemControl",
    "listitem": "ListItemControl",
    "checkbox": "CheckBoxControl",
    "radio": "RadioButtonControl",
    "textbox": "EditControl",
}
_CONTROL_TO_ROLE = {value: key for key, value in _ROLE_TO_CONTROL.items()}
_ACTIONABLE_TYPES = frozenset(
    {
        *_CONTROL_TO_ROLE,
        "SplitButtonControl",
    }
)


class AgentRequestError(ValueError):
    """A bounded typed request was invalid or safely refused."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _exact_object(
    value: object,
    *,
    required: frozenset[str] = frozenset(),
    optional: frozenset[str] = frozenset(),
    label: str = "request",
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AgentRequestError(400, "invalid_schema", f"{label} must be an object")
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing or unknown:
        raise AgentRequestError(
            400,
            "invalid_schema",
            f"{label} has missing={sorted(missing)} unknown={sorted(unknown)}",
        )
    return value


def _bounded_int(value: object, label: str, *, limit: int = 1_000_000) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > limit:
        raise AgentRequestError(
            400, "invalid_schema", f"{label} must be a bounded integer"
        )
    return value


# -- legacy WAA command interpreter (no exec) -----------------------------------
#
# ``/execute_windows`` used to ``exec()`` whatever Python the client sent. It now
# accepts only the closed WAA-shaped vocabulary that ``WindowsBackend``'s legacy
# path emits -- ``import pyautogui`` plus ``pyautogui.<input>(<literals>)`` calls
# -- parsed with ``ast`` and dispatched to a fixed table. Nothing the client sends
# is compiled or executed, so the route grants no capability beyond the typed
# ``/input`` route (pointer and keyboard input on the session-1 desktop).

_LEGACY_PYAUTOGUI_CALLS = frozenset(
    {
        "click",
        "doubleClick",
        "tripleClick",
        "rightClick",
        "middleClick",
        "moveTo",
        "dragTo",
        "mouseDown",
        "mouseUp",
        "scroll",
        "hscroll",
        "write",
        "typewrite",
        "press",
        "hotkey",
        "keyDown",
        "keyUp",
    }
)
_LEGACY_KEYWORDS = frozenset(
    {"x", "y", "clicks", "interval", "button", "duration", "presses"}
)
_MAX_LEGACY_COMMAND_CHARS = 16_384
_MAX_LEGACY_CALLS = 64
_MAX_LEGACY_ARGS = 16

LegacyCall = tuple[str, tuple[Any, ...], dict[str, Any]]
LegacyFn = Callable[[list[LegacyCall]], dict[str, Any]]


def _unsupported_legacy(message: str) -> AgentRequestError:
    return AgentRequestError(400, "unsupported_legacy_command", message)


def _legacy_literal(node: ast.expr) -> Any:
    """Return a literal argument value, refusing anything that is not one."""
    if isinstance(node, ast.Starred):
        raise _unsupported_legacy("starred arguments are not allowed")
    try:
        value = ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        raise _unsupported_legacy("arguments must be literals") from None
    items = value if isinstance(value, (list, tuple)) else (value,)
    if len(items) > _MAX_LEGACY_ARGS:
        raise _unsupported_legacy("literal sequence too long")
    for item in items:
        if item is None or isinstance(item, bool):
            continue
        if isinstance(item, int):
            if abs(item) > 1_000_000:
                raise _unsupported_legacy("integer argument out of range")
            continue
        if isinstance(item, float):
            if not (abs(item) <= 1_000_000.0):  # also rejects NaN
                raise _unsupported_legacy("float argument out of range")
            continue
        if isinstance(item, str):
            if len(item) > _MAX_TEXT_CHARS:
                raise _unsupported_legacy("string argument too long")
            continue
        raise _unsupported_legacy(f"argument type {type(item).__name__} is not allowed")
    return value


def parse_legacy_command(command: str) -> list[LegacyCall]:
    """Parse a legacy ``/execute_windows`` command into allowed input calls.

    Accepted statements are ``import pyautogui`` and expression statements of
    the form ``pyautogui.<name>(<literal>, ..., <kw>=<literal>)`` with ``name``
    in a fixed input vocabulary. Every other construct -- other imports, names,
    attribute chains, subscripts, operators, assignments, control flow,
    ``**kwargs`` -- is refused with ``AgentRequestError`` (400). The command is
    only parsed; it is never compiled to bytecode or executed.
    """
    if len(command) > _MAX_LEGACY_COMMAND_CHARS:
        raise AgentRequestError(
            413, "legacy_command_too_large", "legacy command is too large"
        )
    try:
        tree = ast.parse(command, mode="exec")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        raise _unsupported_legacy("command is not a parseable statement list") from None
    calls: list[LegacyCall] = []
    for index, stmt in enumerate(tree.body):
        if isinstance(stmt, ast.Import):
            if [(alias.name, alias.asname) for alias in stmt.names] == [
                ("pyautogui", None)
            ]:
                continue
            raise _unsupported_legacy(f"statement {index}: only 'import pyautogui'")
        call = stmt.value if isinstance(stmt, ast.Expr) else None
        if not isinstance(call, ast.Call):
            raise _unsupported_legacy(
                f"statement {index}: only pyautogui input calls are allowed"
            )
        func = call.func
        if not (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "pyautogui"
            and func.attr in _LEGACY_PYAUTOGUI_CALLS
        ):
            raise _unsupported_legacy(
                f"statement {index}: only pyautogui input calls are allowed"
            )
        if len(call.args) + len(call.keywords) > _MAX_LEGACY_ARGS:
            raise _unsupported_legacy(f"statement {index}: too many arguments")
        args = tuple(_legacy_literal(arg) for arg in call.args)
        kwargs: dict[str, Any] = {}
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg not in _LEGACY_KEYWORDS:
                raise _unsupported_legacy(
                    f"statement {index}: keyword {keyword.arg!r} is not allowed"
                )
            kwargs[keyword.arg] = _legacy_literal(keyword.value)
        calls.append((func.attr, args, kwargs))
    if not calls:
        raise _unsupported_legacy("command contains no pyautogui input call")
    if len(calls) > _MAX_LEGACY_CALLS:
        raise _unsupported_legacy("command contains too many calls")
    return calls


def _perform_legacy_calls(calls: list[LegacyCall]) -> dict[str, Any]:
    """Dispatch parsed legacy calls to pyautogui's fixed input functions."""
    import pyautogui  # noqa: PLC0415 - Windows guest only

    # The compiled replay legitimately drives the cursor into screen corners.
    pyautogui.FAILSAFE = False
    for name, args, kwargs in calls:
        if name not in _LEGACY_PYAUTOGUI_CALLS:  # parse_legacy_command guarantees
            raise AgentRequestError(400, "unsupported_legacy_command", name)
        getattr(pyautogui, name)(*args, **kwargs)
    return {"status": "ok", "output": "", "calls": len(calls)}


def _delivery_receipt(
    operation: str,
    *,
    native: bool,
    target_fingerprint: Optional[str] = None,
) -> dict[str, Any]:
    """Receipt for input delivery only -- never an outcome assertion."""
    return {
        "status": "delivered",
        "receipt_id": secrets.token_hex(12),
        "operation": operation,
        "native": native,
        "target_fingerprint": target_fingerprint,
        "delivered_at": datetime.now(timezone.utc).isoformat(),
        "outcome_verified": False,
    }


def _perform_input(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one bounded physical-input request in the interactive session."""
    data = _exact_object(
        payload,
        required=frozenset({"action"}),
        optional=frozenset(
            {
                "x",
                "y",
                "double",
                "button",
                "end_x",
                "end_y",
                "text",
                "interval_s",
                "keys",
                "horizontal_notches",
                "vertical_notches",
            }
        ),
        label="input request",
    )
    action = data["action"]
    if action not in {"click", "drag", "type_text", "press", "scroll"}:
        raise AgentRequestError(400, "unsupported_action", "unsupported input action")

    if action == "click":
        expected = {"action", "x", "y", "double", "button"}
        if set(data) - expected or not {"x", "y"}.issubset(data):
            raise AgentRequestError(400, "invalid_schema", "invalid click fields")
        x = _bounded_int(data["x"], "x")
        y = _bounded_int(data["y"], "y")
        double = data.get("double", False)
        if not isinstance(double, bool):
            raise AgentRequestError(400, "invalid_schema", "double must be boolean")
        button = data.get("button", "left")
        if button not in {"left", "right"}:
            raise AgentRequestError(
                400, "invalid_schema", "button must be left or right"
            )
        if double and button != "left":
            raise AgentRequestError(
                400, "invalid_schema", "double right click is unsupported"
            )
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        if double:
            pyautogui.doubleClick(x, y, button=button)
        else:
            pyautogui.click(x, y, button=button)
        return _delivery_receipt(
            "physical_double_click"
            if double
            else ("physical_right_click" if button == "right" else "physical_click"),
            native=False,
        )

    if action == "drag":
        if set(data) != {"action", "x", "y", "end_x", "end_y"}:
            raise AgentRequestError(400, "invalid_schema", "invalid drag fields")
        x = _bounded_int(data["x"], "x")
        y = _bounded_int(data["y"], "y")
        end_x = _bounded_int(data["end_x"], "end_x")
        end_y = _bounded_int(data["end_y"], "end_y")
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        down_sent = False
        try:
            pyautogui.moveTo(x, y)
            pyautogui.mouseDown(button="left")
            down_sent = True
            pyautogui.moveTo(end_x, end_y, duration=0.2)
        finally:
            if down_sent:
                pyautogui.mouseUp(button="left")
        return _delivery_receipt("physical_drag", native=False)

    if action == "type_text":
        expected = {"action", "text", "interval_s"}
        if set(data) - expected or "text" not in data:
            raise AgentRequestError(400, "invalid_schema", "invalid type_text fields")
        text = data["text"]
        interval = data.get("interval_s", 0.05)
        if not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS:
            raise AgentRequestError(
                400, "invalid_schema", "text exceeds the bounded string contract"
            )
        if (
            isinstance(interval, bool)
            or not isinstance(interval, (int, float))
            or not 0 <= float(interval) <= 1
        ):
            raise AgentRequestError(
                400, "invalid_schema", "interval_s must be between 0 and 1"
            )
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        if text:
            if text.isascii():
                pyautogui.write(text, interval=float(interval))
            else:
                encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
                ps = (
                    "Set-Clipboard -Value ([Text.Encoding]::UTF8.GetString("
                    f"[Convert]::FromBase64String('{encoded}')))"
                )
                completed = subprocess.run(
                    ["powershell", "-NoProfile", "-Command", ps],
                    capture_output=True,
                    timeout=10,
                )
                if completed.returncode != 0:
                    raise RuntimeError("Set-Clipboard failed")
                time.sleep(0.2)
                pyautogui.hotkey("ctrl", "v")
        return _delivery_receipt("physical_type_text", native=False)

    if action == "press":
        if set(data) != {"action", "keys"}:
            raise AgentRequestError(400, "invalid_schema", "invalid press fields")
        keys = data["keys"]
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 4
            or any(not isinstance(key, str) or not 1 <= len(key) <= 32 for key in keys)
        ):
            raise AgentRequestError(
                400, "invalid_schema", "keys must contain 1-4 bounded strings"
            )
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        if len(keys) == 1:
            pyautogui.press(keys[0])
        else:
            pyautogui.hotkey(*keys)
        return _delivery_receipt("physical_press", native=False)

    if set(data) != {"action", "horizontal_notches", "vertical_notches"}:
        raise AgentRequestError(400, "invalid_schema", "invalid scroll fields")
    horizontal = _bounded_int(
        data["horizontal_notches"], "horizontal_notches", limit=1000
    )
    vertical = _bounded_int(data["vertical_notches"], "vertical_notches", limit=1000)
    import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

    pyautogui.FAILSAFE = False
    if vertical:
        pyautogui.scroll(vertical)
    if horizontal:
        pyautogui.hscroll(horizontal)
    return _delivery_receipt("physical_scroll", native=False)


def _control_value(control: object, attr: str, default: object = "") -> object:
    try:
        return getattr(control, attr, default)
    except Exception:  # noqa: BLE001 - accessibility providers are fallible
        return default


def _parent(control: object) -> Optional[object]:
    method = _control_value(control, "GetParentControl", None)
    try:
        return method() if callable(method) else None
    except Exception:  # noqa: BLE001
        return None


def _window_name(control: object) -> Optional[str]:
    node: Optional[object] = control
    for _ in range(_MAX_UIA_DEPTH):
        if str(_control_value(node, "ControlTypeName", "")) == "WindowControl":
            value = " ".join(str(_control_value(node, "Name", "") or "").split())
            return value or None
        node = _parent(node)
        if node is None:
            break
    return None


def _role(control: object) -> Optional[str]:
    control_type = str(_control_value(control, "ControlTypeName", ""))
    if control_type == "SplitButtonControl":
        return "button"
    return _CONTROL_TO_ROLE.get(control_type)


def _bounds(control: object) -> Optional[list[int]]:
    rect: Any = _control_value(control, "BoundingRectangle", None)
    try:
        if rect is None or rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return [int(rect.left), int(rect.top), int(rect.right), int(rect.bottom)]
    except Exception:  # noqa: BLE001
        return None


def _uniquely_names(auto: Any, locator: dict[str, Any], control: object) -> bool:
    """True when ``locator`` matches exactly one element now, and it is ``control``."""
    found, truncated = _find_candidates(locator, auto)
    if truncated or len(found) != 1:
        return False
    mine = _candidate(control)
    return mine is not None and hmac.compare_digest(
        mine["fingerprint"], found[0][1]["fingerprint"]
    )


def _centered_on(control: object, x: int, y: int) -> bool:
    """True when (x, y) lies in the middle half of the control, on both axes."""
    bounds = _bounds(control)
    if bounds is None:
        return False
    left, top, right, bottom = bounds
    return (
        abs(2 * x - (left + right)) <= (right - left) / 2
        and abs(2 * y - (top + bottom)) <= (bottom - top) / 2
    )


def _locator_from_control(control: object) -> Optional[dict[str, Any]]:
    automation_id = str(_control_value(control, "AutomationId", "") or "") or None
    name = " ".join(str(_control_value(control, "Name", "") or "").split()) or None
    role = _role(control)
    if not automation_id and not (role and name):
        return None
    return {
        "automation_id": automation_id,
        "role": role,
        "name": name,
        "window_name": _window_name(control),
    }


_MAX_IDENTITY_TEXT = 512


class _IdentityUnreadable(Exception):
    """One identity read failed; the identity is refused, never truncated."""


def _identity_text(control: object, attr: str) -> Optional[str]:
    try:
        raw = getattr(control, attr, "")
    except Exception as exc:  # noqa: BLE001 - providers are fallible
        raise _IdentityUnreadable(attr) from exc
    value = " ".join(str(raw or "").split())
    if len(value) > _MAX_IDENTITY_TEXT:
        # Two long rows sharing a prefix would read the same if cut short.
        raise _IdentityUnreadable(attr)
    return value or None


def _identity_parent(control: object) -> Optional[object]:
    try:
        return control.GetParentControl()  # type: ignore[attr-defined]
    except Exception as exc:  # noqa: BLE001 - a fault is not the root
        raise _IdentityUnreadable("parent") from exc


def _element_identity(control: object) -> Optional[dict[str, Any]]:
    """Describe one element by role, name, AutomationId and its window path.

    The ancestry runs parent-first up to and including the top-level window,
    the outermost ``WindowControl`` below the desktop root. An owned dialog
    sits under its owner, so ``window`` is then the owner's name and the
    dialog appears in ``ancestry``. Returns None, never a partial identity,
    when the element has neither an AutomationId nor a role and name, when a
    provider read fails, when a name is longer than the bound, or when the
    chain does not reach a desktop root within ``_MAX_UIA_DEPTH`` parents.
    ``process`` is None when the executable cannot be read; a consumer must
    never treat two None processes as the same.
    """
    try:
        role = _role(control)
        name = _identity_text(control, "Name")
        automation_id = _identity_text(control, "AutomationId")
        if not automation_id and not (role and name):
            return None
        chain: list[object] = []
        node = _identity_parent(control)
        for _ in range(_MAX_UIA_DEPTH):
            if node is None:
                break
            chain.append(node)
            node = _identity_parent(node)
        else:
            return None
        if (
            not chain
            or str(_control_value(chain[-1], "ControlTypeName", "")) != "PaneControl"
        ):
            return None  # the last node must be the desktop root
        window_index = None
        for index, ancestor in enumerate(chain[:-1]):
            if str(_control_value(ancestor, "ControlTypeName", "")) == "WindowControl":
                window_index = index
        if window_index is None:
            return None
        ancestry = [
            [_role(ancestor), _identity_text(ancestor, "Name")]
            for ancestor in chain[: window_index + 1]
        ]
    except _IdentityUnreadable:
        return None
    process_id = _control_value(control, "ProcessId", 0)
    return {
        "source": "uia",
        "role": role,
        "name": name,
        "automation_id": automation_id,
        "ancestry": ancestry,
        "window": ancestry[-1][1],
        "process": _process_image_identifier(
            process_id if isinstance(process_id, int) else 0
        ),
    }


def _locator_payload(value: object) -> dict[str, Any]:
    data = _exact_object(
        value,
        optional=frozenset({"automation_id", "role", "name", "window_name"}),
        label="locator",
    )
    out: dict[str, Any] = {}
    for key in ("automation_id", "role", "name", "window_name"):
        item = data.get(key)
        if item is not None and (
            not isinstance(item, str) or not 1 <= len(item) <= 512
        ):
            raise AgentRequestError(
                400, "invalid_schema", f"locator.{key} must be a bounded string or null"
            )
        out[key] = item or None
    if not out["automation_id"] and not (out["role"] and out["name"]):
        raise AgentRequestError(
            400, "invalid_locator", "locator needs automation_id or exact role+name"
        )
    if out["role"] and out["role"] not in _ROLE_TO_CONTROL:
        raise AgentRequestError(400, "invalid_locator", "unsupported locator role")
    return out


def _runtime_id(control: object) -> list[int]:
    """Return UIA's per-element runtime id as a bounded JSON value."""
    method = _control_value(control, "GetRuntimeId", None)
    try:
        value = list(method()) if callable(method) else []
    except Exception:  # noqa: BLE001 - some providers omit RuntimeId
        return []
    if len(value) > 64 or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        return []
    return value


def _target_fingerprint(control: object, candidate: dict[str, Any]) -> str:
    """Bind resolution to this exact live UIA element and geometry.

    The locator alone is intentionally insufficient: a closed/reopened window
    can expose the same AutomationId/name while referring to a different live
    control. RuntimeId/process/handle and bounds make that replacement (or a
    move that would invalidate the recorded verification point) stale.
    """
    identity = {
        "locator": {
            key: candidate.get(key)
            for key in ("automation_id", "role", "name", "window_name")
        },
        "bounds": candidate.get("bounds"),
        "runtime_id": _runtime_id(control),
        "process_id": _control_value(control, "ProcessId", 0),
        "native_window_handle": _control_value(control, "NativeWindowHandle", 0),
        "class_name": str(_control_value(control, "ClassName", "") or ""),
    }
    canonical = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _candidate(control: object) -> Optional[dict[str, Any]]:
    locator = _locator_from_control(control)
    bounds = _bounds(control)
    if locator is None or bounds is None:
        return None
    role = locator["role"]
    operations = {
        "button": ["invoke"],
        "link": ["invoke"],
        "menuitem": ["invoke"],
        "textbox": ["focus"],
        "checkbox": ["toggle"],
        "radio": ["select"],
        "tab": ["select"],
        "listitem": ["select"],
    }.get(role, [])
    candidate = {
        **locator,
        "bounds": bounds,
        "point": [int((bounds[0] + bounds[2]) / 2), int((bounds[1] + bounds[3]) / 2)],
        "supported_operations": operations,
    }
    candidate["fingerprint"] = _target_fingerprint(control, candidate)
    return candidate


def _find_candidates(
    locator: dict[str, Any], auto: Any
) -> tuple[list[tuple[Any, dict[str, Any]]], bool]:
    try:
        root = auto.GetRootControl()
    except Exception as exc:  # noqa: BLE001
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation is unavailable"
        ) from exc
    stack: list[tuple[Any, int]] = [(root, 0)]
    found: list[tuple[Any, dict[str, Any]]] = []
    visited = 0
    truncated = False
    while stack:
        control, depth = stack.pop()
        visited += 1
        if visited > _MAX_UIA_NODES:
            truncated = True
            break
        candidate = _candidate(control)
        if candidate is not None:
            matches = True
            for key in ("automation_id", "role", "name", "window_name"):
                expected = locator.get(key)
                if expected is not None and candidate.get(key) != expected:
                    matches = False
                    break
            if matches:
                found.append((control, candidate))
                if len(found) >= _MAX_UIA_CANDIDATES:
                    truncated = True
                    break
        if depth >= _MAX_UIA_DEPTH:
            continue
        children = _control_value(control, "GetChildren", None)
        try:
            values = list(children()) if callable(children) else []
        except Exception:  # noqa: BLE001
            values = []
        stack.extend((child, depth + 1) for child in reversed(values))
    return found, truncated


def _structured_text_at(auto: Any, x: int, y: int) -> Optional[str]:
    try:
        element = auto.ControlFromPoint(x, y)
    except Exception:  # noqa: BLE001
        return None
    if element is None:
        return None
    row = element
    found_row = False
    for _ in range(6):
        if str(_control_value(row, "ControlTypeName", "")) in {
            "DataItemControl",
            "ListItemControl",
            "TreeItemControl",
            "TableRowControl",
        }:
            found_row = True
            break
        row = _parent(row)
        if row is None:
            break
    if not found_row or row is None:
        return None
    own = element
    for _ in range(6):
        parent = _parent(own)
        if parent is None:
            own = None
            break
        if parent is row:
            break
        own = parent
    parts: list[str] = []
    children = _control_value(row, "GetChildren", None)
    try:
        for child in children() if callable(children) else []:
            if own is not None and child is own:
                continue
            name = str(_control_value(child, "Name", "") or "")
            if name:
                parts.append(name)
    except Exception:  # noqa: BLE001
        pass
    if not parts:
        name = str(_control_value(row, "Name", "") or "")
        if name:
            parts.append(name)
    value = " ".join(" ".join(parts).split())
    return value or None


_CENSUS_ROW_TYPES = frozenset(
    {"DataItemControl", "ListItemControl", "TreeItemControl", "TableRowControl"}
)
_MAX_CENSUS_ROWS = 2000
_SINGLE_NUMBER = re.compile(r"^\D*(\d+)\D*$")
# Mirror of identity_census.positional_id.
_POSITION_WORDS = (
    "row|item|idx|index|cell|col|column|line|pos|position|listviewitem|node|child"
)
_POSITION_TOKEN = re.compile(rf"^(?:(?:{_POSITION_WORDS})\d+|r\d+c\d+)$", re.IGNORECASE)
_POSITION_WORD_TOKEN = re.compile(rf"^(?:{_POSITION_WORDS}|r|c)$", re.IGNORECASE)


def _positional_id(value: str, index: int) -> bool:
    """Whether a row id names a position (``row-3``, ``r3c2``) not a record."""

    tokens = [t for t in re.split(r"[^A-Za-z0-9]+", value) if t]
    if any(_POSITION_TOKEN.match(t) for t in tokens):
        return True
    for word, following in zip(tokens, tokens[1:]):
        if _POSITION_WORD_TOKEN.match(word) and following.isdigit():
            return True
    if len(tokens) > 1 and all(t.isdigit() and len(t) <= 3 for t in tokens):
        return True
    match = _SINGLE_NUMBER.match(value)
    return bool(match and int(match.group(1)) in (index, index + 1))


def _row_identity(row: object, excluded: Optional[int]) -> Optional[str]:
    """A row's identity as ``_structured_text_at`` reads it, by child index."""

    parts: list[str] = []
    for index, child in enumerate(_children(row)):
        if excluded is not None and index == excluded:
            continue
        name = str(_control_value(child, "Name", "") or "")
        if name:
            parts.append(name)
    if not parts:
        name = str(_control_value(row, "Name", "") or "")
        if name:
            parts.append(name)
    value = " ".join(" ".join(parts).split())
    return value or None


def _census_record_key(
    row: object, branch: Optional[object], index: int
) -> Optional[str]:
    """The row's AutomationIds (row and target branch), minus positional ones."""

    keys: set[str] = set()
    for control in (row, branch):
        if control is None:
            continue
        value = str(_control_value(control, "AutomationId", "") or "").strip()
        if not value or len(value) > 256:
            continue
        if _positional_id(value, index):
            continue
        keys.add(f"automation-id={value}")
    return "\n".join(sorted(keys)) or None


def _identity_candidates_at(auto: Any, x: int, y: int) -> Optional[dict[str, Any]]:
    """Census of the shown rows that read the same as the row at (x, y).

    Mirrors ``_structured_text_at`` (and ``identity_census.tree_row_census``
    in the openadapt_flow package, which this guest copy cannot import): the
    record row is the nearest data, list, tree or table-row item; its identity
    is its children's names except the clicked cell; every sibling row of the
    same control type is read at the same child index. A container whose Grid
    pattern reports more rows than it exposes, or a scrollable container that
    realizes items on demand (ItemContainer pattern), is ``incomplete``: its
    off-screen rows are not in the tree. ``{"row": False}`` when the point is
    in no row; None when UIA fails.
    """
    try:
        element = auto.ControlFromPoint(x, y)
    except Exception:  # noqa: BLE001
        return None
    if element is None:
        return None
    row: Optional[object] = element
    for _ in range(6):
        if str(_control_value(row, "ControlTypeName", "")) in _CENSUS_ROW_TYPES:
            break
        row = _parent(row)
        if row is None:
            break
    else:
        row = None
    if row is None or (
        str(_control_value(row, "ControlTypeName", "")) not in _CENSUS_ROW_TYPES
    ):
        return {"row": False}
    own: Optional[object] = element
    for _ in range(6):
        parent = _parent(own)
        if parent is None:
            own = None
            break
        if parent is row:
            break
        own = parent
    row_children = _children(row)
    excluded = next(
        (i for i, child in enumerate(row_children) if own is not None and child is own),
        None,
    )
    branch = row_children[excluded] if excluded is not None else None
    target_identity = _row_identity(row, excluded)
    if not target_identity:
        return None
    container = _parent(row)
    row_type = str(_control_value(row, "ControlTypeName", ""))
    siblings = _children(container) if container is not None else [row]
    rows = [
        c
        for c in siblings
        if c is row or str(_control_value(c, "ControlTypeName", "")) == row_type
    ]

    # The target row's own wrapper may differ from the sibling's; locate it by
    # runtime id when the provider exposes one, else by bounds.
    def same(a: object, b: object) -> bool:
        if a is b:
            return True
        compare = getattr(auto, "ControlsAreSame", None)
        if callable(compare):
            try:
                return bool(compare(a, b))
            except Exception:  # noqa: BLE001
                pass
        return _bounds(a) is not None and _bounds(a) == _bounds(b)

    row_index = next((i for i, c in enumerate(rows) if same(c, row)), None)
    incomplete = len(rows) > _MAX_CENSUS_ROWS or row_index is None
    if container is not None and not incomplete:
        # A provider that fails while answering is not a complete answer.
        try:
            grid_get = _control_value(container, "GetGridPattern", None)
            grid = grid_get() if callable(grid_get) else None
            declared = int(grid.RowCount) if grid is not None else None
            scroll_get = _control_value(container, "GetScrollPattern", None)
            scroll = scroll_get() if callable(scroll_get) else None
            scrollable = (
                bool(scroll.VerticallyScrollable) if scroll is not None else False
            )
            items_get = _control_value(container, "GetItemContainerPattern", None)
            realizes = callable(items_get) and items_get() is not None
        except Exception:  # noqa: BLE001
            declared, scrollable, realizes, incomplete = None, True, True, True
        if declared is not None and declared > len(rows):
            incomplete = True
        if scrollable and realizes:
            incomplete = True
    result: dict[str, Any] = {
        "record_key": _census_record_key(row, branch, row_index or 0),
        "incomplete": incomplete,
        "top_level": True,
        "candidates": [],
    }
    if incomplete:
        return result
    origin = _bounds(row)
    for index, candidate in enumerate(rows):
        is_self = index == row_index
        if is_self:
            observed: Optional[str] = target_identity
            candidate_branch = branch
        else:
            observed = _row_identity(candidate, excluded)
            kids = _children(candidate)
            if row_children and not kids:
                # A row whose cells are not built yet: it cannot be read.
                result["incomplete"] = True
                result["candidates"] = []
                return result
            candidate_branch = (
                kids[excluded]
                if excluded is not None and excluded < len(kids)
                else None
            )
        if observed != target_identity:
            continue
        here = _bounds(candidate)
        result["candidates"].append(
            {
                "self": is_self,
                "record_key": (
                    result["record_key"]
                    if is_self
                    else _census_record_key(candidate, candidate_branch, index)
                ),
                "dx": here[0] - origin[0] if here and origin else None,
                "dy": here[1] - origin[1] if here and origin else None,
            }
        )
    return result


_ROW_TYPES = frozenset(
    {
        "DataItemControl",
        "ListItemControl",
        "TreeItemControl",
        "TableRowControl",
        "CustomControl",
    }
)
_MAX_READ_TEXT = 4_096
_MAX_READ_ROWS = 200
_MAX_ROW_DEPTH = 4


def _value_text(control: object) -> Optional[str]:
    """The text a person sees in ``control``: its value, else its name.

    Read-only. Edit boxes and grid cells expose their content through the
    Value pattern (an empty box reads as empty, never as its name); other
    controls through the legacy MSAA value or their Name.
    """
    method = _control_value(control, "GetValuePattern", None)
    if callable(method):
        try:
            pattern = method()
            value = getattr(pattern, "Value", None) if pattern is not None else None
        except Exception:  # noqa: BLE001 - pattern not supported
            value = None
        if isinstance(value, str):
            return value[:_MAX_READ_TEXT]
    method = _control_value(control, "GetLegacyIAccessiblePattern", None)
    if callable(method):
        try:
            value = getattr(method(), "Value", None)
        except Exception:  # noqa: BLE001 - pattern not supported
            value = None
        if isinstance(value, str) and value:
            return value[:_MAX_READ_TEXT]
    name = _control_value(control, "Name", "")
    if isinstance(name, str) and name:
        return name[:_MAX_READ_TEXT]
    return None


def _children(control: object) -> list[object]:
    method = _control_value(control, "GetChildren", None)
    try:
        return list(method()) if callable(method) else []
    except Exception:  # noqa: BLE001
        return []


def _row_text(row: object) -> Optional[str]:
    parts = [text for text in (_value_text(c) for c in _children(row)) if text]
    if not parts:
        own = _value_text(row)
        return " ".join(own.split()) if own else None
    return " ".join(" ".join(parts).split())[:_MAX_READ_TEXT] or None


def _read_rows(container: object) -> tuple[list[dict[str, Any]], bool]:
    """Row-type descendants of ``container`` (grid or list rows), in order."""
    rows: list[dict[str, Any]] = []
    stack: list[tuple[object, int]] = [
        (child, 1) for child in reversed(_children(container))
    ]
    truncated = False
    while stack:
        control, depth = stack.pop()
        if str(_control_value(control, "ControlTypeName", "")) in _ROW_TYPES:
            bounds = _bounds(control)
            text = _row_text(control)
            if bounds is not None and text:
                cells = []
                for cell in _children(control)[:_MAX_READ_ROWS]:
                    cell_bounds = _bounds(cell)
                    cell_text = _value_text(cell)
                    if cell_bounds is not None and cell_text:
                        cells.append(
                            {
                                "text": " ".join(cell_text.split()),
                                "point": [
                                    int((cell_bounds[0] + cell_bounds[2]) / 2),
                                    int((cell_bounds[1] + cell_bounds[3]) / 2),
                                ],
                            }
                        )
                rows.append(
                    {
                        "text": text,
                        "point": [
                            int((bounds[0] + bounds[2]) / 2),
                            int((bounds[1] + bounds[3]) / 2),
                        ],
                        "cells": cells,
                    }
                )
                if len(rows) >= _MAX_READ_ROWS:
                    truncated = True
                    break
            continue
        if depth < _MAX_ROW_DEPTH:
            stack.extend((c, depth + 1) for c in reversed(_children(control)))
    return rows, truncated


def _perform_uia_initialized(
    auto: Any, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Perform one typed UIA operation in an initialized COM apartment."""
    if operation in {
        "locator-at",
        "element-identity-at",
        "text-at-point",
        "focused-at-point",
        "identity-candidates-at",
    }:
        data = _exact_object(payload, required=frozenset({"x", "y"}), label=operation)
        x = _bounded_int(data["x"], "x")
        y = _bounded_int(data["y"], "y")
        if operation == "text-at-point":
            return {"status": "ok", "text": _structured_text_at(auto, x, y)}
        if operation == "identity-candidates-at":
            return {"status": "ok", "census": _identity_candidates_at(auto, x, y)}
        try:
            element = auto.ControlFromPoint(x, y)
        except Exception as exc:  # noqa: BLE001
            raise AgentRequestError(
                503, "uia_unavailable", "UI Automation is unavailable"
            ) from exc
        if element is None:
            if operation == "element-identity-at":
                return {"status": "ok", "identity": None}
            return {"status": "ok", "locator": None}
        actionable: Optional[object] = None
        node: Optional[object] = element
        for _ in range(6):
            if str(_control_value(node, "ControlTypeName", "")) in _ACTIONABLE_TYPES:
                actionable = node
                break
            node = _parent(node)
            if node is None:
                break
        if operation == "focused-at-point":
            point_control = actionable or element
            try:
                focused = auto.GetFocusedControl()
            except Exception as exc:  # noqa: BLE001
                raise AgentRequestError(
                    503, "uia_unavailable", "focused UIA control is unavailable"
                ) from exc
            if focused is None:
                return {"status": "ok", "focused": False}
            focused_actionable: Optional[object] = None
            node = focused
            for _ in range(6):
                if (
                    str(_control_value(node, "ControlTypeName", ""))
                    in _ACTIONABLE_TYPES
                ):
                    focused_actionable = node
                    break
                node = _parent(node)
                if node is None:
                    break
            focused_control = focused_actionable or focused
            point_candidate = _candidate(point_control)
            focused_candidate = _candidate(focused_control)
            if point_candidate is None or focused_candidate is None:
                return {"status": "ok", "focused": False}
            matches = hmac.compare_digest(
                point_candidate["fingerprint"],
                focused_candidate["fingerprint"],
            )
            return {
                "status": "ok",
                "focused": matches,
                "target_fingerprint": (
                    point_candidate["fingerprint"] if matches else None
                ),
            }
        if operation == "element-identity-at":
            return {
                "status": "ok",
                "identity": _element_identity(actionable or element),
            }
        if actionable is None and not _centered_on(element, x, y):
            # A structural replay acts at the element's centre. A painted
            # toolbar or grid is one large pane whose centre is some other
            # button, so it is no locator for this click.
            return {"status": "ok", "locator": None}
        target = actionable or element
        locator = _locator_from_control(target)
        if locator is not None and not _uniquely_names(auto, locator, target):
            # WPF gives every text box's inner edit AutomationId "textBox":
            # a locator that matches others too could only ever be refused.
            locator = None
        return {"status": "ok", "locator": locator}

    if operation == "read":
        data = _exact_object(
            payload,
            required=frozenset({"locator"}),
            optional=frozenset({"rows"}),
            label="uia read",
        )
        if not isinstance(data.get("rows", False), bool):
            raise AgentRequestError(400, "invalid_schema", "rows must be a boolean")
        locator = _locator_payload(data["locator"])
        found, truncated = _find_candidates(locator, auto)
        if len(found) != 1 or truncated:
            return {
                "status": "ok",
                "match": "ambiguous" if found or truncated else "not_found",
                "candidate_count": len(found),
                "text": None,
            }
        control, candidate = found[0]
        result: dict[str, Any] = {
            "status": "ok",
            "match": "unique",
            "candidate_count": 1,
            "text": _value_text(control),
            "point": candidate["point"],
        }
        if data.get("rows"):
            rows, rows_truncated = _read_rows(control)
            result["rows"] = rows
            result["rows_truncated"] = rows_truncated
        return result

    data = _exact_object(
        payload,
        required=frozenset({"locator"}),
        optional=frozenset({"operation", "expected_fingerprint"}),
        label=f"uia {operation}",
    )
    locator = _locator_payload(data["locator"])
    found, truncated = _find_candidates(locator, auto)
    candidates = [candidate for _, candidate in found]
    if operation == "find":
        match = (
            "ambiguous"
            if len(found) > 1 or truncated
            else "unique"
            if len(found) == 1
            else "not_found"
        )
        return {
            "status": "ok",
            "match": match,
            "candidate_count": len(found),
            "truncated": truncated,
            "candidates": candidates,
        }
    if operation != "act":
        raise AgentRequestError(404, "not_found", "unknown UIA operation")
    if len(found) != 1 or truncated:
        code = "ambiguous_target" if found or truncated else "target_not_found"
        raise AgentRequestError(409, code, "UIA target is not uniquely resolvable")
    requested = data.get("operation")
    expected_fingerprint = data.get("expected_fingerprint")
    if requested not in {"click", "double_click"}:
        raise AgentRequestError(400, "unsupported_action", "unsupported UIA action")
    if not isinstance(expected_fingerprint, str) or len(expected_fingerprint) != 64:
        raise AgentRequestError(
            400, "invalid_schema", "expected_fingerprint is required"
        )
    control, candidate = found[0]
    if not hmac.compare_digest(expected_fingerprint, candidate["fingerprint"]):
        raise AgentRequestError(
            409, "stale_target", "UIA target changed after resolution"
        )
    if requested == "double_click":
        raise AgentRequestError(
            409, "native_action_unavailable", "native double-click is unavailable"
        )
    if not _hits_target(auto, control, candidate):
        raise AgentRequestError(
            409,
            "target_obscured",
            "another window or dialog is over the UIA target",
        )
    role = candidate.get("role")
    user32 = _user32_factory() if os.name == "nt" else None
    if user32 is not None:
        _window_guard(user32, _window_handle(control), tuple(candidate["point"]))
    try:
        if role in {"button", "link", "menuitem"}:
            hwnd = _control_value(control, "NativeWindowHandle", 0)
            if (
                role == "button"
                and user32 is not None
                and isinstance(hwnd, int)
                and hwnd
                and _is_push_button(user32, hwnd)
            ):
                _post_button_click(user32, hwnd)
                delivered = "win32_bm_click"
            else:
                _invoke_off_thread(locator, candidate["fingerprint"])
                delivered = "uia_invoke"
        elif role == "textbox":
            if control.SetFocus() is False:
                raise AgentRequestError(
                    409, "native_action_failed", "native UIA focus was rejected"
                )
            delivered = "uia_focus"
        elif role == "checkbox":
            control.GetTogglePattern().Toggle()
            delivered = "uia_toggle"
        elif role in {"radio", "tab", "listitem"}:
            control.GetSelectionItemPattern().Select()
            delivered = "uia_select"
        else:
            raise AgentRequestError(
                409, "native_action_unavailable", "no native action pattern"
            )
    except AgentRequestError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AgentRequestError(
            409, "native_action_failed", "native UIA action failed"
        ) from exc
    return {
        "status": "ok",
        "candidate_count": 1,
        "receipt": _delivery_receipt(
            delivered,
            native=True,
            target_fingerprint=candidate["fingerprint"],
        ),
    }


def _hits_target(auto: Any, control: object, candidate: dict[str, Any]) -> bool:
    """Whether the element at the target's own point is the target.

    UI Automation reaches controls that another window covers, or that a
    modal dialog blocks. A native action must only go to a control a person
    could click there, so the element under its point (or one of that
    element's ancestors) must be the target itself. Without a runtime id the
    two cannot be compared and the answer is no.
    """
    target_id = _runtime_id(control)
    if not target_id:
        return False
    x, y = candidate["point"]
    try:
        node: Optional[object] = auto.ControlFromPoint(int(x), int(y))
    except Exception:  # noqa: BLE001 - no hit test, no action
        return False
    for _ in range(_MAX_UIA_DEPTH):
        if node is None:
            return False
        if _runtime_id(node) == target_id:
            return True
        node = _parent(node)
    return False


_BM_CLICK = 0x00F5
_GA_ROOT = 2
_GA_ROOTOWNER = 3
_PUSH_BUTTON_CLASSES = ("button", "windowsforms10.button")


class _User32:
    """The user32 calls the native-action guards make (Windows only)."""

    def __init__(self) -> None:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        self._ctypes = ctypes
        self._wintypes = wintypes
        dll = getattr(ctypes, "WinDLL")("user32", use_last_error=True)
        for name, args, res in (
            ("IsWindow", [wintypes.HWND], wintypes.BOOL),
            ("IsWindowEnabled", [wintypes.HWND], wintypes.BOOL),
            ("IsWindowVisible", [wintypes.HWND], wintypes.BOOL),
            ("GetAncestor", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            ("GetForegroundWindow", [], wintypes.HWND),
            ("WindowFromPoint", [wintypes.POINT], wintypes.HWND),
            (
                "GetClassNameW",
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
            (
                "PostMessageW",
                [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM],
                wintypes.BOOL,
            ),
            ("GetWindow", [wintypes.HWND, wintypes.UINT], wintypes.HWND),
            (
                "GetWindowThreadProcessId",
                [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)],
                wintypes.DWORD,
            ),
            (
                "GetWindowRect",
                [wintypes.HWND, ctypes.POINTER(wintypes.RECT)],
                wintypes.BOOL,
            ),
            (
                "GetWindowTextW",
                [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int],
                ctypes.c_int,
            ),
        ):
            fn = getattr(dll, name)
            fn.argtypes = args
            fn.restype = res
        self._dll = dll
        self._enum_proc = getattr(ctypes, "WINFUNCTYPE")(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )
        dll.EnumWindows.argtypes = [self._enum_proc, wintypes.LPARAM]
        dll.EnumWindows.restype = wintypes.BOOL
        try:
            dwm = getattr(ctypes, "WinDLL")("dwmapi")
            dwm.DwmGetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwm.DwmGetWindowAttribute.restype = ctypes.c_long
            self._dwm = dwm
        except OSError:
            self._dwm = None

    def is_window(self, hwnd: int) -> bool:
        return bool(self._dll.IsWindow(hwnd))

    def is_enabled(self, hwnd: int) -> bool:
        return bool(self._dll.IsWindowEnabled(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._dll.IsWindowVisible(hwnd))

    def ancestor(self, hwnd: int, flag: int) -> int:
        return int(self._dll.GetAncestor(hwnd, flag) or 0)

    def foreground(self) -> int:
        return int(self._dll.GetForegroundWindow() or 0)

    def window_from_point(self, x: int, y: int) -> int:
        return int(self._dll.WindowFromPoint(self._wintypes.POINT(x, y)) or 0)

    def class_name(self, hwnd: int) -> str:
        buffer = self._ctypes.create_unicode_buffer(256)
        self._dll.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def post(self, hwnd: int, message: int) -> bool:
        return bool(self._dll.PostMessageW(hwnd, message, 0, 0))

    def top_level_windows(self) -> list[int]:
        """Every top-level window, topmost first (EnumWindows is z-order)."""
        found: list[int] = []

        def collect(hwnd, _param):
            found.append(int(hwnd or 0))
            return len(found) < 4096

        self._dll.EnumWindows(self._enum_proc(collect), 0)
        return [hwnd for hwnd in found if hwnd]

    def owner(self, hwnd: int) -> int:
        return int(self._dll.GetWindow(hwnd, 4) or 0)  # GW_OWNER

    def pid_of(self, hwnd: int) -> int:
        pid = self._wintypes.DWORD()
        self._dll.GetWindowThreadProcessId(hwnd, self._ctypes.byref(pid))
        return int(pid.value)

    def rect(self, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        """The window's visible frame: DWM's extended frame bounds, which
        leave out the invisible resize border GetWindowRect includes."""
        r = self._wintypes.RECT()
        if self._dwm is not None and (
            self._dwm.DwmGetWindowAttribute(
                hwnd, 9, self._ctypes.byref(r), self._ctypes.sizeof(r)
            )
            == 0
        ):
            return (int(r.left), int(r.top), int(r.right), int(r.bottom))
        if not self._dll.GetWindowRect(hwnd, self._ctypes.byref(r)):
            return None
        return (int(r.left), int(r.top), int(r.right), int(r.bottom))

    def outer_rect(self, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        """Everything the window may paint: GetWindowRect joined with the
        visible frame (a self-drawn frame can paint into the border DWM
        leaves out)."""
        r = self._wintypes.RECT()
        frame = self.rect(hwnd)
        if not self._dll.GetWindowRect(hwnd, self._ctypes.byref(r)):
            return frame
        outer = (int(r.left), int(r.top), int(r.right), int(r.bottom))
        if frame is None:
            return outer
        return (
            min(outer[0], frame[0]),
            min(outer[1], frame[1]),
            max(outer[2], frame[2]),
            max(outer[3], frame[3]),
        )

    def per_monitor_dpi(self) -> bool:
        """Put this thread in per-monitor-v2 DPI awareness, so window rects
        and points are physical pixels like the screenshot's."""
        return bool(self._dll.SetThreadDpiAwarenessContext(self._ctypes.c_void_p(-4)))

    def title(self, hwnd: int) -> str:
        buffer = self._ctypes.create_unicode_buffer(512)
        self._dll.GetWindowTextW(hwnd, buffer, 512)
        return buffer.value

    def is_cloaked(self, hwnd: int) -> Optional[bool]:
        """A cloaked window (another virtual desktop, a suspended UWP app) is
        'visible' to IsWindowVisible but not on screen. None: unknown."""
        if self._dwm is None:
            return None
        value = self._ctypes.c_int(0)
        result = self._dwm.DwmGetWindowAttribute(
            hwnd, 14, self._ctypes.byref(value), self._ctypes.sizeof(value)
        )
        if result != 0:
            return None
        return value.value != 0


#: Replaced in tests; Windows builds the real binding.
_user32_factory: Callable[[], Any] = _User32


def _window_handle(control: object) -> int:
    """The control's own window handle, or the nearest ancestor's."""
    node: Optional[object] = control
    for _ in range(_MAX_UIA_DEPTH):
        if node is None:
            break
        hwnd = _control_value(node, "NativeWindowHandle", 0)
        if isinstance(hwnd, int) and hwnd:
            return hwnd
        node = _parent(node)
    return 0


def _window_guard(user32: Any, hwnd: int, point: tuple[int, int]) -> None:
    """Refuse a native action that the target's window could not take.

    Checked with Win32 because UI Automation's own hit test passes through
    windows that are still loading (seen live with Notepad over the app):
    the window under the point must belong to the target's top-level
    window, and that window and the target's must be enabled. A window a
    modal dialog has disabled would drop the action without a word.
    """
    if not hwnd or not user32.is_window(hwnd):
        raise AgentRequestError(
            409, "stale_target", "UIA target changed after resolution"
        )
    root = user32.ancestor(hwnd, _GA_ROOT) or hwnd
    if not user32.is_enabled(hwnd) or not user32.is_enabled(root):
        raise AgentRequestError(
            409, "target_blocked", "the UIA target's window is not accepting input"
        )
    under = user32.window_from_point(int(point[0]), int(point[1]))
    if not under or (user32.ancestor(under, _GA_ROOT) or under) != root:
        raise AgentRequestError(
            409,
            "target_obscured",
            "another window or dialog is over the UIA target",
        )


def _is_push_button(user32: Any, hwnd: int) -> bool:
    name = user32.class_name(hwnd).casefold()
    return name == "button" or name.startswith(_PUSH_BUTTON_CLASSES[1])


def _post_button_click(user32: Any, hwnd: int) -> None:
    """Click a Win32 or WinForms push button by posting it ``BM_CLICK``.

    This is the message UI Automation's own Win32 button provider sends for
    Invoke, but posted: the click runs in the app's message loop and this
    returns at once, so a click handler that opens a modal dialog cannot hold
    the agent inside a call. Only a visible push button whose top-level
    window is the foreground one gets it (Windows documents that BM_CLICK
    can be dropped by an inactive dialog). The receipt is ``win32_bm_click``:
    the message was queued, and the outcome is for the run's checks.
    """
    if not user32.is_visible(hwnd):
        raise AgentRequestError(
            409, "target_obscured", "the UIA target's window is not visible"
        )
    owner = user32.ancestor(hwnd, _GA_ROOTOWNER) or hwnd
    foreground = user32.foreground()
    if (
        not foreground
        or (user32.ancestor(foreground, _GA_ROOTOWNER) or foreground) != owner
    ):
        raise AgentRequestError(
            409, "target_blocked", "the UIA target's window is not in front"
        )
    if not user32.post(hwnd, _BM_CLICK):
        raise AgentRequestError(409, "native_action_failed", "native UIA action failed")


def _invoke_off_thread(locator: dict[str, Any], fingerprint: str) -> None:
    """Invoke the exact resolved control without waiting on a modal loop.

    UIA elements belong to the thread that found them, so a worker thread
    resolves the locator again and invokes only the control with the same
    fingerprint, after hit-testing it again. A call still inside the provider
    after ``_INVOKE_RETURN_S`` is reported as an uncertain delivery (HTTP 503):
    a modal loop and a hung app look the same from here. A failed or
    mismatched resolution raises before any input.
    """
    state: dict[str, Any] = {}
    started = threading.Event()
    finished = threading.Event()

    def work() -> None:
        try:
            import uiautomation as auto  # noqa: PLC0415 - Windows-only, lazy

            with auto.UIAutomationInitializerInThread():
                found, truncated = _find_candidates(locator, auto)
                if (
                    len(found) != 1
                    or truncated
                    or not hmac.compare_digest(fingerprint, found[0][1]["fingerprint"])
                ):
                    state["error"] = "stale_target"
                    return
                control, candidate = found[0]
                if not _hits_target(auto, control, candidate):
                    state["error"] = "target_obscured"
                    return
                pattern = control.GetInvokePattern()
                started.set()
                pattern.Invoke()
                state["returned"] = True
        except Exception as exc:  # noqa: BLE001 - reported below
            state["error"] = type(exc).__name__
        finally:
            started.set()
            finished.set()

    threading.Thread(target=work, name="uia-invoke", daemon=True).start()
    if not started.wait(_INVOKE_START_S):
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation did not resolve the target"
        )
    finished.wait(_INVOKE_RETURN_S)
    error = state.get("error")
    if error == "stale_target":
        raise AgentRequestError(
            409, "stale_target", "UIA target changed after resolution"
        )
    if error == "target_obscured":
        raise AgentRequestError(
            409, "target_obscured", "another window or dialog is over the UIA target"
        )
    if error is None and not state.get("returned"):
        # Still inside the provider: a modal loop, or an app that is not
        # answering. The two look the same from here, so this is not a
        # delivery the agent can vouch for.
        raise AgentRequestError(
            503, "delivery_uncertain", "the UIA invoke has not returned"
        )
    if error is not None:
        raise AgentRequestError(409, "native_action_failed", "native UIA action failed")


_MAX_OWNER_CHAIN = 32
_WINDOW_OPERATIONS = frozenset({"window-root-at", "window-bounds", "owned-window-over"})


def _intersects(
    rect: tuple[int, int, int, int], region: tuple[int, int, int, int]
) -> bool:
    left, top, right, bottom = rect
    x, y, w, h = region
    return left < x + w and x < right and top < y + h and y < bottom


def _window_operation(
    operation: str, payload: dict[str, Any], user32: Any
) -> dict[str, Any]:
    """Win32 window facts for repair: the top-level window at a point, and
    whether a window the app itself owns now sits over a region.

    ``owned-window-over`` walks the live top-level windows from the top down
    to the captured window and answers with a window only when all of this
    holds: no visible window of another process, and none of the captured
    process that its owner chain does not tie to the captured window, meets
    the region anywhere above the captured window; and an owned window meets
    the region (its visible frame, not the resize border). The topmost such
    window is the answer, with its visible frame as ``region``
    ``[x, y, w, h]``. A 1x1 region asks about one point. Whether the covered part hides anything that
    matters is for the caller, which still sees the uncovered part.
    A captured window that is gone or changed process, a cloak state that
    cannot be read for one of the app's windows, and anything else answer
    ``None``.
    """
    if not user32.per_monitor_dpi():
        raise AgentRequestError(
            503, "uia_unavailable", "per-monitor DPI awareness is unavailable"
        )
    if operation == "window-root-at":
        data = _exact_object(payload, required=frozenset({"x", "y"}), label=operation)
        hit = user32.window_from_point(
            _bounded_int(data["x"], "x"), _bounded_int(data["y"], "y")
        )
        root = user32.ancestor(hit, _GA_ROOT) if hit else 0
        if not root or not user32.is_window(root):
            return {"status": "ok", "window": None}
        # The verified target's window is the foreground app's; anything else
        # (a topmost window that slid over the point) is not a capture.
        foreground = user32.foreground()
        front = (
            (user32.ancestor(foreground, _GA_ROOTOWNER) or foreground)
            if foreground
            else 0
        )
        if not front or (user32.ancestor(root, _GA_ROOTOWNER) or root) != front:
            return {"status": "ok", "window": None}
        return {
            "status": "ok",
            "window": {"hwnd": root, "pid": user32.pid_of(root)},
        }
    if operation == "window-bounds":
        data = _exact_object(
            payload, required=frozenset({"hwnd", "pid"}), label=operation
        )
        hwnd, pid = data["hwnd"], data["pid"]
        if any(
            not isinstance(v, int) or isinstance(v, bool) or v <= 0 for v in (hwnd, pid)
        ):
            raise AgentRequestError(400, "invalid_schema", "hwnd and pid required")
        if (
            not user32.is_window(hwnd)
            or user32.pid_of(hwnd) != pid
            or not user32.is_visible(hwnd)
        ):
            return {"status": "ok", "window": None}
        rect = user32.rect(hwnd)
        if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
            return {"status": "ok", "window": None}
        return {
            "status": "ok",
            "window": {
                "hwnd": hwnd,
                "pid": pid,
                # The visible frame (DWM bounds), in screenshot pixels.
                "bounds": [rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]],
            },
        }
    data = _exact_object(
        payload, required=frozenset({"hwnd", "pid", "region"}), label=operation
    )
    hwnd, pid, region = data["hwnd"], data["pid"], data["region"]
    if (
        not isinstance(hwnd, int)
        or isinstance(hwnd, bool)
        or hwnd <= 0
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(region, list)
        or len(region) != 4
        or any(isinstance(v, bool) or not isinstance(v, int) for v in region)
        or region[2] <= 0
        or region[3] <= 0
    ):
        raise AgentRequestError(400, "invalid_schema", "hwnd, pid and region required")
    if not user32.is_window(hwnd) or user32.pid_of(hwnd) != pid:
        return {"status": "ok", "window": None}
    box = (region[0], region[1], region[2], region[3])
    owned: Optional[dict[str, Any]] = None
    reached = False
    for candidate in user32.top_level_windows():
        if candidate == hwnd:
            reached = True
            break
        if not user32.is_visible(candidate):
            continue
        same_process = user32.pid_of(candidate) == pid
        cloaked = user32.is_cloaked(candidate)
        if cloaked is None and same_process:
            return {"status": "ok", "window": None}  # cannot tell if it shows
        if cloaked:
            continue
        # Another process's window is measured by everything it may paint;
        # the app's own by its visible frame.
        rect = user32.rect(candidate) if same_process else user32.outer_rect(candidate)
        if rect is None or rect[2] <= rect[0] or rect[3] <= rect[1]:
            continue
        if not _intersects(rect, box):
            continue
        if not same_process:
            return {"status": "ok", "window": None}  # a foreign occluder
        owner = user32.owner(candidate)
        link = owner
        for _ in range(_MAX_OWNER_CHAIN):
            if not link or link == hwnd:
                break
            link = user32.owner(link)
        if link != hwnd:
            return {"status": "ok", "window": None}  # not tied to the capture
        if owned is None:
            owned = {
                "title": user32.title(candidate)[:512] or None,
                "hwnd": candidate,
                "owner_hwnd": owner,
                "pid": pid,
                # Its visible frame (DWM bounds, no shadow), in the region's
                # pixels, so the caller can check what it covers.
                "region": [rect[0], rect[1], rect[2] - rect[0], rect[3] - rect[1]],
            }
    if not reached:
        return {"status": "ok", "window": None}  # nothing proves the order
    return {"status": "ok", "window": owned}


def _perform_uia(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Perform one typed UIA operation on one COM-affine server thread."""
    if operation in _WINDOW_OPERATIONS:
        if os.name != "nt":
            raise AgentRequestError(503, "uia_unavailable", "Win32 is unavailable")
        try:
            return _window_operation(operation, payload, _user32_factory())
        except AgentRequestError:
            raise
        except Exception as exc:  # noqa: BLE001 - bounded, never a crash
            raise AgentRequestError(
                503, "uia_unavailable", "window state is unavailable"
            ) from exc
    try:
        import uiautomation as auto  # noqa: PLC0415 - Windows-only, lazy
    except Exception as exc:  # noqa: BLE001
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation is unavailable"
        ) from exc
    if os.name == "nt":
        # The guest interpreter is installed under Program Files. comtypes
        # otherwise tries to emit generated UIAutomationCore wrappers beside
        # the package and the interactive (non-admin) desktop user gets EACCES.
        # Official comtypes behavior supports gen_dir=None for in-memory-only
        # wrapper generation, preserving least privilege without preinstall
        # mutations or an elevation requirement.
        import comtypes.client  # noqa: PLC0415

        comtypes.client.gen_dir = None
    try:
        with auto.UIAutomationInitializerInThread():
            return _perform_uia_initialized(auto, operation, payload)
    except AgentRequestError:
        raise
    except Exception as exc:  # noqa: BLE001 - provider/COM failures are bounded
        raise AgentRequestError(
            503, "uia_unavailable", "UI Automation is unavailable"
        ) from exc


@dataclass
class AgentConfig:
    """Runtime configuration for the in-guest agent server.

    Args:
        host: Bind address. Defaults to loopback (``127.0.0.1``) -- the
            input endpoints are not exposed off-host unless this is set to
            ``0.0.0.0`` (or the guest IP) explicitly.
        port: TCP port (matches the WAA default the SSH tunnel expects).
        token: Optional bearer token. When set, ``/screenshot`` and
            ``/execute_windows`` require ``Authorization: Bearer <token>``.
            When None the server is unauthenticated (loopback-only is then the
            only safeguard).
        certfile: PEM certificate path. When set (with ``keyfile``) the listener
            serves **HTTPS** -- the PHI-bearing channel is encrypted in transit
            and the client pins this cert's fingerprint. Provisioned per run by
            the control plane (``win_agent.tls.generate_self_signed_cert``).
        keyfile: PEM private-key path matching ``certfile``. Required with it.
        allow_legacy_exec: Expose the legacy ``/execute_windows`` route, which
            accepts only allow-listed ``pyautogui`` input calls with literal
            arguments (never arbitrary Python). False by default; production
            uses only bounded typed operations. The field keeps its historical
            name for API compatibility.
    """

    host: str = "127.0.0.1"
    port: int = 5000
    token: Optional[str] = None
    certfile: Optional[str] = None
    keyfile: Optional[str] = None
    allow_legacy_exec: bool = False

    def authed(self) -> bool:
        """True when a bearer token is required."""
        return bool(self.token)

    def tls_enabled(self) -> bool:
        """True when the listener serves HTTPS (a cert/key pair is set)."""
        return bool(self.certfile and self.keyfile)

    def __post_init__(self) -> None:
        """Reject a half-configured TLS pair (fail closed, never silent HTTP)."""
        if bool(self.certfile) != bool(self.keyfile):
            raise ValueError(
                "TLS needs BOTH certfile and keyfile (got only one) -- refusing "
                "to fall back to plaintext HTTP for a PHI channel"
            )


def _grab_desktop_png() -> bytes:
    """Capture the full virtual desktop as PNG bytes (mss + Pillow).

    Imported lazily and only on the screenshot path so the module loads on any
    OS. ``monitors[0]`` is the union of all monitors, so multi-monitor / DPI
    layouts are captured whole with absolute coordinates.
    """
    import mss  # noqa: PLC0415 - Windows-only, imported lazily by design
    from PIL import Image  # noqa: PLC0415

    with mss.mss() as sct:
        mon = sct.monitors[0]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _active_console_session() -> int:
    """Which console session this process is attached to (-1 if unknown)."""
    try:
        import ctypes  # noqa: PLC0415

        # ``ctypes.windll`` exists only on Windows; access it dynamically so
        # this stays importable + type-checkable on macOS/Linux CI.
        windll = getattr(ctypes, "windll", None)
        if windll is None:
            return -1
        return int(windll.kernel32.WTSGetActiveConsoleSessionId())
    except Exception:  # noqa: BLE001 - non-Windows / probe failure
        return -1


def _bounded_application_identifier(name: str) -> Optional[str]:
    """Canonicalize one executable basename into the public context-id syntax."""

    stem = os.path.splitext(os.path.basename(name))[0]
    ascii_name = (
        unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    )
    identifier = "".join(
        char.casefold()
        if char.isascii() and (char.isalnum() or char in "._:/-")
        else "-"
        for char in ascii_name
    )
    identifier = "-".join(part for part in identifier.split("-") if part)
    identifier = identifier.strip("-._:/")[:128]
    if not identifier or not identifier[0].isalnum():
        return None
    return identifier


def _process_image_identifier(pid: int) -> Optional[str]:
    """Canonical executable basename of one live process, or None."""

    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        kernel32 = win_dll("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        process = kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            size = wintypes.DWORD(32768)
            path = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(
                process, 0, path, ctypes.byref(size)
            ):
                return None
            return _bounded_application_identifier(path.value)
        finally:
            kernel32.CloseHandle(process)
    except Exception:  # noqa: BLE001 - unavailable means unverifiable
        return None


def _foreground_application_identity() -> Optional[str]:
    """Observe the foreground executable without reading its window title."""

    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        user32 = win_dll("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        if (
            not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            or not pid.value
        ):
            return None
    except Exception:  # noqa: BLE001 - unavailable means unverifiable
        return None
    return _process_image_identifier(int(pid.value))


def _native_session_digest() -> Optional[str]:
    """Hash the live machine + interactive Windows logon-session identity."""

    if os.name != "nt":
        return None
    try:
        import ctypes  # noqa: PLC0415
        import importlib  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class Luid(ctypes.Structure):
            _fields_ = [
                ("LowPart", wintypes.DWORD),
                ("HighPart", wintypes.LONG),
            ]

        class TokenStatistics(ctypes.Structure):
            _fields_ = [
                ("TokenId", Luid),
                ("AuthenticationId", Luid),
                ("ExpirationTime", ctypes.c_longlong),
                ("TokenType", wintypes.DWORD),
                ("ImpersonationLevel", wintypes.DWORD),
                ("DynamicCharged", wintypes.DWORD),
                ("DynamicAvailable", wintypes.DWORD),
                ("GroupCount", wintypes.DWORD),
                ("PrivilegeCount", wintypes.DWORD),
                ("ModifiedId", Luid),
            ]

        win_dll = getattr(ctypes, "WinDLL", None)
        if win_dll is None:
            return None
        kernel32 = win_dll("kernel32", use_last_error=True)
        advapi32 = win_dll("advapi32", use_last_error=True)
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.ProcessIdToSessionId.argtypes = [
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        session_id = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session_id)):
            return None
        if int(session_id.value) != _active_console_session():
            return None

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)
        ):
            return None
        try:
            stats = TokenStatistics()
            returned = wintypes.DWORD()
            if not advapi32.GetTokenInformation(
                token,
                10,  # TokenStatistics
                ctypes.byref(stats),
                ctypes.sizeof(stats),
                ctypes.byref(returned),
            ):
                return None
        finally:
            kernel32.CloseHandle(token)

        winreg = importlib.import_module("winreg")
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
        ) as key:
            machine_guid = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        authentication_id = (
            f"{int(stats.AuthenticationId.HighPart)}:"
            f"{int(stats.AuthenticationId.LowPart)}"
        )
        material = (
            f"windows-session-v1\0{machine_guid}\0{int(session_id.value)}"
            f"\0{authentication_id}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 - unavailable means unverifiable
        return None


def _execution_context_identity() -> dict[str, Any]:
    """Return PHI-free identities observed from live Windows OS state."""

    return {
        "status": "ok",
        "application": _foreground_application_identity(),
        "session": _native_session_digest(),
        "workflow_state": None,
    }


def make_handler_class(
    config: AgentConfig,
    grab_fn: GrabFn = _grab_desktop_png,
    input_fn: InputFn = _perform_input,
    uia_fn: UiaFn = _perform_uia,
    context_fn: ContextFn = _execution_context_identity,
    legacy_fn: LegacyFn = _perform_legacy_calls,
) -> type[BaseHTTPRequestHandler]:
    """Build the request-handler class bound to ``config`` and ``grab_fn``.

    ``grab_fn`` is injectable so tests drive the real HTTP roundtrip with a
    deterministic fake frame (no mss / no live desktop).
    """

    class AgentHandler(BaseHTTPRequestHandler):
        server_version = "OAFlowWinAgent/1.0"

        def log_message(self, *args: object) -> None:  # noqa: D401 - silence
            """Suppress the default stderr access log (noisy in-guest)."""

        # -- helpers ---------------------------------------------------------

        def _send(self, status: int, body: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _send_json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload).encode("utf-8"), "application/json")

        def _authorized(self) -> bool:
            """Constant-time bearer-token check (True when auth disabled)."""
            if not config.authed():
                return True
            header = self.headers.get("Authorization", "")
            prefix = "Bearer "
            if not header.startswith(prefix):
                return False
            presented = header[len(prefix) :].strip()
            return hmac.compare_digest(presented, config.token or "")

        def _reject_unauthorized(self) -> None:
            self._send_json(401, {"status": "error", "error": "unauthorized"})

        # -- routes ----------------------------------------------------------

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path == "/health":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "agent": "openadapt_flow.win_agent",
                        "active_console_session": _active_console_session(),
                        "auth_required": config.authed(),
                        "capabilities": [
                            "screenshot",
                            "context_identity_v1",
                            "typed_input_v1",
                            "guarded_input_v1",
                            "uia_v1",
                            "uia_read_v1",
                            "window_bounds_v1",
                            *(
                                ["legacy_waa_input_v1"]
                                if config.allow_legacy_exec
                                else []
                            ),
                        ],
                    },
                )
                return
            if self.path == "/screenshot":
                if not self._authorized():
                    self._reject_unauthorized()
                    return
                try:
                    png = grab_fn()
                except Exception as e:  # noqa: BLE001 - report, never crash loop
                    # The traceback stays in the guest's own log; the HTTP
                    # client gets only the bounded error message.
                    traceback.print_exc(file=sys.stderr)
                    self._send_json(500, {"status": "error", "error": str(e)})
                    return
                if not png.startswith(_PNG_SIGNATURE):
                    self._send_json(
                        500, {"status": "error", "error": "grabber did not return PNG"}
                    )
                    return
                self._send(200, png, "image/png")
                return
            self._send_json(404, {"status": "error", "error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - stdlib naming
            typed_routes = {
                "/input": ("input", ""),
                "/input/guarded": ("guarded-input", ""),
                "/context/identity": ("context", "identity"),
                "/uia/locator-at": ("uia", "locator-at"),
                "/uia/element-identity-at": ("uia", "element-identity-at"),
                "/window/root-at": ("uia", "window-root-at"),
                "/window/owned-over": ("uia", "owned-window-over"),
                "/window/bounds": ("uia", "window-bounds"),
                "/uia/text-at-point": ("uia", "text-at-point"),
                "/uia/identity-candidates-at": ("uia", "identity-candidates-at"),
                "/uia/find": ("uia", "find"),
                "/uia/act": ("uia", "act"),
                "/uia/read": ("uia", "read"),
            }
            is_legacy = self.path == "/execute_windows" and config.allow_legacy_exec
            if self.path not in typed_routes and not is_legacy:
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            if not self._authorized():
                self._reject_unauthorized()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "code": "invalid_content_length",
                        "error": "Content-Length must be an integer",
                    },
                )
                return
            if length < 0:
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "code": "invalid_content_length",
                        "error": "Content-Length must be non-negative",
                    },
                )
                return
            if length > _MAX_BODY_BYTES:
                self._send_json(
                    413,
                    {
                        "status": "error",
                        "code": "body_too_large",
                        "error": "request body exceeds the bounded contract",
                    },
                )
                return
            raw = self.rfile.read(length) if length else b""
            try:
                data = json.loads(raw or b"{}")
            except Exception:  # noqa: BLE001
                self._send_json(400, {"status": "error", "error": "invalid JSON body"})
                return
            if not isinstance(data, dict):
                self._send_json(
                    400,
                    {
                        "status": "error",
                        "code": "invalid_schema",
                        "error": "body must be an object",
                    },
                )
                return
            if self.path in typed_routes:
                kind, operation = typed_routes[self.path]
                try:
                    if kind == "context":
                        _exact_object(data, label="context identity request")
                    if kind == "guarded-input":
                        guarded = _exact_object(
                            data,
                            required=frozenset(
                                {
                                    "expected_frame_sha256",
                                    "expected_context",
                                    "input",
                                }
                            ),
                            optional=frozenset({"focus_point"}),
                            label="guarded input request",
                        )
                        expected_frame = guarded["expected_frame_sha256"]
                        if (
                            not isinstance(expected_frame, str)
                            or len(expected_frame) != 64
                            or any(
                                char not in "0123456789abcdef"
                                for char in expected_frame
                            )
                        ):
                            raise AgentRequestError(
                                400,
                                "invalid_schema",
                                "expected_frame_sha256 must be lowercase SHA-256",
                            )
                        expected_context = _exact_object(
                            guarded["expected_context"],
                            required=frozenset(
                                {"application", "session", "workflow_state"}
                            ),
                            label="expected_context",
                        )
                        for key, value in expected_context.items():
                            if value is not None and (
                                not isinstance(value, str) or not 1 <= len(value) <= 128
                            ):
                                raise AgentRequestError(
                                    400,
                                    "invalid_schema",
                                    f"expected_context.{key} is invalid",
                                )
                        input_payload = _exact_object(
                            guarded["input"],
                            optional=frozenset(
                                {
                                    "action",
                                    "x",
                                    "y",
                                    "double",
                                    "button",
                                    "end_x",
                                    "end_y",
                                    "text",
                                    "interval_s",
                                    "keys",
                                    "horizontal_notches",
                                    "vertical_notches",
                                }
                            ),
                            label="guarded input payload",
                        )
                        current_png = grab_fn()
                        if not current_png.startswith(_PNG_SIGNATURE):
                            raise AgentRequestError(
                                503,
                                "capture_unavailable",
                                "guarded frame capture is unavailable",
                            )
                        if not hmac.compare_digest(
                            hashlib.sha256(current_png).hexdigest(),
                            expected_frame,
                        ):
                            raise AgentRequestError(
                                409,
                                "stale_frame",
                                "desktop frame changed after identity verification",
                            )
                        current_context = context_fn()
                        for key, expected in expected_context.items():
                            if current_context.get(key) != expected:
                                raise AgentRequestError(
                                    409,
                                    "stale_context",
                                    "execution context changed after identity "
                                    "verification",
                                )
                        focus_point = guarded.get("focus_point")
                        action = input_payload.get("action")
                        if action in {"type_text", "press"}:
                            focus = _exact_object(
                                focus_point,
                                required=frozenset({"x", "y"}),
                                label="focus_point",
                            )
                            focus_result = uia_fn(
                                "focused-at-point",
                                {
                                    "x": _bounded_int(focus["x"], "focus_point.x"),
                                    "y": _bounded_int(focus["y"], "focus_point.y"),
                                },
                            )
                            if focus_result.get("focused") is not True:
                                raise AgentRequestError(
                                    409,
                                    "stale_focus",
                                    "focused UIA target changed after identity "
                                    "verification",
                                )
                        elif focus_point is not None:
                            raise AgentRequestError(
                                400,
                                "invalid_schema",
                                "focus_point is only valid for keyboard input",
                            )
                        result = input_fn(input_payload)
                    else:
                        result = (
                            input_fn(data)
                            if kind == "input"
                            else context_fn()
                            if kind == "context"
                            else uia_fn(operation, data)
                        )
                except AgentRequestError as exc:
                    self._send_json(
                        exc.status,
                        {"status": "error", "code": exc.code, "error": str(exc)},
                    )
                    return
                except Exception:  # noqa: BLE001 - bounded generic error, no traceback/PHI
                    self._send_json(
                        500,
                        {
                            "status": "error",
                            "code": "operation_failed",
                            "error": "typed agent operation failed",
                        },
                    )
                    return
                self._send_json(200, result)
                return
            command = data.get("command")
            if not isinstance(command, str):
                self._send_json(
                    400, {"status": "error", "error": "command must be a string"}
                )
                return
            self._run_legacy_command(command)

        def _run_legacy_command(self, command: str) -> None:
            """Run a legacy WAA command through the closed interpreter.

            The command is parsed by :func:`parse_legacy_command` and only the
            resulting allow-listed pyautogui input calls are dispatched; client
            text is never compiled or executed. A refused command is HTTP 400
            (or 413) with a stable ``code``; a failing input call is HTTP 500
            with a bounded message and no traceback, so a wrong-write still
            surfaces as an ERROR rather than a silent no-op.
            """
            try:
                calls = parse_legacy_command(command)
                result = legacy_fn(calls)
            except AgentRequestError as exc:
                self._send_json(
                    exc.status,
                    {"status": "error", "code": exc.code, "error": str(exc)},
                )
                return
            except Exception:  # noqa: BLE001 - bounded error, no traceback/PHI
                self._send_json(
                    500,
                    {
                        "status": "error",
                        "code": "operation_failed",
                        "error": "legacy input operation failed",
                    },
                )
                return
            self._send_json(200, result)

    return AgentHandler


def create_server(
    config: Optional[AgentConfig] = None,
    *,
    grab_fn: GrabFn = _grab_desktop_png,
    input_fn: InputFn = _perform_input,
    uia_fn: UiaFn = _perform_uia,
    context_fn: ContextFn = _execution_context_identity,
    legacy_fn: LegacyFn = _perform_legacy_calls,
) -> HTTPServer:
    """Build (but do not start) the COM-affine agent HTTP server.

    Args:
        config: Bind/auth configuration (defaults to loopback, no token).
        grab_fn: Desktop-capture callable returning PNG bytes (injectable for
            tests).

    Returns:
        An ``HTTPServer`` bound to ``config.host:config.port``. When ``config``
        carries a cert/key pair the listening socket is wrapped in TLS (the
        server speaks HTTPS). Requests are serialized on the serve thread
        because UIA controls are COM-apartment-bound.
    """
    config = config or AgentConfig()
    handler = make_handler_class(
        config, grab_fn, input_fn, uia_fn, context_fn, legacy_fn
    )
    # UIAutomation controls and patterns are apartment/thread-affine. A
    # single-threaded HTTPServer keeps every request on the serve_forever
    # thread; _perform_uia initializes that thread's COM apartment per request.
    server = HTTPServer((config.host, config.port), handler)
    if config.tls_enabled():
        # Keep this deployed script genuinely self-contained: launch_agent()
        # copies only server.py into the guest, not the openadapt_flow package.
        # The control plane mints the cert; the guest needs stdlib ssl only.
        import ssl  # noqa: PLC0415

        assert config.certfile is not None and config.keyfile is not None
        for path in (config.certfile, config.keyfile):
            if not os.path.exists(path):
                raise FileNotFoundError(f"TLS material not found: {path}")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(config.certfile, config.keyfile)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
    return server


def main(argv: Optional[list[str]] = None) -> None:
    """CLI entry point: run the agent server until interrupted."""
    parser = argparse.ArgumentParser(
        description="OpenAdapt-flow in-guest Windows agent"
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default loopback; use 0.0.0.0 to expose to the host)",
    )
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_ENV_VAR),
        help=(
            "optional bearer token required on /screenshot and "
            f"/execute_windows (falls back to ${TOKEN_ENV_VAR})"
        ),
    )
    parser.add_argument(
        "--certfile",
        default=os.environ.get(CERTFILE_ENV_VAR),
        help=(
            "PEM certificate; with --keyfile serves HTTPS (encrypt PHI in "
            f"transit). Falls back to ${CERTFILE_ENV_VAR}."
        ),
    )
    parser.add_argument(
        "--keyfile",
        default=os.environ.get(KEYFILE_ENV_VAR),
        help=f"PEM private key matching --certfile (falls back to ${KEYFILE_ENV_VAR})",
    )
    parser.add_argument(
        "--allow-legacy-commands",
        "--allow-legacy-exec",
        dest="allow_legacy_exec",
        action="store_true",
        help=(
            "DEVELOPMENT ONLY: expose /execute_windows for allow-listed pyautogui "
            "input calls (arbitrary Python is always refused). "
            "--allow-legacy-exec is the deprecated spelling."
        ),
    )
    args = parser.parse_args(argv)
    config = AgentConfig(
        host=args.host,
        port=args.port,
        token=args.token,
        certfile=args.certfile,
        keyfile=args.keyfile,
        allow_legacy_exec=args.allow_legacy_exec,
    )
    server = create_server(config)
    scheme = "https" if config.tls_enabled() else "http"
    print(
        f"[win-agent] listening on {scheme}://{config.host}:{config.port} "
        f"(tls={'on' if config.tls_enabled() else 'OFF'}, "
        f"auth={'on' if config.authed() else 'OFF'}, "
        f"legacy_commands={'on' if config.allow_legacy_exec else 'OFF'}, "
        f"session={_active_console_session()})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - interactive
        pass
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    main()
