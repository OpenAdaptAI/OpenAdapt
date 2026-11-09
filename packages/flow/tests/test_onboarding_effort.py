"""Unit tests for the onboarding-effort benchmark harness.

These run without any live application: no container, no browser, no
network. ``subprocess.run`` and ``urlopen`` are replaced with functions that
fail the test, so a workflow that reaches for Trac or NetBox here is a bug.
They check that the frozen wave still matches its manifest, that the frozen
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
FROZEN = [w for wave in MANIFEST["waves"] for w in wave["workflows"]]


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


#: Harness files changed after the wave-1 freeze, before any counted run:
#: implementer v2 (``ledger.IMPLEMENTER_VERSION``) and a paced demonstration
#: recorder. The task, item and oracle files are untouched. PROTOCOL.md asks
#: for a new protocol version before a measured run uses them; until then any
#: further change to these files fails here too.
POST_FREEZE_HARNESS = {
    "implementer.py": "85aa1a8baf1bb4616d37bb6669a43027cf2b9f3c0af0aa4f2706fd65c2b6141c",
    "ledger.py": "661bb1423a7922d8d66ec8235d691572ece7bb4393cec649d239548246a0ca20",
    "pool/base.py": "01afdc92122008695a1fb152c98f2710495c1cb6a384dd21db624ac99cf3f7d5",
}


def test_every_frozen_file_still_matches_the_manifest() -> None:
    for wave in MANIFEST["waves"]:
        for rel, digest in wave["files"].items():
            path = HERE / rel
            assert path.is_file(), f"wave {wave['wave']}: {rel} is gone"
            got = hashlib.sha256(path.read_bytes()).hexdigest()
            digest = POST_FREEZE_HARNESS.get(rel, digest)
            assert got == digest, (
                f"wave {wave['wave']}: {rel} changed after the freeze; a change "
                "needs a new protocol version (PROTOCOL.md)"
            )


def test_wave_validation_is_all_ok() -> None:
    for wave in MANIFEST["waves"]:
        rows = wave["validation"]
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
    # The engineer's SQLite read path is the only machine-specific value; take
    # its directory from the frozen spec rather than from this machine.
    match = re.search(
        r"sqlite_database: (\S+)/trac-db/trac\.db", frozen["verify_yaml"]["first"]
    )
    if match:
        monkeypatch.setenv("OE_SCRATCH", match.group(1))
    assert freeze.spec(wid) == frozen


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
