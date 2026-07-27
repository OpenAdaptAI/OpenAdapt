"""The composed free path, end to end: record -> compile -> certify -> run -> report.

Every per-repo gate was green while this loop was broken, because nothing
exercised the loop.  ``replay`` could only ever terminate
``COMPLETED_UNVERIFIED`` (the Demo profile sets ``require_effect_contracts=False``)
and ``report-run`` refuses anything that is not ``VERIFIED``, so the free tier
could not reach the state its own shareable artifact requires.  Unit tests for
each command passed; the composition had no test at all.

This module is that test.  It asserts, on one real browser run against the
bundled application:

* the free path terminates ``VERIFIED``, under the ``standard`` profile, with
  the effect confirmed by an independent read of the system of record;
* the shareable receipt is emitted, and its bytes carry no PHI carrier;
* ``replay`` still cannot reach ``VERIFIED`` -- the Demo profile was not
  weakened to get here;
* ``report-run`` still refuses a non-``VERIFIED`` run;
* the verification is not vacuous: when the backend silently drops or mangles
  the write, the same bundle does NOT verify.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from openadapt_flow import tutorial as tutorial_module
from openadapt_flow.__main__ import main as cli_main
from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import RunReport
from openadapt_flow.mockmed.fault_server import FaultDB, serve
from openadapt_flow.tutorial import (
    TUTORIAL_NOTE,
    TutorialResult,
    certify_tutorial,
    record_tutorial,
    run_tutorial,
    run_tutorial_workflow,
)


@pytest.fixture(scope="module")
def free_path(tmp_path_factory: pytest.TempPathFactory) -> TutorialResult:
    """Run the documented free path once for the whole module."""

    return run_tutorial(tmp_path_factory.mktemp("free-path"))


def _report(run_dir: Path) -> RunReport:
    return RunReport.model_validate_json(
        (run_dir / "report.json").read_text(encoding="utf-8")
    )


def test_free_path_terminates_verified_with_independent_effect_evidence(
    free_path: TutorialResult,
) -> None:
    """The whole point: the free tier reaches VERIFIED, and it earned it."""

    assert free_path.execution_outcome == "VERIFIED"
    assert free_path.transaction_outcome == "VERIFIED"
    assert free_path.execution_profile == "standard"
    # COMPLETED_UNVERIFIED is never billable; VERIFIED under a production
    # profile is the only outcome that is.
    assert free_path.transaction_billable is True
    # A healthy run makes no model calls.
    assert free_path.model_calls == 0
    # The evidence that did not exist before: every declared effect confirmed,
    # at INDEPENDENT_SYSTEM strength (tier 1), stronger than the tier 3 the
    # Standard profile requires.
    assert free_path.effects_required >= 1
    assert free_path.effects_confirmed == free_path.effects_required
    assert free_path.effect_tier == 1
    # And the write really is in the independent store.
    assert free_path.system_of_record_records == 1

    report = _report(free_path.run_dir)
    assert report.success is True
    assert report.production_eligible is True
    confirmed = [
        evidence
        for result in report.results
        for evidence in result.effect_evidence
        if evidence.final_verdict == "confirmed"
    ]
    assert confirmed, "no confirmed effect evidence was retained"
    assert all(evidence.substrate == "rest" for evidence in confirmed)


def test_free_path_payoff_screen_leads_with_verified(
    free_path: TutorialResult,
) -> None:
    """The first line a new user reads is no longer a warning."""

    first_line = (
        (free_path.run_dir / "REPORT.md").read_text(encoding="utf-8").split("\n", 1)[0]
    )
    assert "VERIFIED" in first_line
    assert "COMPLETED_UNVERIFIED" not in first_line


def test_free_path_emits_a_shareable_receipt(free_path: TutorialResult) -> None:
    """The growth artifact is produced by the free run itself."""

    for key in ("json", "markdown", "png"):
        assert free_path.receipt_paths[key].is_file()

    payload = json.loads(free_path.receipt_paths["json"].read_text(encoding="utf-8"))
    assert payload["outcome"] == "VERIFIED"
    assert payload["provenance"] == "synthetic-tutorial"
    assert payload["model_calls"] == 0
    assert payload["over_halt_count"] == 0
    # The bundle digest is what makes the claim checkable by a stranger.
    assert payload["bundle_digest"] == free_path.bundle_digest
    assert payload["receipt_digest"]


def test_receipt_carries_no_phi_carrier_from_the_local_report(
    free_path: TutorialResult,
) -> None:
    """The receipt is generated from an allow-list, so it cannot leak.

    The rich local ``REPORT.md`` legitimately holds screenshots, typed values,
    OCR identity text, parameters, and URLs.  None of them may appear in the
    artifact a person posts.
    """

    receipt_bytes = free_path.receipt_paths["json"].read_bytes()
    receipt_text = receipt_bytes.decode("utf-8")
    report = _report(free_path.run_dir)

    forbidden = [
        TUTORIAL_NOTE,  # a parameter value
        report.workflow_name,  # a name the operator chose
        "127.0.0.1",  # a hostname
        "http",  # any URL
        ".png",  # any image reference
        "Jane Sample",  # record identity text visible on screen
    ]
    for needle in forbidden:
        assert needle not in receipt_text, f"receipt leaked {needle!r}"

    # And the local report really does carry what the receipt refuses to.
    report_md = (free_path.run_dir / "REPORT.md").read_text(encoding="utf-8")
    assert TUTORIAL_NOTE in report_md
    assert "![" in report_md


def test_report_run_emits_the_receipt_locally_for_a_verified_run(
    free_path: TutorialResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``report-run --receipt`` is a local, network-free emission path."""

    out = tmp_path / "share"
    assert (
        cli_main(
            [
                "report-run",
                str(free_path.run_dir),
                "--receipt",
                str(out),
                "--production",
            ]
        )
        == 0
    )
    captured = capsys.readouterr().out
    assert "LOCAL ONLY" in captured
    assert (out / "receipt.json").is_file()
    assert (out / "receipt.png").is_file()
    payload = json.loads((out / "receipt.json").read_text(encoding="utf-8"))
    assert payload["outcome"] == "VERIFIED"
    assert payload["transaction_outcome"] == "VERIFIED"
    assert payload["provenance"] == "production"


