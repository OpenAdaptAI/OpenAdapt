"""In-guest Windows agent server (WAA-contract HTTP shim, session 1).

Runs INSIDE the Windows VM's *interactive* desktop session and exposes exactly
the endpoints ``openadapt_flow.backends.windows_backend.WindowsBackend`` calls,
matching the Windows Agent Arena Flask contract (the WAADirect pattern):

    GET  /screenshot        -> raw PNG bytes of the desktop (Content-Type
                               image/png; NOT base64 JSON)
    POST /input             -> bounded physical input operations (typed JSON)
    POST /uia/locator-at    -> stable UIA locator at a demonstrated point
    POST /uia/text-at-point -> structured row text for identity verification
    POST /uia/find          -> zero / unique / ambiguous exact candidates
    POST /uia/act           -> unique native UIA action + delivery receipt
    POST /execute_windows   -> legacy arbitrary Python execution, DISABLED by
                               default and available only with the explicit
                               ``--allow-legacy-exec`` development switch.
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
* **Typed actions by default.** Production observation and actuation use a
  bounded JSON schema. The legacy arbitrary-exec compatibility route is off
  unless an operator explicitly enables it for local development.
* **Loopback by default.** The default bind is ``127.0.0.1``. Exposing the
  agent on the guest LAN interface is an explicit opt-in.
* **Optional bearer token.** The PHI at-rest audit flagged this shim as
  unauthenticated. When a token is configured (``--token`` or the
  ``OAFLOW_AGENT_TOKEN`` env var) every ``/screenshot`` and ``/execute_windows``
  request must carry ``Authorization: Bearer <token>`` or is rejected 401. The
  comparison is constant-time. ``/health`` stays unauthenticated (liveness only,
  no desktop bytes, no exec).
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
pyautogui/uiautomation used by the exec'd commands) import LAZILY inside the
request handlers, and the desktop grabber is injectable so tests exercise the
full HTTP roundtrip with a fake frame.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import json
import os
import secrets
import subprocess
import time
import traceback
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

_MAX_BODY_BYTES = 1_048_576
_MAX_TEXT_CHARS = 65_536
_MAX_UIA_NODES = 5_000
_MAX_UIA_CANDIDATES = 16
_MAX_UIA_DEPTH = 24

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
    if action not in {"click", "type_text", "press", "scroll"}:
        raise AgentRequestError(400, "unsupported_action", "unsupported input action")

    if action == "click":
        expected = {"action", "x", "y", "double"}
        if set(data) - expected or not {"x", "y"}.issubset(data):
            raise AgentRequestError(400, "invalid_schema", "invalid click fields")
        x = _bounded_int(data["x"], "x")
        y = _bounded_int(data["y"], "y")
        double = data.get("double", False)
        if not isinstance(double, bool):
            raise AgentRequestError(400, "invalid_schema", "double must be boolean")
        import pyautogui  # noqa: PLC0415 - Windows-only, lazy by design

        pyautogui.FAILSAFE = False
        (pyautogui.doubleClick if double else pyautogui.click)(x, y)
        return _delivery_receipt(
            "physical_double_click" if double else "physical_click", native=False
        )

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


def _perform_uia_initialized(
    auto: Any, operation: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Perform one typed UIA operation in an initialized COM apartment."""
    if operation in {"locator-at", "text-at-point"}:
        data = _exact_object(payload, required=frozenset({"x", "y"}), label=operation)
        x = _bounded_int(data["x"], "x")
        y = _bounded_int(data["y"], "y")
        if operation == "text-at-point":
            return {"status": "ok", "text": _structured_text_at(auto, x, y)}
        try:
            element = auto.ControlFromPoint(x, y)
        except Exception as exc:  # noqa: BLE001
            raise AgentRequestError(
                503, "uia_unavailable", "UI Automation is unavailable"
            ) from exc
        if element is None:
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
        return {"status": "ok", "locator": _locator_from_control(actionable or element)}

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
    role = candidate.get("role")
    try:
        if role in {"button", "link", "menuitem"}:
            control.GetInvokePattern().Invoke()
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


def _perform_uia(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Perform one typed UIA operation on one COM-affine server thread."""
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
            arbitrary-exec endpoint is not exposed off-host unless this is set
            to ``0.0.0.0`` (or the guest IP) explicitly.
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
        allow_legacy_exec: Expose the arbitrary-Python compatibility route.
            False by default; production uses only bounded typed operations.
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


def make_handler_class(
    config: AgentConfig,
    grab_fn: GrabFn = _grab_desktop_png,
    input_fn: InputFn = _perform_input,
    uia_fn: UiaFn = _perform_uia,
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
                            "typed_input_v1",
                            "uia_v1",
                            *(["legacy_exec"] if config.allow_legacy_exec else []),
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
                    self._send_json(
                        500,
                        {
                            "status": "error",
                            "error": str(e),
                            "trace": traceback.format_exc(),
                        },
                    )
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
                "/uia/locator-at": ("uia", "locator-at"),
                "/uia/text-at-point": ("uia", "text-at-point"),
                "/uia/find": ("uia", "find"),
                "/uia/act": ("uia", "act"),
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
                    result = (
                        input_fn(data) if kind == "input" else uia_fn(operation, data)
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
            self._exec_command(command)

        def _exec_command(self, command: str) -> None:
            """exec() bare Python; return 200 + captured stdout, 500 on error.

            The command runs with a fresh module-like namespace. Its stdout is
            captured and echoed in the response body so a UIA read snippet's
            ``<<OAFLOW_STRUCTURED>>...<<END_OAFLOW_STRUCTURED>>`` sentinel
            reaches the backend. A raised exception becomes HTTP 500 with the
            traceback, so a wrong-write surfaces as an ERROR rather than a
            silent no-op (the runtime halts on a non-200).
            """
            import contextlib  # noqa: PLC0415

            # pyautogui's fail-safe raises when the cursor reaches a screen
            # corner; the compiled replay legitimately drives the cursor
            # anywhere, so disable it for this process (best-effort).
            try:
                import pyautogui  # noqa: PLC0415

                pyautogui.FAILSAFE = False
            except Exception:  # noqa: BLE001 - not always present at exec time
                pass

            scope: dict = {"__name__": "__oaflow_agent_exec__"}
            out = io.StringIO()
            try:
                with contextlib.redirect_stdout(out):
                    exec(command, scope)  # noqa: S102 - the WAA contract IS remote exec
            except Exception as e:  # noqa: BLE001
                self._send_json(
                    500,
                    {
                        "status": "error",
                        "error": str(e),
                        "trace": traceback.format_exc(),
                        "output": out.getvalue(),
                    },
                )
                return
            self._send_json(200, {"status": "ok", "output": out.getvalue()})

    return AgentHandler


def create_server(
    config: Optional[AgentConfig] = None,
    *,
    grab_fn: GrabFn = _grab_desktop_png,
    input_fn: InputFn = _perform_input,
    uia_fn: UiaFn = _perform_uia,
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
    handler = make_handler_class(config, grab_fn, input_fn, uia_fn)
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
        "--allow-legacy-exec",
        action="store_true",
        help="DEVELOPMENT ONLY: expose the arbitrary-Python compatibility route",
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
        f"legacy_exec={'on' if config.allow_legacy_exec else 'OFF'}, "
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
