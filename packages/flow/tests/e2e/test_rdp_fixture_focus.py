"""Required real-X startup counterfactual for the synthetic RDP fixture."""

from __future__ import annotations

import hashlib
import json
import os
import runpy
import select
import socket
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

_PARENT_SHA256 = "661676e561c3080631d4be507d9423b175caa0cd5669a42ae38927c51f64575d"
_ANCHOR = b'    root = tk.Tk()\n'
_OVERRIDE = b'    root.overrideredirect(True)\n'
_ROWS = [("AC200001", "Alex Archer", ""), ("AC200002", "Brook Brooks", "")]


def _line(stream: int, limit: int, timeout: float) -> bytes:
    if not select.select([stream], [], [], timeout)[0]:
        raise TimeoutError("readiness")
    value = os.read(stream, limit + 1)
    if len(value) > limit or not value.endswith(b"\n"):
        raise ValueError("readiness shape")
    return value.strip()


def _owned(server: subprocess.Popen[bytes], number: bytes) -> bool:
    if server.poll() is not None:
        return False
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(2)
        peer.connect("\0/tmp/.X11-unix/X" + number.decode("ascii"))
        pid, uid, _ = struct.unpack(
            "3i", peer.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
        )
    return pid == server.pid and uid == os.getuid() and server.poll() is None


def _close(process: subprocess.Popen[bytes] | None) -> dict:
    if process is None:
        return {"started": False, "waited": True, "exit": None}
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        process.wait(timeout=5)
        return {"started": True, "waited": True, "exit": process.returncode}
    except Exception:
        return {"started": True, "waited": False, "exit": None}


def _child(source: Path, digest: str, database: Path) -> None:
    import tkinter as tk

    if hashlib.sha256(source.read_bytes()).hexdigest() != digest:
        raise ValueError("source changed")
    original = tk.Tk.mainloop

    def observed_mainloop(root, *args, **kwargs):
        def ready():
            # Raw handles stay only in this owned pipe, never in evidence.
            print(
                json.dumps(
                    {
                        "wrapper": int(root.frame(), 0),
                        "mapped": bool(root.winfo_ismapped()),
                        "geometry": [
                            root.winfo_rootx(), root.winfo_rooty(),
                            root.winfo_width(), root.winfo_height(),
                        ],
                        "tk_version": str(root.tk.call("package", "require", "Tk")),
                    }
                ),
                flush=True,
            )

        root.after(250, ready)
        return original(root, *args, **kwargs)

    tk.Tk.mainloop = observed_mainloop
    sys.argv = [str(source), str(database)]
    runpy.run_path(str(source), run_name="__main__")


