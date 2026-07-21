"""The standalone EffectBench artifact: reproducibility + parity with the engine.

EffectBench ships as a self-contained, pip-installable package under
``benchmark/effectbench/`` so a third party can run the Silent Wrong-Effect Rate
(SWER) benchmark WITHOUT the openadapt-flow codebase (pydantic is its only
dependency). Because the standalone package vendors the effect MECHANISM (the
typed ``Effect`` contract, the substrate-independent judge, the outcome
classifier) that is otherwise defined in ``openadapt_flow.runtime.effects`` and
``openadapt_flow.benchmark.effectbench``, this test guards against the two copies
drifting: it asserts the standalone classifier and judge agree BYTE-FOR-BYTE with
the engine on the reference inputs, and that the standalone reference reproduces
the published headline. It also re-runs the standalone source-availability
boundary check inside the engine CI.

The standalone package is not installed into the engine's environment; it is
imported directly from its subtree, exactly as a third party would ``pip
install ./benchmark/effectbench``.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest

_STANDALONE_ROOT = Path(__file__).resolve().parent.parent / "benchmark" / "effectbench"


@pytest.fixture(scope="module", autouse=True)
def _standalone_on_path():
    """Import the standalone ``effectbench`` package from its subtree."""
    root = str(_STANDALONE_ROOT)
    inserted = root not in sys.path
    if inserted:
        sys.path.insert(0, root)
    try:
        yield
    finally:
        if inserted and root in sys.path:
            sys.path.remove(root)
        for name in list(sys.modules):
            if name == "effectbench" or name.startswith("effectbench."):
                del sys.modules[name]


def test_standalone_reference_reproduces_the_published_headline():
    from effectbench.reference import reference_result

    result = reference_result()
    assert result["arms"]["screen_only"]["swer"]["numerator"] == 50
    assert result["arms"]["screen_only"]["swer"]["denominator"] == 90
    assert result["arms"]["screen_only"]["swer_wrong_write"]["numerator"] == 40
    assert result["arms"]["screen_only"]["swer_phantom"]["numerator"] == 10
    assert result["arms"]["effect_verified"]["swer"]["numerator"] == 0
    tm = result["transactional_silently_mishandled"]
    assert (tm["silent"], tm["total"]) == (5, 7)


def test_classifier_parity_with_engine_over_the_full_truth_table():
    """The standalone classifier equals the engine classifier on every input."""
    import effectbench.oracle as sa

    from openadapt_flow.benchmark.effectbench import oracle as eng

    scoreable = ["CORRECT", "WRONG_PERSISTED", "ABSENT"]
    for reported, state_name, avail in itertools.product(
        (True, False), scoreable, (True, False)
    ):
        sa_label, sa_variant, _ = sa.classify_outcome(
            reported_success=reported,
            true_state=sa.TrueEffectState[state_name],
            correct_action_available=avail,
        )
        eng_label, eng_variant, _ = eng.classify_outcome(
            reported_success=reported,
            true_state=eng.TrueEffectState[state_name],
            correct_action_available=avail,
        )
        assert sa_label.value == eng_label.value, (reported, state_name, avail)
        assert sa_variant.value == eng_variant.value, (reported, state_name, avail)


def test_judge_parity_with_engine_over_the_fault_states():
    """The standalone judge equals the engine judge on the reference fault states."""
    import effectbench.effect as sa_e
    import effectbench.judge as sa_j

    from openadapt_flow.runtime.effects import _common as eng_j
    from openadapt_flow.runtime.effects import effect as eng_e

    def _row(id_, note="n", type_="Triage", patient="p1"):
        return {
            "id": id_,
            "patient_id": patient,
            "type": type_,
            "note": note,
            "source": "replay",
            "key": None,
        }

    concurrent = _row(9, type_="Urgent")
    # (pre, post) fault states straight from the reference study.
    cases = {
        "ok": ([], [_row(1)]),
        "partial": ([], [_row(1, note="")]),
        "duplicate": ([], [_row(1), _row(2)]),
        "optimistic": ([], []),
        "stale": ([concurrent], [_row(1)]),
    }
    match = {"patient_id": "p1", "type": "Triage"}

    for pre, post in cases.values():
        sa_eff = sa_e.Effect(
            kind=sa_e.EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            forbid_collateral_loss=True,
            timeout_s=0.0,
        )
        eng_eff = eng_e.Effect(
            kind=eng_e.EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            forbid_collateral_loss=True,
            timeout_s=0.0,
        )
        sa_before = sa_e.EffectState(substrate="s", reachable=True, records=pre)
        eng_before = eng_e.EffectState(substrate="s", reachable=True, records=pre)
        sa_v = sa_j.judge_records(sa_eff, sa_before, post, substrate="s")
        eng_v = eng_j.judge_records(eng_eff, eng_before, post, substrate="s")
        assert sa_v.verdict.value == eng_v.verdict.value
        assert sa_v.observed_count == eng_v.observed_count


def test_contract_hash_parity_with_engine():
    """A standalone effect's contract hash equals the engine's (submission
    hashes cross-check against the reference implementation)."""
    import effectbench.effect as sa_e

    from openadapt_flow.runtime.effects import effect as eng_e

    match = {"patient_id": "p1", "type": "Triage"}
    sa_hash = sa_e.Effect(
        kind=sa_e.EffectKind.RECORD_WRITTEN, match=match, expected_count=1
    ).contract_hash()
    eng_hash = eng_e.Effect(
        kind=eng_e.EffectKind.RECORD_WRITTEN, match=match, expected_count=1
    ).contract_hash()
    assert sa_hash == eng_hash


def test_standalone_boundary_excludes_crown_jewels_and_the_engine():
    """No engine import and no crown-jewel token in the shipped package tree."""
    package_dir = _STANDALONE_ROOT / "effectbench"
    forbidden = (
        "openadapt_flow",
        "adversary_corpus",
        "grown_corpus",
        "tuned_adversary",
        "deployment_thresholds",
        "oracle_recipe",
        "real_emr",
        "held_out_corpus",
        "openemr",
        "frappe",
        "openimis",
    )
    for path in sorted(package_dir.rglob("*.py")):
        lowered = path.read_text(encoding="utf-8").lower()
        assert "import openadapt_flow" not in lowered, path
        assert "from openadapt_flow" not in lowered, path
        for token in forbidden:
            assert token not in lowered, f"{token!r} leaked into {path}"


def test_standalone_wheel_packages_only_the_effectbench_package():
    """The standalone pyproject packages only ``effectbench`` -- the sibling
    container recipes and the engine re-expression never ride in the artifact."""
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    pyproject = (_STANDALONE_ROOT / "pyproject.toml").read_text()
    data = tomllib.loads(pyproject)
    assert data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        "effectbench"
    ]
    sdist = data["tool"]["hatch"]["build"]["targets"]["sdist"]
    assert "/task_pack" in sdist["exclude"]
    assert "/reference_fault_model.py" in sdist["exclude"]
