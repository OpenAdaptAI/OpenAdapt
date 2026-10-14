#!/usr/bin/env python3
"""Probe Flow's transaction outcome taxonomy against a real persistence boundary.

This extends ``run_current_flow_local_benchmark.py`` (which measures compiled
replay against selector controls) with the question that benchmark cannot ask:
when a consequential write meets a *backend* fault, what does the runtime say it
knows about the business effect?

Flow >= 1.21 refines the coarse ``execution_outcome`` into a terminal
:class:`~openadapt_flow.transaction.TransactionOutcome` -- ``VERIFIED`` /
``HALTED_BEFORE_EFFECT`` / ``RECONCILIATION_REQUIRED`` / ``FAILED_PLATFORM`` /
``COMPLETED_UNVERIFIED`` / ``CANCELED`` / ``REJECTED_POLICY`` / ``ROLLED_BACK``
-- plus a per-step effect journal.  Those labels are *claims about the system of
record*, so they must be judged against the system of record, never against the
runtime's own report or the screen.

The bundled MockMed ``fault_server`` provides exactly that: it serves the same
static app but adds a real persistence boundary with an in-process store that
``GET /api/db`` exposes as independent ground truth.  Each ``?fault=`` mode
reproduces a named consequential-write failure:

* ``ok``          -- the write is persisted normally (control).
* ``timeout``     -- the server COMMITS the row, then hangs past the client
                     timeout.  The client sees an error though the write landed:
                     this is the uncertain-delivery case.
* ``optimistic``  -- the server REJECTS the write after the app already painted
                     a success banner.  The screen lies; nothing is persisted.
* ``session``     -- the write returns 401; nothing is persisted.
* ``duplicate``   -- every arriving write is accepted, so a double-delivered
                     click writes TWO rows.

Every fault mode is run under two configurations, because the taxonomy's
guarantee is only as good as what was actually verified:

* ``unverified``      -- the stock compiled bundle, no effect verifier (what a
                         user gets by default).
* ``effect_verified`` -- the same bundle with the consequential save step
                         carrying a ``record_written`` + ``field_equals``
                         contract, checked by ``RestRecordVerifier`` against the
                         system of record.

Methodology matches the sibling benchmark: no retries, ``>=3`` trials per cell,
one arm-independent oracle shared by every cell, and healthy runs make no model
calls and no network calls beyond loopback.

The invariants in :func:`evaluate_invariants` are the point of this script.  A
violated invariant is reported, counted, and (unless ``--no-fail-on-violation``)
exits non-zero.  It is never silently downgraded.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import platform
import sys
import tempfile
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SIBLING = Path(__file__).resolve().parent / "run_current_flow_local_benchmark.py"
_SPEC = importlib.util.spec_from_file_location("_current_flow_local_benchmark", _SIBLING)
assert _SPEC is not None and _SPEC.loader is not None
_BENCH = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BENCH)
capture_runtime = _BENCH.capture_runtime

# Reuse the sibling's exact source-binding discipline so the two reports can
# never drift on how they pin a wheel, a commit, or a release tag.
_sha256 = _BENCH._sha256
_tree_sha256 = _BENCH._tree_sha256
_source_binding = _BENCH._source_binding
_extract_wheel = _BENCH._extract_wheel
_git = _BENCH._git

#: The consequential write. Everything before it is navigation/data entry.
CONSEQUENTIAL_STEP_ID = "step_010"

#: The intended record, as the demonstration wrote it.
INTENDED_PATIENT_ID = "p1"
INTENDED_ENCOUNTER_TYPE = "Triage"

FAULT_MODES = ("ok", "timeout", "optimistic", "session", "duplicate")
VERIFICATION_MODES = ("unverified", "effect_verified")

#: What the system of record proves happened, independent of screen and runtime.
BUSINESS_EFFECTS = (
    "intended_once",
    "absent",
    "duplicate",
    "partial",
    "collateral_loss",
)

#: Outcomes that assert NO business effect occurred. Claiming one of these
#: while the system of record holds the write is a false absence claim.
_ASSERTS_NO_EFFECT = frozenset(
    {
        "HALTED_BEFORE_EFFECT",
        "FAILED_PLATFORM",
        "CANCELED",
        "REJECTED_POLICY",
    }
)

#: The only outcome the runtime may treat as a production success.
_PRODUCTION_SUCCESS = "VERIFIED"


def _records(url: str) -> list[dict[str, Any]]:
    """Read the system of record. This is the only oracle in this script."""

    with urllib.request.urlopen(f"{url}api/db", timeout=10) as response:
        payload = json.loads(response.read())
    records = payload.get("records", payload) if isinstance(payload, dict) else payload
    return list(records)


def _business_effect(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    note: str,
) -> dict[str, Any]:
    """Classify what actually landed in the system of record.

    Computed purely from the store's before/after snapshots: neither the
    runtime's report nor the screen contributes. The intended effect is exactly
    one NEW encounter for the intended patient, of the intended type, carrying
    this run's note, with no pre-existing row destroyed.
    """

    def key(record: dict[str, Any]) -> tuple[str, ...]:
        return tuple(str(record.get(field, "")) for field in ("id", "patient_id", "type", "note"))

    before_keys = Counter(key(record) for record in before)
    new = [record for record in after if before_keys[key(record)] == 0]
    targeted = [
        record
        for record in new
        if str(record.get("patient_id")) == INTENDED_PATIENT_ID
        and str(record.get("type")) == INTENDED_ENCOUNTER_TYPE
    ]
    exact = [record for record in targeted if str(record.get("note", "")) == note]
    after_keys = Counter(key(record) for record in after)
    lost = [record for record in before if after_keys[key(record)] == 0]

    if lost:
        effect = "collateral_loss"
    elif len(exact) == 1 and len(targeted) == 1:
        effect = "intended_once"
    elif len(targeted) > 1:
        effect = "duplicate"
    elif targeted and not exact:
        effect = "partial"
    else:
        effect = "absent"

    return {
        "business_effect": effect,
        "effect_landed": effect != "absent",
        "new_record_count": len(new),
        "targeted_record_count": len(targeted),
        "exact_record_count": len(exact),
        "collateral_lost_count": len(lost),
        "records_before": len(before),
        "records_after": len(after),
    }


def evaluate_invariants(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Check the safety invariants the taxonomy is supposed to guarantee.

    Each returned entry is one invariant with its exact violating runs. These
    are contract checks, not thresholds: any violation count above zero is a
    finding. Nothing here is tuned, sampled, or averaged.
    """

    def _detail(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "fault_mode": row["fault_mode"],
            "verification": row["verification"],
            "trial": row["trial"],
            "transaction_outcome": row["transaction_outcome"],
            "execution_outcome": row["execution_outcome"],
            "business_effect": row["business_effect"],
            "targeted_record_count": row["targeted_record_count"],
            "verification_performed": row["verification_performed"],
            "attempt_state": row["attempt_state"],
        }

    # (id, statement, applies_to, is_violation). ``applies_to`` records how many
    # counted runs could have violated the invariant, so a guarantee that no run
    # exercised is reported as VACUOUS rather than as passing.
    specs: list[tuple[str, str, Any, Any]] = [
        (
            "completed_unverified_is_never_success",
            (
                "COMPLETED_UNVERIFIED must never be reported as a production "
                "success and must never be billable."
            ),
            lambda row: row["transaction_outcome"] == "COMPLETED_UNVERIFIED",
            lambda row: row["transaction_billable"] or row["production_success_claimed"],
        ),
        (
            "only_verified_is_billable",
            "Only VERIFIED may be billable.",
            lambda row: row["transaction_outcome"] is not None,
            lambda row: (
                row["transaction_billable"] and row["transaction_outcome"] != _PRODUCTION_SUCCESS
            ),
        ),
        (
            "no_false_absence_claim",
            (
                "An outcome that asserts no business effect occurred "
                "(HALTED_BEFORE_EFFECT / FAILED_PLATFORM / CANCELED / "
                "REJECTED_POLICY) must not be reported when the system of "
                "record shows the write landed."
            ),
            lambda row: row["transaction_outcome"] in _ASSERTS_NO_EFFECT,
            lambda row: row["effect_landed"],
        ),
        (
            "unverified_delivered_write_needs_reconciliation",
            (
                "A consequential step that reached actuation but whose effect "
                "was never verified cannot be classified as a proven absence; "
                "it must be RECONCILIATION_REQUIRED."
            ),
            lambda row: (
                row["attempt_state"] in ("delivered", "delivery_uncertain")
                and not row["verification_performed"]
            ),
            lambda row: row["transaction_outcome"] in _ASSERTS_NO_EFFECT,
        ),
        (
            "no_blind_retry_of_consequential_write",
            (
                "A single run must never write the intended record more than "
                "once. A duplicate row from a run whose backend is NOT the "
                "double-accepting 'duplicate' mode would be a blind retry."
            ),
            lambda row: row["fault_mode"] != "duplicate",
            lambda row: row["targeted_record_count"] > 1,
        ),
        (
            "verified_requires_a_real_effect",
            "VERIFIED must never be reported when nothing landed.",
            lambda row: row["transaction_outcome"] == _PRODUCTION_SUCCESS,
            lambda row: not row["effect_landed"],
        ),
        (
            "zero_model_calls_and_cost",
            "A healthy compiled run makes no model calls and costs $0.",
            lambda row: True,
            lambda row: row["model_calls"] != 0 or row["model_cost_usd"] != 0.0,
        ),
    ]

    checks: list[dict[str, Any]] = []
    for check_id, statement, applies_to, is_violation in specs:
        applicable = [row for row in rows if applies_to(row)]
        violating = [row for row in applicable if is_violation(row)]
        checks.append(
            {
                "id": check_id,
                "statement": statement,
                "applicable_count": len(applicable),
                "violation_count": len(violating),
                "holds": not violating,
                # A guarantee no counted run could exercise proves nothing.
                "vacuous": not applicable,
                "violations": [_detail(row) for row in violating],
            }
        )
    return checks


