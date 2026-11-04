"""The RL environment contract: reward rule, licences, task packs, staging."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from openadapt_flow.rl_env.reward import episode_reward, render_verify_config
from openadapt_flow.rl_env.tasks import (
    EXTRA_TASKS_ENV,
    Licence,
    TaskError,
    load_environment,
    read_tasks,
)

ROOT = Path(__file__).resolve().parents[1]
ENVS = ROOT / "environments" / "openenv"
SERVABLE = sorted(
    p.parent.parent for p in ENVS.glob("*/tasks/public.jsonl") if p.read_text().strip()
)


@pytest.mark.parametrize(
    ("verdict", "claim", "feasible", "reward"),
    [
        ("CONFIRMED", "success", True, 1.0),
        ("CONFIRMED", "failure", True, 0.0),
        ("REFUTED", "success", True, 0.0),
        ("REFUTED", "failure", True, 0.0),
        ("REFUTED", "failure", False, 1.0),
        # A silent wrong effect: the screen said saved, the record says no.
        ("REFUTED", "success", False, 0.0),
        ("CONFIRMED", "failure", False, 0.0),
        ("INDETERMINATE", "success", True, 0.0),
        ("INDETERMINATE", "failure", False, 0.0),
        ("CONFIRMED", None, True, 0.0),
    ],
)
def test_reward_follows_the_record(verdict, claim, feasible, reward) -> None:
    assert episode_reward(verdict, claim, feasible) == reward


def test_failure_report_pays_only_after_the_write_was_sent() -> None:
    assert episode_reward("REFUTED", "failure", False, attempted=False) == 0.0
    assert episode_reward("REFUTED", "failure", False, attempted=True) == 1.0


def test_infeasible_tasks_need_declared_write_requests(tmp_path: Path) -> None:
    import shutil

    import yaml

    env_dir = tmp_path / "env"
    shutil.copytree(ENVS / "mockmed", env_dir)
    manifest = yaml.safe_load((env_dir / "env.yaml").read_text())
    manifest["agent"]["write_requests"] = []
    (env_dir / "env.yaml").write_text(yaml.safe_dump(manifest))
    with pytest.raises(TaskError, match="write_requests"):
        load_environment(env_dir)


def test_copyleft_app_cannot_be_bundled() -> None:
    with pytest.raises(ValueError, match="upstream image"):
        Licence(environment="MIT", app="AGPL-3.0-only", app_source="bundled")
    with pytest.raises(ValueError, match="name its images"):
        Licence(environment="MIT", app="GPL-3.0", app_source="upstream_image")
    Licence(
        environment="MIT",
        app="GPL-3.0",
        app_source="upstream_image",
        upstream_images=["example/app:1@sha256:" + "0" * 64],
    )


def test_every_environment_records_its_licence() -> None:
    manifests = sorted(ENVS.glob("*/env.yaml"))
    assert {p.parent.name for p in manifests} >= {
        "mockmed",
        "mockloan",
        "openemr",
        "frappe_lending",
        "openimis_claims",
    }
    import yaml

    for path in manifests:
        licence = Licence.model_validate(yaml.safe_load(path.read_text())["licence"])
        if licence.app_source == "upstream_image":
            assert all("@sha256:" in image for image in licence.upstream_images)


def _task(**overrides) -> dict:
    task = {
        "id": "t1",
        "instruction": "do it",
        "solution": [{"kind": "done", "claim": "success"}],
    }
    task.update(overrides)
    return task


def test_pack_refuses_a_non_public_split(tmp_path: Path) -> None:
    pack = tmp_path / "public.jsonl"
    pack.write_text(json.dumps(_task(split="heldout")) + "\n")
    with pytest.raises(TaskError, match="only the 'public' split"):
        read_tasks(pack, public_only=True)


def test_pack_needs_a_solution_that_ends_in_done(tmp_path: Path) -> None:
    pack = tmp_path / "public.jsonl"
    pack.write_text(
        json.dumps(_task(solution=[{"kind": "click", "selector": "#x"}])) + "\n"
    )
    with pytest.raises(TaskError, match="ends with a done"):
        read_tasks(pack, public_only=True)


def test_extra_splits_load_only_from_outside(tmp_path: Path, monkeypatch) -> None:
    inside = ENVS / "mockmed" / "tasks" / "extra.jsonl"
    monkeypatch.setenv(EXTRA_TASKS_ENV, str(inside))
    with pytest.raises(TaskError, match="outside"):
        load_environment(ENVS / "mockmed")
    outside = tmp_path / "blind.jsonl"
    outside.write_text(json.dumps(_task(id="blind-1", split="blind")) + "\n")
    monkeypatch.setenv(EXTRA_TASKS_ENV, str(outside))
    env = load_environment(ENVS / "mockmed")
    assert "blind" in env.splits()
    assert env.task("blind-1").split == "blind"


@pytest.mark.parametrize("env_dir", SERVABLE, ids=lambda p: p.name)
def test_environment_loads_and_hides_its_answers(env_dir: Path) -> None:
    env = load_environment(env_dir)
    assert env.tasks and all(t.split == "public" for t in env.tasks)
    for task in env.tasks:
        view = json.dumps(task.public_view())
        assert task.fault is None or f"fault={task.fault}" not in view
        assert "solution" not in view
    cfg = render_verify_config(
        env.verify_text, "http://127.0.0.1:1", "http://127.0.0.1:2"
    )
    assert cfg.effects


def test_committed_mock_packs_match_the_generator() -> None:
    spec = importlib.util.spec_from_file_location(
        "make_mock_tasks", ENVS / "make_mock_tasks.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for path, build in module.PACKS.items():
        assert path.read_text() == module.render(build()), path


def test_feasibility_matches_the_fault_server() -> None:
    env = load_environment(ENVS / "mockmed")
    feasible = {t.fault for t in env.tasks if t.feasible}
    assert feasible == {"ok", "idempotent", "timeout"}


@pytest.mark.parametrize("name", ["mockmed", "mockloan"])
def test_staged_space_passes_openenv_static_validation(
    name: str, tmp_path: Path
) -> None:
    pytest.importorskip("openenv")
    from openenv.validation.parsers.openenv_yaml import OpenEnvYamlParser

    sys.path.insert(0, str(ENVS))
    try:
        from stage import stage
    finally:
        sys.path.remove(str(ENVS))
    out = stage(
        name, tmp_path / name, wheels=[], flow_requirement="openadapt-flow[openenv]"
    )
    manifest = OpenEnvYamlParser().parse(out)
    tasks = load_environment(out).tasks
    assert manifest.capabilities.declared_task_count == {"public": len(tasks)}
    assert (out / "server" / "Dockerfile").is_file()
    assert (out / "oracle" / "solve.sh").is_file()
    assert (out / "server" / "app.py").is_file()


def test_public_packs_hold_no_heldout_family_and_no_fault_in_ids() -> None:
    mockmed = load_environment(ENVS / "mockmed")
    heldout = {"no_persist", "wrong_record", "duplicate"}
    assert not {t.fault for t in mockmed.tasks} & heldout
    assert load_environment(ENVS / "mockloan").tasks == []
    for task in mockmed.tasks:
        assert task.fault not in task.id and task.fault not in task.tags


def test_extra_tasks_cannot_skip_the_write_requests_rule(tmp_path, monkeypatch):
    extra = tmp_path / "x.jsonl"
    extra.write_text(
        json.dumps(_task(id="x-1", split="heldout", feasible=False)) + "\n"
    )
    env_dir = tmp_path / "env"
    import shutil

    import yaml

    shutil.copytree(ENVS / "mockloan", env_dir)
    manifest = yaml.safe_load((env_dir / "env.yaml").read_text())
    manifest["agent"]["write_requests"] = []
    (env_dir / "env.yaml").write_text(yaml.safe_dump(manifest))
    monkeypatch.setenv(EXTRA_TASKS_ENV, str(extra))
    with pytest.raises(TaskError, match="write_requests"):
        load_environment(env_dir)


def test_missing_environment_files_fail_as_task_errors() -> None:
    with pytest.raises(TaskError):
        load_environment(ENVS / "openimis_claims")


def test_manifest_states_its_checks_and_hashes_its_pack() -> None:
    env = load_environment(ENVS / "mockmed")
    assert env.manifest.checks
    assert env.pack_sha256["public.jsonl"].startswith("sha256:")


PRIME = ROOT / "environments" / "prime"


def _run_eval_module():
    path = PRIME / "openadapt_web_envs" / "openadapt_web_envs" / "run_eval.py"
    spec = importlib.util.spec_from_file_location("run_eval", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_eval_wrapper_never_uploads_without_the_explicit_flag() -> None:
    run_eval = _run_eval_module()
    argv = run_eval.build_argv(["--env.taskset.base-url", "http://x", "-m", "m"])
    assert argv[0] == "openadapt-web-envs"
    assert "--no-push" in argv
    assert argv[argv.index("--env.player.runtime.type") + 1] == "subprocess"
    with pytest.raises(SystemExit, match="allow-upload"):
        run_eval.build_argv(["--push", "True"])
    allowed = run_eval.build_argv(["--allow-upload"])
    assert "--no-push" not in allowed and "--allow-upload" not in allowed


def test_our_tooling_calls_eval_only_through_the_wrapper() -> None:
    import re

    direct = re.compile(
        r"(^|[\s\"'/])(uv run )?(vf-)?eval(\"|')?[\s,]+(\"|')?openadapt-web-envs"
    )
    offenders = []
    for path in (ROOT / "environments").rglob("*"):
        if path.suffix not in {".py", ".md", ".sh", ".toml", ".yaml"}:
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if direct.search(line) and "run_eval" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == []


def test_a_stuck_step_ends_the_episode_with_no_reward(monkeypatch) -> None:
    pytest.importorskip("openenv")
    import time

    from openadapt_flow.rl_env import environment as E

    monkeypatch.setattr(E, "STEP_TIMEOUT_S", 0.2)
    env = E.OpenAdaptWebEnvironment(ENVS / "mockmed")
    env._task = env.env.tasks[0]
    env._step = lambda *a, **k: time.sleep(2)  # type: ignore[method-assign]
    obs = env.step(E.WebAction(kind="wait", seconds=1))
    assert (obs.done, obs.reward, obs.verdict) == (True, 0.0, "INDETERMINATE")
    assert env._worker is None and env._task is None and env.state.done
