"""`openadapt-flow flight` refuses unenforced egress and marks a bypassed run.

These run with ``--no-scrub --i-accept-unscrubbed`` so they need no scrubber
(the scrubber is covered in test_flight_scrub and test_flight_phi_payload);
the opt-out ledger is redirected to the test's folder.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pytest

from openadapt_flow.flight import egress as egress_mod
from tests.fixtures.flight.fake_provider import FakeProvider


@pytest.fixture(autouse=True)
def _ledger(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))


@pytest.fixture
def provider(monkeypatch):
    fake = FakeProvider().start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", fake.url)
    yield fake
    fake.stop()


def _run(tmp_path: Path, code: str, *flags: str) -> tuple[int, Path]:
    from openadapt_flow.flight.cli import add_parser

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers())
    run_dir = tmp_path / "run"
    args = parser.parse_args(
        [
            "flight",
            "--out",
            str(run_dir),
            "--gate",
            "refuse",
            "--no-scrub",
            "--i-accept-unscrubbed",
            *flags,
            "--",
            sys.executable,
            "-c",
            code,
        ]
    )
    return args.func(args), run_dir


def test_no_egress_block_refuses_to_start_by_default(
    tmp_path, provider, monkeypatch, capsys
):
    monkeypatch.setattr(
        egress_mod, "egress_block_available", lambda: (False, "test: no block")
    )
    code, run_dir = _run(tmp_path, "print('ran')")
    assert code == 2
    assert "egress block is not available" in capsys.readouterr().err
    assert not (run_dir / "flight.json").exists()


def test_the_opt_out_runs_and_is_recorded(tmp_path, provider, monkeypatch):
    monkeypatch.setattr(
        egress_mod, "egress_block_available", lambda: (False, "test: no block")
    )
    code, run_dir = _run(tmp_path, "print('ran')", "--allow-unenforced-egress")
    assert code == 0
    egress = json.loads((run_dir / "flight.json").read_text())["details"]["egress"]
    assert egress["enforced"] is False
    assert egress["opt_out"] == "--allow-unenforced-egress"
    assert egress["refused"] is False
    assert json.loads((run_dir / "egress.json").read_text())["mode"] == "unscrubbed"


def test_more_calls_reported_than_proxied_exits_3_and_is_marked(
    tmp_path, provider, monkeypatch
):
    monkeypatch.setattr(
        egress_mod, "egress_block_available", lambda: (False, "test: no block")
    )
    agent = (
        "import json,os\n"
        "open(os.environ['OPENADAPT_USAGE_PATH'],'w').write(json.dumps("
        "{'schema':'openadapt.agent-usage/v1','calls':2}))\n"
    )
    code, run_dir = _run(tmp_path, agent, "--allow-unenforced-egress")
    assert code == 3
    details = json.loads((run_dir / "flight.json").read_text())["details"]
    assert details["exit_code"] == 3 and details["agent_exit_code"] == 0
    assert details["egress"]["refused"] is True
    assert "reports 2 model calls but 0" in details["egress"]["refusal"]
    assert json.loads((run_dir / "egress.json").read_text())["mode"] == "blocked"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_on_macos_the_agent_runs_under_the_block_and_a_direct_call_fails(
    tmp_path, provider
):
    agent = (
        "import json,os,socket\n"
        "s=socket.socket();s.settimeout(3)\n"
        "try:\n"
        "    s.connect(('192.0.2.1',443));r='connected'\n"
        "except PermissionError:\n"
        "    r='EPERM'\n"
        "open(os.path.join(os.environ['OPENADAPT_FLIGHT_RUN_DIR'],'probe.txt'),'w')"
        ".write(r)\n"
    )
    code, run_dir = _run(tmp_path, agent)
    assert code == 0
    assert (run_dir / "probe.txt").read_text() == "EPERM"
    egress = json.loads((run_dir / "flight.json").read_text())["details"]["egress"]
    assert egress["enforced"] is True and egress["block"] == "sandbox-exec"
    assert egress["opt_out"] is None


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS only")
def test_no_egress_block_keeps_the_old_behaviour_and_is_recorded(tmp_path, provider):
    code, run_dir = _run(tmp_path, "print('ran')", "--no-egress-block")
    assert code == 0
    egress = json.loads((run_dir / "flight.json").read_text())["details"]["egress"]
    assert egress["enforced"] is False and egress["opt_out"] == "--no-egress-block"


def test_a_trapped_direct_call_exits_3(tmp_path, provider, monkeypatch):
    monkeypatch.setattr(
        egress_mod, "egress_block_available", lambda: (False, "test: no block")
    )
    agent = (
        "import urllib.request\n"
        "try:\n"
        "    urllib.request.urlopen('https://api.openai.invalid/v1/responses',"
        "timeout=10)\n"
        "except Exception:\n"
        "    pass\n"
    )
    code, run_dir = _run(tmp_path, agent, "--allow-unenforced-egress")
    assert code == 3
    egress = json.loads((run_dir / "flight.json").read_text())["details"]["egress"]
    assert egress["checks"]["trapped_hosts"] == ["api.openai.invalid:443"]


def test_a_safe_label_with_a_commit_verb_is_refused(tmp_path, provider, capsys):
    code, run_dir = _run(tmp_path, "print('ran')", "--safe-label", "Save draft")
    assert code == 2
    assert "commit verb" in capsys.readouterr().err
    assert not (run_dir / "flight.json").exists()