def _aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    outcomes = Counter(row["transaction_outcome"] for row in rows)
    effects = Counter(row["business_effect"] for row in rows)
    return {
        "n": len(rows),
        "transaction_outcomes": dict(sorted(outcomes.items())),
        "business_effects": dict(sorted(effects.items())),
        "billable_count": sum(bool(row["transaction_billable"]) for row in rows),
        "production_success_claimed_count": sum(
            bool(row["production_success_claimed"]) for row in rows
        ),
        "effect_landed_count": sum(bool(row["effect_landed"]) for row in rows),
        "verification_performed_count": sum(bool(row["verification_performed"]) for row in rows),
        "model_calls_total": sum(int(row["model_calls"]) for row in rows),
        "model_cost_usd_total": round(sum(float(row["model_cost_usd"]) for row in rows), 8),
        "steady_wall_s_median": (
            _BENCH.statistics.median(row["steady_wall_s"] for row in rows) if rows else 0.0
        ),
    }


def _render_markdown(report: dict[str, Any]) -> str:
    flow = report["source"]["flow"]
    lines = [
        "# Flow transaction outcome taxonomy probe",
        "",
        (
            "Does the terminal transaction outcome match what the system of "
            "record can actually prove? Judged only against MockMed's "
            "independent store, never against the runtime's report or the "
            "screen. **Not** a zero-shot computer-use comparison."
        ),
        "",
        "## Source and environment",
        "",
        f"- Flow commit: `{flow['commit']}` (version `{flow['version']}`; tracked-clean source)",
        f"- Release tag: `{flow['release_tag']}`",
        f"- Wheel SHA-256: `{flow['artifact']['sha256']}`",
        f"- Evals base commit: `{report['source']['evals']['commit']}`",
        f"- Runner SHA-256: `{report['source']['runner_sha256']}`",
        f"- Platform: `{report['environment']['platform']}`",
        f"- Python: `{report['environment']['python']}`",
        f"- Playwright: `{report['environment']['playwright']}`",
        f"- Chromium: `{report['environment']['chromium']}`",
        "- Network/provider use: loopback bundled MockMed fault server only; "
        "no cloud VM, hosted runner, or model API",
        "",
        "## Counted result",
        "",
        (
            f"{report['trials_per_cell']} trials per cell, no retries. The "
            "oracle is the system-of-record snapshot delta at "
            "`GET /api/db`: the intended effect is exactly one new "
            f"`{INTENDED_ENCOUNTER_TYPE}` encounter for the intended patient "
            "carrying this run's note, with no pre-existing row destroyed."
        ),
        "",
        (
            "| Fault mode | Verification | Runs | Ground-truth effect | "
            "Transaction outcome | Billable | Verification performed | "
            "Model calls |"
        ),
        "|---|---|---:|---|---|---:|---:|---:|",
    ]

    def _fmt(counter: dict[str, int]) -> str:
        return ", ".join(f"{name} {count}" for name, count in counter.items()) or "-"

    for mode in report["fault_modes"]:
        for verification in report["verification_modes"]:
            cell = report["aggregate"][verification][mode]
            lines.append(
                f"| `{mode}` | `{verification}` | {cell['n']} | "
                f"{_fmt(cell['business_effects'])} | "
                f"{_fmt(cell['transaction_outcomes'])} | "
                f"{cell['billable_count']} | "
                f"{cell['verification_performed_count']} | "
                f"{cell['model_calls_total']} |"
            )

    lines.extend(
        [
            "",
            "`timeout` commits the row and then hangs past the client timeout, "
            "so the client sees an error though the write landed. "
            "`optimistic` paints a success banner the server then rejects. "
            "`session` returns 401 and persists nothing. `duplicate` accepts "
            "every arriving write.",
            "",
            "## Invariants",
            "",
            "An invariant no counted run could exercise is reported as "
            "`vacuous`; it proves nothing and is not a pass.",
            "",
            "| Invariant | Holds | Applicable runs | Violations |",
            "|---|---|---:|---:|",
        ]
    )
    for check in report["invariants"]:
        if check["vacuous"]:
            verdict = "vacuous"
        elif check["holds"]:
            verdict = "yes"
        else:
            verdict = "**NO**"
        lines.append(
            f"| {check['statement']} | {verdict} | "
            f"{check['applicable_count']} | {check['violation_count']} |"
        )

    failed = [check for check in report["invariants"] if not check["holds"]]
    lines.extend(["", "## Findings", ""])
    if not failed:
        lines.append("- Every invariant held across every counted trial.")
    for check in failed:
        lines.append(
            f"- **`{check['id']}` FAILED ({check['violation_count']} runs).** {check['statement']}"
        )
        for violation in check["violations"]:
            lines.append(
                f"  - `{violation['fault_mode']}` / `{violation['verification']}` "
                f"trial {violation['trial']}: reported "
                f"`{violation['transaction_outcome']}` while the system of "
                f"record holds `{violation['business_effect']}` "
                f"({violation['targeted_record_count']} targeted row(s)); "
                f"attempt state `{violation['attempt_state']}`, verification "
                f"performed: {violation['verification_performed']}."
            )
    lines.extend(
        [
            "",
            "## Caveats",
            "",
            "- Synthetic MockMed fault server, one workflow, one macOS host, headless Chromium.",
            "- The probe runs the exact published wheel named above, extracted "
            "locally, bound to the release-tagged tracked-clean source.",
            "- The `effect_verified` configuration is authored by this script, "
            "not mined by the compiler; it shows what the taxonomy can prove "
            "when a verifier IS configured, and is not a claim that a compiled "
            "bundle ships one by default.",
            "- No hosted lifecycle, Windows UIA, RDP, Citrix, or real customer "
            "application is represented.",
            "",
        ]
    )
    return "\n".join(lines)


