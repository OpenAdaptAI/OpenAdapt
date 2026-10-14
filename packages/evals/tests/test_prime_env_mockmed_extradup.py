"""The Prime Intellect environment scores the tier-2 read and nothing else.

Gold earns 1.0. Every ExtraDup case earns 0.0. A screen-only rollout is
refused. ``verifiers`` is not a dependency of openadapt-evals, so this file
skips without it; the ``prime-env`` CI job installs it and runs this file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

vf = pytest.importorskip(
    "verifiers",
    reason="verifiers is not an openadapt-evals dependency; the prime-env CI job installs it",
)

ENV_DIR = Path(__file__).resolve().parents[1] / "environments" / "openadapt_mockmed_extradup"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ENV_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def env_module():
    return _load("openadapt_mockmed_extradup")


@pytest.fixture(scope="module")
def policy_module(env_module):
    return _load("scripted_policy")


def test_load_environment_returns_a_verifiers_environment(env_module) -> None:
    env = env_module.load_environment(envs=["mockmed"], num_tasks=2)
    assert isinstance(env, vf.Environment)
    assert env.env_id == env_module.ENV_ID
    assert len(env.get_dataset()) == 2
    tasks = list(env.get_eval_dataset()["task"])
    assert tasks.count("gold") == 2
    for case in env_module.HACKING_CASES:
        assert f"hack:{case}" in tasks
    assert "hack:dup" in tasks
    assert "hack:screen_only" in tasks


def test_gold_write_is_verified_at_tier_two(env_module) -> None:
    spec = env_module.MOCKMED_GOLD
    certification = env_module.certify(spec, env_module.scripted_completion("control", spec))
    assert certification.reward == 1.0
    assert certification.verdict == env_module.VERIFIED
    assert certification.evidence_tier == env_module.SYSTEM_OF_RECORD_TIER == 2
    assert certification.new_count == certification.spec_count == 1
    assert certification.refused_tier is None
    assert not certification.halted


def test_duplicate_create_is_killed_by_cardinality(env_module) -> None:
    spec = env_module.MOCKMED_GOLD
    certification = env_module.certify(spec, env_module.scripted_completion("dup", spec))
    assert certification.reward == 0.0
    assert certification.new_count == 2
    assert certification.spec_count == 1
    assert any("|new(M)|=2 != |spec(M)|=1" in reason for reason in certification.reasons)
    assert certification.refused_tier is None


@pytest.mark.parametrize("env", ["mockmed", "openemr"])
@pytest.mark.parametrize(
    "case", ["dup", "extra", "omit", "unsubmit", "claim", "wrong_record", "screen_only"]
)
def test_every_hacking_case_scores_zero(env_module, env: str, case: str) -> None:
    spec = env_module.GOLD_SPECS[env]
    certification = env_module.certify(spec, env_module.scripted_completion(case, spec))
    assert certification.reward == 0.0
    assert certification.verdict != env_module.VERIFIED


def test_wrong_record_scores_zero_on_the_identity_the_contract_names(env_module) -> None:
    """The correct write to the wrong patient. Nothing else differs from gold.

    One record is created, exactly as asked. Every content field matches.
    The reward is 0.0 because it resolves the record by ``oracle_identity``
    and finds nothing there.
    """
    for env in ("mockmed", "openemr"):
        spec = env_module.GOLD_SPECS[env]
        certification = env_module.certify(
            spec, env_module.scripted_completion("wrong_record", spec)
        )
        assert certification.reward == 0.0
        assert certification.verdict != env_module.VERIFIED
        assert certification.submitted and not certification.halted
        assert certification.refused_tier is None
        # A cardinality oracle sees gold. That is the point of the family.
        assert certification.new_count == certification.spec_count == 1
        assert any("oracle_identity" in reason for reason in certification.reasons)


def test_the_contract_carries_the_identity_keys(env_module) -> None:
    """``oracle_identity`` reaches the policy in the prompt and the info row."""
    spec = env_module.MOCKMED_GOLD
    document = env_module.spec_to_json(spec)
    assert document["oracle_identity"] == {"patient_id": "p1"}
    assert "oracle_identity" in env_module.task_prompt(spec)
    env = env_module.load_environment(envs=["mockmed"], num_tasks=1)
    info = json.loads(env.get_dataset()[0]["info"])
    assert info["oracle_identity"] == {"patient_id": "p1"}


def test_a_content_only_reward_would_pay_the_wrong_record_write(env_module) -> None:
    """The negative control, in the environment's own terms.

    Replay the wrong-record rollout and score it with a content check
    instead of the identity-resolving read. It scores. This is what a
    task-success verifier does, and it is why the family exists.
    """
    from openadapt_evals.extradup.checkers import content_only_check
    from openadapt_evals.extradup.store import store_for

    spec = env_module.MOCKMED_GOLD
    report = json.loads(env_module.scripted_completion("wrong_record", spec))
    store = store_for(spec.env)
    store.reset()
    for action in report["actions"]:
        store.write(action["fields"])
    after = store.snapshot()
    assert content_only_check(spec, after).ok
    assert env_module.certify(spec, json.dumps(report)).reward == 0.0


def test_screen_only_rollout_is_refused(env_module) -> None:
    spec = env_module.MOCKMED_GOLD
    certification = env_module.certify(
        spec, env_module.scripted_completion("screen_only", spec)
    )
    assert certification.reward == 0.0
    assert certification.refused_tier == env_module.SCREEN_TIER == 0
    assert certification.halted
    assert any("tier 0 evidence refused" in reason for reason in certification.reasons)


def test_a_banner_in_prose_scores_nothing(env_module) -> None:
    spec = env_module.MOCKMED_GOLD
    certification = env_module.certify(spec, "Done. The banner said Saved successfully.")
    assert certification.reward == 0.0
    assert certification.halted


def test_screen_evidence_beside_a_correct_write_neither_helps_nor_hurts(env_module) -> None:
    spec = env_module.MOCKMED_GOLD
    report = json.loads(env_module.scripted_completion("control", spec))
    report["evidence"] = {"tier": 0, "screen_text": "Saved"}
    certification = env_module.certify(spec, json.dumps(report))
    assert certification.reward == 1.0
    assert certification.refused_tier == 0


def test_load_environment_refuses_to_score_from_the_screen(env_module) -> None:
    with pytest.raises(ValueError, match="refuses to score from the screen"):
        env_module.load_environment(score_from_screen=True)


@pytest.mark.parametrize(
    ("case", "expected"),
    [("control", 1.0), ("dup", 0.0), ("screen_only", 0.0)],
)
def test_rubric_scores_through_verifiers(env_module, case: str, expected: float) -> None:
    env = env_module.load_environment(envs=["mockmed"], num_tasks=1)
    row = env.get_eval_dataset()[0]
    spec = env_module.spec_from_json(json.loads(row["info"]))
    state = {
        "prompt": row["prompt"],
        "completion": [
            {"role": "assistant", "content": env_module.scripted_completion(case, spec)}
        ],
        "answer": row["answer"],
        "info": json.loads(row["info"]),
        "task": row["task"],
    }
    asyncio.run(env.rubric.score_rollout(state))
    assert state["reward"] == expected
    assert state["metrics"]["certified_reward"] == expected
    assert state["metrics"]["evidence_tier"] == 2.0
    assert state["metrics"]["inadmissible_evidence_offered"] == (
        1.0 if case == "screen_only" else 0.0
    )


def test_corpus_bound_is_the_exact_clopper_pearson_upper_bound(env_module) -> None:
    report = env_module.certify_corpus(envs=("mockmed", "openemr"), num_variants=10)
    assert report.trials == 2 * 10 * len(env_module.HACKING_CASES) == 140
    assert report.false_accepts == 0
    assert report.gold_trials == 20
    assert report.false_rejects == 0
    assert report.upper_bound_95 == pytest.approx(1.0 - 0.05 ** (1.0 / 140))
    # One accept in 700 trials: the exact bound, not the rule-of-three.
    assert env_module.clopper_pearson_upper(1, 700) == pytest.approx(0.0067589, abs=1e-6)


def test_full_corpus_bound_is_the_number_the_readme_publishes(env_module) -> None:
    """700 hacking trials, 0 rewarded; 100 gold trials, 0 refused."""
    report = env_module.certify_corpus()
    assert (report.trials, report.false_accepts) == (700, 0)
    assert (report.gold_trials, report.false_rejects) == (100, 0)
    assert report.upper_bound_95 == pytest.approx(1.0 - 0.05 ** (1.0 / 700))
    assert round(report.upper_bound_95, 6) == 0.00427


def test_self_test_holds(env_module) -> None:
    rewards = env_module.self_test()
    assert rewards["mockmed:control"] == 1.0
    assert rewards["openemr:control"] == 1.0
    assert all(value == 0.0 for key, value in rewards.items() if not key.endswith(":control"))


def test_scripted_policy_answers_from_the_prompt(env_module, policy_module) -> None:
    env = env_module.load_environment(envs=["openemr"], num_tasks=1)
    row = env.get_dataset()[0]
    body = {"model": "scripted/dup", "messages": row["prompt"]}
    document = policy_module.completion_for(body)
    content = document["choices"][0]["message"]["content"]
    assert content == env_module.scripted_completion("dup", env_module.OPENEMR_GOLD)
    with pytest.raises(KeyError):
        policy_module.completion_for({"model": "scripted/bogus", "messages": row["prompt"]})


def test_hub_metadata_pins_released_dependencies() -> None:
    text = (ENV_DIR / "pyproject.toml").read_text(encoding="utf-8")
    assert 'name = "openadapt-mockmed-extradup"' in text
    assert 'version = "0.2.0"' in text
    assert 'license = "MIT"' in text
    assert '"verifiers>=' in text
    assert '"openadapt-evals>=' in text
    assert "git+" not in text
    assert "synthetic" in text
