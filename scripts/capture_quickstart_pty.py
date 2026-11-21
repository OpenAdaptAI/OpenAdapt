"""Run ``openadapt quickstart`` in a pseudo-terminal and save what it prints.

Headless only: nothing here opens a window. The pty gets a fixed size and a
``TERM_PROGRAM`` so the output is what that terminal would receive. Nothing
answers terminal queries, so image detection sees a terminal that doesn't
speak an image protocol. The publish question (when a local stub endpoint
says hosted Seals are live) is answered "n"; nothing is uploaded.

Usage:
  python scripts/capture_quickstart_pty.py --out DIR --name NAME \
      --term-program Apple_Terminal --cols 120 -- openadapt quickstart --break-it
"""

from __future__ import annotations

import argparse
import fcntl
import http.server
import json
import os
import pty
import re
import select
import struct
import subprocess
import sys
import termios
import threading
import time
from pathlib import Path


class _Stub(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"enabled": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 - never reached: the answer is "n"
        self.send_response(500)
        self.end_headers()

    def log_message(self, *_args):
        pass


_ANSI = re.compile(
    r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(\x07|\x1b\\)|\x1b[78=>]"
)


def strip(text: str) -> str:
    return _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "")


def render(raw: bytes, cols: int, rows: int) -> str:
    """What the terminal shows at the end, scrollback included (needs pyte).

    Spinners and live redraws resolve the way a terminal resolves them, so
    this is the text a person would read. Without pyte, the escapes are
    stripped instead.
    """
    try:
        import pyte
    except ImportError:
        return strip(raw.decode("utf-8", "replace"))
    screen = pyte.HistoryScreen(cols, rows, history=10000, ratio=0.5)
    stream = pyte.ByteStream(screen)
    stream.feed(raw.replace(b"\n", b"\r\n").replace(b"\r\r\n", b"\r\n"))

    def text(line) -> str:
        return "".join(line[x].data for x in range(cols)).rstrip()

    lines = [text(line) for line in screen.history.top]
    lines += [line.rstrip() for line in screen.display]
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--term-program", default="Apple_Terminal")
    parser.add_argument("--cols", type=int, default=120)
    parser.add_argument("--rows", type=int, default=50)
    parser.add_argument("--cwd", type=Path, default=None)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("DISPLAY", "WAYLAND_DISPLAY", "CI", "TMUX", "STY", "NO_COLOR")
        and not key.startswith(("ITERM", "LC_TERMINAL", "WEZTERM", "KITTY", "TERM_"))
    }
    env.update(
        TERM="xterm-256color",
        TERM_PROGRAM=args.term_program,
        COLORTERM="truecolor",
        COLUMNS=str(args.cols),
        LINES=str(args.rows),
        # Never a visible browser on this Mac: the seal page is never opened
        # (display_available() is False over SSH) and the bundled headless
        # shell is the browser.
        SSH_TTY="/dev/ttys999",
        OPENADAPT_FLOW_BROWSER="bundled",
        UV_NO_PROGRESS="1",
        OPENADAPT_SEAL_ENDPOINT=f"http://127.0.0.1:{server.server_address[1]}",
    )
    master, slave = pty.openpty()
    fcntl.ioctl(
        slave, termios.TIOCSWINSZ, struct.pack("HHHH", args.rows, args.cols, 0, 0)
    )
    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        env=env,
        cwd=args.cwd,
        start_new_session=True,
    )
    os.close(slave)
    raw = b""
    answered_enter = answered_publish = False
    while True:
        if time.monotonic() - started > args.timeout:
            proc.kill()
            raw += b"\n[capture: timed out]\n"
            break
        ready, _, _ = select.select([master], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                if proc.poll() is not None:
                    break
                continue
            raw += chunk
            text = strip(raw.decode("utf-8", "replace"))
            if not answered_enter and "(Enter)" in text:
                time.sleep(0.3)
                os.write(master, b"\r")
                answered_enter = True
            if not answered_publish and "Publish? [y/N]" in text:
                time.sleep(0.3)
                os.write(master, b"n\r")
                answered_publish = True
        elif proc.poll() is not None:
            break
    code = proc.wait()
    server.shutdown()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / f"{args.name}.ansi").write_bytes(raw)
    (args.out / f"{args.name}.txt").write_text(render(raw, args.cols, args.rows))
    meta = {
        "command": command,
        "term_program": args.term_program,
        "cols": args.cols,
        "exit_code": code,
        "seconds": round(time.monotonic() - started, 1),
    }
    (args.out / f"{args.name}.json").write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps(meta))
    return 0


if __name__ == "__main__":
    sys.exit(main())
