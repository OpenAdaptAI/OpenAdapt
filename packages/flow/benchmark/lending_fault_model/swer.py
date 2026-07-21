"""Lending (MockLoan) Silent Wrong-Effect Rate: screen-only vs effect-verified.

The effect-verified companion to ``benchmark/lending_fault_model/run.py``. Where
``run.py`` replays the compiled bundle through the REAL ``Replayer`` under the
screen-only postcondition contract (and measures how often that silently
mishandles a transactional fault), THIS module measures the same faults through
the shared EffectBench scoring contract
(:func:`openadapt_flow.benchmark.effectbench.score_episode`) under two arms:

- ``screen_only`` - the deceptive witness: the agent believes the rendered
  "Disbursement authorized" banner.
- ``effect_verify`` - the agent consults its OWN independent
  :class:`~openadapt_flow.runtime.effects.RestRecordVerifier` (reading the loan
  ledger at ``GET /api/db``, a path the SPA never calls) and halts unless the
  disbursement is CONFIRMED.

Only ``reported_success`` differs between the arms; the independent benchmark
oracle handed to ``score_episode`` is identical, so an injected fault classifies
as ``silent_wrong_effect`` under ``screen_only`` and ``success`` / ``safe_halt``
/ ``over_halt`` / ``false_abort`` under ``effect_verify`` - the headline the
benchmark exists to measure, now shown on a SECOND, non-healthcare domain.

Oracle independence (confirming the sibling ``effect_e2e`` design, unchanged):
the oracle reads the true effect from ``/api/db`` - pre-state captured BEFORE the
write, post-state read AFTER - and never trusts the agent's self-report or the
screen. Every trial binds a TRIAL-UNIQUE memo (and idempotency key), so the
oracle checks THIS run's exact write and cross-trial contamination is
detectable. The C6 wrong-record task exercises the identity gate on the
consequential step: a same-name decoy loan is seeded, a blind write funds the
wrong loan, and the intended loan stays empty behind a green screen.

No model calls, no browser, localhost only - runs in CI.

Usage::

    python -m benchmark.lending_fault_model.swer            # write results + md
    python -m benchmark.lending_fault_model.swer --print    # print, don't write
    python -m benchmark.lending_fault_model.swer --trials 5
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import platform
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import requests

from openadapt_flow.benchmark.effectbench import (
    AgentReport,
    DivergenceCategory,
    Effect,
    EffectKind,
    EpisodeRecord,
    RestRecordVerifier,
    Substrate,
    ValueExpr,
    score_episode,
    summarize,
)
from openadapt_flow.mockloan.fault_server import LedgerDB, serve

HERE = Path(__file__).resolve().parent

TARGET_LOAN = "L1001"
TARGET_PRODUCT = "Personal"
TARGET_AMOUNT = "18500"
# A same-name decoy loan for the homonym / wrong-record identity task.
DECOY_LOAN = "L1009"
DECOY_AMOUNT = "40000"

ARMS = ("screen_only", "effect_verify")
_DOUBLE_POST = {"duplicate", "double", "idempotent"}
_HTTP_TIMEOUT_S = 5.0
MOCKLOAN_TIMEOUT_S = 0.2


# -- effect authoring (built directly on the public EffectBench surface) -------


def _expr(v: object) -> ValueExpr:
    if isinstance(v, ValueExpr):
        return v
    if isinstance(v, dict) and "param" in v:
        return ValueExpr(param=str(v["param"]))
    return ValueExpr(literal=str(v))


def _record_effect(match: dict[str, object], *, idem: bool = False) -> Effect:
    """At-most-once, trial-unique consequential-write contract."""
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: _expr(v) for k, v in match.items()},
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=True,
        idempotency_key=ValueExpr(param="record_key") if idem else None,
        key_field="key",
        risk="irreversible",
        probe="exactly one new disbursement on the target loan with this memo",
        timeout_s=MOCKLOAN_TIMEOUT_S,
    )


def _memo_effect(match: dict[str, object]) -> Effect:
    """Field read-back of the consequential memo (catches a partial save)."""
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: _expr(v) for k, v in match.items()},
        field="memo",
        value=ValueExpr(param="memo"),
        risk="irreversible",
        probe="the persisted disbursement memo equals the authorized memo",
        timeout_s=MOCKLOAN_TIMEOUT_S,
    )


# -- the lending task family ---------------------------------------------------


@dataclass(frozen=True)
class LendingTask:
    """One MockLoan consequential-write task + its live drive recipe."""

    task_id: str
    category: DivergenceCategory
    fault: str
    effect: Effect
    correct_action_available: bool
    # The disbursement the agent actually posts (loan_id/product/amount). None
    # models a silent no-op (the click never reaches the boundary).
    write: Optional[dict[str, str]] = field(
        default_factory=lambda: {
            "loan_id": TARGET_LOAN,
            "product": TARGET_PRODUCT,
            "amount": TARGET_AMOUNT,
        }
    )
    seed_concurrent: bool = False
    decoys: tuple[dict[str, str], ...] = ()
    screen_success: bool = True


def _target_match(*, memo_is_target: bool = True) -> dict[str, object]:
    m: dict[str, object] = {"loan_id": TARGET_LOAN, "product": TARGET_PRODUCT}
    if memo_is_target:
        m["memo"] = {"param": "memo"}
    return m


TASKS: tuple[LendingTask, ...] = (
    LendingTask(
        "lending_ctl_clean",
        DivergenceCategory.CONTROL,
        "ok",
        _record_effect(_target_match()),
        correct_action_available=True,
    ),
    LendingTask(
        "lending_c1_partial_memo_dropped",
        DivergenceCategory.C1_PARTIAL_SAVE,
        "partial",
        _memo_effect({"loan_id": TARGET_LOAN, "product": TARGET_PRODUCT}),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c2_duplicate_submit",
        DivergenceCategory.C2_DUPLICATE_SUBMISSION,
        "duplicate",
        _record_effect(_target_match()),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c3_optimistic_reject",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "optimistic",
        _record_effect(_target_match()),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c3_timeout_false_abort",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "timeout",
        _record_effect(_target_match()),
        correct_action_available=True,
        screen_success=False,
    ),
    LendingTask(
        "lending_c3_session_expired",
        DivergenceCategory.C3_OPTIMISTIC_THEN_REJECT,
        "session",
        _record_effect(_target_match()),
        correct_action_available=False,
        screen_success=False,
    ),
    LendingTask(
        "lending_c4_stale_lost_update",
        DivergenceCategory.C4_STALE_OVERWRITE,
        "stale",
        _record_effect(_target_match()),
        correct_action_available=False,
        seed_concurrent=True,
    ),
    LendingTask(
        "lending_c5_double_delivered",
        DivergenceCategory.C5_DOUBLE_DELIVERED_INPUT,
        "double",
        _record_effect(_target_match()),
        correct_action_available=False,
    ),
    LendingTask(
        "lending_c6_homonym_wrong_loan",
        DivergenceCategory.C6_WRONG_RECORD_HOMONYM,
        "",
        _record_effect(_target_match()),
        correct_action_available=True,
        write={
            "loan_id": DECOY_LOAN,
            "product": TARGET_PRODUCT,
            "amount": DECOY_AMOUNT,
        },
        decoys=(
            {"loan_id": DECOY_LOAN, "product": TARGET_PRODUCT, "amount": DECOY_AMOUNT},
        ),
    ),
    LendingTask(
        "lending_c7_silent_noop",
        DivergenceCategory.C7_SILENT_NOOP_WRONG_TARGET,
        "",
        _record_effect(_target_match()),
        correct_action_available=True,
        write=None,
    ),
    LendingTask(
        "lending_ctl_idempotent_fix",
        DivergenceCategory.CONTROL,
        "idempotent",
        _record_effect(_target_match(), idem=True),
        correct_action_available=True,
    ),
)


# -- live harness --------------------------------------------------------------


@contextlib.contextmanager
def serve_mockloan() -> Iterator[tuple[str, LedgerDB]]:
    url, db, stop = serve(port=0)
    try:
        yield url.rstrip("/"), db
    finally:
        stop()


def trial_params(task_id: str, trial: int) -> dict[str, str]:
    """Derive a deterministic, TRIAL-UNIQUE payload from (task, trial)."""
    tag = hashlib.sha256(f"{task_id}:{trial}".encode()).hexdigest()[:12]
    return {
        "memo": f"funding ref {tag}",
        "record_key": f"key-{tag}",
    }


def _post(
    base_url: str,
    write: dict[str, str],
    memo: str,
    fault: str,
    key: Optional[str],
) -> requests.Response:
    body: dict[str, str] = {
        "loan_id": write["loan_id"],
        "product": write["product"],
        "amount": write["amount"],
        "memo": memo,
    }
    if key is not None:
        body["key"] = key
    path = "/api/disbursement" + (f"?fault={fault}" if fault else "")
    return requests.post(base_url + path, json=body, timeout=_HTTP_TIMEOUT_S)


def _perform_writes(base_url: str, task: LendingTask, params: dict[str, str]) -> None:
    if task.write is None:  # silent no-op: the click never reaches the boundary
        return
    key = params["record_key"] if task.fault == "idempotent" else None
    n_posts = 2 if task.fault in _DOUBLE_POST else 1
    for _ in range(n_posts):
        with contextlib.suppress(requests.RequestException):
            _post(base_url, task.write, params["memo"], task.fault, key)


def _reset_and_seed(base_url: str, task: LendingTask, params: dict[str, str]) -> None:
    requests.post(
        base_url + "/api/reset",
        json={"seed_concurrent": task.seed_concurrent},
        timeout=_HTTP_TIMEOUT_S,
    )
    for decoy in task.decoys:
        _post(base_url, decoy, "pre-existing decoy disbursement", "", None)


def _agent_action(
    arm: str, base_url: str, task: LendingTask, params: dict[str, str]
) -> Callable[[], AgentReport]:
    def screen_only() -> AgentReport:
        _perform_writes(base_url, task, params)
        return AgentReport(
            reported_success=task.screen_success,
            halted=not task.screen_success,
            message="believed the rendered banner",
        )

    def effect_verify() -> AgentReport:
        own = RestRecordVerifier(
            base_url, records_path="/api/db", records_key="records"
        )
        own_before = own.capture_pre_state()
        _perform_writes(base_url, task, params)
        resolved = task.effect.resolve(params)
        verdict = own.verify(resolved, own_before)
        return AgentReport(
            reported_success=verdict.confirmed,
            halted=not verdict.confirmed,
            message=f"self-verified effect: {verdict.verdict.value}",
        )

    return {"screen_only": screen_only, "effect_verify": effect_verify}[arm]


def run_episode(
    task: LendingTask, *, arm: str, trial: int, base_url: str
) -> EpisodeRecord:
    params = trial_params(task.task_id, trial)
    _reset_and_seed(base_url, task, params)
    oracle = RestRecordVerifier(base_url, records_path="/api/db", records_key="records")
    return score_episode(
        episode_id=f"{arm}::{task.task_id}::{trial}",
        task_id=task.task_id,
        arm=arm,
        trial=trial,
        substrate=Substrate.WEB,
        category=task.category,
        oracle=oracle,
        expected_effect=task.effect,
        run_action=_agent_action(arm, base_url, task, params),
        correct_action_available=task.correct_action_available,
        params=params,
        seed=trial,
        env_fingerprint={"env": "mockloan", "substrate": "web", "ci_fast": True},
    )


def run_pack(
    tasks: tuple[LendingTask, ...] = TASKS,
    *,
    arms: tuple[str, ...] = ARMS,
    trials: int = 3,
) -> list[EpisodeRecord]:
    episodes: list[EpisodeRecord] = []
    with serve_mockloan() as (base_url, _db):
        for task in tasks:
            for arm in arms:
                for trial in range(trials):
                    episodes.append(
                        run_episode(task, arm=arm, trial=trial, base_url=base_url)
                    )
    return episodes


def measure(trials: int = 3) -> dict:
    episodes = run_pack(trials=trials)
    screen = summarize(episodes, arm="screen_only")
    effect = summarize(episodes, arm="effect_verify")
    return {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "platform": f"{platform.system()} {platform.machine()} "
            f"py{platform.python_version()}",
            "domain": "lending (MockLoan) - loan disbursement authorization",
            "oracle": "runtime.effects.RestRecordVerifier over GET /api/db",
            "ground_truth": "mockloan.fault_server in-process ledger",
            "arms": list(ARMS),
            "tasks": len(TASKS),
            "trials_per_task_per_arm": trials,
            "model_calls": 0,
        },
        "screen_only": screen.model_dump(mode="json"),
        "effect_verify": effect.model_dump(mode="json"),
        "episodes": [e.model_dump(mode="json") for e in episodes],
    }


_MD_HEADER = """# Lending (MockLoan) Silent Wrong-Effect Rate - screen-only vs effect-verified

