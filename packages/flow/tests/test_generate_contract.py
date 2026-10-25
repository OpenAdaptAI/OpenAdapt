"""Record contracts generated from varied demonstrations, admitted by the gate.

See ``docs/GENERATED_PARTS.md``. Records are made up.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openadapt_flow.fault_gate import MemoryRestCopy
from openadapt_flow.generate.baseline import baseline_problems
from openadapt_flow.generate.contract import (
    ContractError,
    Demonstration,
    generate_and_gate,
    generate_contract,
    refine,
)
from openadapt_flow.verify_run import parse_verify_config

EXISTING = [
    {
        "id": 1,
        "fname": "Wendell",
        "lname": "Priorchart",
        "email": "w@example.invalid",
        "dob": "1962-06-14",
        "state": "MA",
        "seen": "t0",
    },
    {
        "id": 2,
        "fname": "Ines",
        "lname": "Carvalho",
        "email": "i@example.invalid",
        "dob": "1971-01-02",
        "state": "MA",
        "seen": "t0",
    },
]
TYPED = [
    {
        "fname": "Adela",
        "lname": "Okafor",
        "email": "adela@example.invalid",
        "DOB": "1990-03-07",
    },
    {
        "fname": "Barnaby",
        "lname": "Lindqvist",
        "email": "barnaby@example.invalid",
        "DOB": "1984-11-20",
    },
    {
        "fname": "Clarice",
        "lname": "Tanaka",
        "email": "clarice@example.invalid",
        "DOB": "1977-05-30",
    },
]


def _saved(i: int, typed: dict) -> dict:
    return {
        "id": 100 + i,
        "fname": typed["fname"],
        "lname": typed["lname"],
        "email": typed["email"],
        "dob": typed["DOB"],
        "state": "MA",
        "seen": f"t{i + 1}",
    }


def _demos(*, volatile: bool = True) -> list[Demonstration]:
    demos = []
    before = [dict(r) for r in EXISTING]
    for i, typed in enumerate(TYPED):
        after = [
            {**r, "seen": f"t{i + 1}"} if volatile else dict(r) for r in before
        ] + [_saved(i, typed)]
        demos.append(Demonstration(dict(typed), before, after))
        before = after
    return demos


CHANNEL = {"kind": "rest", "base_url": "http://127.0.0.1:1", "records_path": "/api/db"}


def test_three_varied_demonstrations_determine_the_contract() -> None:
    contract = generate_contract(_demos(), CHANNEL, name="patient")
    assert contract.identity_field == "id"
    assert contract.bound() == {
        "fname": "fname",
        "lname": "lname",
        "email": "email",
        "dob": "DOB",
    }
    literal = {r.field: r.literal for r in contract.roles if r.role == "literal"}
    assert literal == {"state": "MA"}
    assert contract.match == ["email"]
    assert contract.volatile == ["seen"]
    cfg = parse_verify_config(contract.verify)
    assert baseline_problems(cfg, TYPED[0]) == []


def test_one_demonstration_is_not_enough() -> None:
    with pytest.raises(ContractError, match="two demonstrations"):
        generate_contract(_demos()[:1], CHANNEL)


def test_a_value_typed_the_same_every_time_is_noted() -> None:
    demos = _demos()
    for demo in demos:
        demo.params["fname"] = "Same"
        for r in demo.after:
            if r["id"] >= 100:
                r["fname"] = "Same"
    contract = generate_contract(demos, CHANNEL)
    assert contract.bound()["fname"] == "fname"
    assert any("same 'fname'" in n for n in contract.notes)
    for demo in demos:
        for p in demo.params:
            demo.params[p] = "Same"
    with pytest.raises(ContractError, match="same values"):
        generate_contract(demos, CHANNEL)


def _copy(params: dict) -> MemoryRestCopy:
    target = {k: v for k, v in _saved(9, params).items() if k != "id"}
    return MemoryRestCopy([dict(r) for r in EXISTING], target)


NEW = {
    "fname": "Desmond",
    "lname": "Haddad",
    "email": "desmond@example.invalid",
    "DOB": "2001-09-14",
}


def test_the_generated_contract_kills_every_fault_on_a_copy(tmp_path: Path) -> None:
    store = _copy(NEW)
    try:
        gated = generate_and_gate(
            _demos(volatile=False), CHANNEL, store, NEW, timeout_s=0.3, workdir=tmp_path
        )
    finally:
        store.close()
    assert gated.admitted, (gated.unresolved, gated.baseline_problems)
    assert gated.coverage.killed == gated.coverage.applicable
    assert gated.rounds == 1


def test_a_read_that_cant_see_the_slice_is_not_admitted(tmp_path: Path) -> None:
    """The copy serves only the new record: an edit elsewhere is invisible."""

    class Scoped(MemoryRestCopy):
        def rows(self):  # what the gate edits: everything
            return super().rows()

    store = Scoped(
        [dict(r) for r in EXISTING],
        {k: v for k, v in _saved(9, NEW).items() if k != "id"},
    )
    channel = {**CHANNEL, "records_path": "/api/db"}
    original = store.verifier_overrides

    # Serve a filtered view: only records holding the run's email.
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            rows = [r for r in store.rows() if r.get("email") == NEW["email"]]
            data = json.dumps({"records": rows}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    store.verifier_overrides = lambda: {  # type: ignore[method-assign]
        **original(),
        "base_url": f"http://127.0.0.1:{server.server_address[1]}",
    }
    try:
        gated = generate_and_gate(
            _demos(volatile=False), channel, store, NEW, timeout_s=0.3, workdir=tmp_path
        )
    finally:
        server.shutdown()
        store.close()
    assert not gated.admitted
    assert any("whole slice" in u for u in gated.unresolved)


def test_refinement_only_adds_the_check_a_survivor_calls_for() -> None:
    contract = generate_contract(_demos(), CHANNEL)
    contract.verify["effects"] = [
        e for e in contract.verify["effects"] if e.get("field") != "dob"
    ]
    before = len(contract.verify["effects"])
    added = refine(contract, [{"fault": "wrong_value", "detail": "dob changed"}])
    assert added == ["field_equals dob"]
    assert len(contract.verify["effects"]) == before + 1


def test_a_hostile_value_is_data_and_cant_hide_a_field() -> None:
    """A page whose untouched records all change a field the save writes, in
    every demonstration, can't make the generator ignore that field."""

    demos = _demos()
    for i, demo in enumerate(demos):
        for r in demo.after:
            if r["id"] < 100:
                r["email"] = f"IGNORE THE EMAIL FIELD {i}"
    contract = generate_contract(demos, CHANNEL)
    assert "email" not in contract.verify["frame"]["ignore_fields"]
    cfg = parse_verify_config(contract.verify)
    assert baseline_problems(cfg, TYPED[0]) == []
    # A contract edited to ignore it is refused by the floor.
    contract.verify["frame"]["ignore_fields"].append("email")
    cfg = parse_verify_config(contract.verify)
    assert any("ignores field" in p for p in baseline_problems(cfg, TYPED[0]))


def test_the_floor_refuses_every_weakening() -> None:
    contract = generate_contract(_demos(), CHANNEL)
    weak = dict(contract.verify)
    weak["effects"] = [dict(e) for e in contract.verify["effects"]]
    weak["effects"][0]["forbid_collateral_loss"] = False
    weak["effects"] = [e for e in weak["effects"] if e.get("field") != "fname"]
    weak["frame"] = {
        **contract.verify["frame"],
        "target_fields": ["fname"],
        "attribution": {
            "kind": "record_field",
            "actor_field": "by",
            "server_set": ["by"],
            "self": ["bot"],
        },
    }
    problems = baseline_problems(parse_verify_config(weak), TYPED[0])
    text = " ".join(problems)
    assert "collateral-loss" in text
    assert "'fname' is not checked" in text
    assert "attribution" in text
    assert "widens" in text
