"""Unattended on-prem service: loop, health, restart, outbound-traffic audit."""

from __future__ import annotations

import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import textwrap
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from openadapt_flow import onprem_service as svc
from openadapt_flow.onprem_service import (
    ServiceConfig,
    classify,
    egress_audit,
    health,
    parse_lsof,
    process_tree,
    restart_command,
    run_service,
)

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "on-prem"


def _queue_script(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    script = tmp_path / "run-queue.sh"
    script.write_text("#!/usr/bin/env bash\nexit 0\n")
    return script


def _fake_runner(root: Path, calls: list):
    """Stands in for run-queue.sh: records the env and leaves one finished run."""

    def run(argv, env):
        calls.append((list(argv), dict(env)))
        run_dir = root / "runs" / f"job-{len(calls)}"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "report.json").write_text(
            json.dumps({"run_id": run_dir.name, "execution_outcome": "VERIFIED"})
        )
        return 0

    return run


def _service(tmp_path: Path, **kw) -> "tuple[Path, list, int]":
    root = tmp_path / "root"
    calls: list = []
    enforced: list = []
    rc = run_service(
        ServiceConfig(
            root=root,
            queue_script=_queue_script(tmp_path),
            poll_seconds=0.01,
            max_cycles=kw.pop("cycles", 2),
            allow_outbound=kw.pop("allow_outbound", False),
        ),
        env=kw.pop("env", {}),
        runner=_fake_runner(root, calls),
        enforce=lambda env: enforced.append(env),
        install_signals=False,
    )
    calls.append(("enforced", enforced))
    return root, calls, rc


def test_service_runs_the_queue_seals_runs_and_writes_a_heartbeat(tmp_path):
    root, calls, rc = _service(tmp_path)
    assert rc == 0
    queue_calls = [c for c in calls if c[0] != "enforced"]
    assert len(queue_calls) == 2
    argv, env = queue_calls[0]
    assert argv[-1] == "once"
    assert env["OPENADAPT_NO_OUTBOUND"] == "1"
    assert env["OPENADAPT_FLOW_SCRUB"] == "on"
    assert env["OPENADAPT_SEAL_HOME"] == str(root / "seal")
    # This process enforced the mode too.
    assert calls[-1][1] and calls[-1][1][0]["OPENADAPT_NO_OUTBOUND"] == "1"
    for run in (root / "runs").iterdir():
        assert (run / "seal.json").is_file()
    beat = json.loads((root / "service" / "heartbeat.json").read_text())
    assert beat["cycles"] == 2 and beat["no_outbound"] is True
    assert beat["state"] == "stopped"
    assert beat["last_cycle"]["errors"] == []
    # Seals were folded into a local batch; nothing was submitted.
    batches = list((root / "seal" / "batch" / "batches").glob("*.json"))
    assert batches and all(
        json.loads(b.read_text())["status"] == "built" for b in batches
    )


def test_service_refuses_to_start_when_outbound_is_explicitly_on(tmp_path):
    _, calls, rc = _service(tmp_path, env={"OPENADAPT_NO_OUTBOUND": "0"})
    assert rc == 2 and [c for c in calls if c[0] != "enforced"] == []
    root, calls, rc = _service(
        tmp_path / "b", env={"OPENADAPT_NO_OUTBOUND": "0"}, allow_outbound=True
    )
    assert rc == 0
    queue_env = [c for c in calls if c[0] != "enforced"][0][1]
    assert queue_env.get("OPENADAPT_NO_OUTBOUND") == "0"
    assert (
        json.loads((root / "service" / "heartbeat.json").read_text())["no_outbound"]
        is False
    )


def test_service_stops_on_signal_event(tmp_path):
    root = tmp_path / "root"
    stop = threading.Event()
    calls: list = []
    t = threading.Thread(
        target=run_service,
        args=(
            ServiceConfig(
                root=root, queue_script=_queue_script(tmp_path), poll_seconds=0.05
            ),
        ),
        kwargs=dict(
            env={},
            runner=_fake_runner(root, calls),
            stop=stop,
            enforce=lambda env: None,
            install_signals=False,
        ),
    )
    t.start()
    deadline = time.time() + 10
    while not calls and time.time() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(10)
    assert not t.is_alive()
    assert (
        json.loads((root / "service" / "heartbeat.json").read_text())["state"]
        == "stopped"
    )