def run_probe(
    flow_source: Path,
    flow_wheel: Path,
    out_dir: Path,
    *,
    trials: int = 3,
    headed: bool = False,
) -> dict[str, Any]:
    if trials < 3:
        raise ValueError("comparative evaluation requires at least three trials")
    flow_source = flow_source.resolve()
    flow_wheel = flow_wheel.resolve()
    out_dir = out_dir.resolve()
    if not (flow_source / "openadapt_flow").is_dir():
        raise ValueError(f"not an openadapt-flow source checkout: {flow_source}")
    if not flow_wheel.is_file() or flow_wheel.suffix != ".whl":
        raise ValueError(f"not a Flow wheel: {flow_wheel}")

    source = _source_binding(flow_source)
    artifact_temp = tempfile.TemporaryDirectory(prefix="oa-flow-wheel-")
    artifact_root = Path(artifact_temp.name).resolve()
    _extract_wheel(flow_wheel, artifact_root)
    sys.path.insert(0, str(artifact_root))

    from openadapt_flow.backends.playwright_backend import (  # noqa: PLC0415
        PlaywrightBackend,
    )
    from openadapt_flow.benchmark.dom_arm import note_for_slot  # noqa: PLC0415
    from openadapt_flow.compiler import compile_recording  # noqa: PLC0415
    from openadapt_flow.demo_driver import record_triage_demo  # noqa: PLC0415
    from openadapt_flow.ir import Workflow  # noqa: PLC0415
    from openadapt_flow.mockmed.fault_server import serve  # noqa: PLC0415
    from openadapt_flow.runtime import Replayer  # noqa: PLC0415
    from openadapt_flow.runtime.effects import (  # noqa: PLC0415
        Effect,
        EffectKind,
        RestRecordVerifier,
        ValueExpr,
    )

    imported = Path(sys.modules["openadapt_flow"].__file__).resolve()
    if artifact_root not in imported.parents:
        raise RuntimeError(f"Flow import escaped requested wheel: {imported}")
    flow_version = str(sys.modules["openadapt_flow"].__version__)
    release_tag = f"v{flow_version}"
    if release_tag not in source["tags"]:
        raise RuntimeError(f"source commit is not tagged {release_tag}: {source['tags']}")

    evals_root = Path(__file__).resolve().parents[1]
    evals_commit = _git(evals_root, "rev-parse", "HEAD")
    out_dir.mkdir(parents=True, exist_ok=False)

    def _save_effects() -> list[Any]:
        target = {
            "patient_id": ValueExpr(literal=INTENDED_PATIENT_ID),
            "type": ValueExpr(literal=INTENDED_ENCOUNTER_TYPE),
        }
        return [
            Effect(
                kind=EffectKind.RECORD_WRITTEN,
                match=dict(target),
                expected_count=1,
                count_new_only=True,
                risk="irreversible",
                probe="exactly one new Triage encounter for the intended patient",
            ),
            Effect(
                kind=EffectKind.FIELD_EQUALS,
                match=dict(target),
                field="note",
                value=ValueExpr(param="note"),
                risk="irreversible",
                probe="the saved note equals this run's note",
            ),
        ]

    url, store, stop = serve(port=0)
    rows: list[dict[str, Any]] = []
    setup: dict[str, Any] = {}
    try:
        with tempfile.TemporaryDirectory(prefix="oa-flow-transaction-probe-") as tmp:
            root = Path(tmp)
            started = time.monotonic()
            recording = record_triage_demo(
                url,
                root / "recording",
                note_text="Transaction probe setup",
                headed=headed,
            )
            record_wall_s = time.monotonic() - started
            bundle = root / "bundle"
            started = time.monotonic()
            compile_recording(recording, bundle, name="flow-transaction-probe")
            compile_wall_s = time.monotonic() - started
            setup = {
                "record_wall_s": record_wall_s,
                "compile_wall_s": compile_wall_s,
                "bundle_tree_sha256": _tree_sha256(bundle),
                "workflow_sha256": _sha256(bundle / "workflow.json"),
                "manifest_sha256": _sha256(bundle / "manifest.json"),
            }

            slot = 0
            for verification in VERIFICATION_MODES:
                for mode in FAULT_MODES:
                    for trial in range(1, trials + 1):
                        note = note_for_slot("compiled", slot)
                        slot += 1
                        workflow = Workflow.load(bundle)
                        verifier = None
                        if verification == "effect_verified":
                            saves = [
                                step for step in workflow.steps if step.id == CONSEQUENTIAL_STEP_ID
                            ]
                            if not saves:
                                raise RuntimeError(
                                    "compiled bundle has no step "
                                    f"{CONSEQUENTIAL_STEP_ID}: cannot bind the "
                                    "consequential effect contract"
                                )
                            saves[0].effects = _save_effects()
                            verifier = RestRecordVerifier(base_url=url)

                        # Each trial gets a fresh system of record, mirroring
                        # the sibling benchmark's fresh-browser-per-run rule.
                        # Without it, rows accumulated by earlier trials make a
                        # uniqueness contract ambiguous for reasons that have
                        # nothing to do with the run under test.
                        store.reset()
                        before = _records(url)
                        backend, close = PlaywrightBackend.launch(
                            f"{url}?fault={mode}", headless=not headed
                        )
                        run_started = time.monotonic()
                        error: str | None = None
                        report = None
                        try:
                            report = Replayer(backend, effect_verifier=verifier).run(
                                workflow,
                                params={"note": note},
                                bundle_dir=bundle,
                                run_dir=root / f"{verification}-{mode}-{trial}",
                            )
                        except Exception as exc:  # a failed run is evidence
                            error = f"{type(exc).__name__}: {exc}"
                        finally:
                            steady_wall_s = time.monotonic() - run_started
                            close()
                        after = _records(url)

                        journal = list(report.effect_journal) if report is not None else []
                        consequential = [
                            entry for entry in journal if entry.step_id == CONSEQUENTIAL_STEP_ID
                        ]
                        entry = consequential[0] if consequential else None
                        outcome = report.transaction_outcome if report is not None else None
                        row: dict[str, Any] = {
                            "fault_mode": mode,
                            "verification": verification,
                            "trial": trial,
                            "note_sha256": _BENCH.hashlib.sha256(note.encode("utf-8")).hexdigest(),
                            "steady_wall_s": steady_wall_s,
                            "error": error,
                            "execution_outcome": (
                                report.execution_outcome if report is not None else None
                            ),
                            "transaction_outcome": outcome,
                            "transaction_billable": bool(
                                report.transaction_billable if report is not None else False
                            ),
                            "transaction_platform_fault": bool(
                                report.transaction_platform_fault if report is not None else False
                            ),
                            "production_success_claimed": bool(outcome == _PRODUCTION_SUCCESS),
                            "runtime_reported_success": bool(
                                report.success if report is not None else False
                            ),
                            "effect_journal_entries": len(journal),
                            "attempt_state": entry.attempt_state if entry else None,
                            "observed_effect": entry.observed_effect if entry else None,
                            "verification_performed": bool(
                                entry.verification_performed if entry else False
                            ),
                            "effect_verified": entry.effect_verified if entry else None,
                            "model_calls": 0,
                            "model_cost_usd": 0.0,
                        }
                        row.update(_business_effect(before, after, note))
                        rows.append(row)
                        print(
                            f"{verification} {mode} trial={trial}: "
                            f"txn={row['transaction_outcome']} "
                            f"effect={row['business_effect']} "
                            f"verified={row['verification_performed']} "
                            f"steady={steady_wall_s:.3f}s",
                            flush=True,
                        )
    finally:
        stop()

    aggregate = {
        verification: {
            mode: _aggregate(
                [
                    row
                    for row in rows
                    if row["verification"] == verification and row["fault_mode"] == mode
                ]
            )
            for mode in FAULT_MODES
        }
        for verification in VERIFICATION_MODES
    }
    invariants = evaluate_invariants(rows)
    report_doc = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "transaction outcome taxonomy against a real persistence boundary",
        "task": (
            "MockMed triage against the fault-injection persistence boundary: "
            "sign in, open intended referral, create Triage encounter, enter a "
            "trial-unique note, save"
        ),
        "oracle": (
            "system-of-record snapshot delta at GET /api/db: exactly one new "
            "Triage encounter for the intended patient carrying this run's "
            "note, with no pre-existing row destroyed"
        ),
        "business_effects": list(BUSINESS_EFFECTS),
        "trials_per_cell": trials,
        "fault_modes": list(FAULT_MODES),
        "verification_modes": list(VERIFICATION_MODES),
        "consequential_step_id": CONSEQUENTIAL_STEP_ID,
        "source": {
            "flow": {
                **source,
                "version": flow_version,
                "release_tag": release_tag,
                "artifact": {
                    "filename": flow_wheel.name,
                    "sha256": _sha256(flow_wheel),
                    "import_mode": "locally extracted published wheel",
                },
            },
            "evals": {"commit": evals_commit},
            "runner_sha256": _sha256(Path(__file__).resolve()),
        },
        "environment": {
            "platform": platform.platform(),
            "python": platform.python_version(),
            "playwright": importlib.metadata.version("playwright"),
            "chromium": "Playwright-managed headless Chromium",
        },
        "runtime": capture_runtime(out_dir, browser_required=True),
        "setup": setup,
        "aggregate": aggregate,
        "invariants": invariants,
        "runs": rows,
        "paid_or_remote_mutations": [],
        "caveats": [
            "Synthetic MockMed fault server, one workflow, one macOS host, headless Chromium.",
            "Published Flow wheel and release-tagged source are exact-bound.",
            "The effect contract in the effect_verified cells is authored by this script.",
            "No hosted, Windows, RDP, Citrix, or customer workflow is represented.",
        ],
    }
    (out_dir / "results.json").write_text(
        json.dumps(report_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (out_dir / "REPORT.md").write_text(_render_markdown(report_doc), encoding="utf-8")

    failed = [check for check in invariants if not check["holds"]]
    if failed:
        (out_dir / "invariant_violations.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "title": "transaction outcome invariant violations",
                    "flow": report_doc["source"]["flow"],
                    "runner_sha256": report_doc["source"]["runner_sha256"],
                    "oracle": report_doc["oracle"],
                    "required_trials": trials,
                    "failed_invariants": failed,
                    "reproduce": (
                        "python scripts/run_flow_transaction_probe.py "
                        f"--flow-source <{release_tag}-checkout> "
                        f"--flow-wheel <{flow_wheel.name}> "
                        "--out <new-output-directory>"
                    ),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"Wrote {out_dir / 'results.json'} and {out_dir / 'REPORT.md'}", flush=True)
    artifact_temp.cleanup()
    return report_doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Probe Flow's transaction outcome taxonomy against a real persistence boundary."
    )
    parser.add_argument("--flow-source", type=Path, required=True)
    parser.add_argument("--flow-wheel", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--headed", action="store_true")
    parser.add_argument(
        "--no-fail-on-violation",
        action="store_true",
        help="Record invariant violations without exiting non-zero.",
    )
    args = parser.parse_args(argv)
    report = run_probe(
        args.flow_source,
        args.flow_wheel,
        args.out,
        trials=args.trials,
        headed=args.headed,
    )
    failed = [check for check in report["invariants"] if not check["holds"]]
    if failed:
        for check in failed:
            print(
                f"INVARIANT VIOLATED: {check['id']} "
                f"({check['violation_count']} runs) — {check['statement']}",
                file=sys.stderr,
            )
        if not args.no_fail_on_violation:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
