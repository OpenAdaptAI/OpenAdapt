"""``openadapt verify draft``: drafting is a model's job, admission is ours.

The model is replaced by a fixed drafter, so nothing here calls a model.
The sandbox is MockLoan's fault server (SQLite, in-process).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest
import requests
import yaml

from openadapt_flow.verify_draft import admit as A
from openadapt_flow.verify_draft import draft as D
from openadapt_flow.verify_draft import inputs as I
from openadapt_flow.verify_draft import model as M
from openadapt_flow.verify_draft.sandbox import load_sandbox

GOOD = textwrap.dedent(
    """\
    schema_version: openadapt.verify/v1
    verifier:
      kind: surfaces
      surfaces:
        record:
          kind: sql
          sql_query: SELECT CAST(id AS TEXT) AS id, loan_id, product, amount, memo, source, record_key, surface FROM records
    effects:
    - kind: record_written
      probe: surface=record|one new disbursement with the task's values
      match:
        loan_id: {param: loan_id}
        product: {param: product}
        amount: {param: amount}
        memo: {param: memo}
        surface: {literal: disbursements}
      expected_count: 1
      count_new_only: true
    frame:
      identity_field: id
      database:
        dialect: sqlite
        views: {records: record}
        new_row_fields: {records: [id, source, record_key, surface]}
        settle_s: 0.2
        recheck_after_s: 300
    """
)
#: Matches the loan only (the other columns are waved through as fields a
#: new row may set), and schedules no recheck.
WEAK = (
    GOOD.replace("    product: {param: product}\n    amount: {param: amount}\n    memo: {param: memo}\n", "")
    .replace("    recheck_after_s: 300\n", "")
    .replace("[id, source, record_key, surface]", "[id, source, record_key, surface, product, amount, memo]")
)
PAYLOADS = [
    {"amount": f"{1000 + i * 111}.00", "memo": f"Probe memo {i}", "product": ["Auto", "Home", "Personal"][i % 3]}
    for i in range(8)
]


class Fixed:
    """A drafter that answers with given contracts, in order."""

    def __init__(self, *contracts: str) -> None:
        self.contracts = list(contracts)
        self.seen: list[list[dict[str, Any]]] = []

    def draft(self, messages: list[dict[str, Any]]) -> M.Draft:
        self.seen.append(list(messages))
        text = self.contracts[min(len(self.seen) - 1, len(self.contracts) - 1)]
        return M.Draft(text, PAYLOADS, "fixed", None)


@pytest.fixture()
def mockloan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from openadapt_flow.mockloan import fault_server

    monkeypatch.setenv("OPENADAPT_LOCAL_SEAL", "off")
    url, db, stop = fault_server.serve(0)
    base = url.rstrip("/")
    for loan, prod, amt in [("LN-1", "Auto", "10.00"), ("LN-2", "Home", "20.00")]:
        requests.post(base + "/api/disbursement", json={"loan_id": loan, "product": prod, "amount": amt, "memo": "seed"}, timeout=10)
    seeded = tmp_path / "seeded.sqlite3"
    shutil.copy(db.database_path, seeded)
    params = {"loan_id": "LN-9", "product": "Personal", "amount": "31.50", "memo": "Kitchen"}
    trace = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": base + "/api/disbursement",
                        "headers": [{"name": "Content-Type", "value": "application/json"},
                                    {"name": "Authorization", "value": "Bearer secret-demo-token"}],
                        "postData": {"mimeType": "application/json", "text": json.dumps({**params, "password": "hunter2"})},
                    },
                    "response": {"status": 200, "content": {"text": "{}"}},
                }
            ]
        }
    }
    (tmp_path / "trace.har").write_text(json.dumps(trace))
    (tmp_path / "task.yaml").write_text(yaml.safe_dump({"instruction": "Book it.", "params": params}))
    (tmp_path / "sandbox.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.verify-sandbox/v1",
                "app": {"base_url": base},
                "actors": {"agent": {"id": "a1"}, "other": {"id": "b2"}},
                "store": {"dialect": "sqlite", "read": {"sqlite_database": str(db.database_path)},
                          "admin": {"sqlite_database": str(db.database_path)}},
            }
        )
    )
    yield tmp_path
    stop()


def _run(root: Path, drafter: Any, **kw: Any) -> D.Outcome:
    return D.run(
        describe="db",
        trace_path=str(root / "trace.har"),
        task_path=str(root / "task.yaml"),
        sandbox_path=str(root / "sandbox.yaml"),
        out=root / "out",
        drafter=drafter,
        **kw,
    )


@pytest.mark.timeout(300)
def test_good_draft_is_admitted_and_bound(mockloan: Path) -> None:
    drafter = Fixed(GOOD)
    outcome = _run(mockloan, drafter, rounds=1)
    assert outcome.admitted, outcome.reason
    assert outcome.decisions == 0
    adm = outcome.admissions[-1]
    names = {c.name for c in adm.cases if c.group == "fault" and c.applicable}
    assert {"wrong_row", "duplicate", "sibling_edit", "late_revert"} <= names
    assert any(n.startswith("wrong_value:") for n in names)
    assert sum(c.passed for c in adm.cases if c.group == "clean") == A.CLEAN_PROBES
    assert sum(c.passed for c in adm.cases if c.group == "probe") == 4
    path = outcome.contract_path
    assert path is not None and D.admission_problem(path) is None
    prov = yaml.safe_load(path.read_text())["provenance"]
    assert prov["drafted_by"]["model"] == M.MODEL
    assert prov["drafted_by"]["prompt_sha256"] == M.prompt_sha256()
    # The model saw no sign-in header and no secret field.
    sent = drafter.seen[0][0]["content"]
    assert "secret-demo-token" not in sent and "hunter2" not in sent
    # An edit to the check after admission voids it.
    data = yaml.safe_load(path.read_text())
    data["effects"][0]["match"].pop("memo")
    edited = mockloan / "edited.yaml"
    edited.write_text(yaml.safe_dump(data))
    assert D.admission_problem(edited) == "the check changed after admission"


@pytest.mark.timeout(400)
def test_weak_draft_is_refused_with_reasons_fed_back(mockloan: Path) -> None:
    drafter = Fixed(WEAK, WEAK)
    outcome = _run(mockloan, drafter, rounds=2)
    assert not outcome.admitted and outcome.contract_path is None
    assert not (mockloan / "out" / "contract.yaml").exists()
    failed = {c.name for c in outcome.admissions[0].cases if not c.passed}
    assert "late_revert" in failed  # no recheck window
    assert any(n.startswith("wrong_value:amount") for n in failed)
    feedback = drafter.seen[1][-1]["content"]
    assert "late_revert" in feedback and "wrong_value:amount" in feedback
    assert outcome.rounds == 2


def test_rounds_stop_at_three_and_unsafe_drafts_never_run(mockloan: Path) -> None:
    unsafe = GOOD.replace("  kind: surfaces\n", "  kind: surfaces\n  session: my.hooks:login\n")
    drafter = Fixed(unsafe, "not: [valid", GOOD.replace("FROM records", "FROM records; DELETE FROM records"))
    outcome = _run(mockloan, drafter, rounds=3)
    assert not outcome.admitted
    assert len(drafter.seen) == 3 and outcome.rounds == 3
    assert not outcome.admissions  # nothing was run against the sandbox
    assert "session" in drafter.seen[1][-1]["content"]


def test_rounds_are_capped() -> None:
    with pytest.raises(SystemExit):
        D.main(["--trace", "t", "--task", "t", "--sandbox", "s", "--out", "o", "--rounds", "4"])


def test_attribution_flags_masks(tmp_path: Path) -> None:
    seen = {("agent", 0): "a", ("agent", 1): "b", ("other", 0): "a", ("other", 1): "b"}
    assert A._cause(seen, ["agent", "other"]) == "payload"
    seen = {("agent", 0): "7", ("agent", 1): "7", ("other", 0): "9", ("other", 1): "9"}
    assert A._cause(seen, ["agent", "other"]) == "actor"
    clock = {k: f"2026-11-21 07:0{i}:00" for i, k in enumerate(seen)}
    assert A._cause(clock, ["agent", "other"]) == "clock"


def test_spend_ledger_refuses_past_the_cap(tmp_path: Path) -> None:
    ledger = M.SpendLedger(tmp_path / "spend.jsonl", cap_usd=1.0)
    ledger.add(M.Call(M.MODEL, M.MODEL, 1, 1, 0, 0, 0.6, 1.0), "x")
    with pytest.raises(M.BudgetExceeded):
        ledger.check()


def test_descriptions_and_masking(tmp_path: Path) -> None:
    api = {"openapi": "3.1.0", "info": {"title": "T", "version": "1"},
           "paths": {"/r/{slug}": {"patch": {"summary": "Update", "requestBody": {"content": {"application/json": {"schema": {"$ref": "#/components/schemas/R"}}}}}}},
           "components": {"schemas": {"R": {"properties": {"name": {"type": "string"}}}}}}
    (tmp_path / "api.json").write_text(json.dumps(api))
    d = I.load_description(str(tmp_path / "api.json"))
    assert d.kind == "openapi" and "PATCH /r/{slug} body=R" in d.text and "R{name:string}" in d.text
    (tmp_path / "cap.json").write_text(json.dumps({"resourceType": "CapabilityStatement", "rest": [{"resource": [{"type": "Patient", "interaction": [{"code": "update"}]}]}]}))
    assert I.load_description(str(tmp_path / "cap.json")).text.endswith("Patient: update")
    (tmp_path / "s.graphql").write_text("type Query { a: Int }\ntype Task { id: ID title: String }")
    assert I.load_description(str(tmp_path / "s.graphql")).kind == "graphql"
    assert I.load_description("none").kind == "none"
    req = I.Request("POST", "http://x/a?token=t1&q=1", {"Cookie": "c", "X-Api-Key": "k", "Accept": "*/*"},
                    "username=u&password=p", "application/x-www-form-urlencoded")
    m = I.masked(req)
    assert "t1" not in m["url"] and "Cookie" not in m["headers"] and "X-Api-Key" not in m["headers"]
    assert "password=%2A%2A%2A" in m["body"] and "username=u" in m["body"]


def test_run_path_makes_no_model_calls(mockloan: Path) -> None:
    """An admitted contract is judged by the verifier alone: the model
    module and the Anthropic SDK are never imported on the run path."""

    contract = A.prepare(GOOD, load_sandbox(str(mockloan / "sandbox.yaml")))
    path = mockloan / "contract.yaml"
    path.write_text(yaml.safe_dump(contract))
    code = textwrap.dedent(
        f"""
        import sys
        sys.modules["anthropic"] = None  # any import of the SDK fails
        from openadapt_flow.verifier import Actor, Contract, Ledger, MemoryLedger, begin_write, verify_write
        c = Contract.load({str(path)!r})
        p = begin_write(c, {{"loan_id": "LN-1", "product": "Auto", "amount": "10.00", "memo": "seed", "actor_id": "a1"}},
                        actor=Actor("agent", "a1"), ledger=Ledger(MemoryLedger()))
        r = verify_write(p, seal=False)
        assert not any(m.startswith("openadapt_flow.verify_draft") for m in sys.modules), "draft code imported"
        print(r.outcome.value)
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         env={"OPENADAPT_LOCAL_SEAL": "off", "PATH": "/usr/bin:/bin", "HOME": str(mockloan)})
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() in ("REFUTED", "UNCERTAIN")  # no new record was written
