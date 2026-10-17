"""In-guest WAA-contract HTTP shim (runs INSIDE the Windows VM).

Serves the desktop benchmark harness
(``openadapt_flow.benchmark.desktop_benchmark``):

    GET  /screenshot          -> raw PNG bytes of the desktop (Flask send_file)
    GET  /health              -> ``{"status": "ok", ...}`` liveness probe
    GET  /uia?title=...       -> UIA accessibility-tree dump for the arm-B
                                 steelman and the UIA-tree-quality metric
    POST /tasks/quiet-desktop -> disable toast notifications (fixed registry
                                 writes, no client-supplied command)
    POST /tasks/uia-arm       -> run ``uia_arm.run`` from the shim's own
                                 directory with five bounded string arguments

``POST /execute_windows`` -- the WAA arbitrary-Python route -- is gone and
answers 410. Nothing the client sends is ever exec()'d. Production replay uses
the hardened, typed ``openadapt_flow.backends.win_agent`` server instead.

Hardening: the default bind is ``127.0.0.1`` (``launch_shim`` passes
``--host 0.0.0.0`` explicitly for host access); when ``--token`` or
``OAFLOW_AGENT_TOKEN`` is set, every route except ``/health`` requires
``Authorization: Bearer <token>`` (constant-time compare); error responses carry
a bounded message and never a traceback (tracebacks go to the guest's stderr).

This process MUST run in the interactive desktop session (session 1), not
the SYSTEM/session-0 context ``prlctl exec`` lands in, or pyautogui input and
mss screenshots address the wrong (blank) desktop. The control layer launches
it via a scheduled task carrying the logged-on user's interactive token.

Vision-only by construction: PNG frames out, pixel-coordinate input in. The
``/uia`` endpoint is deliberately separate — it exists only to *measure* the
accessibility substrate (the incumbent's territory), never to drive the
compiled-replay arm.
"""

from __future__ import annotations

import argparse
import hmac
import importlib.util
import io
import os
import re
import subprocess
import sys
import traceback
from pathlib import Path

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

TOKEN_ENV_VAR = "OAFLOW_AGENT_TOKEN"
_SHIM_DIR = Path(__file__).resolve().parent
_MAX_TASK_ARG_CHARS = 512
_UIA_ARM_FIELDS = ("search", "first", "last", "note", "mode")
_UIA_ARM_MODES = frozenset({"positional", "identity"})
_QUIET_DESKTOP_KEYS = (
    (
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\PushNotifications",
        "ToastEnabled",
    ),
    (
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Notifications\Settings",
        "NOC_GLOBAL_SETTING_TOASTS_ENABLED",
    ),
)


def _error(status: int, message: str) -> tuple[object, int]:
    """A bounded JSON error; the traceback (if any) stays in the guest log."""
    return jsonify({"status": "error", "error": message}), status


@app.before_request
def _require_token() -> object:
    """Constant-time bearer check on every route except /health."""
    token = app.config.get("SHIM_TOKEN")
    if not token or request.path == "/health":
        return None
    header = request.headers.get("Authorization", "")
    presented = header[len("Bearer ") :].strip() if header.startswith("Bearer ") else ""
    if not hmac.compare_digest(presented, token):
        return _error(401, "unauthorized")
    return None


def _grab_png() -> bytes:
    """Capture the full virtual desktop as PNG bytes via mss + Pillow."""
    import mss
    from PIL import Image

    with mss.mss() as sct:
        # monitors[0] is the union of all monitors; use it so multi-mon and
        # DPI-scaled layouts are captured whole (coordinates stay absolute).
        mon = sct.monitors[0]
        raw = sct.grab(mon)
        img = Image.frombytes("RGB", raw.size, raw.rgb)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


@app.get("/health")
def health() -> object:
    """Liveness + session diagnostics (which desktop are we attached to)."""
    import ctypes

    try:
        session_id = ctypes.windll.kernel32.WTSGetActiveConsoleSessionId()
    except Exception:  # noqa: BLE001
        session_id = -1
    return jsonify(
        {
            "status": "ok",
            "active_console_session": int(session_id),
            "shim": "waa_shim",
        }
    )


@app.get("/screenshot")
def screenshot() -> object:
    """Return the current desktop frame as raw PNG bytes."""
    try:
        png = _grab_png()
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _error(500, "screenshot failed")
    return send_file(io.BytesIO(png), mimetype="image/png")


@app.post("/execute_windows")
def execute_windows() -> object:
    """Retired: the arbitrary-Python WAA route no longer exists."""
    return _error(
        410,
        "/execute_windows was removed; use the typed win_agent server "
        "(openadapt_flow.backends.win_agent) or a /tasks/* route",
    )


