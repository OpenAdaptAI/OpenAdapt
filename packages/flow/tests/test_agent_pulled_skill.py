"""openadapt-agent refuses a pulled skill until it is qualified for the app.

These drive the real path: ``registry.pull`` of a bundle from a registry
index on disk, then AgentBridge -> RunService/FlowCliEngine -> FlowRunner ->
the real ``openadapt-flow run`` subprocess. Nothing in the refusal path is
stubbed.

Before this fix the agent listed an unqualified pulled bundle as available,
started the CLI, and reported the CLI's pulled-skill refusal (exit 1, no
report) as ``result_unreadable``: "not sure if saved, have a person check
the record", for a run that never started. A qualified skill could never
pass at all: ``current_certification_matches`` needs a trusted policy, and
the pulled-skill check passed none.

The unqualified cases pull the repository's own ``openemr/new-referral``
bundle. The qualified cases need a real certification that Flow recomputes
in the CLI process, so they pull a small certifiable bundle through the same
``pull`` and qualify it with ``skill qualify``. Only the qualification
campaign against a live app is replaced: it certifies the bundle in place
with ``certify_project`` on signed case evidence, as
``tests/test_repair_readmission.py`` does.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow import registry, skills
from openadapt_flow.agent.bridge import AgentBridge
from openadapt_flow.agent.runner import FlowRunner, RunnerConfig
from openadapt_flow.ir import Workflow
from openadapt_flow.policy import load_policy
from openadapt_flow.qualification import certify_project, save_qualified_workflow
from openadapt_flow.verification import VerificationTier
from tests.test_qualification_project import (
    _configure,
    _qualification_visual_fixture,
    _record_passing_campaign,
    _workflow,
)
from tests.test_skills_registry import CONTRACT, VARS

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8765"
OTHER = "http://127.0.0.1:8766"


@pytest.fixture(autouse=True)
def _own_state(tmp_path, monkeypatch):
    """Own pull ledger, and the CLI subprocess imports this source tree."""

    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "openadapt-home"))
    monkeypatch.setenv(
        "PYTHONPATH",
        os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH")) if p),
    )


def _pull_new_referral(tmp_path: Path) -> Path:
    """The repository's openemr/new-referral, pulled from its index."""

    return registry.pull(
        "openemr/new-referral",
        tmp_path / "bundles" / "new-referral",
        index_url=str(ROOT / "registry/index.json"),
        allow_unsigned=True,
    )