def _beat(root: Path, **over) -> None:
    beat = {
        "schema": svc.HEARTBEAT_SCHEMA,
        "pid": os.getpid(),
        "started_at": "2026-10-17T12:00:00Z",
        "last_beat": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "poll_seconds": 10,
        "no_outbound": True,
        "state": "running",
        "cycles": 1,
        "last_cycle": {"queue_rc": 0, "sealed": 0, "errors": []},
    }
    beat.update(over)
    (root / "service").mkdir(parents=True, exist_ok=True)
    (root / "service" / "heartbeat.json").write_text(json.dumps(beat))


def _status(report, name):
    return next(c["status"] for c in report["checks"] if c["name"] == name)


def test_health_passes_for_a_live_fresh_service(tmp_path):
    _beat(tmp_path)
    report = health(tmp_path)
    assert report["healthy"], report


def test_health_fails_without_a_heartbeat(tmp_path):
    assert not health(tmp_path)["healthy"]


def test_health_fails_when_stale_dead_or_stopped(tmp_path):
    _beat(tmp_path, last_beat="2026-01-01T00:00:00Z")
    assert _status(health(tmp_path), "fresh") == "fail"
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _beat(tmp_path, pid=dead.pid)
    assert _status(health(tmp_path), "running") == "fail"
    _beat(tmp_path, state="stopped")
    assert not health(tmp_path)["healthy"]


def test_health_fails_on_a_stuck_job_and_a_readable_key(tmp_path):
    _beat(tmp_path)
    job = tmp_path / "jobs" / "processing" / "j1.job"
    job.parent.mkdir(parents=True)
    job.write_text("bundle=/b\n")
    old = time.time() - 7200
    os.utime(job, (old, old))
    assert _status(health(tmp_path), "stuck_jobs") == "fail"
    job.unlink()
    from openadapt_flow.offline_seal import public_key_entry

    public_key_entry(tmp_path / "seal")
    key = tmp_path / "seal" / "keys" / "ed25519.pem"
    assert _status(health(tmp_path), "seal_key") == "pass"
    key.chmod(0o644)
    assert _status(health(tmp_path), "seal_key") == "fail"


def test_health_warns_on_refused_outbound_attempts_and_allow_outbound(tmp_path):
    _beat(tmp_path, no_outbound=False)
    log = tmp_path / "audit" / "outbound-blocked.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text('{"event":"socket.connect","host":"192.0.2.1"}\n')
    report = health(tmp_path)
    assert report["healthy"]
    assert _status(report, "blocked_outbound") == "warn"
    assert _status(report, "no_outbound") == "warn"


def test_restart_commands():
    assert restart_command("x", "darwin") == [
        "launchctl",
        "kickstart",
        "-k",
        f"gui/{os.getuid()}/x",
    ]
    assert restart_command("x", "linux") == ["systemctl", "restart", "x"]
    assert restart_command("x", "win32")[-1] == "Restart-Service -Name x"


def test_health_cli_restarts_an_unhealthy_service(tmp_path, monkeypatch, capsys):
    from openadapt_flow.__main__ import main

    seen = []
    monkeypatch.setattr(svc, "restart", lambda label, **k: seen.append(label) or 0)
    assert (
        main(["service", "health", "--root", str(tmp_path), "--restart-on-fail"]) == 1
    )
    assert seen == ["ai.openadapt.flow-service"]
    _beat(tmp_path)
    seen.clear()
    assert (
        main(["service", "health", "--root", str(tmp_path), "--restart-on-fail"]) == 0
    )
    assert seen == []


PS = """\
  100     1
  200   100
  201   200
  300     1
  202   201
"""

