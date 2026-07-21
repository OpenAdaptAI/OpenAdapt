"""End-to-end silent-wrong-effect (SWER) harness through the REAL replayer.

This is the genuinely independent, end-to-end version of the silent-wrong-action
result. The older ``openadapt_flow.benchmark.silent_wrong_action`` is
*definitional*: it issues raw ``requests.post`` (bypassing the
:class:`~openadapt_flow.runtime.replayer.Replayer`), and its effect verifier and
ground-truth judge both read the SAME in-process ``FaultDB`` object while the
effect contract restates the ground-truth definition of "correct" -- so its
``0/90`` is guaranteed by construction (circular).

This harness removes every leg of that circularity. It drives a compiled
one-step workflow through the ACTUAL governed replay/actuation path (the
replayer's ``api`` tier -> :class:`~openadapt_flow.runtime.actuators.ApiActuator`
-> a real HTTP write), and keeps THREE data-access mechanisms genuinely distinct
(see ``benchmark/effect_e2e/record_service.py`` and ``.../ground_truth.py``):

- **(a) WRITE** -- the workflow's actuation: ``POST /api/encounter`` issued by
  the replayer's ApiActuator, mutating an on-disk SQLite database.
- **(b) EFFECT VERIFIER read-back** -- an out-of-band oracle reading the record
  through a DIFFERENT interface/connection than the write: ``GET /api/records``
  (a different HTTP verb + endpoint + connection), consumed by the replayer's
  ``effect_verifier`` to CONFIRM / REFUTE / HALT. Never sees the write's reply.
- **(c) GROUND TRUTH** -- an independent judge that opens the SQLite FILE
  directly (read-only, its own connection), inspects EVERY table, and
  classifies with its OWN logic (it does not restate the verifier's contract).

Why each is independent: the write and the verifier both traverse the service
but via different HTTP methods, handlers, and connections; the ground truth
bypasses the service entirely and reads storage directly, so a bug or lie in
the service's read handler cannot fool it, and the write's HTTP success flag
never reaches either the verifier or the ground truth.

We inject the real SWER fault modes at the persistence/actuation boundary and
run each fault END-TO-END under three arms, all through the same replayer:

- ``screen``      -- the weak oracle: reads the app's SELF-REPORTED banner
  (``GET /api/ui/last-save``), the surface a vision postcondition reads.
- ``effect_rest`` -- the DEFAULT out-of-band record oracle: reads the
  encounters surface (``GET /api/records``).
- ``effect_full`` -- effect verification with a COMPLETE read path: read-only
  SQL over every mutable surface (encounters AND billing), which closes the one
  structural gap ``effect_rest`` has.

We report the REAL end-to-end SWER for each arm, judged by the independent
ground truth. If effect verification does not catch everything, we say so:
the collateral write to an unaudited surface slips the encounters-scoped record
oracle, because an out-of-band oracle catches exactly what its read path covers.

No model calls, localhost only, ``$0`` -- runs in CI.

Usage::

    python -m benchmark.effect_e2e.run                 # write results.json + md
    python -m benchmark.effect_e2e.run --n 9           # 9 trials / fault (default)
    python -m benchmark.effect_e2e.run --print         # print, do not write files
"""

from __future__ import annotations

import argparse
import json
import platform
import sqlite3
import struct
import sys
import tempfile
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

import requests

