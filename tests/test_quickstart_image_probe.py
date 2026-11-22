"""The image-protocol query against a real pseudo-terminal.

``terminal_answers`` writes a query and a device-attributes request to the
controlling terminal and reads the reply with echo off. These tests run it in
a child whose controlling terminal is a pty, with the parent playing the
terminal: answering the protocol and the device attributes, answering only
the device attributes, or staying silent. The child also checks that the
terminal's mode is restored afterwards.
"""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX pseudo-terminals only"
)

DA1 = b"\x1b[?62;22c"
ANSWERS = {
    "kitty": b"\x1b_Gi=31;OK\x1b\\",
    "iterm2": b"\x1b]1337;ReportCellSize=17;8\x07",
}


def _probe(protocol: str, reply: str) -> dict:
    import pty

    read_end, write_end = os.pipe()
    pid, master = pty.fork()
    if pid == 0:  # the child: its controlling terminal is the pty
        os.close(read_end)
        code = 0
        try:
            import termios

            from openadapt.quickstart.images import terminal_answers

            before = termios.tcgetattr(0)
            started = time.monotonic()
            answered = terminal_answers(protocol, timeout_s=1.0)
            after = termios.tcgetattr(0)
            # PENDIN is the kernel's own "input pending" state bit, which it
            # may set when canonical mode comes back; it is not a mode.
            pendin = getattr(termios, "PENDIN", 0)
            before[3] &= ~pendin
            after[3] &= ~pendin
            result = {
                "answered": answered,
                "restored": after == before,
                "seconds": time.monotonic() - started,
            }
            os.write(write_end, json.dumps(result).encode())
        except BaseException as exc:  # noqa: BLE001 - reported to the parent
            os.write(write_end, json.dumps({"error": repr(exc)}).encode())
            code = 1
        os._exit(code)
    os.close(write_end)
    seen = b""
    deadline = time.monotonic() + 5
    while b"\x1b[c" not in seen and time.monotonic() < deadline:
        try:
            seen += os.read(master, 1024)
        except OSError:
            break
    if reply == "protocol":
        os.write(master, ANSWERS[protocol] + DA1)
    elif reply == "attributes":
        os.write(master, DA1)
    out = b""
    while True:
        chunk = os.read(read_end, 4096)
        if not chunk:
            break
        out += chunk
    os.waitpid(pid, 0)
    os.close(master)
    result = json.loads(out)
    result["query"] = seen
    return result


@pytest.mark.parametrize("protocol", ["kitty", "iterm2"])
def test_a_terminal_that_answers_the_protocol_is_used(protocol):
    result = _probe(protocol, "protocol")
    assert "error" not in result, result
    assert result["answered"] is True and result["restored"] is True
    assert result["query"].endswith(b"\x1b[c")


@pytest.mark.parametrize("protocol", ["kitty", "iterm2"])
def test_device_attributes_alone_mean_no_images_without_waiting(protocol):
    result = _probe(protocol, "attributes")
    assert result["answered"] is False and result["restored"] is True
    assert result["seconds"] < 0.9  # stopped at the attributes, not the timeout


def test_a_silent_terminal_times_out_and_is_restored():
    result = _probe("kitty", "silent")
    assert result["answered"] is False and result["restored"] is True