def _pull_certifiable(tmp_path: Path) -> Path:
    """A certifiable bundle with a shipped contract, published and pulled."""

    reg = tmp_path / "registry"
    shutil.copytree(ROOT / "registry", reg)
    bundle = reg / "bundles/openemr/new-referral"
    shutil.rmtree(bundle)
    (bundle / "templates").mkdir(parents=True)
    (bundle / "templates/save.png").write_bytes(_qualification_visual_fixture()[1])
    _workflow().save(bundle)
    (bundle / "verify.yaml").write_text(CONTRACT)
    index = json.loads((reg / "index.json").read_text())
    for item in index["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["files"] = registry.build_entry_files(bundle)
            item.update(
                version="1.0.0",
                contract="verify.yaml",
                contract_vars=VARS,
                app_license="GPL-3.0",
            )
    (reg / "index.json").write_text(json.dumps(index))
    return registry.pull(
        "openemr/new-referral",
        tmp_path / "bundles" / "skill",
        index_url=str(reg / "index.json"),
        allow_unsigned=True,
    )


def _campaign(bundle, config, *, out, certify, **_kw):
    """Certify the bundle in place on the bound instance, as qualify does."""

    workflow = Workflow.load(bundle)
    _configure(
        workflow, tier=VerificationTier.INDEPENDENT_SYSTEM, identity=config.app.origin
    )
    _record_passing_campaign(workflow, Path(out))
    assert certify_project(
        workflow, policy=load_policy(certify), evidence_root=Path(out)
    ).passed
    save_qualified_workflow(workflow, bundle)
    return SimpleNamespace(certified=True)


def _qualified(tmp_path: Path) -> Path:
    dest = _pull_certifiable(tmp_path)
    state = tmp_path / "state.json"
    state.write_text("{}")
    skills.bind(
        dest,
        {
            "APP_URL": ORIGIN,
            "RECORD_URL": ORIGIN,
            "RECORD_TOKEN_VAR": "MY_EMR_TOKEN",
            "SIGNED_IN_STATE": str(state),
            "RESET_COMMAND": "./reset-test-copy.sh",
        },
    )
    skills.qualify_skill(
        dest, certify="clinical-write", run=_campaign, log=lambda _m: None
    )
    return dest


def _bridge(tmp_path: Path, **config) -> AgentBridge:
    cfg = RunnerConfig(runs_dir=tmp_path / "runs", timeout_s=300.0, **config)
    return AgentBridge(tmp_path / "bundles", cfg, allow_run=True, mode="production")


def _only(bridge: AgentBridge) -> tuple[str, str, dict[str, str]]:
    ((name, entry),) = bridge.catalog.items()
    return name, entry.legacy_id, dict(entry.info.params)


def _run_workflow(bridge: AgentBridge, **extra) -> dict:
    name, _, params = _only(bridge)
    return bridge.dispatch(
        "run_workflow",
        {
            "workflow": name,
            "inputs": {**params, **extra},
            "request_id": "req-0001",
            "wait_seconds": 120,
        },
    )


def _assert_refused(result: dict, check: str, tmp_path: Path) -> None:
    assert result["reason"] == "not_ready_to_run"
    assert result["outcome"] == "did_not_run"
    assert result["record_changed"] == "no"
    assert result["failed_checks"] == [check]
    # The agent never sees the bundle path or the qualified address.
    text = json.dumps(result)
    assert str(tmp_path) not in text and "127.0.0.1" not in text


def _flow_started(tmp_path: Path) -> bool:
    """Whether any governed Flow run wrote a run directory."""

    runs = tmp_path / "runs"
    return runs.is_dir() and any(
        p.is_dir() and p.name.startswith("run-") for p in runs.iterdir()
    )


# -- an unqualified pulled skill ---------------------------------------------------


def test_an_unqualified_pulled_skill_is_refused_before_flow_starts(tmp_path):
    _pull_new_referral(tmp_path)
    bridge = _bridge(tmp_path, url=ORIGIN)

    _assert_refused(_run_workflow(bridge), "skill_not_qualified", tmp_path)
    _, legacy_id, params = _only(bridge)
    legacy = bridge.dispatch(f"run_{legacy_id}", params)
    assert legacy["status"] == "refused"
    assert legacy["reason"] == "not_ready_to_run"
    assert legacy["failed_checks"] == ["skill_not_qualified"]
    assert not _flow_started(tmp_path)


def test_the_clis_own_pulled_skill_refusal_reaches_the_agent_typed(tmp_path):
    """Past the agent's check (a race, say), the CLI still refuses it."""

    dest = _pull_new_referral(tmp_path)
    runner = FlowRunner(RunnerConfig(runs_dir=tmp_path / "runs", url=ORIGIN))
    outcome = runner._run(
        workflow="w", bundle_dir=dest, params=dict(Workflow.load(dest).params)
    )
    assert outcome.exit_code == 1
    assert outcome.status == "refused"
    assert outcome.reason == "not_ready_to_run"
    assert outcome.failed_checks == ["skill_not_qualified"]


def test_stripping_the_provenance_is_still_refused(tmp_path):
    dest = _pull_new_referral(tmp_path)
    (dest / skills.LOCK).unlink()
    workflow = Workflow.load(dest)
    workflow.registry = None
    workflow.save(dest)
    assert Workflow.load(dest).registry is None
    bridge = _bridge(tmp_path, url=ORIGIN)

    _assert_refused(_run_workflow(bridge), "skill_not_qualified", tmp_path)
    assert not _flow_started(tmp_path)


def test_an_ordinary_bundle_is_not_affected(tmp_path):
    shutil.copytree(
        ROOT / "registry/bundles/openemr/new-referral", tmp_path / "bundles" / "b"
    )
    assert not skills.is_pulled_skill(tmp_path / "bundles" / "b")
    runner = FlowRunner(RunnerConfig(runs_dir=tmp_path / "runs"))
    assert (
        runner._pulled_skill_refusal("w", tmp_path / "bundles" / "b", None, None)
        is None
    )


# -- a qualified pulled skill -------------------------------------------------------


def test_a_qualified_skill_passes_the_gate_on_its_own_address(tmp_path):
    dest = _qualified(tmp_path)
    # A real certification, recomputed against the policy skill qualify used.
    assert skills.pulled_skill_refusal(dest, target_url=f"{ORIGIN}/app") is None
    bridge = _bridge(tmp_path, url=f"{ORIGIN}/")
    _, legacy_id, params = _only(bridge)

    outcome = bridge.dispatch(f"run_{legacy_id}", params)
    # The real CLI started and its admission gates decided: this test bundle
    # is not encrypted, so clinical-write refuses it there, not as a skill.
    assert outcome["reason"] == "not_ready_to_run"
    assert "skill_not_qualified" not in outcome["failed_checks"]
    assert "not_encrypted" in outcome["failed_checks"]


def test_a_qualified_skill_is_refused_on_another_address(tmp_path):
    _qualified(tmp_path)
    bridge = _bridge(tmp_path, url=OTHER)

    _assert_refused(_run_workflow(bridge), "skill_not_qualified", tmp_path)
    assert not _flow_started(tmp_path)


def test_a_url_override_to_another_address_is_refused(tmp_path):
    _qualified(tmp_path)
    bridge = _bridge(tmp_path, url=ORIGIN, allow_url_override=True)
    _, legacy_id, params = _only(bridge)
    info = bridge.workflows[legacy_id]
    from openadapt_flow.agent.bundles import url_override_key

    outcome = bridge.dispatch(
        f"run_{legacy_id}", {**params, url_override_key(info): f"{OTHER}/"}
    )
    assert outcome["status"] == "refused"
    assert outcome["failed_checks"] == ["skill_not_qualified"]
    assert not _flow_started(tmp_path)


def test_a_qualified_skill_without_an_address_is_refused(tmp_path):
    _qualified(tmp_path)
    bridge = _bridge(tmp_path)

    _assert_refused(_run_workflow(bridge), "app_target_missing", tmp_path)
    assert not _flow_started(tmp_path)


@pytest.mark.parametrize(
    "policy",
    ["permissive", "record-write", "clinical-write.yaml.missing", "policy.yaml", None],
)
def test_only_the_policy_skill_qualify_used_passes(tmp_path, monkeypatch, policy):
    """The lock names the policy; naming another one, or none, never passes."""

    dest = _qualified(tmp_path)
    assert skills.pulled_skill_refusal(dest, target_url=ORIGIN) is None
    lock = skills.read_lock(dest)
    if policy is None:
        del lock["qualified"]["policy"]
    else:
        lock["qualified"]["policy"] = policy
    if policy == "policy.yaml":
        # A relative path never loads, even when a file of that name exists.
        monkeypatch.chdir(tmp_path)
        shutil.copy(ROOT / "openadapt_flow/policies/clinical-write.yaml", "policy.yaml")
    (dest / skills.LOCK).write_text(json.dumps(lock))
    assert "no current certification" in skills.pulled_skill_refusal(
        dest, target_url=ORIGIN
    )


def test_a_published_bundle_cannot_ship_a_qualified_lock(tmp_path):
    """The index refuses a lock inside a bundle; pull writes its own."""

    qualified = _qualified(tmp_path / "first")
    lock = skills.read_lock(qualified)
    assert lock["qualified"]["policy"] == "clinical-write"
    reg = tmp_path / "registry"
    shutil.copytree(ROOT / "registry", reg)
    bundle = reg / "bundles/openemr/new-referral"
    shutil.rmtree(bundle)
    shutil.copytree(qualified, bundle)
    (bundle / skills.BINDING).unlink()
    index = json.loads((reg / "index.json").read_text())
    for item in index["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["files"] = registry.build_entry_files(bundle)
            item.update(version="1.0.0", app_license="GPL-3.0")
    (reg / "index.json").write_text(json.dumps(index))
    with pytest.raises(registry.RegistryError, match="not a file a public bundle"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "bundles" / "again",
            index_url=str(reg / "index.json"),
            allow_unsigned=True,
        )