from benchmark.effect_e2e import ground_truth
from benchmark.effect_e2e.record_service import (
    TARGET_PATIENT,
    TARGET_TYPE,
    serve,
)
from benchmark.effect_e2e.verifiers import CompositeSqlVerifier
from openadapt_flow.ir import (
    ActionKind,
    ApiBinding,
    Postcondition,
    PostconditionKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.actuators import ApiActuator
from openadapt_flow.runtime.effects import (
    Effect,
    EffectKind,
    RestRecordVerifier,
    ValueExpr,
)
from openadapt_flow.runtime.effects.sql import SqlRecordVerifier
from openadapt_flow.runtime.replayer import Replayer

HERE = Path(__file__).resolve().parent

#: This run's consequential free-text. A partial save drops it, so field_equals
#: catches that; the value is bound as a run param so the write and the
#: verification target the SAME text.
NOTE = "End-to-end SWER benchmark triage note"

#: The API actuator's per-request timeout. Shorter than the service's
#: ``timeout`` hang, so a committed-then-timed-out write surfaces as an unknown
#: outcome the governed actuator HALTs on (never double-written).
ACTUATOR_TIMEOUT_S = 0.6
#: How long a verifier may poll the system of record per effect. The write is
#: synchronous through the ApiActuator (the record is settled before we verify),
#: so a short deadline is not a source of flakiness -- a REFUTED verdict polls
#: to the deadline, so keeping it small keeps the suite fast.
EFFECT_TIMEOUT_S = 0.3

DEFAULT_N = 9
DEFAULT_OUT_DIR = HERE

ARMS = ("screen", "effect_rest", "effect_full")


@dataclass(frozen=True)
class Scenario:
    """One transactional-fault scenario driven at the persistence boundary."""

    name: str
    seed_concurrent: bool
    blurb: str


#: The fault suite. The first block is the clean control; the middle block is
#: the 2xx-but-wrong classes an out-of-band effect oracle must catch and a
#: screen oracle cannot; the last block is the transport/rejection classes the
#: actuation layer's no-double-write contract handles in BOTH arms.
SCENARIOS: tuple[Scenario, ...] = (
    Scenario("ok", False, "control: a clean accepted write"),
    Scenario("no_persist", False, "2xx banner but nothing persisted (phantom write)"),
    Scenario("partial", False, "row persisted but the note field was dropped"),
    Scenario("duplicate", False, "double-delivered write: the row landed twice"),
    Scenario("wrong_record", False, "the write landed on the wrong patient (p2)"),
    Scenario("stale", True, "last-write-wins destroyed a concurrent actor's row"),
    Scenario(
        "collateral_unaudited",
        False,
        "target row correct BUT a stray row hit an unaudited (billing) surface",
    ),
    Scenario("optimistic", False, "server rejected (409) after the optimistic UI"),
    Scenario("session", False, "session expired (401): nothing persisted"),
    Scenario("timeout", False, "row committed, then the client timed out (unknown)"),
)


# -- minimal, self-contained replayer fakes ---------------------------------
# The api tier returns before any GUI resolve/act, so these only need to satisfy
# the replayer's construction and the (unreached) settle path. Kept local so the
# harness does not depend on the test package.


def _tiny_png() -> bytes:
    """A valid 1x1 PNG (the api-tier path never renders it, but be safe)."""
    raw = b"\x00\xff\xff\xff"  # one white pixel, filter byte 0
    idat = zlib.compress(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


_PNG = _tiny_png()
_VIEWPORT = (2, 2)


class _NullVision:
    def wait_settled(self, backend, **_kw: Any) -> bytes:
        return backend.screenshot()

    def text_present(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def find_text(self, *_a: Any, **_kw: Any) -> None:
        return None

    def find_template(self, *_a: Any, **_kw: Any) -> None:
        return None

    def find_structural_template(self, *_a: Any, **_kw: Any) -> None:
        return None

    def ocr(self, *_a: Any, **_kw: Any) -> list:
        return []

    def pixels_changed(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def phash_png(self, *_a: Any, **_kw: Any) -> str:
        return "aa"

    def phash_distance(self, *_a: Any, **_kw: Any) -> int:
        return 0


class _NullBackend:
    @property
    def viewport(self):
        return _VIEWPORT

    def screenshot(self) -> bytes:
        return _PNG

    def click(self, *_a: Any, **_kw: Any) -> None:
        pass

    def type_text(self, *_a: Any, **_kw: Any) -> None:
        pass

    def press(self, *_a: Any, **_kw: Any) -> None:
        pass

    def scroll(self, *_a: Any, **_kw: Any) -> None:
        pass


# -- effects and workflow ---------------------------------------------------


def _base_effects() -> list[Effect]:
    """The record + field contract every arm verifies on the target surface."""
    match = {
        "patient_id": ValueExpr(literal=TARGET_PATIENT),
        "type": ValueExpr(literal=TARGET_TYPE),
    }
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            forbid_collateral_loss=True,
            risk="irreversible",
            probe="surface=encounters|exactly one target encounter",
            timeout_s=EFFECT_TIMEOUT_S,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=match,
            field="note",
            value=ValueExpr(param="note"),
            risk="irreversible",
            probe="surface=encounters|the note read-back",
            timeout_s=EFFECT_TIMEOUT_S,
        ),
    ]


def _billing_guard_effect() -> Effect:
    """A guard: no NEW row may appear on the billing surface (the collateral
    surface the encounters-scoped record oracle cannot see)."""
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={},  # match every billing row; count_new_only makes it a delta
        expected_count=0,
        count_new_only=True,
        forbid_collateral_loss=False,
        risk="irreversible",
        probe="surface=billing|no collateral billing write",
        timeout_s=EFFECT_TIMEOUT_S,
    )


def _workflow(arm: str, fault: str) -> Workflow:
    """A one-step workflow carrying an ApiBinding so the replayer's api tier
    performs the write. ``effect_full`` also carries the billing guard."""
    effects = _base_effects()
    if arm == "effect_full":
        effects = effects + [_billing_guard_effect()]
    return Workflow(
        name=f"effect-e2e-{arm}-{fault}",
        steps=[
            Step(
                id="save",
                intent="save encounter",
                action=ActionKind.KEY,
                key="Enter",
                expect=[
                    Postcondition(
                        kind=PostconditionKind.TEXT_PRESENT,
                        text="Saved",
                        timeout_s=0.1,
                    )
                ],
                risk="irreversible",
                effects=effects,
                api_binding=ApiBinding(
                    method="POST",
                    url_template=f"/api/encounter?fault={fault}",
                    body_template={
                        "patient_id": TARGET_PATIENT,
                        "type": TARGET_TYPE,
                        "note": "{note}",
                    },
                    timeout_s=ACTUATOR_TIMEOUT_S,
                ),
            )
        ],
        params={"note": NOTE},
    )


def _build_verifier(arm: str, base_url: str, db_path: Path) -> Any:
    """The arm's effect verifier -- what the replayer HALTs (or not) against."""
    if arm == "screen":
        # The app's self-reported banner: the weak, vision-style oracle.
        return RestRecordVerifier(
            base_url,
            records_path="/api/ui/last-save",
            records_key="records",
            timeout_s=EFFECT_TIMEOUT_S,
            poll_interval_s=0.02,
        )
    if arm == "effect_rest":
        # Out-of-band record oracle over the encounters surface only.
        return RestRecordVerifier(
            base_url,
            records_path="/api/records",
            records_key="records",
            timeout_s=EFFECT_TIMEOUT_S,
            poll_interval_s=0.02,
        )

    # effect_full: read-only SQL over EVERY mutable surface, each on its own
    # connection (mode=ro is the defense-in-depth read-only role for the study).
    def _connect_ro() -> sqlite3.Connection:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)

    return CompositeSqlVerifier(
        {
            "encounters": SqlRecordVerifier(
                _connect_ro,
                "SELECT id, patient_id, type, note, key FROM encounters",
                timeout_s=EFFECT_TIMEOUT_S,
                poll_interval_s=0.02,
            ),
            "billing": SqlRecordVerifier(
                _connect_ro,
                "SELECT id, patient_id, amount FROM billing",
                timeout_s=EFFECT_TIMEOUT_S,
                poll_interval_s=0.02,
            ),
        },
        default_surface="encounters",
    )