def test_report_run_refuses_to_guess_receipt_provenance(
    free_path: TutorialResult, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "ambiguous-provenance"
    assert cli_main(["report-run", str(free_path.run_dir), "--receipt", str(out)]) == 2
    assert "--production is required" in capsys.readouterr().out
    assert not out.exists()


def test_demo_replay_still_cannot_reach_verified(
    free_path: TutorialResult, tmp_path: Path
) -> None:
    """The Demo profile was not loosened to make the tutorial verify.

    ``replay`` runs the same bundle with ``require_effect_contracts=False`` and
    no independent verifier.  Faced with a declared consequential effect it
    cannot check, it HALTS rather than guessing -- and in no case reports
    ``VERIFIED``.  ``report-run`` must still refuse the result.
    """

    run_dir = tmp_path / "demo-run"
    assert (
        cli_main(["replay", str(free_path.bundle_dir), "--run-dir", str(run_dir)]) == 1
    )
    report = _report(run_dir)
    assert report.execution_profile == "demo"
    assert report.execution_outcome != "VERIFIED"
    assert report.execution_outcome == "HALTED"
    assert report.transaction_billable is not True

    receipt_dir = tmp_path / "refused"
    assert cli_main(["report-run", str(run_dir), "--receipt", str(receipt_dir)]) == 0
    assert not receipt_dir.exists(), "a non-VERIFIED run emitted a receipt"


@pytest.fixture(scope="module")
def fault_fixture(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, FaultDB, Path, object]]:
    """One recorded, compiled, certified tutorial bundle plus a live backend."""

    root = tmp_path_factory.mktemp("fault-probe")
    base_url, db, stop = serve()
    try:
        record_tutorial(base_url, root / "recording")
        workflow = compile_recording(
            root / "recording", root / "bundle", name="fault-probe", mine_effects=True
        )
        certify_tutorial(workflow)
        yield base_url, db, root / "bundle", workflow
    finally:
        stop()


@pytest.mark.parametrize(
    ("fault", "expected_transaction"),
    [
        # The server rejects a write the UI already painted as saved. The
        # retained verifier establishes absence for only one of two declared
        # effects, so the report must reconcile rather than infer full absence
        # from the test fixture's out-of-band database snapshot.
        ("optimistic", "RECONCILIATION_REQUIRED"),
        # The row persists but the note field is dropped. Something landed, so
        # absence may NOT be claimed: this needs reconciliation.
        ("partial", "RECONCILIATION_REQUIRED"),
    ],
)
def test_verification_is_not_vacuous(
    fault_fixture: tuple[str, FaultDB, Path, object],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    expected_transaction: str,
) -> None:
    """A green screen over a bad write does NOT verify.

    This is what makes the ``VERIFIED`` above worth printing.  The identical
    certified bundle, run against a backend that silently drops or mangles the
    write, halts instead of reporting success.
    """

    base_url, db, bundle_dir, workflow = fault_fixture
    monkeypatch.setattr(
        tutorial_module,
        "TUTORIAL_ENTRY_QUERY",
        f"?fault={fault}&idempotency=demo#tasks",
    )
    report = run_tutorial_workflow(
        base_url=base_url,
        workflow=workflow,
        bundle_dir=bundle_dir,
        run_dir=tmp_path / f"run-{fault}",
    )
    assert report.execution_outcome == "HALTED"
    assert report.transaction_outcome == expected_transaction
    assert report.transaction_billable is not True
    assert report.success is False

    if fault == "optimistic":
        assert db.snapshot()["records"] == []
    else:
        # The row exists and its note was dropped -- exactly the silent
        # partial-save the screen cannot see.
        [record] = db.snapshot()["records"]
        assert record["note"] == ""


def test_tutorial_emits_no_receipt_when_the_run_does_not_verify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One rail, one rule: the shareable artifact requires VERIFIED.

    The tutorial obeys exactly the gate ``report-run`` enforces, so a halt is
    reported honestly and never dressed as a success.
    """

    real_run = tutorial_module.run_tutorial_workflow

    def halting_run(**kwargs: object):
        monkeypatch.setattr(
            tutorial_module,
            "TUTORIAL_ENTRY_QUERY",
            "?fault=optimistic&idempotency=demo#tasks",
        )
        return real_run(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(tutorial_module, "run_tutorial_workflow", halting_run)
    result = tutorial_module.run_tutorial(tmp_path / "halting")

    assert result.execution_outcome == "HALTED"
    assert result.receipt_paths == {}
    assert not (result.run_dir / "receipt.json").exists()
