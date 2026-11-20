"""A synthetic lock-holding descendant for the CI helper's deadline boundary."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def load_helper() -> Any:
    path = ROOT / ".github" / "actions" / "apt-install" / "apt_install.py"
    spec = importlib.util.spec_from_file_location("apt_deadline_subject", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def lock_available(path: Path) -> bool:
    with path.open("r+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def probe(helper: Any, temporary: Path, *, real_sudo: bool, stubborn: bool) -> dict:
    """No package operation; the only child holds this fixture's own lock."""
    if real_sudo and not (
        os.environ.get("GITHUB_ACTIONS") == "true"
        and os.environ.get("RUNNER_OS") == "Linux"
    ):
        raise RuntimeError("real sudo probe requires an ephemeral GitHub Linux runner")
    if real_sudo:
        assert os.geteuid() != 0, "start the privilege probe as the unprivileged runner"
    temporary.mkdir()
    lock, ready = temporary / "lock", temporary / "ready.json"
    lock.touch()  # The runner owns these files even when its child runs as root.
    ready.touch()
    nonce = uuid.uuid4().hex
    child = (
        "import fcntl,json,os,pathlib,signal,sys,time; "
        + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if stubborn else "")
        + "f=open(sys.argv[1],'r+'); fcntl.flock(f,fcntl.LOCK_EX); "
        "pathlib.Path(sys.argv[2]).write_text(json.dumps("
        "{'pid':os.getpid(),'uid':os.geteuid(),'nonce':sys.argv[3]})); time.sleep(6)"
    )
    wrapper = temporary / "wrapper.py"
    wrapper.write_text(
        "import pathlib,subprocess,sys,time\n"
        f"child={child!r}\n"
        "subprocess.Popen([sys.executable,'-c',child,*sys.argv[1:]])\n"
        "while not pathlib.Path(sys.argv[2]).stat().st_size: time.sleep(.01)\n"
        "print('synthetic lock holder ready',flush=True)\n"
        "time.sleep(6)\n"
    )
    command = [sys.executable, str(wrapper), str(lock), str(ready), nonce]
    if real_sudo:
        command = helper._sudo(command)
    try:
        started = time.monotonic()
        code, _ = helper._run(command, timeout=1)
        elapsed = time.monotonic() - started
        identity = json.loads(ready.read_text())
        assert identity["nonce"] == nonce
        assert ready.stat().st_uid == os.geteuid()
        assert identity["uid"] == (0 if real_sudo else os.geteuid())
        available = lock_available(lock)
        assert available, "the owned descendant still holds the fixture lock"
        assert elapsed < 3, (
            elapsed,
            "a descendant must not outlive the deadline budget",
        )
        assert code == 137, (code, "the bounded process group must be terminated")
        return {
            "code": code,
            "elapsed_seconds": round(elapsed, 3),
            "real_sudo": real_sudo,
            "child_uid": identity["uid"],
            "child_ignores_TERM": stubborn,
            "lock_released_when_helper_returned": available,
        }
    finally:
        # Even a regression self-terminates after six seconds. No arbitrary
        # PID or process-group kill is needed to clean a failed probe.
        for _ in range(160):
            if lock_available(lock):
                break
            time.sleep(0.05)
        assert lock_available(lock), "synthetic child did not self-terminate"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-sudo", action="store_true")
    args = parser.parse_args()
    helper = load_helper()
    with tempfile.TemporaryDirectory(dir=os.environ.get("RUNNER_TEMP")) as directory:
        results = [
            probe(
                helper,
                Path(directory) / str(stubborn),
                real_sudo=args.real_sudo,
                stubborn=stubborn,
            )
            for stubborn in (False, True)
        ]
    print(
        json.dumps(
            {
                "result": "PASS",
                "helper_sha256": hashlib.sha256(
                    Path(helper.__file__).read_bytes()
                ).hexdigest(),
                "cases": results,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