LSOF = """\
COMMAND   PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME
python3   100 op    5u  IPv4 0x1      0t0  TCP 127.0.0.1:52000->127.0.0.1:8765 (ESTABLISHED)
python3   100 op    6u  IPv6 0x2      0t0  TCP [::1]:52001->[::1]:8766 (ESTABLISHED)
xfreerdp  200 op    7u  IPv4 0x3      0t0  TCP 10.20.0.9:50000->10.20.0.15:3389 (ESTABLISHED)
python3   100 op    8u  IPv4 0x4      0t0  TCP 127.0.0.1:8765 (LISTEN)
"""

LSOF_LEAK = LSOF + (
    "curl      202 op    9u  IPv4 0x5      0t0  TCP 10.20.0.9:50001->93.184.216.34:443 (ESTABLISHED)\n"
    "python3   100 op   10u  IPv4 0x6      0t0  TCP *:9000 (LISTEN)\n"
)


def test_process_tree_includes_all_descendants():
    assert process_tree(100, PS) == {100, 200, 201, 202}


def test_lsof_parsing_and_classification():
    entries = parse_lsof(LSOF_LEAK)
    assert {e["remote_host"] for e in entries if e["remote_host"]} == {
        "127.0.0.1",
        "::1",
        "10.20.0.15",
        "93.184.216.34",
    }
    from openadapt_flow.no_outbound import parse_allow

    groups = classify(entries, parse_allow("10.20.0.15"))
    assert [e["remote_host"] for e in groups["outbound"]] == ["93.184.216.34"]
    assert [e["local_host"] for e in groups["listening_exposed"]] == ["*"]
    # Without the allow list, the LAN RDP host counts as outbound.
    assert len(classify(entries)["outbound"]) == 2


def _reader(lsof_text):
    calls = []

    def read(argv):
        calls.append(list(argv))
        return PS if argv[0] == "ps" else lsof_text

    return read, calls


def test_egress_audit_is_clean_for_local_and_allowed_peers(tmp_path):
    _beat(tmp_path, pid=os.getpid())
    read, calls = _reader(LSOF.replace(" 100 ", f" {os.getpid()} "))
    report = egress_audit(
        tmp_path,
        samples=3,
        interval_s=0,
        reader=read,
        sleep=lambda s: None,
        env={"OPENADAPT_NO_OUTBOUND_ALLOW": "10.20.0.15"},
    )
    assert report["verdict"] == "clean", report
    assert report["samples_taken"] == 3
    assert sum(1 for c in calls if c[0] == "lsof") == 3
    # The audit report is written and sealed with the local key.
    path = Path(report["report_path"])
    assert path.is_file() and report["sealed"]
    import hashlib

    from openadapt_flow.contracts.seal import parse_key_registry
    from openadapt_flow.offline_seal import public_key_entry, verify_local_seal

    seal = json.loads(Path(str(path) + ".seal.json").read_text())
    reg = parse_key_registry([public_key_entry(tmp_path / "seal")])
    assert verify_local_seal(
        seal, hashlib.sha256(path.read_bytes()).hexdigest(), reg
    ).ok


def test_egress_audit_flags_a_descendant_talking_off_the_machine(tmp_path):
    _beat(tmp_path)
    pid = os.getpid()
    ps = PS.replace("  100     1", f"  {pid}     1").replace(
        "  200   100", f"  200   {pid}"
    )
    read_calls = []

    def read(argv):
        read_calls.append(argv)
        return ps if argv[0] == "ps" else LSOF_LEAK.replace(" 100 ", f" {pid} ")

    report = egress_audit(
        tmp_path,
        samples=1,
        reader=read,
        env={"OPENADAPT_NO_OUTBOUND_ALLOW": "10.20.0.15"},
    )
    assert report["verdict"] == "outbound_seen"
    assert report["outbound_peers"] == [
        {
            "command": "curl",
            "pid": 202,
            "proto": "TCP",
            "remote_host": "93.184.216.34",
            "remote_port": "443",
        }
    ]
    assert report["exposed_listeners"]
    lsof_argv = next(a for a in read_calls if a[0] == "lsof")
    assert "202" in lsof_argv[-1].split(",")


