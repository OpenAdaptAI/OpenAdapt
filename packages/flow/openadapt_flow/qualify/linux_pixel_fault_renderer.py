"""Finite X11 overlays for a session-owned qualification task.

Standalone stdlib/Tk entry point for the guest's system Python. No application
text, screenshot, process list or credential is persisted or returned. The
owner supplies expectations; kernel peer identity and current pixels provide
observations. Readiness is not a qualification receipt.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import stat
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PAIRING = "openadapt.rdp-pairing/v1"
FAULT = "openadapt.qualification-pixel-fault/v1"
REFUSAL_STAGES = frozenset(
    {
        "request",
        "identity",
        "geometry",
        "capture",
        "render",
        "map",
        "liveness",
        "focus_missing_tool",
        "focus_nonzero",
        "focus_timeout",
        "focus_malformed",
        "focus_none",
        "focus_pointer_root",
    }
)
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
NONCE = re.compile(r"[0-9a-f]{32}\Z")
DISPLAY = re.compile(r":([0-9]{1,5})\Z")


def process_start(pid: int) -> int:
    """Read start ticks without parsing a possibly space-containing comm."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if fields[0] in {"Z", "X"}:
        raise ValueError("X11 owner is not live")
    return int(fields[19])


def display_identity(display: str) -> dict[str, Any]:
    """Observe the actual Unix peer and socket generation, without X11 input."""
    match = DISPLAY.fullmatch(display)
    if match is None or sys.platform != "linux":
        raise ValueError("local Linux display required")
    path = Path(f"/tmp/.X11-unix/X{int(match[1])}")
    before = path.lstat()
    if not stat.S_ISSOCK(before.st_mode):
        raise ValueError("X11 path is not a socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(2)
        peer.connect(str(path))
        pid, uid, _gid = struct.unpack(
            "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    if pid <= 1 or uid != os.getuid() or before.st_uid != uid:
        raise ValueError("X11 peer is not owned by the current user")
    start = process_start(pid)
    after = path.lstat()
    if (before.st_dev, before.st_ino, before.st_ctime_ns) != (
        after.st_dev,
        after.st_ino,
        after.st_ctime_ns,
    ):
        raise ValueError("X11 socket generation changed")
    return {
        "schema": "openadapt.linux-x11-display/v1",
        "pid": pid,
        "uid": uid,
        "start_ticks": start,
        "socket_device": after.st_dev,
        "socket_inode": after.st_ino,
        "socket_ctime_ns": after.st_ctime_ns,
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def identity_digest(identity: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def rectangle(value: Any, viewport: tuple[int, int]) -> tuple[int, int, int, int]:
    if isinstance(value, dict) and set(value) == {"x", "y", "width", "height"}:
        value = [value[key] for key in ("x", "y", "width", "height")]
    if (
        not isinstance(value, (list, tuple))
        or len(value) != 4
        or any(type(v) is not int for v in value)
    ):
        raise ValueError("invalid rectangle")
    x, y, w, h = value
    if min(x, y) < 0 or min(w, h) <= 0 or x + w > viewport[0] or y + h > viewport[1]:
        raise ValueError("rectangle outside display")
    return x, y, w, h


def overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> bool:
    x, y, w, h = a
    u, v, c, d = b
    return x < u + c and u < x + w and y < v + d and v < y + h


def validate_request(
    request: dict[str, Any], binding: str, viewport: tuple[int, int]
) -> tuple[int, int, int, int]:
    if (
        not isinstance(request, dict)
        or not NONCE.fullmatch(str(request.get("nonce", "")))
        or not HEX64.fullmatch(binding)
        or len(viewport) != 2
        or any(type(v) is not int or not 0 < v <= 8192 for v in viewport)
    ):
        raise ValueError("invalid task binding")
    if request.get("schema") == PAIRING:
        if (
            set(request)
            != {
                "schema",
                "operation",
                "nonce",
                "connection_digest",
                "viewport",
                "region",
                "lifetime_seconds",
            }
            or request["operation"] != "pairing_witness"
            or request["connection_digest"] != binding
            or request["viewport"] != list(viewport)
            or type(request["lifetime_seconds"]) is not int
            or not 1 <= request["lifetime_seconds"] <= 300
        ):
            raise ValueError("invalid pairing request")
        region = rectangle(request["region"], viewport)
        if region[2:] != (52, 52):
            raise ValueError("invalid witness dimensions")
        return region
    if (
        request.get("schema") != FAULT
        or request.get("session_identity_sha256") != binding
    ):
        raise ValueError("invalid fault binding")
    if request.get("screen") != {"width": viewport[0], "height": viewport[1]}:
        raise ValueError("changed viewport")
    base = {"schema", "nonce", "kind", "target", "screen", "session_identity_sha256"}
    target = rectangle(request.get("target"), viewport)
    if target[2] * target[3] > viewport[0] * viewport[1] // 2:
        raise ValueError("unbounded target")
    if request.get("kind") == "ambiguity":
        if set(request) != base | {"duplicate", "search_pad"}:
            raise ValueError("invalid ambiguity request")
        duplicate = rectangle(request["duplicate"], viewport)
        pad = request["search_pad"]
        if (
            type(pad) is not int
            or not 0 < pad <= max(viewport)
            or duplicate[2:] != target[2:]
            or overlap(target, duplicate)
            or abs(duplicate[0] - target[0]) > pad
            or abs(duplicate[1] - target[1]) > pad
        ):
            raise ValueError("duplicate outside original search area")
        return duplicate
    if request.get("kind") not in {"wrong_identity", "stale_identity"} or set(
        request
    ) != base | {"identifier_region"}:
        raise ValueError("unsupported fault")
    region = rectangle(request["identifier_region"], viewport)
    if region[2] * region[3] > viewport[0] * viewport[1] // 2:
        raise ValueError("unbounded identifier")
    return region


def witness_ppm(nonce: str, connection: str, guest: str) -> bytes:
    if not NONCE.fullmatch(nonce) or not all(
        HEX64.fullmatch(v) for v in (connection, guest)
    ):
        raise ValueError("invalid witness identities")
    digest = hashlib.sha256(
        "\0".join((PAIRING, nonce, connection, guest)).encode("ascii")
    ).digest()
    pixels = bytearray()
    for y in range(52):
        for x in range(52):
            bit = ((y - 2) // 3) * 16 + (x - 2) // 3
            on = 2 <= x < 50 and 2 <= y < 50 and digest[bit // 8] & (128 >> (bit % 8))
            pixels.extend((255, 255, 255) if on else (0, 0, 0))
    return b"P6\n52 52\n255\n" + pixels


def focus(env: dict[str, str], *, stage: list[str] | None = None) -> int:
    outer_stage = stage[0] if stage is not None else None
    try:
        result = subprocess.run(
            ["/usr/bin/xdotool", "getwindowfocus"],
            env=env,
            capture_output=True,
            timeout=3,
            check=True,
        )
    except FileNotFoundError:
        if stage is not None:
            stage[0] = "focus_missing_tool"
        raise
    except subprocess.CalledProcessError:
        if stage is not None:
            stage[0] = "focus_nonzero"
        raise
    except subprocess.TimeoutExpired:
        if stage is not None:
            stage[0] = "focus_timeout"
        raise
    value = result.stdout.strip()
    if not value.isdigit() or int(value) <= 1:
        if stage is not None:
            stage[0] = (
                "focus_malformed"
                if not value.isdigit()
                else "focus_none" if int(value) == 0 else "focus_pointer_root"
            )
        raise ValueError("display focus unavailable")
    if stage is not None and outer_stage is not None:
        stage[0] = outer_stage
    return int(value)


def render(envelope: dict[str, Any], *, stage: list[str]) -> None:
    import tkinter as tk

    display = envelope["display"]
    expected = envelope["identity"]
    request = envelope["request"]
    binding = envelope["binding"]
    viewport = tuple(envelope["viewport"])
    stage[0] = "identity"
    actual = display_identity(display)
    if actual != expected:
        raise ValueError("display generation changed")
    guest = identity_digest(actual)
    stage[0] = "geometry"
    region = validate_request(request, binding, viewport)
    pairing = request["schema"] == PAIRING
    if not pairing and binding != guest:
        raise ValueError("fault belongs to a different guest")
    # Inherited environment is deliberately restricted by the session owner.
    env = {key: os.environ[key] for key in ("PATH", "HOME", "DISPLAY")}
    if env["DISPLAY"] != display:
        raise ValueError("display mismatch")
    stage[0] = "capture"
    focused = focus(env, stage=stage)
    expires = time.monotonic() + (request["lifetime_seconds"] if pairing else 8)
    if pairing:
        pixels = witness_ppm(request["nonce"], binding, guest)
    elif request["kind"] == "ambiguity":
        x, y, w, h = rectangle(request["target"], viewport)
        result = subprocess.run(
            [
                "/usr/bin/import",
                "-display",
                display,
                "-window",
                "root",
                "-crop",
                f"{w}x{h}+{x}+{y}",
                "-depth",
                "8",
                "ppm:-",
            ],
            env=env,
            capture_output=True,
            timeout=3,
            check=True,
        )
        pixels = result.stdout
        if len(pixels) > w * h * 6 + 4096:
            raise ValueError("unbounded image response")
    else:
        pixels = None
    stage[0] = "render"
    root = tk.Tk(screenName=display)
    root.withdraw()
    try:
        if (root.winfo_screenwidth(), root.winfo_screenheight()) != viewport:
            raise ValueError("observed display size changed")
        x, y, w, h = region
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.geometry(f"{w}x{h}+{x}+{y}")
        canvas = tk.Canvas(
            root,
            width=w,
            height=h,
            highlightthickness=0,
            borderwidth=0,
            background="white",
            takefocus=0,
        )
        canvas.pack()
        if pixels is not None:
            picture = tk.PhotoImage(data=pixels, format="PPM", master=root)
            if (picture.width(), picture.height()) != (w, h):
                raise ValueError("image dimensions changed")
            canvas.create_image(0, 0, image=picture, anchor="nw")
        else:
            text = (
                "Different record"
                if request["kind"] == "wrong_identity"
                else "Identity unavailable"
            )
            canvas.create_text(
                2, h // 2, text=text, anchor="w", fill="black", font=("DejaVu Sans", 14)
            )
        if (
            display_identity(display) != expected
            or focus(env, stage=stage) != focused
            or time.monotonic() >= expires
        ):
            raise ValueError("display changed before mapping")
        stage[0] = "map"
        root.deiconify()
        root.update()
        if (
            display_identity(display) != expected
            or focus(env, stage=stage) != focused
            or time.monotonic() >= expires
            or (
                root.winfo_rootx(),
                root.winfo_rooty(),
                root.winfo_width(),
                root.winfo_height(),
            )
            != region
        ):
            raise ValueError("overlay changed display focus or geometry")
        print(f"ready:{request['nonce']}:{guest}", flush=True)

        failed = [False]

        def tick() -> None:
            if time.monotonic() >= expires:
                root.quit()
                return
            try:
                current = (
                    time.monotonic() < expires
                    and display_identity(display) == expected
                    and focus(env, stage=stage) == focused
                )
            except Exception:
                current = False
            if not current:
                failed[0] = True
                root.quit()
                return
            root.after(100, tick)

        stage[0] = "liveness"
        root.after(100, tick)
        root.mainloop()
        if failed[0]:
            raise ValueError("display changed while overlay was live")
    finally:
        root.destroy()


def main() -> int:
    stage = ["request"]
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384:
            raise ValueError("request too large")
        render(json.loads(raw), stage=stage)
        return 0
    except Exception:
        # No request contents, guest metadata, pixels or exception text escape.
        print(
            "refused:" + (stage[0] if stage[0] in REFUSAL_STAGES else "request"),
            flush=True,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