# -- one end-to-end run -----------------------------------------------------


def run_one(
    base_url: str,
    db_path: Path,
    arm: str,
    scenario: Scenario,
    *,
    work_root: Path,
    index: int,
) -> dict[str, Any]:
    """Drive one (arm x scenario) END-TO-END through the real replayer.

    Returns the run row: what the arm decided (halted / reported success) and
    the INDEPENDENT ground-truth verdict on what actually persisted.
    """
    # Setup (NOT the scored action): reset the system of record and seed a
    # concurrent actor's row where the scenario needs one.
    requests.post(
        f"{base_url}/api/reset",
        json={"seed_concurrent": scenario.seed_concurrent},
        timeout=5.0,
    )

    # Ground-truth pre-snapshot: read the FILE directly (independent of the
    # service and of either oracle).
    before_gt = ground_truth.capture(db_path)

    workflow = _workflow(arm, scenario.name)
    verifier = _build_verifier(arm, base_url, db_path)
    run_dir = work_root / f"{arm}-{scenario.name}-{index}"
    bundle_dir = run_dir / "bundle"
    (bundle_dir / "templates").mkdir(parents=True, exist_ok=True)

    replayer = Replayer(
        _NullBackend(),
        vision=_NullVision(),
        effect_verifier=verifier,
        api_actuator=ApiActuator(base_url, timeout_s=ACTUATOR_TIMEOUT_S),
        poll_interval_s=0.01,
    )
    report = replayer.run(workflow, bundle_dir=bundle_dir, run_dir=run_dir / "out")

    # Ground-truth post-snapshot + verdict (independent classifier + delta audit).
    after_gt = ground_truth.capture(db_path)
    gt = ground_truth.judge(before_gt, after_gt, intended_note=NOTE)

    reported_success = bool(report.success)
    halted = not reported_success
    step = report.results[0] if report.results else None
    return {
        "arm": arm,
        "scenario": scenario.name,
        "blurb": scenario.blurb,
        "i": index,
        "reported_success": reported_success,
        "halted": halted,
        "actuation": getattr(step, "actuation", None) if step else None,
        "effect_verified": getattr(step, "effect_verified", None) if step else None,
        "error": (getattr(step, "error", None) if step else None),
        "gt_correct": gt.correct,
        "gt_fault": gt.fault_class,
        "gt_detail": gt.detail,
        "table_deltas": gt.table_deltas,
        # Derived, judged by the INDEPENDENT ground truth (never self-report):
        "silent_wrong": (not gt.correct) and reported_success,
        "caught": (not gt.correct) and halted,
        "false_abort": gt.correct and halted,
        "clean_success": gt.correct and reported_success,
    }


