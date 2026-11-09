"""Unit tests for the onboarding-effort benchmark harness.

These run without any live application: no container, no browser, no
network. ``subprocess.run`` and ``urlopen`` are replaced with functions that
fail the test, so a workflow that reaches for Trac or NetBox here is a bug.
They check that the live (protocol v2) wave still matches its manifest and
that the superseded v1 wave stays recorded, that the frozen
specs are what the code produces, that item generation is deterministic, the
oracles' verdicts on hand-built database snapshots, and the effort ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest

HERE = Path(__file__).resolve().parents[1] / "benchmark" / "onboarding_effort"
MANIFEST = json.loads((HERE / "pool" / "MANIFEST.json").read_text())
#: Waves not superseded by a later freeze; only these are pinned to the tree.
LIVE = [w for w in MANIFEST["waves"] if not w.get("superseded_by")]
FROZEN = [w for wave in LIVE for w in wave["workflows"]]
PROTOCOL_VERSION = "onboarding-effort/v2"


def _no_live_app(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError(f"a unit test reached for a live app: {args[:1]!r}")


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail on any subprocess or HTTP call; keep the environment untouched."""

    monkeypatch.setattr(subprocess, "run", _no_live_app)
    monkeypatch.setattr(subprocess, "Popen", _no_live_app)
    import urllib.request

    monkeypatch.setattr(urllib.request, "urlopen", _no_live_app)
    # pool/netbox.py stages a throwaway reader password with setdefault at
    # import; keep that out of the rest of the test session.
    if "NB_READER_PASSWORD" not in os.environ:
        monkeypatch.setenv("NB_READER_PASSWORD", "unit-test")
    from benchmark.onboarding_effort import ledger

    monkeypatch.setattr(ledger, "host_load", lambda: -1.0)


def _registry() -> Any:
    from benchmark.onboarding_effort import registry

    return registry


# -- the frozen wave ------------------------------------------------------------


def test_live_wave_files_match_the_manifest() -> None:
    assert LIVE, "no live wave in the manifest"
    for wave in LIVE:
        assert wave.get("protocol") == PROTOCOL_VERSION
        for rel, digest in wave["files"].items():
            path = HERE / rel
            assert path.is_file(), f"wave {wave['wave']}: {rel} is gone"
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            assert got == digest, (
                f"wave {wave['wave']}: {rel} changed after the freeze; a change "
                "needs a new protocol version (PROTOCOL.md)"
            )


def test_superseded_waves_stay_in_the_manifest() -> None:
    numbers = {w["wave"] for w in MANIFEST["waves"]}
    superseded = [w for w in MANIFEST["waves"] if w.get("superseded_by")]
    assert [w["wave"] for w in superseded] == [1]
    for wave in superseded:
        assert wave["superseded_by"] in numbers
        assert wave["superseded_by"] > wave["wave"]
        assert wave["protocol"] == "onboarding-effort/v1"
        assert wave["files"] and wave["workflows"]
    live = [w for wave in LIVE for w in wave["workflows"]]
    assert len(live) == len(set(live)), "a workflow is frozen in two live waves"


def test_wave_validation_is_ok_or_pending() -> None:
    for wave in MANIFEST["waves"]:
        rows = wave["validation"]
        if wave.get("validation_pending"):
            assert rows == []
            continue
        assert {(r["workflow"], r["installation"]) for r in rows} >= {
            (w, "first") for w in wave["workflows"]
        }
        for row in rows:
            assert row["ok"] is True
            assert set(row["idle"]) == {"MISSING"}
            assert set(row["performed"]) == {"VERIFIED"}


