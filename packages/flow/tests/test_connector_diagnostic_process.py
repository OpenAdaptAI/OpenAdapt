"""Cross-platform capture through the connector's real subprocess boundary."""

import os
import sys
import time

from openadapt_flow.connector.diagnostics import MAX_OUTPUT
from openadapt_flow.connector.executor import _subprocess_runner


def test_refusal_tail_works_with_inherited_stdout(tmp_path):
    # The grandchild inherits stdout. The connector must finish the governed
    # child and deliver its last diagnostic without waiting for that grandchild.
    release = tmp_path / "release"
    expired = tmp_path / "expired"
    stopped = tmp_path / "stopped"
    descendant = (
        "import pathlib, sys, time\n"
        "release, expired, stopped = map(pathlib.Path, sys.argv[1:])\n"
        "for _ in range(200):\n"
        "    if release.exists(): break\n"
        "    time.sleep(0.05)\n"
        "else: expired.touch()\n"
        "stopped.touch()\n"
    )
    parent = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', *sys.argv[1:]], stdout=sys.stdout, stderr=sys.stderr)\n"
        f"print('discarded-start' + 'x' * {MAX_OUTPUT + 1000}, flush=True)\n"
        "print('synthetic admission gate', flush=True)\n"
        "print('synthetic refusal', file=sys.stderr, flush=True)\n"
        "sys.exit(2)\n"
    )
    try:
        outcome = _subprocess_runner(
            [
                sys.executable,
                "-c",
                parent,
                descendant,
                str(release),
                str(expired),
                str(stopped),
            ],
            tmp_path,
            os.environ,
        )
        assert outcome.returncode == 2
        assert not expired.exists()
        assert "discarded-start" not in outcome.output
        assert len(outcome.output) <= MAX_OUTPUT
        assert outcome.output.splitlines()[-2:] == [
            "synthetic admission gate",
            "synthetic refusal",
        ]
    finally:
        release.touch()
        deadline = time.monotonic() + 5
        while not stopped.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert stopped.exists()