# -- aggregation ------------------------------------------------------------


def _all_same(rows: list[dict[str, Any]], key: str) -> Any:
    values = {r[key] for r in rows}
    if len(values) == 1:
        return next(iter(values))
    return "MIXED:" + ",".join(str(v) for v in sorted(values, key=str))


def _rate(rows: list[dict[str, Any]], key: str) -> float:
    return (sum(1 for r in rows if r[key]) / len(rows)) if rows else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the headline SWER metrics per arm and the per-scenario breakdown."""
    per_arm: dict[str, Any] = {}
    for arm in ARMS:
        arm_rows = [r for r in rows if r["arm"] == arm]
        wrong_rows = [r for r in arm_rows if not r["gt_correct"]]
        correct_rows = [r for r in arm_rows if r["gt_correct"]]
        silent = [r for r in arm_rows if r["silent_wrong"]]
        per_scenario: dict[str, Any] = {}
        for sc in SCENARIOS:
            srows = [r for r in arm_rows if r["scenario"] == sc.name]
            if not srows:
                continue
            per_scenario[sc.name] = {
                "n": len(srows),
                "gt_correct": _all_same(srows, "gt_correct"),
                "gt_fault": _all_same(srows, "gt_fault"),
                "halted": _all_same(srows, "halted"),
                "silent_wrong_rate": _rate(srows, "silent_wrong"),
            }
        per_arm[arm] = {
            "n_runs": len(arm_rows),
            "n_wrong_effect": len(wrong_rows),
            "n_correct_effect": len(correct_rows),
            "silent_wrong_count": len(silent),
            "silent_wrong_action_rate": _rate(arm_rows, "silent_wrong"),
            "undetected_wrong_rate": (
                len(silent) / len(wrong_rows) if wrong_rows else 0.0
            ),
            "caught_count": sum(1 for r in arm_rows if r["caught"]),
            "false_abort_count": sum(1 for r in arm_rows if r["false_abort"]),
            "false_abort_rate": (
                sum(1 for r in correct_rows if r["false_abort"]) / len(correct_rows)
                if correct_rows
                else 0.0
            ),
            "silent_wrong_scenarios": sorted({r["scenario"] for r in silent}),
            "per_scenario": per_scenario,
        }
    return {"per_arm": per_arm}


def run_benchmark(
    n: int = DEFAULT_N, *, log: Callable[[str], None] = print
) -> dict[str, Any]:
    """Run every (arm x scenario) ``n`` times end-to-end and assemble results."""
    with tempfile.TemporaryDirectory(prefix="effect-e2e-") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "record.db"
        handle = serve(db_path)
        rows: list[dict[str, Any]] = []
        try:
            for arm in ARMS:
                for sc in SCENARIOS:
                    for i in range(n):
                        rows.append(
                            run_one(
                                handle.base_url,
                                db_path,
                                arm,
                                sc,
                                work_root=tmp_path,
                                index=i,
                            )
                        )
                    last = rows[-1]
                    log(
                        f"{arm:12s} {sc.name:20s} gt={last['gt_fault']:16s} "
                        f"halted={last['halted']!s:5s} "
                        f"silent_wrong={last['silent_wrong']}"
                    )
        finally:
            handle.stop()

    metrics = aggregate(rows)
    screen = metrics["per_arm"]["screen"]
    rest = metrics["per_arm"]["effect_rest"]
    return {
        "instrument": "silent-wrong-effect rate, end-to-end through the replayer",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "model_calls": 0,
        "n_per_scenario": n,
        "arms": list(ARMS),
        "scenarios": [sc.name for sc in SCENARIOS],
        "independence": {
            "write_path": "POST /api/encounter via the replayer's ApiActuator "
            "(governed actuation) -> on-disk SQLite mutation",
            "verifier_readback_path": "GET /api/records (screen arm: GET "
            "/api/ui/last-save) -- a different HTTP verb/endpoint/connection "
            "than the write; the replayer's effect_verifier CONFIRMS/REFUTES/HALTs",
            "ground_truth_path": "direct read-only SQLite file connection over "
            "every persisted system-of-record table (discovered dynamically from "
            "sqlite_master, not a hardcoded pair; the UI-echo banner excluded), "
            "with an independent classifier and its OWN per-table delta audit "
            "(NOT the effect kit's audit_table_deltas) -- bypasses the service; "
            "the write's success flag never reaches it",
        },
        "headline": {
            "screen_silent_wrong": screen["silent_wrong_count"],
            "screen_n_runs": screen["n_runs"],
            "effect_rest_silent_wrong": rest["silent_wrong_count"],
            "effect_rest_n_runs": rest["n_runs"],
            "effect_full_silent_wrong": metrics["per_arm"]["effect_full"][
                "silent_wrong_count"
            ],
            "effect_full_n_runs": metrics["per_arm"]["effect_full"]["n_runs"],
            "slips_through_effect_rest": rest["silent_wrong_scenarios"],
        },
        "metrics": metrics,
        "runs": rows,
    }


# -- rendering --------------------------------------------------------------


def _arm_label(arm: str) -> str:
    return {
        "screen": "screen-verify (banner)",
        "effect_rest": "effect-verify (REST record oracle)",
        "effect_full": "effect-verify (complete SQL read path)",
    }[arm]


def render_markdown(results: dict[str, Any]) -> str:
    m = results["metrics"]["per_arm"]
    date = results["generated_at"][:10]
    n = results["n_per_scenario"]
    ind = results["independence"]

    headline_rows = ""
    for arm in ARMS:
        a = m[arm]
        headline_rows += (
            f"| {_arm_label(arm)} | "
            f"**{a['silent_wrong_action_rate']:.1%}** "
            f"({a['silent_wrong_count']}/{a['n_runs']}) | "
            f"{a['undetected_wrong_rate']:.1%} | "
            f"{a['caught_count']} | "
            f"{a['false_abort_rate']:.1%} ({a['false_abort_count']}) |\n"
        )

    # Per-scenario matrix (one row per fault; a column per arm).
    matrix = (
        "| scenario | ground-truth effect | "
        + " | ".join(_arm_label(a) for a in ARMS)
        + " |\n|---|---|"
        + "|".join(["---"] * len(ARMS))
        + "|\n"
    )
    for sc in results["scenarios"]:
        ref = m["effect_rest"]["per_scenario"][sc]
        gt = "correct" if ref["gt_correct"] is True else f"WRONG ({ref['gt_fault']})"
        cells = ""
        for arm in ARMS:
            ps = m[arm]["per_scenario"][sc]
            if ps["silent_wrong_rate"]:
                verdict = "SILENT WRONG"
            elif ps["gt_correct"] is True and ps["halted"] is True:
                verdict = "false-abort"
            elif ps["gt_correct"] is True:
                verdict = "clean pass"
            elif ps["halted"] is True:
                verdict = "caught (halt)"
            else:
                verdict = str(ps["halted"])
            cells += f" {verdict} |"
        matrix += f"| `{sc}` | {gt} |{cells}\n"

    rest = m["effect_rest"]
    slips = rest["silent_wrong_scenarios"]
    slips_txt = ", ".join(f"`{s}`" for s in slips) if slips else "none"

    return f"""# End-to-end silent-wrong-effect rate (through the real replayer)