@app.post("/tasks/quiet-desktop")
def quiet_desktop() -> object:
    """Disable toast notifications for the interactive user (fixed writes)."""
    failed = 0
    for path, value in _QUIET_DESKTOP_KEYS:
        proc = subprocess.run(
            ["reg", "add", path, "/v", value, "/t", "REG_DWORD", "/d", "0", "/f"],
            capture_output=True,
            check=False,
        )
        failed += int(proc.returncode != 0)
    if failed:
        return _error(500, f"{failed} registry write(s) failed")
    return jsonify({"status": "ok"})


@app.post("/tasks/uia-arm")
def uia_arm_task() -> object:
    """Run the benchmark's UIA arm (``uia_arm.run``) with bounded arguments.

    The module is loaded only from the shim's own directory; the client
    supplies five strings, never code or a path.
    """
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or set(data) != set(_UIA_ARM_FIELDS):
        return _error(400, f"body must have exactly {list(_UIA_ARM_FIELDS)}")
    args = [data[field] for field in _UIA_ARM_FIELDS]
    if not all(isinstance(a, str) and len(a) <= _MAX_TASK_ARG_CHARS for a in args):
        return _error(400, "every field must be a string of bounded length")
    if data["mode"] not in _UIA_ARM_MODES:
        return _error(400, "unsupported mode")
    module_path = _SHIM_DIR / "uia_arm.py"
    try:
        spec = importlib.util.spec_from_file_location("uia_arm", module_path)
        if spec is None or spec.loader is None:
            return _error(500, "uia_arm.py is not deployed next to the shim")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        result = module.run(*args)
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _error(500, "uia arm failed")
    return jsonify(result)


@app.get("/uia")
def uia() -> object:
    """Dump the UIA accessibility tree of a top-level window (arm-B / metric).

    Query params:
        title: substring match on window title (best-effort, first match).
        depth: max descend depth (default 40).

    Returns a flat list of controls with their automation_id, control_type,
    name, rectangle, and whether a *usable* automation id is present — the
    raw material for the UIA-tree-quality fraction.
    """
    from pywinauto import Desktop  # type: ignore

    title = request.args.get("title", "")[:_MAX_TASK_ARG_CHARS]
    try:
        max_depth = max(0, min(int(request.args.get("depth", "40")), 80))
    except ValueError:
        return _error(400, "depth must be an integer")
    try:
        desktop = Desktop(backend="uia")
        if title:
            win = desktop.window(title_re=f".*{re.escape(title)}.*")
        else:
            win = desktop.windows()[0]
        win.wait("exists", timeout=5)
        elem = win.wrapper_object()
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return _error(500, "UIA window lookup failed")

    nodes: list[dict] = []

    def walk(ctrl: object, depth: int) -> None:
        if depth > max_depth:
            return
        try:
            info = ctrl.element_info  # type: ignore[attr-defined]
            auto_id = getattr(info, "automation_id", "") or ""
            rect = getattr(info, "rectangle", None)
            nodes.append(
                {
                    "depth": depth,
                    "control_type": getattr(info, "control_type", "") or "",
                    "name": (getattr(info, "name", "") or "")[:120],
                    "automation_id": auto_id,
                    "has_usable_id": bool(auto_id and not auto_id.isdigit()),
                    "rect": str(rect) if rect is not None else "",
                }
            )
        except Exception:  # noqa: BLE001
            return
        try:
            for child in ctrl.children():  # type: ignore[attr-defined]
                walk(child, depth + 1)
        except Exception:  # noqa: BLE001
            pass

    walk(elem, 0)
    usable = sum(1 for n in nodes if n["has_usable_id"])
    return jsonify(
        {
            "status": "ok",
            "window": title,
            "node_count": len(nodes),
            "usable_id_count": usable,
            "nodes": nodes,
        }
    )


def main() -> None:
    """Run the shim server."""
    parser = argparse.ArgumentParser(description="In-guest WAA HTTP shim")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address (default loopback; launch_shim passes 0.0.0.0)",
    )
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument(
        "--token",
        default=os.environ.get(TOKEN_ENV_VAR),
        help=f"bearer token for every route but /health (falls back to ${TOKEN_ENV_VAR})",
    )
    args = parser.parse_args()
    app.config["SHIM_TOKEN"] = args.token or None
    # pyautogui failsafe raises if the cursor hits a screen corner; the
    # compiled replay legitimately drives the cursor anywhere, so disable it.
    try:
        import pyautogui

        pyautogui.FAILSAFE = False
    except Exception:  # noqa: BLE001
        pass
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
