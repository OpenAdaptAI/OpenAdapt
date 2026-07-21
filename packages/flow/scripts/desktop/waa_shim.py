"""In-guest WAA-contract HTTP shim (runs INSIDE the Windows VM).

Exposes exactly the two endpoints ``WindowsBackend`` expects, matching the
Windows Agent Arena Flask server contract (the WAADirect pattern):

    GET  /screenshot       -> raw PNG bytes of the desktop (Flask send_file)
    POST /execute_windows  -> exec() of bare Python with pyautogui in scope;
                              body ``{"command": "<python statements>"}``.

Plus two conveniences the Mac-side control layer uses:

    GET  /health           -> ``{"status": "ok", ...}`` liveness probe
    GET  /uia?title=...    -> UIA accessibility-tree dump for the arm-B
                              steelman and the UIA-tree-quality metric.

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
import io
import traceback

from flask import Flask, jsonify, request, send_file

app = Flask(__name__)


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
    except Exception as e:  # noqa: BLE001
        return (
            jsonify({"status": "error", "error": str(e),
                     "trace": traceback.format_exc()}),
            500,
        )
    return send_file(io.BytesIO(png), mimetype="image/png")


@app.post("/execute_windows")
def execute_windows() -> object:
    """exec() bare Python statements (pyautogui et al. importable).

    Returns 200 on success (WindowsBackend only checks the status code) and
    500 with a traceback on failure, so wrong-writes surface as errors rather
    than silent no-ops.
    """
    data = request.get_json(force=True, silent=True) or {}
    command = data.get("command", "")
    if not isinstance(command, str):
        return jsonify({"status": "error", "error": "command must be str"}), 400
    scope: dict = {"__name__": "__waa_exec__"}
    try:
        exec(command, scope)  # noqa: S102 - the WAA contract is remote exec
    except Exception as e:  # noqa: BLE001
        return (
            jsonify({"status": "error", "error": str(e),
                     "trace": traceback.format_exc()}),
            500,
        )
    return jsonify({"status": "ok"})


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

    title = request.args.get("title", "")
    max_depth = int(request.args.get("depth", "40"))
    try:
        desktop = Desktop(backend="uia")
        if title:
            win = desktop.window(title_re=f".*{title}.*")
        else:
            win = desktop.windows()[0]
        win.wait("exists", timeout=5)
        elem = win.wrapper_object()
    except Exception as e:  # noqa: BLE001
        return jsonify({"status": "error", "error": str(e)}), 500

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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5000)
    args = parser.parse_args()
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