Date: {date}. This is the genuinely independent, end-to-end measurement of the
silent-wrong-effect (SWER) result. Unlike the definitional
`openadapt_flow.benchmark.silent_wrong_action` (raw `requests.post`; effect
verifier and ground truth read the SAME in-process object; the effect contract
restates the ground-truth definition -- so its `0/90` is circular by
construction), every write below goes through the ACTUAL governed replay path
(`Replayer` -> `ApiActuator` -> a real HTTP write to an on-disk SQLite system of
record), and the three judgments come from three genuinely distinct paths.
{n} trials per fault per arm. Zero model calls, localhost only.

## The three independent paths (why this is not circular)

1. **WRITE** -- {ind["write_path"]}.
2. **EFFECT VERIFIER read-back** -- {ind["verifier_readback_path"]}.
3. **GROUND TRUTH** -- {ind["ground_truth_path"]}.

The write and the verifier both traverse the record service but via different
HTTP methods, handlers, and connections; the ground truth bypasses the service
entirely and reads storage directly, so a bug or lie in the service's read
handler cannot fool it, and the write's HTTP success flag never reaches either
the verifier or the ground truth. The verifier evaluates typed `Effect`
contracts via `judge_records`; the ground truth uses its own before/after row
classifier plus its OWN per-table delta audit (NOT the effect kit's
`audit_table_deltas`) -- a different code path that does not restate the
verifier's contract. The ground truth audits every persisted system-of-record
table it discovers in the database (from `sqlite_master`), not a hardcoded pair,
excluding only the app's own `banner` echo (the surface the screen oracle reads).