@pytest.mark.parametrize("wid", FROZEN)
def test_frozen_spec_is_what_the_code_produces(
    wid: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmark.onboarding_effort import freeze

    frozen = json.loads((HERE / "pool" / "specs" / f"{wid}.json").read_text())
    # A local override of the read path never reaches a frozen spec.
    monkeypatch.setenv("OE_TRAC_DB_DIR", "/somewhere/else")
    assert freeze.spec(wid) == frozen
    assert os.environ["OE_TRAC_DB_DIR"] == "/somewhere/else"


@pytest.mark.parametrize("wid", FROZEN)
def test_frozen_spec_carries_no_machine_path(wid: str) -> None:
    text = (HERE / "pool" / "specs" / f"{wid}.json").read_text()
    for path in re.findall(r"sqlite_database: (\S+)", text):
        assert not Path(path).is_absolute(), path
        assert not path.startswith("~"), path
    assert str(Path.home()) not in text
    assert "/Users/" not in text and "/home/" not in text


def test_trac_read_path_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmark.onboarding_effort.apps import trac_app
    from benchmark.onboarding_effort.pool import trac

    monkeypatch.delenv("OE_TRAC_DB_DIR", raising=False)
    assert trac.host_db() == "benchmark/onboarding_effort/.state/trac-db/trac.db"
    flow = Path(__file__).resolve().parents[1]
    assert Path(trac_app.host_db_dir_abs()) == flow / trac_app.DEFAULT_DB_DIR
    monkeypatch.setenv("OE_TRAC_DB_DIR", "/srv/trac-db")
    assert trac.host_db() == "/srv/trac-db/trac.db"
    assert "sqlite_database: /srv/trac-db/trac.db" in _registry().get(
        "tr-ticket-new"
    ).verify_yaml("first")


def test_registry_lists_the_frozen_pool() -> None:
    reg = _registry()
    assert sorted(reg.pool_ids()) == sorted(FROZEN)
    assert all(not wid.startswith("cal-") for wid in reg.pool_ids())


# -- items ----------------------------------------------------------------------


@pytest.mark.parametrize("wid", FROZEN)
def test_items_are_deterministic_and_held_out_items_are_new(wid: str) -> None:
    wf = _registry().get(wid)
    for inst in ("first", "second"):
        heldout = wf.items("heldout", 25, inst)
        assert heldout == wf.items("heldout", 25, inst)
        demo = wf.items("demo", 5, inst)
        assert not [i for i in demo if i in heldout]
    assert wf.items("heldout", 3, "first") != wf.items("heldout", 3, "second")


# -- Trac oracles ---------------------------------------------------------------


def _ticket(tid: int, **fields: str) -> dict[str, Any]:
    base = {
        "id": tid,
        "summary": f"t{tid}",
        "description": "",
        "type": "task",
        "priority": "major",
        "component": "intake",
        "status": "new",
        "resolution": "",
        "milestone": "",
    }
    return {**base, **fields}


def _trac(
    *tickets: dict[str, Any], changes: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {"tickets": {t["id"]: t for t in tickets}, "changes": list(changes or [])}


def test_trac_new_ticket_verdicts() -> None:
    wf = _registry().get("tr-ticket-new")
    item = wf.items("heldout", 1)[0]
    fields = {
        k: item[k] for k in ("summary", "description", "type", "priority", "component")
    }
    before = _trac(_ticket(1))
    good = _trac(_ticket(1), _ticket(2, **fields))
    assert wf.judge(item, before, before).verdict == "MISSING"
    assert wf.judge(item, before, good).verdict == "VERIFIED"
    dupe = _trac(_ticket(1), _ticket(2, **fields), _ticket(3, **fields))
    assert wf.judge(item, before, dupe).verdict == "WRONG"
    wrong_field = _trac(_ticket(1), _ticket(2, **{**fields, "priority": "bogus"}))
    assert wf.judge(item, before, wrong_field).verdict == "WRONG"
    collateral = _trac(_ticket(1, status="closed"), _ticket(2, **fields))
    assert wf.judge(item, before, collateral).verdict == "WRONG"


def test_trac_resolve_verdicts() -> None:
    wf = _registry().get("tr-ticket-resolve")
    fixed = {"ticket": "#2", "resolution": "fixed", "duplicate_of": ""}
    before = _trac(_ticket(1), _ticket(2))
    assert wf.judge(fixed, before, before).verdict == "MISSING"
    closed = _trac(_ticket(1), _ticket(2, status="closed", resolution="fixed"))
    assert wf.judge(fixed, before, closed).verdict == "VERIFIED"
    assert (
        wf.judge({**fixed, "resolution": "wontfix"}, before, closed).verdict == "WRONG"
    )
    other_too = _trac(
        _ticket(1, status="closed"), _ticket(2, status="closed", resolution="fixed")
    )
    assert wf.judge(fixed, before, other_too).verdict == "WRONG"
    assert wf.judge(fixed, before, _trac(_ticket(1))).verdict == "WRONG"

    dup = {"ticket": "#2", "resolution": "duplicate", "duplicate_of": "#1"}
    comment = {
        "ticket": 2,
        "time": 1,
        "author": "a",
        "field": "comment",
        "oldvalue": "",
        "newvalue": "Duplicate of #1",
    }
    closed_dup = _trac(
        _ticket(1),
        _ticket(2, status="closed", resolution="duplicate"),
        changes=[comment],
    )
    assert wf.judge(dup, before, closed_dup).verdict == "VERIFIED"
    no_comment = _trac(_ticket(1), _ticket(2, status="closed", resolution="duplicate"))
    assert wf.judge(dup, before, no_comment).verdict == "WRONG"


def test_trac_triage_loop_verdicts() -> None:
    wf = _registry().get("tr-triage-loop")
    item = {"ticket": "#2", "milestone": "2027-Q1"}
    before = _trac(_ticket(1), _ticket(2))
    assert wf.judge(item, before, before).verdict == "MISSING"

    def note(text: str, t: int = 1) -> dict[str, Any]:
        return {
            "ticket": 2,
            "time": t,
            "author": "a",
            "field": "comment",
            "oldvalue": "",
            "newvalue": text,
        }

    done = _trac(
        _ticket(1), _ticket(2, milestone="2027-Q1"), changes=[note("Triaged.")]
    )
    assert wf.judge(item, before, done).verdict == "VERIFIED"
    twice = _trac(
        _ticket(1),
        _ticket(2, milestone="2027-Q1"),
        changes=[note("Triaged."), note("Triaged.", 2)],
    )
    assert wf.judge(item, before, twice).verdict == "WRONG"
    wrong_ms = _trac(
        _ticket(1), _ticket(2, milestone="2026-Q4"), changes=[note("Triaged.")]
    )
    assert wf.judge(item, before, wrong_ms).verdict == "WRONG"
    decoy_touched = _trac(
        _ticket(1, milestone="2027-Q1"), _ticket(2, milestone="2027-Q1")
    )
    assert wf.judge_batch_collateral(before, decoy_touched, {2}) == [1]


# -- NetBox oracles -------------------------------------------------------------


def test_netbox_device_add_verdicts() -> None:
    wf = _registry().get("nb-device-add")
    item = wf.items("heldout", 1)[0]
    row = {
        "id": 2,
        **{k: item[k] for k in ("name", "site", "role", "device_type", "serial")},
    }
    existing = {
        "id": 1,
        "name": "core-1",
        "site": "Oslo Edge",
        "role": "Core Switch",
        "device_type": "SX-24",
        "serial": "SYN1",
    }
    before = {1: existing}
    assert wf.judge(item, before, before).verdict == "MISSING"
    assert wf.judge(item, before, {1: existing, 2: row}).verdict == "VERIFIED"
    assert (
        wf.judge(item, before, {1: existing, 2: {**row, "serial": "x"}}).verdict
        == "WRONG"
    )
    moved = {**existing, "site": "Quito Branch"}
    assert wf.judge(item, before, {1: moved, 2: row}).verdict == "WRONG"


def test_netbox_ip_paginated_verdicts() -> None:
    wf = _registry().get("nb-ip-paginated")
    item = wf.items("heldout", 1)[0]
    addr = item["address"]
    decoy = "203.0.113.9/24"
    before = {
        addr: {"id": 1, "address": addr, "description": ""},
        decoy: {"id": 2, "address": decoy, "description": "decoy, untagged"},
    }
    assert wf.judge(item, before, before).verdict == "MISSING"
    done = {**before, addr: {**before[addr], "description": item["description"]}}
    assert wf.judge(item, before, done).verdict == "VERIFIED"
    wrong = {**before, addr: {**before[addr], "description": "something else"}}
    assert wf.judge(item, before, wrong).verdict == "WRONG"
    assert wf.judge(item, before, {decoy: before[decoy]}).verdict == "WRONG"
    decoy_touched = {
        **done,
        decoy: {**before[decoy], "description": item["description"]},
    }
    assert wf.judge_batch_collateral(before, decoy_touched, set()) == [decoy]


def test_netbox_vlan_conditional_verdicts() -> None:
    wf = _registry().get("nb-vlan-conditional")
    item = {"vid": "301", "name": "onb-heldout-first-01", "group": "Campus"}
    before: dict[int, Any] = {}
    assert wf.judge(item, before, before).verdict == "MISSING"
    row = {
        "id": 1,
        "vid": "301",
        "name": item["name"],
        "grp": "Campus",
        "cost_center": "",
    }
    assert wf.judge(item, before, {1: row}).verdict == "VERIFIED"
    assert wf.judge(item, before, {1: {**row, "grp": ""}}).verdict == "WRONG"
    second = {**item, "cost_center": "CC-123"}
    assert wf.judge(second, before, {1: row}).verdict == "WRONG"
    assert (
        wf.judge(second, before, {1: {**row, "cost_center": "CC-123"}}).verdict
        == "VERIFIED"
    )


def test_netbox_vlan_conditional_collateral_is_wrong() -> None:
    wf = _registry().get("nb-vlan-conditional")
    item = {"vid": "301", "name": "onb-heldout-first-01", "group": "Campus"}
    other = {"id": 7, "vid": "20", "name": "voice", "grp": "", "cost_center": ""}
    row = {
        "id": 8,
        "vid": "301",
        "name": item["name"],
        "grp": "Campus",
        "cost_center": "",
    }
    before = {7: other}
    assert wf.judge(item, before, {7: other, 8: row}).verdict == "VERIFIED"
    renamed = {**other, "name": "voice-2"}
    verdict = wf.judge(item, before, {7: renamed, 8: row})
    assert verdict.verdict == "WRONG"
    assert verdict.detail["changed"] == [7]
    # A deleted VLAN is collateral too, and so is damage with nothing created.
    assert wf.judge(item, before, {8: row}).verdict == "WRONG"
    assert wf.judge(item, before, {7: renamed}).verdict == "WRONG"


# -- ledger -----------------------------------------------------------------------


def test_ledger_rate_card_and_append(tmp_path: Path) -> None:
    from benchmark.onboarding_effort.ledger import Ledger, Row, engineer_minutes

    row = Row(
        workflow="w",
        installation="first",
        phase="demonstrate",
        action="a",
        demos_scripted=1,
        questions=2,
        commands=1,
        failed_commands=1,
        detail={"recorded_actions": 10},
    )
    # 2 + 0.1 * 10 + 2 * 1 + 0.5 + 5
    assert engineer_minutes(row) == 10.5
    led = Ledger(tmp_path / "ledger.jsonl", manifest_sha256="m", engine_commit="c")
    led.add(row)
    led.add(
        Row(
            workflow="w",
            installation="first",
            phase="config",
            action="b",
            config_lines=3,
        )
    )
    rows = led.rows()
    assert [r["engineer_min"] for r in rows] == [10.5, 3.0]
    assert {r["manifest_sha256"] for r in rows} == {"m"}
    assert {r["engine_commit"] for r in rows} == {"c"}


def test_items_use_their_own_rng() -> None:
    wf = _registry().get("tr-ticket-new")
    random.seed(1)
    first = wf.items("heldout", 3)
    random.seed(2)
    assert wf.items("heldout", 3) == first
