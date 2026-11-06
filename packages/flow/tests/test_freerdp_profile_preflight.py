"""Software controls for the real-parser campaign boundary; no display/input."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e import test_linux_rdp_campaign as campaign
from tests.fixtures import freerdp_profile_parser as parser


def proof():
    payload = parser._profile_json(campaign.PROFILE.client_args())
    return {
        "schema": parser.SCHEMA,
        "profile_sha256": hashlib.sha256(payload.encode()).hexdigest(),
        "version": "3.32.1",
        "parse_status": 0,
        "codecs": dict.fromkeys(parser.CODECS, False),
    }


def response(record=None, *, output=None, code=0):
    return SimpleNamespace(
        returncode=code,
        stdout=json.dumps(proof() if record is None else record).encode()
        if output is None
        else output,
    )


def failure_response(case):
    record = proof()
    if case in {"exit139", "signal", "missing_api", "nonzero"}:
        return response(
            code={"exit139": 139, "signal": -11, "missing_api": 3, "nonzero": 1}[case]
        )
    if case == "bad_json":
        return response(output=b"not json")
    if case == "oversized":
        return response(output=b" " * 4097)
    if case == "empty":
        return response(output=b"")
    if case == "not_object":
        return response(output=b"[]")
    if case == "wrong_profile":
        record["profile_sha256"] = "0" * 64
    elif case == "missing_flags":
        del record["codecs"]
    elif case == "missing_flag":
        del record["codecs"][parser.CODECS[0]]
    elif case == "extra_flag":
        record["codecs"]["unknown"] = False
    elif case == "type_flag":
        record["codecs"][parser.CODECS[0]] = 0
    elif case == "string_flag":
        record["codecs"][parser.CODECS[0]] = "false"
    elif case == "type_status":
        record["parse_status"] = False
    elif case == "parser_nonzero":
        record["parse_status"] = -1000
    elif case == "wrong_version":
        record["version"] = "unknown"
    elif case == "extra_field":
        record["unregistered"] = True
    elif case == "all_enabled":
        record["codecs"] = dict.fromkeys(parser.CODECS, True)
    elif case in parser.CODECS:
        record["codecs"][case] = True
    else:
        raise AssertionError("unknown controlled failure")
    return response(record)


FAILURES = [
    "exit139",
    "signal",
    "missing_api",
    "nonzero",
    "timeout",
    "unavailable",
    "bad_json",
    "oversized",
    "empty",
    "not_object",
    "wrong_profile",
    "missing_flags",
    "missing_flag",
    "extra_flag",
    "type_flag",
    "string_flag",
    "type_status",
    "parser_nonzero",
    "wrong_version",
    "extra_field",
    "all_enabled",
    *parser.CODECS,
]


@pytest.mark.parametrize("case", FAILURES)
def test_every_parser_refusal_precedes_real_campaign_open_and_keeps_failure_evidence(
    tmp_path, monkeypatch, case
):
    calls = []
    opened = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if case == "timeout":
            raise subprocess.TimeoutExpired(argv, 5)
        if case == "unavailable":
            raise FileNotFoundError("not exported")
        return failure_response(case)

    def forbidden_open(_owner):
        opened.append(True)
        raise AssertionError("native session must not open after failed parser proof")

    monkeypatch.setattr(parser.subprocess, "run", run)
    monkeypatch.setattr(campaign.CampaignOwner, "open", forbidden_open)
    with pytest.raises(parser.ProfilePreflightError) as caught:
        campaign.test_loopback_six_primary_pixel_cases(tmp_path)
    assert len(calls) == 1 and opened == []
    assert str(caught.value).startswith("freerdp_codec_preflight_")
    evidence = json.loads(
        (tmp_path / "rdp-campaign/campaign-evidence.json").read_text()
    )
    assert evidence["status"] == "FAILED"
    assert evidence["exception_type"] == "ProfilePreflightError"
    assert evidence["codec_preflight"] == {
        "status": "refused",
        "reason": caught.value.reason,
        "child_returncode": caught.value.returncode,
    }
    assert evidence["primary_planned"] == 6 and evidence["primary_completed"] == 0
    assert evidence["informational_planned"] == evidence["informational_completed"] == 0
    assert evidence["sessions"] == evidence["oracles"] == []


def test_successful_proof_is_bound_to_one_isolated_child_and_current_profile(
    tmp_path, monkeypatch
):
    calls = []
    expected = proof()

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return response(deepcopy(expected))

    monkeypatch.setattr(parser.subprocess, "run", run)
    assert (
        parser.assert_lossless_profile(campaign.PROFILE.client_args(), cwd=tmp_path)
        == expected
    )
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert argv[:3] == [sys.executable, "-I", "-S"]
    assert Path(argv[3]) == Path(parser.__file__).resolve()
    assert json.loads(argv[4]) == campaign.PROFILE.client_args()
    assert kwargs == {
        "cwd": tmp_path,
        "env": {"PATH": os.defpath, "HOME": str(tmp_path), "LC_ALL": "C"},
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.DEVNULL,
        "timeout": 5,
        "check": False,
    }


def test_all_off_proof_reaches_first_open_handoff_with_original_exception_preserved(
    tmp_path, monkeypatch
):
    sentinel = RuntimeError("software handoff stop")
    opened = []
    expected = proof()
    monkeypatch.setattr(
        parser.subprocess, "run", lambda *args, **kwargs: response(expected)
    )

    def open_handoff(_owner):
        opened.append(True)
        raise sentinel

    monkeypatch.setattr(campaign.CampaignOwner, "open", open_handoff)
    with pytest.raises(RuntimeError) as caught:
        campaign.test_loopback_six_primary_pixel_cases(tmp_path)
    assert caught.value is sentinel and opened == [True]
    evidence = json.loads(
        (tmp_path / "rdp-campaign/campaign-evidence.json").read_text()
    )
    assert evidence["codec_preflight"] == expected
    assert evidence["status"] == "FAILED" and evidence["primary_completed"] == 0
    assert evidence["sessions"] == []


@pytest.mark.parametrize(
    "args", [["/v:foreign"], ["/from-stdin"], ["/bpp:24", "/p:private"], []]
)
def test_profile_probe_does_not_accept_connection_or_unregistered_arguments(
    tmp_path, monkeypatch, args
):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("invalid profile must not spawn a child")

    monkeypatch.setattr(parser.subprocess, "run", forbidden)
    with pytest.raises(ValueError, match="invalid fixed profile"):
        parser.assert_lossless_profile(args, cwd=tmp_path)