## Headline (measured end-to-end)

These are counts over a small, hand-authored, DETERMINISTIC fault taxonomy
({n} identical replays per fault per arm, variance approx. 0), not a sampled
population rate -- read the table as a coverage matrix, not an incidence
estimate (see "How to read these numbers" below).

| arm | silent-wrong-effect rate | undetected-wrong rate | wrong effects caught | false-abort rate |
|---|---|---|---|---|
{headline_rows}
- **silent-wrong-effect rate** = fraction of ALL runs where the independent
  ground truth says a WRONG effect persisted AND the arm still reported success
  (the wrong write would go undetected).
- **undetected-wrong rate** = P[reported success | a wrong effect actually
  occurred] -- the apples-to-apples oracle comparison.
- **false-abort rate** = P[the arm halted | the effect was actually correct].

**The realistic headline is the ladder, and the middle rung is the number a
real deployment ships.** A production deployment typically stands up ONE
out-of-band record oracle over the surface the workflow touches -- the
`effect-verify (REST record oracle)` arm -- and that arm's residual silent-wrong
rate is {m["effect_rest"]["silent_wrong_action_rate"]:.1%}
({m["effect_rest"]["silent_wrong_count"]}/{m["effect_rest"]["n_runs"]}), driven
entirely by the collateral-write class. The
`effect-verify (complete SQL read path)` arm reaches
{m["effect_full"]["silent_wrong_action_rate"]:.1%}
({m["effect_full"]["silent_wrong_count"]}/{m["effect_full"]["n_runs"]}) ONLY
because its read path is widened to cover every mutable surface -- the
least-realistic, most-instrumented deployment. Do not read the `0%` as the
expected field result; the honest field number is the middle rung.

