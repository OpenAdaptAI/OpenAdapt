"""``openadapt verify draft``: drafting is a model's job, admission is ours.

The model is replaced by a fixed drafter, so nothing here calls a model.
The sandbox is MockLoan's fault server (SQLite, in-process).
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

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
    GOOD.replace(
        "    product: {param: product}\n    amount: {param: amount}\n    memo: {param: memo}\n",
        "",
    )
    .replace("    recheck_after_s: 300\n", "")
    .replace(
        "[id, source, record_key, surface]",
        "[id, source, record_key, surface, product, amount, memo]",
    )
)
PAYLOADS = [
    {
        "amount": f"{1000 + i * 111}.00",
        "memo": f"Probe memo {i}",
        "product": ["Auto", "Home", "Personal"][i % 3],
    }
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
    # Admission records are signed with this computer's key: keep it here.
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))
    monkeypatch.delenv("OPENADAPT_SEAL_KEY", raising=False)
    monkeypatch.delenv("OPENADAPT_SEAL_KEY_FILE", raising=False)
    url, db, stop = fault_server.serve(0)
    base = url.rstrip("/")
    for loan, prod, amt in [("LN-1", "Auto", "10.00"), ("LN-2", "Home", "20.00")]:
        requests.post(
            base + "/api/disbursement",
            json={"loan_id": loan, "product": prod, "amount": amt, "memo": "seed"},
            timeout=10,
        )
    seeded = tmp_path / "seeded.sqlite3"
    shutil.copy(db.database_path, seeded)
    params = {
        "loan_id": "LN-9",
        "product": "Personal",
        "amount": "31.50",
        "memo": "Kitchen",
    }
    trace = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": base + "/api/disbursement",
                        "headers": [
                            {"name": "Content-Type", "value": "application/json"},
                            {
                                "name": "Authorization",
                                "value": "Bearer secret-demo-token",
                            },
                        ],
                        "postData": {
                            "mimeType": "application/json",
                            "text": json.dumps({**params, "password": "hunter2"}),
                        },
                    },
                    "response": {"status": 200, "content": {"text": "{}"}},
                }
            ]
        }
    }
    (tmp_path / "trace.har").write_text(json.dumps(trace))
    (tmp_path / "task.yaml").write_text(
        yaml.safe_dump({"instruction": "Book it.", "params": params})
    )
    (tmp_path / "sandbox.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "openadapt.verify-sandbox/v1",
                "app": {"base_url": base},
                "actors": {"agent": {"id": "a1"}, "other": {"id": "b2"}},
                "store": {
                    "dialect": "sqlite",
                    "read": {"sqlite_database": str(db.database_path)},
                    "admin": {"sqlite_database": str(db.database_path)},
                },
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
    assert sum(c.passed for c in adm.cases if c.group == "probe") == 5
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
    unsafe = GOOD.replace(
        "  kind: surfaces\n", "  kind: surfaces\n  session: my.hooks:login\n"
    )
    drafter = Fixed(
        unsafe,
        "not: [valid",
        GOOD.replace("FROM records", "FROM records; DELETE FROM records"),
    )
    outcome = _run(mockloan, drafter, rounds=3)
    assert not outcome.admitted
    assert len(drafter.seen) == 3 and outcome.rounds == 3
    assert not outcome.admissions  # nothing was run against the sandbox
    assert "session" in drafter.seen[1][-1]["content"]


def test_rounds_are_capped() -> None:
    with pytest.raises(SystemExit):
        D.main(
            [
                "--trace",
                "t",
                "--task",
                "t",
                "--sandbox",
                "s",
                "--out",
                "o",
                "--rounds",
                "4",
            ]
        )


def test_attribution_flags_masks(tmp_path: Path) -> None:
    seen = {("agent", 0): "a", ("agent", 1): "b", ("other", 0): "a", ("other", 1): "b"}
    assert A._cause(seen, ["agent", "other"]) == "payload"
    seen = {("agent", 0): "7", ("agent", 1): "7", ("other", 0): "9", ("other", 1): "9"}
    assert A._cause(seen, ["agent", "other"]) == "actor"
    clock = {k: f"2026-11-21 07:0{i}:00" for i, k in enumerate([*seen, ("agent", 2)])}
    assert A._cause(clock, ["agent", "other"]) == "clock"
    # A new id on every write: it differs between two identical writes.
    fresh = {**{k: str(i) for i, k in enumerate(seen)}, ("agent", 2): "99"}
    assert A._cause(fresh, ["agent", "other"]) == "fresh"


def test_spend_ledger_refuses_past_the_cap(tmp_path: Path) -> None:
    ledger = M.SpendLedger(tmp_path / "spend.jsonl", cap_usd=1.0)
    ledger.add(M.Call(M.MODEL, M.MODEL, 1, 1, 0, 0, 0.6, 1.0), "x")
    with pytest.raises(M.BudgetExceeded):
        ledger.check()


def test_descriptions_and_masking(tmp_path: Path) -> None:
    api = {
        "openapi": "3.1.0",
        "info": {"title": "T", "version": "1"},
        "paths": {
            "/r/{slug}": {
                "patch": {
                    "summary": "Update",
                    "requestBody": {
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/R"}
                            }
                        }
                    },
                }
            }
        },
        "components": {"schemas": {"R": {"properties": {"name": {"type": "string"}}}}},
    }
    (tmp_path / "api.json").write_text(json.dumps(api))
    d = I.load_description(str(tmp_path / "api.json"))
    assert (
        d.kind == "openapi"
        and "PATCH /r/{slug} body=R" in d.text
        and "R{name:string}" in d.text
    )
    (tmp_path / "cap.json").write_text(
        json.dumps(
            {
                "resourceType": "CapabilityStatement",
                "rest": [
                    {
                        "resource": [
                            {"type": "Patient", "interaction": [{"code": "update"}]}
                        ]
                    }
                ],
            }
        )
    )
    assert I.load_description(str(tmp_path / "cap.json")).text.endswith(
        "Patient: update"
    )
    (tmp_path / "s.graphql").write_text(
        "type Query { a: Int }\ntype Task { id: ID title: String }"
    )
    assert I.load_description(str(tmp_path / "s.graphql")).kind == "graphql"
    assert I.load_description("none").kind == "none"
    req = I.Request(
        "POST",
        "http://x/a?token=t1&q=1",
        {"Cookie": "c", "X-Api-Key": "k", "Accept": "*/*"},
        "username=u&password=p",
        "application/x-www-form-urlencoded",
    )
    m = I.masked(req)
    assert (
        "t1" not in m["url"]
        and "Cookie" not in m["headers"]
        and "X-Api-Key" not in m["headers"]
    )
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
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "OPENADAPT_LOCAL_SEAL": "off",
            "PATH": "/usr/bin:/bin",
            "HOME": str(mockloan),
        },
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() in ("REFUTED", "UNCERTAIN")  # no new record was written


@pytest.mark.parametrize(
    "edit, why",
    [
        # A REST read would point the check at a URL the model chose.
        (
            lambda c: c.update(
                verifier={"kind": "rest", "base_url": "http://x", "records_path": "/a"}
            ),
            "verifier",
        ),
        (
            lambda c: c["verifier"]["surfaces"]["record"].update(
                auth={"bearer_env": "ANTHROPIC_API_KEY"}
            ),
            "auth",
        ),
        (
            lambda c: c["effects"][0]["match"].update(
                memo={"literal": "${ANTHROPIC_API_KEY}"}
            ),
            "environment",
        ),
        (lambda c: c["frame"]["database"].update(profile="openemr"), "profile"),
        (lambda c: c.update(admission={"coverage": "x.json"}), "admission"),
        (lambda c: c["frame"].update(learned={"excuses": []}), "learned"),
    ],
)
def test_drafts_are_allowlisted(mockloan: Path, edit: Any, why: str) -> None:
    contract = yaml.safe_load(GOOD)
    edit(contract)
    with pytest.raises(A.DraftProblem, match=why):
        A.prepare(json.dumps(contract), load_sandbox(str(mockloan / "sandbox.yaml")))


def test_good_draft_as_json_text_loads(mockloan: Path) -> None:
    data = A.prepare(
        json.dumps(yaml.safe_load(GOOD)), load_sandbox(str(mockloan / "sandbox.yaml"))
    )
    assert data["frame"]["database"]["read"]["sqlite_database"]


def test_untestable_is_never_a_pass() -> None:
    case = A.Case(
        "fault", "lost_update", "", "refuted", untestable=True, note="no second user"
    )
    A.Qualifier._judge(case)
    assert not case.passed
    adm = A.Admission(False, "sha256:" + "0" * 64, [case], [], [])
    assert adm.only_untestable() and "not the contract's fault" in adm.feedback()


@pytest.mark.parametrize(
    ("status", "kept", "credited"),
    [
        (409, True, True),
        (412, True, True),
        (503, True, False),  # an outage observed nothing
        (401, True, False),  # an expired session observed nothing
        (409, False, False),  # a conflict that lost the other user's save
    ],
)
def test_a_refused_stale_save_is_credited_only_for_a_kept_conflict(
    status: int, kept: bool, credited: bool
) -> None:
    from types import SimpleNamespace

    from openadapt_flow.verify_draft.sandbox import Move

    base = {"t": {1: {"v": "a"}}}
    saved = {"t": {1: {"v": "other"}}}
    state = {"rows": base}
    q = object.__new__(A.Qualifier)
    q.sandbox = SimpleNamespace(actors={"agent": {}, "other": {}})
    q.demo = A.Demo(
        writes=[],
        params={"v": "a"},
        base=base,
        after={"t": {1: {"v": "b"}}},
        main=Move("t", 1, "changed", ["v"], {"v": "a"}, {"v": "b"}),
        param_columns={"v": "v"},
    )

    def send(writes: Any, actor: str, swaps: Any) -> list[int]:
        state["rows"] = saved  # the other user's save lands
        return [200]

    q.app = SimpleNamespace(send=send)
    q.store = SimpleNamespace(read_moved=lambda _base: state["rows"])
    q.refused_statuses = []

    def run(contract: Any, case: A.Case, *, values: Any, before: Any) -> None:
        assert before() is None
        if not kept:
            state["rows"] = {"t": {1: {"v": "agent"}}}
        q.refused_statuses = [status]
        case.applicable = False
        case.untestable = True
        case.note = f"the app refused the write (HTTP {status})"

    q.run = run  # type: ignore[method-assign]
    (case,) = q._fault({}, "lost_update", {"v": "b"}, [{"v": "other"}])
    A.Qualifier._judge(case)
    assert case.untestable is (not credited)
    assert case.passed is credited


def test_a_single_user_sandbox_never_admits(mockloan: Path) -> None:
    sandbox = yaml.safe_load((mockloan / "sandbox.yaml").read_text())
    sandbox["actors"].pop("other")
    (mockloan / "sandbox.yaml").write_text(yaml.safe_dump(sandbox))
    outcome = _run(mockloan, Fixed(GOOD), rounds=1)
    assert not outcome.admitted
    adm = outcome.admissions[-1]
    (missing,) = [c for c in adm.cases if c.name == "attribution:other"]
    assert missing.untestable and not missing.passed
    assert adm.only_untestable()


def test_one_question_at_most(mockloan: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A masked column is asked about once; the answer is a decision, and
    'bookkeeping' lets the same draft be qualified again."""

    calls: list[set[str]] = []

    def fake_qualify(
        self: A.Qualifier, contract: Any, payloads: Any, **kw: Any
    ) -> A.Admission:
        calls.append(set(self.bookkeeping))
        ok = "records.memo" in self.bookkeeping
        masked = [] if ok else [{"fault": "sibling_edit", "column": "records.memo"}]
        return A.Admission(ok, "sha256:" + "0" * 64, [], [], [], masked)

    monkeypatch.setattr(A.Qualifier, "qualify", fake_qualify)
    asked: list[str] = []

    def ask(question: str, columns: Any) -> dict[str, str]:
        asked.append(question)
        return {c: "bookkeeping" for c in columns}

    outcome = _run(mockloan, Fixed(GOOD), rounds=3, ask=ask)
    assert outcome.admitted and outcome.decisions == 1 and len(asked) == 1
    assert calls == [set(), {"records.memo"}]
    # Without an answer the draft is refused, never admitted on a guess.
    calls.clear()
    outcome = _run(mockloan, Fixed(GOOD), rounds=1, ask=lambda q, c: None)
    assert not outcome.admitted and outcome.decisions == 0