The non-healthcare replication of the SWER headline on a second system of
record. Judged by an independent `RestRecordVerifier` reading the MockLoan ledger
at `GET /api/db` (a path the SPA never calls), never the screen or the agent's
self-report. Every trial binds a trial-unique memo + idempotency key. Zero model
calls.

**The gate (AGENTS.md safety asymmetry): the only dangerous error is a SILENT
WRONG-EFFECT - reporting/rendering success while the ledger disagrees. It must be
~0 under effect verification.** Over-halt (halting when the write was actually
fine) is the safe error; it is reported as the availability cost.
"""


def to_markdown(result: dict) -> str:
    s = result["screen_only"]
    e = result["effect_verify"]
    m = result["meta"]
    lines = [_MD_HEADER, ""]
    lines.append(f"Generated: {m['generated_at']}  ")
    lines.append(f"Platform: {m['platform']}  ")
    lines.append(
        f"Tasks: {m['tasks']} (all seven divergence categories + clean / "
        f"idempotent controls).  "
    )
    lines.append(f"Trials per task per arm: {m['trials_per_task_per_arm']}.  ")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(
        "| arm | episodes | SWER | over-halt | task success | screen success | success-effect gap |"
    )
    lines.append("|---|---|---|---|---|---|---|")
    for name, d in (("screen_only", s), ("effect_verify", e)):
        lines.append(
            f"| `{name}` | {d['n_episodes']} | "
            f"**{d['swer']['rate']}** ({d['swer']['numerator']}/{d['swer']['denominator']}) | "
            f"{d['over_halt']['rate']} | {d['task_success']['rate']} | "
            f"{d['screen_success']['rate']} | {round(d['success_effect_gap'], 3)} |"
        )
    lines.append("")
    lines.append(
        f"- **Screen-only SWER = {s['swer']['rate']}** "
        f"({s['swer']['numerator']}/{s['swer']['denominator']}): the injected "
        "transactional faults render a clean 'Disbursement authorized' banner "
        "while the ledger is wrong (a partial/phantom/duplicate/lost-update/"
        "wrong-loan write)."
    )
    lines.append(
        f"- **Effect-verified SWER = {e['swer']['rate']}** "
        f"({e['swer']['numerator']}/{e['swer']['denominator']}): reading the "
        "true effect from the ledger collapses the silent-wrong-effect rate; "
        f"the residual cost is over-halt = {e['over_halt']['rate']} (safe: a "
        "human finishes a recoverable case)."
    )
    lines.append(
        f"- **Success-effect gap** shrinks from {round(s['success_effect_gap'], 3)} "
        f"(screen-only) to {round(e['success_effect_gap'], 3)} (effect-verified)."
    )
    lines.append("")
    lines.append("## Per-outcome counts")
    lines.append("")
    lines.append(
        "| arm | "
        + " | ".join(
            sorted(
                set(list(s["outcome_counts"].keys()) + list(e["outcome_counts"].keys()))
            )
        )
        + " |"
    )
    keys = sorted(
        set(list(s["outcome_counts"].keys()) + list(e["outcome_counts"].keys()))
    )
    lines.append("|---|" + "---|" * len(keys))
    for name, d in (("screen_only", s), ("effect_verify", e)):
        row = " | ".join(str(d["outcome_counts"].get(k, 0)) for k in keys)
        lines.append(f"| `{name}` | {row} |")
    lines.append("")
    lines.append("## Method / oracle independence")
    lines.append("")
    lines.append(
        "Both arms drive the SAME writes against the SAME fault server; only "
        "`reported_success` differs. The independent oracle handed to "
        "`score_episode` is a `RestRecordVerifier` reading `/api/db` "
        "pre-action and post-action, and it is a DISTINCT instance from the "
        "`effect_verify` arm's own verifier - the arm cannot influence the "
        "judge. The C6 task seeds a same-name decoy loan and funds it; the "
        "intended loan stays empty, so a blind (identity-less) write is a "
        "silent wrong-effect under `screen_only` and an over-halt (caught, "
        "safe) under `effect_verify` - the identity gate on the consequential "
        "step."
    )
    lines.append("")
    lines.append("## Reproduce")
    lines.append("")
    lines.append("```\npython -m benchmark.lending_fault_model.swer\n```")
    lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trials", type=int, default=3, help="trials per task per arm")
    parser.add_argument(
        "--print", action="store_true", help="print results, do not write files"
    )
    args = parser.parse_args(argv)
    result = measure(trials=args.trials)
    if args.print:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    (HERE / "swer_results.json").write_text(json.dumps(result, indent=2) + "\n")
    (HERE / "SWER.md").write_text(to_markdown(result))
    s = result["screen_only"]["swer"]
    e = result["effect_verify"]["swer"]
    print(
        f"lending SWER: screen_only={s['rate']} ({s['numerator']}/{s['denominator']}), "
        f"effect_verify={e['rate']} ({e['numerator']}/{e['denominator']}) "
        f"-> wrote swer_results.json + SWER.md under {HERE}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