## Per-fault, per-arm outcome

{matrix}
## What slips through, and why (reported honestly)

- **screen-verify** misses every 2xx-but-wrong persistence fault: the banner is
  painted regardless of what landed, so it silently accepts phantom, partial,
  duplicate, wrong-record, stale, and collateral writes.
- **effect-verify (REST record oracle)** catches the record-surface faults by
  reading the encounters record out of band -- but it silently accepts
  {slips_txt}: a collateral write to the **billing** surface its read path does
  not cover. This is not a bug in effect verification; it is the structural
  limit of an out-of-band oracle -- **it catches exactly what its read path can
  read.** The independent ground truth catches it via a full table-delta audit.
- **effect-verify (complete SQL read path)** closes that gap by auditing every
  mutable surface (read-only SQL over encounters AND billing), driving the
  end-to-end silent-wrong-effect rate to
  {m["effect_full"]["silent_wrong_action_rate"]:.1%}
  ({m["effect_full"]["silent_wrong_count"]}/{m["effect_full"]["n_runs"]}) -- but
  see the closed-world caveat immediately below: that `0` is "zero within the
  audited system of record," not an absolute zero.

The `optimistic` (409), `session` (401), and `timeout` (unknown outcome)
classes are handled by the actuation layer's no-double-write contract in BOTH
arms, before any oracle is consulted -- so they do not differentiate the
oracles. `timeout` commits the row server-side yet the governed actuator HALTs
(the outcome is unknown to the client), which is a safe false-abort, not a
silent wrong effect.

## The `0` is zero in a closed world (load-bearing caveat)

The independent ground truth audits every persisted table it finds in the
SQLite system of record (dynamically, from `sqlite_master`), so it is open-world
over that database -- a collateral write to any surface, even one added later,
is caught. Two honest limits remain, and the `effect_full` `0` is conditioned on
both:

1. **Outside the database is invisible.** An effect that lands OUTSIDE this
   SQLite system of record -- an outbound HL7 or message-queue publish, a
   filesystem side-channel, a downstream service call -- is seen by neither the
   `effect_full` read path nor the ground truth. No in-database audit can see
   it. So `0/90` means "zero silent-wrong-effects within the audited SQLite
   system of record," not "zero silent-wrong-effects" in the absolute.
2. **Shared specification, not shared code.** The ground truth and the effect
   contract read through independent code and independent connections, but they
   encode the SAME business intent (the target patient/type and the intended
   note). A fault class no one thought to define is invisible to all three
   paths. Independence of code and read path is not independence of
   specification.

The realistic, foregrounded result is therefore the middle rung
({m["effect_rest"]["silent_wrong_count"]}/{m["effect_rest"]["n_runs"]} residual
under one out-of-band oracle); the `0` is the best case under a complete
in-database read path, in a closed world.

## How to read these numbers (deterministic, not sampled)

Every run here is localhost, model-call-free, and deterministic: a given
(arm x fault) produces the same outcome every repeat, so the {n} repeats have
approximately ZERO sampling variance. These counts are a COVERAGE MATRIX over a
small, hand-authored fault taxonomy (the differentiating middle block is a
handful of classes; the `effect_rest`-vs-`effect_full` gap rests on exactly one,
`collateral_unaudited`), NOT an estimate of a population incidence rate. We do
not report confidence intervals because they would be vacuous on variance-0 data.
The taxonomy is an adversarial, transaction-fault lineage chosen to differentiate
oracles; it is NOT weighted to any measured real-world EMR/lending incident
distribution, so a rate like the screen arm's should be read as "fault coverage
under this taxonomy," never as an expected production frequency.

