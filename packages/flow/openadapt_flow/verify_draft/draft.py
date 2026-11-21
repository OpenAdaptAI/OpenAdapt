"""``openadapt verify draft``: a model drafts the check, OpenAdapt qualifies it.

    openadapt verify draft --describe openapi.json --trace demo.har \\
        --task task.yaml --sandbox sandbox.yaml --out contracts/update_recipe

1. The sandbox replays the trace's write once, between two reads of the
   store, to see which rows it moves.
2. The pinned model drafts an ``openadapt.verify/v1`` contract from the
   system's description, the trace (masked), what the write moved and the
   task, plus other valid values to test it with.
3. :mod:`openadapt_flow.verify_draft.admit` qualifies the draft. A failure
   goes back to the model with the failing cases, at most 3 rounds in all;
   then the command gives up and says why.
4. A person is asked at most once, and only when a column the draft
   excludes is the only place a planted fault could go (is it bookkeeping
   or business data?). With no answer available the draft is refused.

Only an admitted contract is written as ``contract.yaml``. Its
``provenance`` section names the model, the prompt digest, the inputs'
digests and the admission's check digest; an edit to the check after
admission no longer matches (:func:`admission_problem`). Running the
contract makes no model calls.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

import yaml

from openadapt_flow.verify_draft import admit as A
from openadapt_flow.verify_draft import inputs as I
from openadapt_flow.verify_draft import model as M
from openadapt_flow.verify_draft.sandbox import App, SandboxConfig, Store, load_sandbox

MAX_ROUNDS = 3
SUITE = "openadapt.verify-draft-admission/v1"


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class Outcome:
    admitted: bool
    rounds: int
    decisions: int
    seconds: float
    usd: float
    reason: str
    contract_path: Optional[Path] = None
    admissions: list[A.Admission] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "rounds": self.rounds,
            "person_decisions": self.decisions,
            "seconds": self.seconds,
            "model_usd": round(self.usd, 4),
            "reason": self.reason,
            "contract": str(self.contract_path) if self.contract_path else None,
            "questions": self.questions,
        }


Asker = Callable[[str, Sequence[str]], Optional[Mapping[str, str]]]


def first_message(
    task: I.Task, desc: I.Description, trace: list[Any], demo: A.Demo, dialect: str
) -> str:
    writes = [I.masked(r) for r in demo.writes]
    return "\n\n".join(
        [
            f"Task: {task.instruction}",
            "Task parameters (the values the demonstration used):\n"
            + json.dumps(task.params, indent=1, default=str),
            f"Store dialect: {dialect}",
            f"System description ({desc.kind}):\n{desc.text or '(none given)'}",
            "The demonstration's write requests (sign-in and secrets masked):\n"
            + json.dumps(writes, indent=1)[:20000],
            "What the write moved in the database (replayed once in the sandbox; "
            "synthetic data):\n" + demo.moved_text()[:20000],
            "Write the contract for this task.",
        ]
    )


def run(
    *,
    describe: Optional[str],
    trace_path: str,
    task_path: str,
    sandbox_path: str,
    out: Path,
    drafter: M.Drafter,
    rounds: int = MAX_ROUNDS,
    ask: Optional[Asker] = None,
    log: Callable[[str], None] = lambda s: None,
) -> Outcome:
    started = time.monotonic()
    out.mkdir(parents=True, exist_ok=True)
    sandbox = load_sandbox(sandbox_path)
    task = I.load_task(task_path)
    trace = I.load_trace(trace_path)
    store = Store(sandbox.store)
    app = App(sandbox)
    desc = I.load_description(describe, schema_text=store.schema_text())
    writes = I.writes(trace, skip_paths=app.login_paths())
    if not writes:
        raise I.InputError("the trace holds no write request")
    problem = read_account_problem(sandbox)
    if problem:
        # An operator fix, not something a model round can repair.
        raise I.InputError(
            f"the sandbox's read account can't read the whole database: {problem}"
        )
    base = store.read()
    log(f"store: {len(store.tables)} tables; trace: {len(writes)} write request(s)")
    demo = A.record_demo(store, app, writes, task.params, base)
    if demo.main is None or any(s >= 400 for s in demo.statuses):
        return _finish(
            out,
            started,
            Outcome(
                False,
                0,
                0,
                0.0,
                0.0,
                f"replaying the trace moved no row holding a task value (HTTP {demo.statuses})",
            ),
            [],
        )
    log(
        f"demonstration: {demo.change} {demo.main.table}, columns {sorted(demo.param_columns)}"
    )
    qualifier = A.Qualifier(sandbox, store, app, demo, workdir=out / "work")
    messages: list[dict[str, Any]] = [
        {
            "role": "user",
            "content": first_message(task, desc, trace, demo, sandbox.store.dialect),
        }
    ]
    usd = 0.0
    decisions = 0
    asked = False
    questions: list[str] = []
    admissions: list[A.Admission] = []
    reason = ""
    contract: Optional[dict[str, Any]] = None
    draft: Optional[M.Draft] = None
    used = 0
    while used < rounds:
        used += 1
        draft = drafter.draft(messages)
        usd += draft.call.usd if draft.call else 0.0
        messages.append(
            {
                "role": "assistant",
                "content": draft.raw
                or json.dumps(
                    {
                        "contract_yaml": draft.contract_yaml,
                        "payloads": draft.payloads,
                        "notes": draft.notes,
                    }
                ),
            }
        )
        (out / "rounds").mkdir(exist_ok=True)
        (out / "rounds" / f"round-{used}.yaml").write_text(
            draft.contract_yaml or "", encoding="utf-8"
        )
        try:
            contract = A.prepare(draft.contract_yaml, sandbox)
        except A.DraftProblem as exc:
            reason = str(exc)
            log(f"round {used}: draft refused before testing: {reason}")
            messages.append(
                {
                    "role": "user",
                    "content": f"The contract could not be tested: {reason}\nFix it and answer with the same JSON object.",
                }
            )
            continue
        adm = qualifier.qualify(contract, draft.payloads)
        if adm.masked and not asked and ask is not None:
            columns = sorted({m["column"] for m in adm.masked})
            question = (
                "The draft excludes these columns, and a planted fault could only go there: "
                + ", ".join(columns)
                + ". Are they bookkeeping the app sets on its own (bookkeeping), or data people edit (business)?"
            )
            questions.append(question)
            asked = True
            answers = ask(question, columns)
            if answers is not None:
                decisions += 1
                keep = [
                    c for c, a in answers.items() if str(a).lower().startswith("book")
                ]
                qualifier.bookkeeping.update(keep)
                if set(keep) >= set(columns):
                    adm = qualifier.qualify(contract, draft.payloads)
        admissions.append(adm)
        (out / "rounds" / f"round-{used}.admission.json").write_text(
            json.dumps(adm.as_dict(), indent=1, default=str), encoding="utf-8"
        )
        passed = sum(c.passed for c in adm.cases)
        log(
            f"round {used}: {'ADMITTED' if adm.admitted else 'rejected'} ({passed}/{len(adm.cases)} cases, {adm.seconds:.0f}s)"
        )
        if adm.admitted:
            break
        reason = adm.feedback()
        messages.append(
            {
                "role": "user",
                "content": "Admission failed. These cases did not behave as required:\n"
                + reason
                + "\nFix the contract (and the payloads if the app refused them). Answer with the same JSON object.",
            }
        )
    admitted = bool(admissions and admissions[-1].admitted)
    outcome = Outcome(
        admitted,
        used,
        decisions,
        0.0,
        usd,
        "admitted" if admitted else f"refused after {used} round(s): {reason}",
        admissions=admissions,
        questions=questions,
    )
    if admitted and contract is not None and draft is not None:
        adm = admissions[-1]
        contract = dict(contract)
        contract["provenance"] = provenance(
            draft, adm, used, describe, desc, trace_path, task_path, qualifier
        )
        path = out / "contract.yaml"
        path.write_text(
            "# Drafted by a model and admitted by OpenAdapt qualification "
            "(openadapt verify draft). Runs make no model calls.\n"
            + yaml.safe_dump(contract, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        outcome.contract_path = path
    return _finish(out, started, outcome, admissions)


def read_account_problem(sandbox: SandboxConfig) -> Optional[str]:
    """Why the read-only connection can't take a whole-database snapshot,
    or None. Checked once, before any model call."""

    from openadapt_flow.verifier import BaselineUnreadable, capture_database

    try:
        capture_database(A.connection(sandbox), sandbox.store.dialect)
    except BaselineUnreadable as exc:
        return str(exc)
    return None


def _finish(
    out: Path, started: float, outcome: Outcome, admissions: list[A.Admission]
) -> Outcome:
    outcome.seconds = round(time.monotonic() - started, 1)
    (out / "report.json").write_text(
        json.dumps(
            {
                **outcome.as_dict(),
                "rounds_detail": [
                    {
                        "admitted": a.admitted,
                        "cases": len(a.cases),
                        "passed": sum(c.passed for c in a.cases),
                        "failed": [c.name for c in a.cases if not c.passed],
                        "problems": a.problems,
                        "attribution_problems": [
                            f"{x.table}.{x.column}" for x in a.attribution if x.problem
                        ],
                    }
                    for a in admissions
                ],
            },
            indent=1,
        ),
        encoding="utf-8",
    )
    return outcome


def provenance(
    draft: M.Draft,
    adm: A.Admission,
    rounds: int,
    describe: Optional[str],
    desc: I.Description,
    trace_path: str,
    task_path: str,
    qualifier: A.Qualifier,
) -> dict[str, Any]:
    faults = [c for c in adm.cases if c.group == "fault"]
    return {
        "drafted_by": {
            "model": M.MODEL,
            "response_model": draft.call.response_model if draft.call else "",
            "effort": M.EFFORT,
            "prompt_version": M.PROMPT_VERSION,
            "prompt_sha256": M.prompt_sha256(),
        },
        "inputs": {
            "description": {
                "kind": desc.kind,
                "file_sha256": _sha(Path(describe).read_bytes())
                if describe and Path(describe).is_file()
                else None,
                "text_sha256": _sha(desc.text.encode("utf-8")),
            },
            "trace_sha256": _sha(Path(trace_path).read_bytes()),
            "task_sha256": _sha(Path(task_path).read_bytes()),
        },
        "admission": {
            "suite": SUITE,
            "check_sha256": adm.check_sha256,
            "rounds": rounds,
            "admitted_at": _dt.datetime.now(_dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
            "recheck_window_tested_s": qualifier.recheck_s,
            "faults": {
                c.name: ("refuted" if c.passed else "missed")
                if c.applicable
                else f"not applicable: {c.note}"
                for c in faults
            },
            "clean_writes_confirmed": sum(
                c.passed for c in adm.cases if c.group == "clean"
            ),
            "attribution_probe_writes": sum(
                c.passed for c in adm.cases if c.group == "probe"
            ),
            "bookkeeping_confirmed_by_person": sorted(qualifier.bookkeeping),
        },
    }


def admission_problem(path: str | Path) -> Optional[str]:
    """None when the contract at ``path`` is still the check that was
    admitted; else why not."""

    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    prov = (data.get("provenance") or {}).get("admission") or {}
    if prov.get("suite") != SUITE:
        return "no admission record"
    if A.check_sha256(data) != prov.get("check_sha256"):
        return "the check changed after admission"
    return None


# -- the command ---------------------------------------------------------------


def _ask_from(answers_path: Optional[str], interactive: bool) -> Optional[Asker]:
    if answers_path:
        given = yaml.safe_load(Path(answers_path).read_text(encoding="utf-8")) or {}

        def from_file(
            question: str, columns: Sequence[str]
        ) -> Optional[Mapping[str, str]]:
            got = {c: str(given[c]) for c in columns if c in given}
            return got if len(got) == len(columns) else None

        return from_file
    if interactive and sys.stdin.isatty():

        def from_tty(
            question: str, columns: Sequence[str]
        ) -> Optional[Mapping[str, str]]:
            print(question, file=sys.stderr)
            out = {}
            for c in columns:
                out[c] = input(f"{c} [bookkeeping/business]: ").strip() or "business"
            return out

        return from_tty
    return None


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="openadapt verify draft",
        description="Draft a write contract with a model and admit it only if OpenAdapt qualifies it.",
        allow_abbrev=False,
    )
    ap.add_argument(
        "--describe",
        default="none",
        help="OpenAPI, GraphQL or FHIR CapabilityStatement file; 'db' for the sandbox's database schema; or 'none'",
    )
    ap.add_argument(
        "--trace", required=True, help="HAR file of one correct demonstration"
    )
    ap.add_argument(
        "--task",
        required=True,
        help="task.yaml: instruction and the demonstration's params",
    )
    ap.add_argument(
        "--sandbox", required=True, help="sandbox.yaml (openadapt.verify-sandbox/v1)"
    )
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument(
        "--rounds", type=int, default=MAX_ROUNDS, choices=range(1, MAX_ROUNDS + 1)
    )
    ap.add_argument(
        "--answers",
        help="answers to the one question (table.column: bookkeeping|business)",
    )
    ap.add_argument("--no-input", action="store_true", help="never ask; refuse instead")
    ap.add_argument(
        "--spend-ledger",
        default=str(Path.home() / ".openadapt" / "verify-draft" / "spend.jsonl"),
    )
    ap.add_argument("--spend-cap-usd", type=float, default=30.0)
    ap.add_argument(
        "--check",
        metavar="CONTRACT",
        help="only check that CONTRACT is still the admitted check",
    )
    args = ap.parse_args(argv)
    if args.check:
        problem = admission_problem(args.check)
        print(problem or "admitted check, unchanged")
        return 1 if problem else 0
    ledger = M.SpendLedger(Path(args.spend_ledger), args.spend_cap_usd)
    drafter = M.ClaudeDrafter(ledger, label=Path(args.out).name)
    outcome = run(
        describe=args.describe,
        trace_path=args.trace,
        task_path=args.task,
        sandbox_path=args.sandbox,
        out=Path(args.out),
        drafter=drafter,
        rounds=args.rounds,
        ask=_ask_from(args.answers, not args.no_input),
        log=lambda s: print(s, file=sys.stderr),
    )
    print(json.dumps(outcome.as_dict(), indent=1))
    return 0 if outcome.admitted else 1


if __name__ == "__main__":
    raise SystemExit(main())
