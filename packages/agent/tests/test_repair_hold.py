"""A halted run that holds its page open: the runner returns and the repair
acts on that page, then releases it."""

from __future__ import annotations

import json
import socket
import sys
import tempfile
import textwrap
import threading
from pathlib import Path

import pytest

from openadapt_agent.repair import HeldRunDemonstrator, RepairError
from openadapt_agent.runner import HOLD_FILE, FlowRunner, RunnerConfig

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="Unix sockets")


def _serve(tmp: Path, log: list) -> dict:
    # macOS caps socket paths near 104 bytes; pytest's tmp_path is longer.
    path = str(Path(tempfile.mkdtemp(prefix="oa-", dir="/tmp")) / "s")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(4)

    def loop() -> None:
        while True:
            conn, _ = server.accept()
            with conn:
                msg = json.loads(conn.recv(65536).decode())
                log.append(msg)
                if msg.get("secret") != "s3cret":
                    reply = {"ok": False}
                elif msg["op"] == "viewport":
                    reply = {"ok": True, "viewport": [640, 480]}
                elif msg["op"] == "screenshot":
                    reply = {"ok": True, "png_base64": "aGk="}
                else:
                    reply = {"ok": True}
                conn.sendall(json.dumps(reply).encode() + b"\n")
                if msg["op"] == "release":
                    server.close()
                    return

    threading.Thread(target=loop, daemon=True).start()
    return {"socket": path, "secret": "s3cret", "exit_code": 1}


def test_held_demonstrator_talks_to_the_held_page(tmp_path: Path) -> None:
    log: list = []
    hold = _serve(tmp_path, log)
    (tmp_path / HOLD_FILE).write_text(json.dumps(hold))
    screen = HeldRunDemonstrator.for_run(tmp_path)
    assert screen is not None
    assert screen.viewport() == (640, 480)
    assert screen.screenshot() == b"hi"
    screen.click(5, 6)
    screen.release()
    assert [m["op"] for m in log] == ["viewport", "screenshot", "click", "release"]
    screen.release()  # a second release is harmless
    with pytest.raises(RepairError):
        screen.click(1, 1)


def test_no_hold_file_means_no_held_screen(tmp_path: Path) -> None:
    assert HeldRunDemonstrator.for_run(tmp_path) is None


def test_runner_returns_when_flow_holds(tmp_path: Path) -> None:
    fake = tmp_path / "fake_flow.py"
    fake.write_text(
        textwrap.dedent(
            """
            import json, sys, time
            from pathlib import Path
            args = sys.argv[1:]
            assert "--hold-on-halt" in args
            run_dir = Path(args[args.index("--run-dir") + 1])
            run_dir.mkdir(parents=True)
            (run_dir / "report.json").write_text(json.dumps({"success": False}))
            print("halted")
            sys.stdout.flush()
            (run_dir / "repair-hold.json").write_text(json.dumps({"exit_code": 1}))
            time.sleep(60)
            """
        )
    )
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    runner = FlowRunner(
        RunnerConfig(
            flow_cli=(sys.executable, str(fake)),
            runs_dir=tmp_path / "runs",
            timeout_s=30,
            hold_on_halt_s=60,
        )
    )
    outcome = runner.run(workflow="w", bundle_dir=bundle, params={})
    assert outcome.exit_code == 1
    assert outcome.status != "success"
    from openadapt_agent import runner as runner_module

    for proc in runner_module._HELD:
        proc.kill()