## Reproduce

```
python -m benchmark.effect_e2e.run --n {n}
```

Serves a local SQLite-backed record service, drives each fault end-to-end
through the real replayer under all three arms, and judges every run by the
independent ground truth. `$0`, no network beyond localhost, no model calls.
The claims are pinned in CI by `tests/test_effect_e2e_harness.py`.
"""


def render_chart(results: dict[str, Any], out_png: Path) -> Path:
    """Bar chart: silent-wrong-effect rate per arm (large -> small -> zero)."""
    from openadapt_flow.benchmark.chart_fonts import configure_bundled_font

    plt = configure_bundled_font()
    m = results["metrics"]["per_arm"]
    surface = "#fcfcfb"
    ink = "#0b0b0b"
    ink2 = "#52514e"
    colors = ["#d64545", "#e0a23b", "#1baf7a"]
    labels = [
        "screen-verify",
        "effect-verify\n(REST record)",
        "effect-verify\n(full SQL)",
    ]
    values = [m[a]["silent_wrong_action_rate"] for a in ARMS]

    fig, ax = plt.subplots(figsize=(7.2, 4.2), facecolor=surface)
    fig.suptitle(
        "End-to-end silent-wrong-effect rate (through the real replayer)",
        color=ink,
        fontsize=11.5,
    )
    bars = ax.bar(labels, values, color=colors, width=0.55, zorder=2)
    ax.set_facecolor(surface)
    ax.set_ylabel("silent-wrong-effect rate", color=ink2, fontsize=9)
    ax.set_ylim(0, max(0.1, max(values) * 1.25))
    ax.tick_params(colors=ink2, labelsize=9)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(ink2)
    ax.grid(axis="y", color="#e6e5e0", linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for bar, arm in zip(bars, ARMS):
        a = m[arm]
        ax.annotate(
            f"{a['silent_wrong_action_rate']:.0%}\n({a['silent_wrong_count']}/{a['n_runs']})",
            (bar.get_x() + bar.get_width() / 2, bar.get_height()),
            ha="center",
            va="bottom",
            fontsize=9,
            color=ink,
        )
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    out_png.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_png, dpi=160, facecolor=surface)
    plt.close(fig)
    return out_png


def write_outputs(results: dict[str, Any], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out_dir / "EFFECT_E2E.md").write_text(render_markdown(results))
    from openadapt_flow.benchmark.chart_fonts import safe_render

    safe_render(render_chart, results, out_dir / "effect_e2e.png")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=DEFAULT_N, help="trials per fault/arm")
    parser.add_argument("--out", default=str(DEFAULT_OUT_DIR), help="output directory")
    parser.add_argument(
        "--print", action="store_true", dest="print_only", help="print, do not write"
    )
    args = parser.parse_args(argv)
    results = run_benchmark(n=args.n)
    if args.print_only:
        json.dump(results, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0
    write_outputs(results, Path(args.out))
    m = results["metrics"]["per_arm"]
    print(
        "\nend-to-end silent-wrong-effect rate  "
        f"screen={m['screen']['silent_wrong_action_rate']:.1%} "
        f"({m['screen']['silent_wrong_count']}/{m['screen']['n_runs']})  "
        f"effect_rest={m['effect_rest']['silent_wrong_action_rate']:.1%} "
        f"({m['effect_rest']['silent_wrong_count']}/{m['effect_rest']['n_runs']})  "
        f"effect_full={m['effect_full']['silent_wrong_action_rate']:.1%} "
        f"({m['effect_full']['silent_wrong_count']}/{m['effect_full']['n_runs']})"
    )
    print(f"Wrote results.json, EFFECT_E2E.md, effect_e2e.png under {args.out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
