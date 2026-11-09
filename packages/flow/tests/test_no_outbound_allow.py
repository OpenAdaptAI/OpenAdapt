"""The strict-mode allow list holds only the session host(s), and shows why."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openadapt_flow import no_outbound
from openadapt_flow.__main__ import main
from openadapt_flow.no_outbound_allow import (
    ALLOW_SCHEMA,
    AllowList,
    AllowListError,
    derive,
    entries_for_targets,
    manual_entry,
    targets_from_file,
    targets_from_rdp_text,
)

ROOT = Path(__file__).resolve().parents[1]

RDP = "\n".join(
    [
        "screen mode id:i:2",
        "full address:s:10.20.0.15:3390",
        "alternate full address:s:10.20.0.15:3390",
        "username:s:svc-records",
        "gatewayusagemethod:i:0",
        "gatewayhostname:s:rdgw.example.internal",
    ]
)


def _session_json(tmp_path: Path, host: str = "10.20.0.15", **extra) -> Path:
    path = tmp_path / "session.json"
    body = {"target": {"host": host, "port": 3389}, "profile": {"width": 1280}}
    body.update(extra)
    path.write_text(json.dumps(body))
    return path


def test_rdp_file_names_the_session_host_and_skips_an_unused_gateway():
    targets = targets_from_rdp_text(RDP, "site.rdp")
    assert [(t.host, t.port, t.role) for t in targets] == [
        ("10.20.0.15", 3390, "session"),
        ("10.20.0.15", 3390, "alternate"),
    ]


def test_rdp_file_includes_a_gateway_in_use():
    text = RDP.replace("gatewayusagemethod:i:0", "gatewayusagemethod:i:1")
    roles = {t.role: (t.host, t.port) for t in targets_from_rdp_text(text)}
    assert roles["gateway"] == ("rdgw.example.internal", 443)


def test_rdp_file_saved_as_utf16_by_windows(tmp_path):
    path = tmp_path / "site.rdp"
    path.write_bytes(("\ufeff" + RDP).encode("utf-16"))
    assert targets_from_file(path)[0].host == "10.20.0.15"


def test_rdp_file_without_full_address_is_refused():
    with pytest.raises(AllowListError, match="full address"):
        targets_from_rdp_text("username:s:x\n")


def test_freerdp_session_file(tmp_path):
    path = _session_json(tmp_path, gateway={"host": "10.20.0.2", "port": 443})
    targets = targets_from_file(path)
    assert [(t.host, t.port, t.role) for t in targets] == [
        ("10.20.0.15", 3389, "session"),
        ("10.20.0.2", 443, "gateway"),
    ]


def test_host_name_resolves_once_and_public_answers_are_refused():
    targets = targets_from_rdp_text("full address:s:records-host.lan\n")
    entries = entries_for_targets(targets, resolver=lambda h: ["192.168.40.7"])
    assert [(e.network, e.host, e.port) for e in entries] == [
        ("192.168.40.7/32", "records-host.lan", 3389)
    ]
    with pytest.raises(AllowListError, match="public address"):
        entries_for_targets(targets, resolver=lambda h: ["8.8.8.8"])


def test_derive_adds_only_the_session_hosts_and_reports_repeats(tmp_path):
    allow_file = tmp_path / "allow.json"
    added, present = derive(_session_json(tmp_path), allow_file)
    assert [e.network for e in added] == ["10.20.0.15/32"]
    assert present == []
    data = json.loads(allow_file.read_text())
    assert data["schema"] == ALLOW_SCHEMA
    assert [e["network"] for e in data["entries"]] == ["10.20.0.15/32"]
    added, present = derive(_session_json(tmp_path), allow_file)
    assert added == [] and len(present) == 1


def test_manual_add_and_remove(tmp_path):
    allow_file = tmp_path / "allow.json"
    allow = AllowList.load_or_empty(allow_file)
    allow.add([manual_entry("10.30.0.0/24")])
    allow.save(allow_file)
    assert AllowList.load(allow_file).entries[0].origin == "manual"
    with pytest.raises(AllowListError, match="public"):
        manual_entry("1.1.1.1")
    with pytest.raises(AllowListError, match="IP addresses"):
        manual_entry("records-host.lan")
    allow = AllowList.load(allow_file)
    assert [e.network for e in allow.remove("10.30.0.0/24")] == ["10.30.0.0/24"]
    assert allow.entries == []


def test_tampered_file_with_public_range_is_refused(tmp_path):
    allow_file = tmp_path / "allow.json"
    allow_file.write_text(
        json.dumps(
            {
                "schema": ALLOW_SCHEMA,
                "entries": [{"network": "0.0.0.0/0", "origin": "manual"}],
            }
        )
    )
    with pytest.raises(AllowListError, match="public range"):
        AllowList.load(allow_file)


def test_allow_from_env_merges_the_file_and_fails_closed_when_missing(tmp_path):
    allow_file = tmp_path / "allow.json"
    derive(_session_json(tmp_path), allow_file)
    nets = no_outbound.allow_from_env(
        {
            no_outbound.ENV_ALLOW: "10.40.0.0/24",
            no_outbound.ENV_ALLOW_FILE: str(allow_file),
        }
    )
    assert [str(n) for n in nets] == ["10.40.0.0/24", "10.20.0.15/32"]
    with pytest.raises(ValueError, match="not found"):
        no_outbound.allow_from_env(
            {no_outbound.ENV_ALLOW_FILE: str(tmp_path / "missing.json")}
        )


def test_cli_derive_shows_the_operator_what_was_added(tmp_path, capsys):
    allow_file = tmp_path / "allow.json"
    rdp = tmp_path / "site.rdp"
    rdp.write_text(RDP)
    assert (
        main(["no-outbound", "allow", "--file", str(allow_file), "derive", str(rdp)])
        == 0
    )
    out = capsys.readouterr().out
    assert "added: 10.20.0.15/32 port 3390" in out
    assert "session host, from" in out
    assert (
        main(["no-outbound", "allow", "--file", str(allow_file), "add", "10.30.0.9"])
        == 0
    )
    assert main(["no-outbound", "allow", "--file", str(allow_file), "list"]) == 0
    out = capsys.readouterr().out
    assert "10.30.0.9/32  (added by hand)" in out
    assert (
        main(["no-outbound", "allow", "--file", str(allow_file), "remove", "10.30.0.9"])
        == 0
    )
    assert (
        main(["no-outbound", "allow", "--file", str(allow_file), "remove", "10.30.0.9"])
        == 1
    )


def test_hook_allows_the_session_host_and_refuses_the_rest_of_the_lan(tmp_path):
    allow_file = tmp_path / "allow.json"
    derive(_session_json(tmp_path, host="10.255.255.1"), allow_file)
    env = {
        k: v for k, v in os.environ.items() if not k.startswith("OPENADAPT_NO_OUTBOUND")
    }
    env.update(
        OPENADAPT_NO_OUTBOUND="1",
        OPENADAPT_NO_OUTBOUND_ALLOW_FILE=str(allow_file),
        PYTHONPATH=str(ROOT),
    )
    code = textwrap.dedent(
        """
        import socket
        from openadapt_flow import no_outbound
        no_outbound.enforce_from_env()
        for host in ("10.255.255.1", "10.255.255.2", "192.168.1.1"):
            s = socket.socket(); s.settimeout(0.2)
            try:
                s.connect((host, 9))
            except no_outbound.OutboundBlocked:
                print(host, "BLOCKED")
            except OSError:
                print(host, "ALLOWED")
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "10.255.255.1 ALLOWED" in proc.stdout, proc.stderr
    assert "10.255.255.2 BLOCKED" in proc.stdout
    assert "192.168.1.1 BLOCKED" in proc.stdout
