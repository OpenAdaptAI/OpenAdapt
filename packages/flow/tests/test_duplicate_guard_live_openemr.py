"""Live: the duplicate guard on a real OpenEMR (#1400) and the business-rule
qualification case (#1399).

Skipped unless ``OPENADAPT_LIVE_OPENEMR=1`` and ``OPENEMR_FIXTURE_STATE`` are
set. Bring the pinned local fixture up first (``benchmark/openemr_local``,
README); these tests do not start or stop containers. They record, compile,
replay and qualify against that fixture, resetting its database between runs.
Made-up patients only.

Environment:

- ``OPENADAPT_LIVE_OPENEMR=1``: opt in;
- ``OPENEMR_FIXTURE_STATE``: the fixture's state directory;
- ``OPENEMR_FIXTURE_LOCK`` (optional): a different environment lock file;
- ``OPENEMR_UI_URL`` / ``OPENEMR_API_URL`` (optional): non-default ports,
  for example ``http://127.0.0.1:9301`` and ``https://127.0.0.1:9300``.

Run from ``packages/flow``::

    OPENADAPT_LIVE_OPENEMR=1 OPENEMR_FIXTURE_STATE=<state dir> \\
      [OPENEMR_UI_URL=... OPENEMR_API_URL=...] \\
      PYTHONPATH=../types:. python -m pytest \\
      tests/test_duplicate_guard_live_openemr.py -v -p no:cacheprovider
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("OPENADAPT_LIVE_OPENEMR") != "1"
    or not os.environ.get("OPENEMR_FIXTURE_STATE"),
    reason=(
        "live OpenEMR: set OPENADAPT_LIVE_OPENEMR=1 and OPENEMR_FIXTURE_STATE "
        "(the fixture must already be up)"
    ),
)

NO_MATCH = "No matches were found."
FLOW = Path(__file__).resolve().parents[1]


def _rosa_params() -> dict[str, str]:
    """The existing-duplicate rule's inputs, as verify.yaml declares them."""

    from openadapt_flow.qualify import load_verify_config

    config = load_verify_config(FLOW / "benchmark/openemr_local/verify.yaml")
    return dict(config.business_rules[0].params)


@pytest.fixture(scope="module")
def recording(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One referral demonstration on the blank baseline (no existing chart)."""

    from benchmark.openemr_local.qualify_hooks import fixture
    from scripts.openemr_local_demo import record

    out = tmp_path_factory.mktemp("live") / "recording"
    return record(fixture(), out, headed=False)


@pytest.fixture(scope="module")
def bundle(recording: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The demo compile path: the compiler, then apply_referral_rules."""

    from scripts.openemr_local_demo import compile_recording

    out = tmp_path_factory.mktemp("live") / "bundle"
    compile_recording(recording, out)
    return out


def test_the_compiler_mines_the_duplicate_guard_from_a_live_recording(
    recording: Path, tmp_path: Path
) -> None:
    """The plain compiler, without any OpenEMR code after it, puts the
    dialog's "No matches were found." on the step that opens it (#create)."""

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.ir import PostconditionKind

    workflow = compile_recording(recording, tmp_path / "raw", name="referral")
    opener = [
        s
        for s in workflow.steps
        if s.anchor is not None
        and s.anchor.structural is not None
        and s.anchor.structural.selector == "#create"
    ]
    assert len(opener) == 1
    guards = [
        p
        for p in opener[0].expect
        if p.kind is PostconditionKind.TEXT_PRESENT and p.text == NO_MATCH
    ]
    assert len(guards) == 1 and guards[0].timeout_s == 10.0
    confirm = workflow.steps[workflow.steps.index(opener[0]) + 1]
    assert confirm.anchor is not None and confirm.anchor.structural is not None
    assert confirm.anchor.structural.selector == "#confirmCreate"


def test_a_possible_duplicate_halts_before_the_save(
    bundle: Path, tmp_path: Path
) -> None:
    """Seed Rosa Delgado born 1958-03-27, replay on Rosa Delgado born
    1958-03-22: OpenEMR's dialog lists the existing chart, the mined guard
    fails, and the run halts at #create with no new or changed chart."""

    from benchmark.openemr_local.qualify_hooks import (
        fixture,
        open_session,
        seed_existing_chart,
    )
    from openadapt_flow.benchmark.openemr_local import (
        OpenEMRPatientOracle,
        SyntheticPatientSpec,
    )
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualify import load_verify_config
    from openadapt_flow.runtime import Replayer
    from scripts.openemr_qualification import sql_patients

    fx = fixture()
    params = _rosa_params()
    spec = SyntheticPatientSpec(
        title=params["title"],
        fname=params["fname"],
        lname=params["lname"],
        dob=params["DOB"],
        sex=params["sex"],
        street=params["street"],
        city=params["city"],
        state_label=params["state_label"],
        postal_code=params["postal_code"],
        phone_home=params["phone_home"],
        email=params["email"],
    )
    fx.reset()
    seed_existing_chart()
    before = sql_patients(fx)
    config = load_verify_config(FLOW / "benchmark/openemr_local/verify.yaml")
    backend, close = open_session(config)
    workflow = Workflow.load(bundle)
    guard_step = next(
        s.id for s in workflow.steps if any(p.text == NO_MATCH for p in s.expect)
    )
    try:
        oracle = OpenEMRPatientOracle(fx.api_base_url, fx.token_session("oracle"), spec)
        report = Replayer(backend, effect_verifier=oracle.verifier).run(
            workflow,
            params=params,
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
    finally:
        close()
    time.sleep(1.0)
    after = sql_patients(fx)
    before_ids = {row["id"] for row in before}
    new_rows = [row for row in after if row["id"] not in before_ids]
    changed = [row for row in after if row["id"] in before_ids and row not in before]
    failed = [r.step_id for r in report.results if not r.ok]
    assert report.transaction_outcome == "HALTED_BEFORE_EFFECT", report.halt
    assert failed and failed[0] == guard_step
    assert new_rows == [] and changed == []


def test_certification_runs_the_existing_duplicate_rule(
    bundle: Path, tmp_path: Path
) -> None:
    """``openadapt-flow qualify --certify`` on the referral bundle runs the
    six cases and then the verify.yaml business rule on the certified bundle:
    every rule trial halts before the save with no new or changed record.
    One trial per case keeps this to a few minutes."""

    from openadapt_flow.qualify import load_verify_config, qualify

    config = load_verify_config(FLOW / "benchmark/openemr_local/verify.yaml")
    result: Any = qualify(
        bundle,
        config,
        out=tmp_path / "qualify",
        trials=1,
        certify=config.campaign.policy,
        log=lambda _line: None,
    )
    assert result.certified
    rule_rows = [r for r in result.rows if r["case_id"] == "rule-existing-duplicate"]
    assert len(rule_rows) == 1
    assert rule_rows[0]["transaction_outcome"] == "HALTED_BEFORE_EFFECT"
    assert rule_rows[0]["oracle"] == "no_mutation"
    entries = [
        c for c in result.result["cases"] if c["case_id"] == "rule-existing-duplicate"
    ]
    assert len(entries) == 1 and entries[0]["passed"] is True
    assert result.result["passed"] is True