def test_actors_sign_in_before_the_store_is_read(
    mockloan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sign-in that writes rows (a session, a last-login time) must be
    starting state, never inside the demonstration's or a case's window."""

    from openadapt_flow.verify_draft import sandbox as S

    events: list[str] = []
    real_session, real_read = S.App.session, S.Store.read

    def session(self: Any, actor: str) -> Any:
        if actor not in self._sessions:
            events.append(f"sign-in:{actor}")
        return real_session(self, actor)

    def read(self: Any, tables: Any = None) -> Any:
        events.append("read")
        return real_read(self, tables)

    monkeypatch.setattr(S.App, "session", session)
    monkeypatch.setattr(S.Store, "read", read)
    _run(mockloan, Fixed("not: [valid"), rounds=1)
    first_read = events.index("read")
    assert {"sign-in:agent", "sign-in:other"} <= set(events[:first_read])


@pytest.mark.timeout(300)
def test_a_skip_of_a_table_that_never_moves_is_refused(mockloan: Path) -> None:
    skipping = GOOD.replace(
        "    dialect: sqlite\n",
        "    dialect: sqlite\n    allow_tables:\n    - {table: metadata, mode: skip, reason: counters}\n",
    )
    outcome = _run(mockloan, Fixed(skipping), rounds=1)
    assert not outcome.admitted
    problems = [a for a in outcome.admissions[0].attribution if a.problem]
    assert any(a.table == "metadata" and "skipped table" in a.problem for a in problems)


def test_masked_redacts_all_secret_shapes() -> None:
    """Every secret shape probed in review is masked before reaching a model."""
    from openadapt_flow.verify_draft.inputs import masked
    from openadapt_flow.verify_draft.sandbox import Request

    # 1. JSON list under a secret key
    r1 = masked(
        Request(
            method="POST",
            url="https://app.test/api/auth",
            headers={},
            body='{"token": ["tok-alpha", "tok-beta"], "user": "ada"}',
            mime="application/json",
            status=200,
            response='{"ok": true}',
        )
    )
    assert "tok-alpha" not in (r1["body"] or "")
    assert "tok-beta" not in (r1["body"] or "")
    assert '"ada"' in (r1["body"] or "")

    # 2. credential, pin, otp, jwt keys
    r2 = masked(
        Request(
            method="POST",
            url="https://app.test/api/login",
            headers={},
            body='{"credential": "c-val", "pin": "4321", "otp": "998877", "jwt": "eyJ.secret.sig", "title": "note"}',
            mime="application/json",
            status=200,
            response=None,
        )
    )
    for leak in ("c-val", "4321", "998877", "eyJ.secret.sig"):
        assert leak not in (r2["body"] or "")
    assert '"note"' in (r2["body"] or "")

    # 3. multipart/form-data password
    boundary = "----TestBoundary7MA4YWxkTrZu0gW"
    mp_body = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="username"\r\n\r\n'
        "ada\r\n"
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="password"\r\n\r\n'
        "super-secret-pw\r\n"
        f"--{boundary}--\r\n"
    )
    r3 = masked(
        Request(
            method="POST",
            url="https://app.test/login",
            headers={},
            body=mp_body,
            mime=f"multipart/form-data; boundary={boundary}",
            status=200,
            response=None,
        )
    )
    assert "super-secret-pw" not in (r3["body"] or "")
    assert "ada" in (r3["body"] or "")

    # 4. text/plain body replaced with placeholder
    r4 = masked(
        Request(
            method="POST",
            url="https://app.test/raw",
            headers={},
            body="Bearer raw-secret-token-123",
            mime="text/plain",
            status=200,
            response=None,
        )
    )
    assert "raw-secret-token-123" not in (r4["body"] or "")
    assert r4["body"] == "<redacted body>"

    # 5. HTML response with csrf_token input replaced with placeholder
    r5 = masked(
        Request(
            method="GET",
            url="https://app.test/form",
            headers={},
            body=None,
            mime="",
            status=200,
            response='<form><input type="hidden" name="csrf_token" value="csrf-secret-999"></form>',
        )
    )
    assert "csrf-secret-999" not in (r5["response"] or "")
    assert r5["response"] == "<redacted non-json response>"

    # 6. Secret inside a JSON response list (refresh)
    r6 = masked(
        Request(
            method="POST",
            url="https://app.test/sessions",
            headers={},
            body=None,
            mime="",
            status=200,
            response='[{"id": 1, "refresh": "ref-secret-xyz"}]',
        )
    )
    assert "ref-secret-xyz" not in (r6["response"] or "")
    assert '"***"' in (r6["response"] or "")


def test_qualifier_run_records_sqlite_driver_error_as_error_case(
    mockloan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DB driver error (e.g. sqlite3.OperationalError) becomes an ERROR case
    rather than aborting the whole qualification report."""
    import sqlite3

    from openadapt_flow.verify_draft import admit as A
    from openadapt_flow.verify_draft import inputs as I
    from openadapt_flow.verify_draft import sandbox as S

    cfg = S.load_sandbox(mockloan / "sandbox.yaml")
    task = I.load_task(mockloan / "task.yaml")
    store = S.Store(cfg.store)
    app = S.App(cfg)
    writes = I.writes(
        I.load_trace(mockloan / "trace.har"), skip_paths=app.login_paths()
    )
    base = store.read()
    demo = A.record_demo(store, app, writes, task.params, base)
    contract = A.prepare(GOOD, cfg)

    q = A.Qualifier(cfg, store, app, demo)
    case = A.Case("clean", "clean-1", "db fault", "confirmed")

    def boom(_base: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(store, "read_moved", boom)
    got = q.run(contract, case, values=demo.params)
    assert got is None
    assert case.outcome == "ERROR"
    assert any("OperationalError" in r for r in case.reasons)
    q._judge(case)
    assert case.passed is False


# -- fix-forward for #2438 (Grok's review, issuecomment-7135157858) -------------


def _qualifier(root: Path) -> tuple[A.Qualifier, dict[str, Any]]:
    from openadapt_flow.verify_draft import sandbox as S

    cfg = S.load_sandbox(root / "sandbox.yaml")
    task = I.load_task(root / "task.yaml")
    store = S.Store(cfg.store)
    app = S.App(cfg)
    writes = I.writes(I.load_trace(root / "trace.har"), skip_paths=app.login_paths())
    demo = A.record_demo(store, app, writes, task.params, store.read())
    return A.Qualifier(cfg, store, app, demo), A.prepare(GOOD, cfg)


@pytest.mark.timeout(300)
def test_a_crashed_recheck_never_passes_a_clean_case(
    mockloan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding 1: a clean write confirmed, then its recheck raised
    ``database is locked``. Before the fix every clean case passed with
    ``recheck=''``."""
    import sqlite3

    import openadapt_flow.verifier as V

    def locked(*_a: Any, **_k: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    q, contract = _qualifier(mockloan)
    monkeypatch.setattr(V, "recheck", locked)
    for i, payload in enumerate(PAYLOADS[: A.CLEAN_PROBES]):
        case = A.Case("clean", f"clean-{i + 1}", "recheck crashes", "confirmed")
        q.run(contract, case, values=q._values(payload), rechecks=True)
        assert case.outcome == "CONFIRMED", case.line()
        assert case.recheck == "ERROR", case.line()
        assert any("database is locked" in r for r in case.reasons)
        q._judge(case)
        assert case.passed is False, case.line()
    adm = q.qualify(contract, PAYLOADS, attribute=False)
    assert adm.admitted is False
    assert not any(c.passed for c in adm.cases if c.group == "clean")


# Finding 2: names the old pattern let through, and names it over-masked.
MASKED_NAMES = [
    "totp",
    "otpCode",
    "userPin",
    "mfa_code",
    "cookie",
    "code",
    "sig",
    "X-Amz-Signature",
    # what the old pattern caught must stay caught
    "token",
    "csrf_token",
    "credential",
    "jwt",
    "refresh",
    "password",
    "api_key",
    "session_id",
    "private_key",
    "access_key",
    "password_hash",
    "salt",
    "bearer",
    "X-Amz-Credential",
    # Grok's round-2 review on #2635: masked on main, must stay masked
    "session",
    "SESSION",
    "JSESSIONID",
    "PHPSESSID",
    "sessid",
    "sso_session",
    "laravel_session",
    "session_key",
    "sessionKey",
    "x-session",
    "authorization",
    "proxy-authorization",
    "oauth_verifier",
    "authn",
    # Grok's round-3 review on #2635: compound names main's pattern masked
    "authorization_code",
    "authorizationCode",
    "sessionkey",
    "SESSIONKEY",
    "session_data",
    "sessionData",
    "sessiondata",
    "session_value",
    "sessionValue",
    "session_handle",
    "sessionHandle",
    "session_ticket",
    "sessionTicket",
    "usersession",
    "pwdhash",
    "passhash",
    "hashkey",
    "authkey",
    "authid",
    "authz",
    "session_state",
    "sessionState",
    "oauth_state",
    "oauthState",
    "oauth_nonce",
]
CLEAR_NAMES = [
    "author",
    "author_id",
    "authored_at",
    "session_count",
    "record_key",
    "productCode",
    "refresh_interval",
    "authorization_id",
    "memo",
]

#: The pattern #2438 shipped (``inputs.SECRET_NAME_RE`` before this fix),
#: kept here only to prove nothing it masked is now sent in clear.
OLD_SECRET_NAME_RE = re.compile(
    r"pass(word)?|secret|token|api[-_]?key|hash|salt|session|csrf|auth"
    r"|credential|bearer|refresh|private[-_]?key|access[-_]?key"
    r"|(^|[^a-z0-9])(cred|pin|otp|tfa|jwt)([^a-z0-9]|$)",
    re.I,
)
#: Names the old pattern matched only by a substring, and that are not
#: secrets: the one allowlist :func:`I.is_secret_name` sends in clear.
OLD_OVERMASKED = I.LEGACY_FALSE_POSITIVES
#: Every name either list names, plus lookalikes the old pattern matched.
NAME_CORPUS = sorted(
    {*MASKED_NAMES, *CLEAR_NAMES, *OLD_OVERMASKED}
    | {
        "user_password",
        "secretAnswer",
        "idToken",
        "apiKey",
        "content_hash",
        "pwSalt",
        "sessionToken",
        "xsrf-token",
        "oauth_token",
        "client_credentials",
        "bearer_token",
        "refresh_token",
        "privateKey",
        "accessKey",
        "user-pin",
        "otp",
        "tfa",
        "jwt",
        "cred",
        "authToken",
        "auth",
        "authCode",
    }
)


@pytest.mark.parametrize(
    "name",
    [
        n
        for n in NAME_CORPUS
        if OLD_SECRET_NAME_RE.search(n) and n not in OLD_OVERMASKED
    ],
)
def test_every_name_the_old_pattern_masked_is_still_masked(name: str) -> None:
    from openadapt_flow.verify_draft.inputs import is_secret_name

    assert is_secret_name(name), f"{name} was masked on main and now is not"


def _generated_names() -> list[str]:
    """Every old-pattern stem with prefixes, suffixes and casings."""
    stems = [
        "pass", "password", "passwd", "pwd", "secret", "token", "apikey",
        "api_key", "api-key", "hash", "salt", "session", "csrf", "auth",
        "authz", "authorization", "credential", "bearer", "refresh",
        "privatekey", "private_key", "accesskey", "access_key", "cred",
        "pin", "otp", "tfa", "jwt",
    ]  # fmt: skip
    prefixes = ["", "user_", "user", "x-", "oauth_", "my", "sso_", "client"]
    suffixes = ["", "_id", "id", "key", "Key", "_data", "Hash", "_state",
                "value", "_ticket", "_code", "Count"]  # fmt: skip
    names = set()
    for stem in stems:
        for pre in prefixes:
            for suf in suffixes:
                name = pre + stem + suf
                names.update({name, name.upper(), name.lower()})
    return sorted(names)


def test_no_name_the_old_pattern_masked_is_sent_in_clear() -> None:
    """For a broad generated corpus: matched by main's pattern and not an
    allowlisted false positive implies masked, at every call site."""
    from openadapt_flow.verify_draft.inputs import masked
    from openadapt_flow.verify_draft.sandbox import Request

    corpus = sorted({*_generated_names(), *NAME_CORPUS})
    expected = [
        n
        for n in corpus
        if OLD_SECRET_NAME_RE.search(n) and n.lower() not in OLD_OVERMASKED
    ]
    assert len(expected) > 2000
    leaked = [n for n in expected if not I.is_secret_name(n)]
    assert leaked == []
    columns = [n for n in expected if n.strip().lower() != "code"]
    assert [n for n in columns if not A._secret_column(n)] == []
    value = "corpus-leak-value"
    for n in expected[::7]:  # every call site, on a sample
        got = masked(
            Request(
                method="POST",
                url="https://app.test/x?" + urlencode({n: value}),
                headers={n: value},
                body=json.dumps({n: value}),
                mime="application/json",
                status=200,
                response=json.dumps({n: value}),
            )
        )
        assert value not in json.dumps(got), n


@pytest.mark.parametrize("name", sorted(OLD_OVERMASKED))
def test_the_old_overmasked_names_are_clear(name: str) -> None:
    assert {"author", "session_count", "authorization_id"} <= OLD_OVERMASKED
    from openadapt_flow.verify_draft.inputs import is_secret_name

    assert OLD_SECRET_NAME_RE.search(name), name  # the list stays honest
    assert not is_secret_name(name)


@pytest.mark.parametrize("name", MASKED_NAMES)
def test_secret_names_are_masked_everywhere(name: str) -> None:
    from urllib.parse import urlencode

    from openadapt_flow.verify_draft.inputs import masked
    from openadapt_flow.verify_draft.sandbox import Request

    value = f"leak-{len(name):02d}-value"
    json_body = masked(
        Request(
            method="POST",
            url="https://app.test/api/x?" + urlencode({name: value, "page": "2"}),
            headers={name: value, "Accept": "application/json"},
            body=json.dumps({name: value, "nested": {name: value}, "title": "kept"}),
            mime="application/json",
            status=200,
            response=json.dumps([{name: value, "id": 1}]),
        )
    )
    form_body = masked(
        Request(
            method="POST",
            url="https://app.test/form",
            headers={},
            body=urlencode({name: value, "title": "kept"}),
            mime="application/x-www-form-urlencoded",
            status=200,
            response=None,
        )
    )
    for got in (json_body, form_body):
        assert value not in json.dumps(got), f"{name} reached the model: {got}"
    assert "page=2" in json_body["url"] and "kept" in (json_body["body"] or "")
    assert json_body["headers"] == {"Accept": "application/json"}


@pytest.mark.parametrize("name", CLEAR_NAMES)
def test_ordinary_names_are_not_masked(name: str) -> None:
    from openadapt_flow.verify_draft.inputs import masked
    from openadapt_flow.verify_draft.sandbox import Request

    value = f"plain-{len(name):02d}-value"
    got = masked(
        Request(
            method="POST",
            url="https://app.test/api/x",
            headers={name: value},
            body=json.dumps({name: value}),
            mime="application/json",
            status=200,
            response=json.dumps({name: value}),
        )
    )
    assert json.loads(got["body"]) == {name: value}
    assert json.loads(got["response"]) == {name: value}
    assert got["headers"] == {name: value}
    assert A._secret_column(name) is False


def test_secret_columns_use_the_shared_names() -> None:
    for name in MASKED_NAMES:
        if name != "code":  # a column named code is an identifier
            assert A._secret_column(name), name
    assert A._secret_column("code") is False


def test_a_json_cookie_key_is_masked() -> None:
    from openadapt_flow.verify_draft.inputs import masked
    from openadapt_flow.verify_draft.sandbox import Request

    got = masked(
        Request(
            method="POST",
            url="https://app.test/api/session",
            headers={},
            body=json.dumps(
                {"cookie": "sessionid=abc123secret; path=/", "user": "ada"}
            ),
            mime="application/json",
            status=200,
            response=None,
        )
    )
    assert "abc123secret" not in (got["body"] or "")
    assert json.loads(got["body"])["user"] == "ada"


# Finding 3: a forged --check record.


def _located(text_: str) -> dict[str, Any]:
    """A draft pointed at a store, as :func:`A.prepare` leaves it."""
    data = yaml.safe_load(text_)
    location = {"sqlite_database": "/nonexistent/verify-draft.sqlite3"}
    data["verifier"]["surfaces"]["record"].update(location)
    data["frame"]["database"]["read"] = dict(location)
    return data


def _admitted_contract(home: Path) -> dict[str, Any]:
    contract = _located(GOOD)
    contract["provenance"] = {
        "drafted_by": {"model": M.MODEL},
        "admission": {
            "suite": D.SUITE,
            "check_sha256": A.check_sha256(contract),
            "rounds": 1,
            "admitted_at": "2026-11-24T12:00:00Z",
            "recheck_window_tested_s": 2.0,
            "faults": {"wrong_row": "refuted", "late_revert": "refuted"},
            "clean_writes_confirmed": 5,
            "attribution_probe_writes": 5,
            "bookkeeping_confirmed_by_person": [],
        },
    }
    return D.sign_admission(contract, home=home)


def _write(path: Path, contract: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(contract, sort_keys=False))
    return path


def test_a_signed_admission_record_passes_check(tmp_path: Path) -> None:
    home = tmp_path / "seal"
    path = _write(tmp_path / "contract.yaml", _admitted_contract(home))
    assert D.admission_problem(path, home=home) is None


def test_a_forged_admission_record_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grok's probe: a WEAK contract with a self-computed check digest."""
    home = tmp_path / "seal"
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(home))
    _admitted_contract(home)  # this computer has a key
    weak = _located(WEAK)
    weak["provenance"] = {
        "admission": {"suite": D.SUITE, "check_sha256": A.check_sha256(weak)}
    }
    path = _write(tmp_path / "forged.yaml", weak)
    assert D.admission_problem(path, home=home) == "the admission record is not signed"
    assert D.main(["--check", str(path)]) == 1


def test_a_signature_moved_onto_another_check_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "seal"
    good = _admitted_contract(home)
    weak = _located(WEAK)
    weak["provenance"] = copy.deepcopy(good["provenance"])
    weak["provenance"]["admission"]["check_sha256"] = A.check_sha256(weak)
    path = _write(tmp_path / "moved.yaml", weak)
    assert (
        D.admission_problem(path, home=home)
        == "the admission record changed after it was signed"
    )


def test_an_edited_admission_record_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "seal"
    contract = _admitted_contract(home)
    contract["provenance"]["admission"]["faults"]["wrong_row"] = "refuted (edited)"
    path = _write(tmp_path / "edited.yaml", contract)
    assert (
        D.admission_problem(path, home=home)
        == "the admission record changed after it was signed"
    )


def test_a_record_signed_by_another_key_is_refused_unless_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.offline_seal import public_key_entry

    other = tmp_path / "other-seal"
    mine = tmp_path / "my-seal"
    path = _write(tmp_path / "foreign.yaml", _admitted_contract(other))
    got = D.admission_problem(path, home=mine)
    assert got is not None and "unknown_key" in got
    registry = tmp_path / "keys.json"
    registry.write_text(json.dumps({"keys": [public_key_entry(other)]}))
    assert D.admission_problem(path, keys=registry, home=mine) is None
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(mine))
    monkeypatch.delenv("OPENADAPT_SEAL_KEY", raising=False)
    monkeypatch.delenv("OPENADAPT_SEAL_KEY_FILE", raising=False)
    assert D.main(["--check", str(path)]) == 1
    assert D.main(["--check", str(path), "--keys", str(registry)]) == 0


def test_an_unsignable_admission_writes_no_contract(
    mockloan: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.offline_seal import LocalSealError

    def no_key(*_a: Any, **_k: Any) -> Any:
        raise LocalSealError("key_unavailable", "test")

    monkeypatch.setattr(D, "sign_subject", no_key)
    outcome = _run(mockloan, Fixed(GOOD), rounds=1)
    assert outcome.admitted is False
    assert "could not be signed" in outcome.reason
    assert not (mockloan / "out" / "contract.yaml").exists()