def _condition(tmp_path: Path, *, literal_parent: bool) -> None:
    out = tmp_path / "fixture-focus"
    out.mkdir()
    result = {
        "schema": "openadapt.fixture-focus/v1",
        "condition": "literal_parent" if literal_parent else "candidate",
        "status": "UNRUN", "stage": "source", "cleanup": {},
    }
    server = child = None
    read_fd = write_fd = None
    try:
        source = Path(__file__).parents[1] / "fixtures" / "rdp_write_app.py"
        candidate = source.read_bytes()
        if candidate.count(_ANCHOR) != 1 or _OVERRIDE in candidate:
            raise ValueError("candidate shape")
        parent = candidate.replace(_ANCHOR, _ANCHOR + _OVERRIDE, 1)
        if hashlib.sha256(parent).hexdigest() != _PARENT_SHA256:
            raise ValueError("literal parent pin")
        if literal_parent:
            source = out / "literal_parent.py"
            source.write_bytes(parent)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        result["source_sha256"] = digest
        home = out / "home"
        home.mkdir()
        database = out / "fixture.db"
        with sqlite3.connect(database) as db:
            db.execute("CREATE TABLE accounts(id TEXT PRIMARY KEY, holder TEXT, note TEXT)")
            db.executemany("INSERT INTO accounts VALUES(?,?,?)", _ROWS)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(home), "LANG": "C.UTF-8"}
        read_fd, write_fd = os.pipe()
        result["stage"] = "xserver"
        server = subprocess.Popen(
            ["/usr/bin/Xvfb", "-displayfd", str(write_fd), "-screen", "0",
             "1280x800x24", "-nolisten", "tcp"],
            env=env, pass_fds=(write_fd,), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        write_fd = None
        number = _line(read_fd, 16, 10)
        if not number.isdigit() or not 0 <= int(number) < 65536:
            raise ValueError("display allocation")
        if not _owned(server, number):
            raise ValueError("server ownership")
        env["DISPLAY"] = ":" + number.decode("ascii")
        result["stage"] = "app_startup"
        child = subprocess.Popen(
            ["/usr/bin/python3", "-I", "-S", str(Path(__file__).resolve()),
             "--child", str(source), digest, str(database)],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if child.stdout is None:
            raise ValueError("child pipe")
        ready = json.loads(_line(child.stdout.fileno(), 1024, 10))
        if (
            not isinstance(ready, dict)
            or set(ready) != {"wrapper", "mapped", "geometry", "tk_version"}
            or type(ready["wrapper"]) is not int or ready["wrapper"] <= 1
            or type(ready["mapped"]) is not bool
            or not isinstance(ready["tk_version"], str)
            or len(ready["tk_version"]) > 32
            or not all(c in "0123456789.ab" for c in ready["tk_version"])
        ):
            raise ValueError("readiness shape")
        result["stage"] = "raw_focus"
        # -f returns XGetInputFocus itself, without ancestor/WM_CLASS traversal.
        observed = subprocess.run(
            ["/usr/bin/xdotool", "getwindowfocus", "-f"], env=env,
            capture_output=True, timeout=3, check=True,
        )
        value = observed.stdout.strip()
        if not value.isdigit() or len(value) > 20:
            raise ValueError("raw focus shape")
        window = int(value)
        result.update(
            raw_focus_kind="none" if window == 0 else "pointer_root" if window == 1 else "window",
            raw_focus_matches_owned_wrapper=window == ready["wrapper"],
            mapped=ready["mapped"], geometry_exact=ready["geometry"] == [0, 0, 1280, 800],
            tk_version=ready["tk_version"], app_alive=child.poll() is None,
            server_owned=_owned(server, number), source_unchanged=hashlib.sha256(source.read_bytes()).hexdigest() == digest,
        )
        with sqlite3.connect(database) as db:
            result["full_database_unchanged"] = db.execute(
                "SELECT id,holder,note FROM accounts ORDER BY id"
            ).fetchall() == _ROWS
        expected = window == 1 if literal_parent else window == ready["wrapper"]
        checks = ("mapped", "geometry_exact", "app_alive", "server_owned",
                  "source_unchanged", "full_database_unchanged")
        result["status"] = "PASSED" if expected and all(result[k] is True for k in checks) else "FAILED"
    except Exception as exc:
        result["status"] = "ERROR"
        result["exception_type"] = type(exc).__name__ if type(exc) in (
            ValueError, TimeoutError, OSError, subprocess.CalledProcessError,
            subprocess.TimeoutExpired, json.JSONDecodeError,
        ) else "Exception"
    finally:
        result["cleanup"] = {"app": _close(child), "server": _close(server)}
        result["pipe_cleanup_complete"] = True
        for descriptor in (read_fd, write_fd):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    result["pipe_cleanup_complete"] = False
        if child is not None and child.stdout is not None:
            try:
                child.stdout.close()
            except OSError:
                result["pipe_cleanup_complete"] = False
        (out / "focus-evidence.json").write_text(json.dumps(result, indent=2))
    if (
        result["status"] != "PASSED"
        or result["pipe_cleanup_complete"] is not True
        or not all(item["waited"] for item in result["cleanup"].values())
    ):
        raise AssertionError("fixture focus control failed; see fixed safe evidence")


def test_literal_parent_retains_pointer_root(tmp_path: Path) -> None:
    _condition(tmp_path, literal_parent=True)


def test_fixture_startup_owns_raw_focus(tmp_path: Path) -> None:
    _condition(tmp_path, literal_parent=False)


if __name__ == "__main__":
    try:
        if len(sys.argv) != 5 or sys.argv[1] != "--child":
            raise ValueError("child arguments")
        _child(Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4]))
    except Exception:
        raise SystemExit(1) from None