def test_egress_audit_unavailable_when_service_is_down(tmp_path):
    report = egress_audit(tmp_path, samples=1, write=False)
    assert report["verdict"] == "unavailable"


@pytest.mark.skipif(shutil.which("lsof") is None, reason="needs lsof")
def test_live_egress_audit_sees_a_real_loopback_connection(tmp_path):
    """Real ps + lsof against a child that holds a loopback connection."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                f"""
                import socket, sys, time
                c = socket.create_connection(("127.0.0.1", {port}))
                print("up", flush=True)
                time.sleep(30)
                """
            ),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        conn, _ = srv.accept()
        assert child.stdout.readline().strip() == "up"
        report = egress_audit(tmp_path, pid=child.pid, samples=2, interval_s=0.2)
        assert report["verdict"] == "clean", report
        assert f"127.0.0.1:{port}" in report["local_peers"]
        assert child.pid in report["pids_seen"]
        conn.close()
    finally:
        child.kill()
        child.wait()
        srv.close()


def _oaf_wrapper(tmp_path: Path) -> Path:
    """An ``openadapt-flow`` executable that runs this checkout."""
    oaf = tmp_path / "venv" / "bin" / "openadapt-flow"
    oaf.parent.mkdir(parents=True)
    oaf.write_text(
        f'#!/bin/sh\nPYTHONPATH="{ROOT}" exec "{sys.executable}" -m openadapt_flow "$@"\n'
    )
    oaf.chmod(0o755)
    return oaf


def _render(tmp_path: Path, *extra: str) -> "tuple[Path, Path, Path, str]":
    oaf = _oaf_wrapper(tmp_path)
    out = tmp_path / "agents"
    root = tmp_path / "root"
    proc = subprocess.run(
        [
            "bash",
            str(DEPLOY / "bin" / "install-macos.sh"),
            "--render-only",
            str(out),
            "--root",
            str(root),
            "--openadapt-flow",
            str(oaf),
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return oaf, out, root, proc.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="bash installer")
def test_launchd_templates_render_to_valid_plists(tmp_path):
    oaf, out, root, _ = _render(tmp_path)
    service = plistlib.loads((out / "ai.openadapt.flow-service.plist").read_bytes())
    assert service["KeepAlive"] is True and service["RunAtLoad"] is True
    assert service["ProgramArguments"][:3] == [str(oaf), "service", "run"]
    env = service["EnvironmentVariables"]
    assert env["OPENADAPT_NO_OUTBOUND"] == "1"
    assert env["OPENADAPT_FLOW_SCRUB"] == "on"
    assert env["OPENADAPT_NO_OUTBOUND_ALLOW_FILE"] == str(
        root / "no-outbound-allow.json"
    )
    health_plist = plistlib.loads((out / "ai.openadapt.flow-health.plist").read_bytes())
    assert health_plist["StartInterval"] == 60
    assert "--restart-on-fail" in health_plist["ProgramArguments"]
    boot = plistlib.loads((out / "ai.openadapt.flow-boot-check.plist").read_bytes())
    assert boot["UserName"] and boot["RunAtLoad"] is True
    assert boot["ProgramArguments"][1:3] == ["service", "boot-check"]
    pf = plistlib.loads((out / "ai.openadapt.flow-pf.plist").read_bytes())
    assert pf["ProgramArguments"] == ["/sbin/pfctl", "-E", "-f", "/etc/pf.conf"]


@pytest.mark.skipif(sys.platform == "win32", reason="bash installer")
def test_installer_starts_with_an_empty_allow_list_and_default_reboot_policy(tmp_path):
    _, out, root, stdout = _render(tmp_path)
    allow = json.loads((root / "no-outbound-allow.json").read_text())
    assert allow["entries"] == []
    assert "only this machine is reachable" in stdout
    settings = json.loads((root / "service" / "service.json").read_text())
    assert settings["reboot"]["auto_login"] is False
    assert settings["alerts"]["channels"] == []
    rules = [
        ln
        for ln in (out / "ai.openadapt.flow.pf").read_text().splitlines()
        if ln and not ln.startswith("#")
    ]
    assert rules[0] == "pass out quick on lo0 all" and len(rules) == 2


@pytest.mark.skipif(sys.platform == "win32", reason="bash installer")
def test_installer_derives_the_session_host_and_shows_it(tmp_path):
    rdp = tmp_path / "site.rdp"
    rdp.write_text("full address:s:10.20.0.15:3390\nusername:s:svc\n")
    _, out, root, stdout = _render(
        tmp_path, "--session-profile", str(rdp), "--auto-login"
    )
    assert "added: 10.20.0.15/32 port 3390" in stdout
    entries = json.loads((root / "no-outbound-allow.json").read_text())["entries"]
    assert [e["network"] for e in entries] == ["10.20.0.15/32"]
    anchor = (out / "ai.openadapt.flow.pf").read_text()
    assert "to 10.20.0.15 port 3390 user" in anchor
    settings = json.loads((root / "service" / "service.json").read_text())
    assert settings["reboot"]["auto_login"] is True


@pytest.mark.skipif(sys.platform == "win32", reason="bash installer")
def test_installer_no_pf_skips_the_anchor_and_daemon(tmp_path):
    _, out, _, _ = _render(tmp_path, "--no-pf")
    assert not (out / "ai.openadapt.flow.pf").exists()
    assert not (out / "ai.openadapt.flow-pf.plist").exists()
    assert (out / "ai.openadapt.flow-boot-check.plist").exists()


def test_health_reports_the_firewall_record(tmp_path):
    root = tmp_path / "root"
    p = svc.paths(root)
    p["heartbeat"].parent.mkdir(parents=True)
    beat = {
        "pid": os.getpid(),
        "poll_seconds": 10,
        "no_outbound": True,
        "state": "running",
        "last_beat": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    p["heartbeat"].write_text(json.dumps(beat))
    checks = {c["name"]: c for c in health(root)["checks"]}
    assert checks["firewall"]["status"] == "warn"
    p["firewall"].write_text(json.dumps({"pf": "installed"}))
    checks = {c["name"]: c for c in health(root)["checks"]}
    assert checks["firewall"]["status"] == "pass"


def test_launchd_templates_parse_as_plists_with_placeholders():
    for name in (
        "ai.openadapt.flow-service.plist",
        "ai.openadapt.flow-health.plist",
        "ai.openadapt.flow-boot-check.plist",
    ):
        data = plistlib.loads((DEPLOY / "launchd" / name).read_bytes())
        assert data["Label"] == name[: -len(".plist")]
        assert data["EnvironmentVariables"]["OPENADAPT_NO_OUTBOUND"] == "1"
    pf = plistlib.loads(
        (DEPLOY / "launchd" / "ai.openadapt.flow-pf.plist").read_bytes()
    )
    assert pf["Label"] == "ai.openadapt.flow-pf"


def test_service_loop_end_to_end_in_a_subprocess_with_the_real_hook(tmp_path):
    """The real CLI, the real audit hook, a queue script that tries to phone home."""
    root = tmp_path / "root"
    script = tmp_path / "run-queue.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            mkdir -p "$OPENADAPT_ONPREM_ROOT/runs/job-1"
            echo '{{"run_id":"job-1","execution_outcome":"VERIFIED"}}' > "$OPENADAPT_ONPREM_ROOT/runs/job-1/report.json"
            "{sys.executable}" -c 'import socket
            from openadapt_flow import no_outbound
            no_outbound.enforce_from_env()
            try:
                socket.create_connection(("192.0.2.10", 443), timeout=1)
            except no_outbound.OutboundBlocked:
                print("child blocked")'
            """
        )
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENADAPT_")}
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "service",
            "run",
            "--root",
            str(root),
            "--queue-script",
            str(script),
            "--poll-seconds",
            "0",
            "--max-cycles",
            "1",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "child blocked" in proc.stdout
    assert (root / "runs" / "job-1" / "seal.json").is_file()
    blocked = (root / "audit" / "outbound-blocked.jsonl").read_text().splitlines()
    assert json.loads(blocked[0])["host"] == "192.0.2.10"
    assert health(root)["checks"]  # readable by the health check
