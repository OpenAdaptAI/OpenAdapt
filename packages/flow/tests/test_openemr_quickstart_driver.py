"""The OpenEMR driver behind ``openadapt quickstart --app openemr`` (no Docker).

The fixture's commands run through a fake runner here; the real run is the
launcher's opt-in ``test_real_openemr_quickstart``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from benchmark.openemr_local import fixture as fx
from benchmark.openemr_local import quickstart as q
from openadapt_flow.ir import PostconditionKind, Workflow

#: The certified bundle's content digest (README, site Seals, #1409).
DIGEST = "ec096afa2b614326b00ec3155f256041c19d86f3998761ce9d772cfd911460b6"
RECORDING = "243fb7853b228a2e0fd93c51c1d43e7b84fff567f1aac917bdcd91a54d044894"


def test_the_committed_program_is_the_certified_bundle(tmp_path):
    workflow = q.copy_program(tmp_path / "bundle")
    assert workflow.manifest is not None
    assert workflow.manifest.content_digest == DIGEST
    assert workflow.recording_id == RECORDING
    assert workflow.name == "openemr-create-synthetic-patient"
    assert len(workflow.steps) == 29
    assert workflow.qualification is not None


def test_the_program_carries_the_shared_duplicate_rule(tmp_path):
    workflow = q.copy_program(tmp_path / "bundle")
    create = next(
        step
        for step in workflow.steps
        if step.anchor is not None
        and step.anchor.structural is not None
        and step.anchor.structural.selector == "#create"
    )
    texts = {p.text for p in create.expect if p.kind is PostconditionKind.TEXT_PRESENT}
    assert texts == {"Confirm Create New Patient", "No matches were found."}
    save = workflow.steps[workflow.steps.index(create) + 1]
    assert save.risk == "irreversible" and save.effects


def test_a_changed_program_file_is_refused(tmp_path):
    q.copy_program(tmp_path / "bundle")
    template = next((tmp_path / "bundle" / "templates").glob("step_*.png"))
    template.write_bytes(template.read_bytes() + b"x")
    with pytest.raises(Exception):
        Workflow.load(tmp_path / "bundle")


def test_committed_files_hold_no_urls_or_session_tokens():
    for path in q.PROGRAM_DIR.rglob("*"):
        if path.suffix in (".json", ".jsonl", ".py", ".md"):
            text = path.read_text(encoding="utf-8")
            assert "token_main" not in text, path
            assert "csrf" not in text.lower(), path
    assert "http" not in q.DEMONSTRATION.read_text()


def test_the_demonstration_is_the_recordings_action_log():
    events = q.demonstration()
    assert [event["i"] for event in events] == list(range(41))
    assert events[-1]["selector"] == "#confirmCreate"
    typed = {
        event["param"]: event["text"] for event in events if event["kind"] == "type"
    }
    assert typed["fname"] == "OpenAdapt" and typed["email"].endswith("@example.invalid")


def test_act_two_and_three_use_the_referral_benchmarks_patients():
    names = [q.referral(key) for key in q.REFERRALS]
    assert len({spec.email for spec in names}) == 5
    assert all(spec.email.endswith("@example.invalid") for spec in names)
    duplicate, existing = q.referral(q.DUPLICATE), q.existing_chart()
    assert (duplicate.fname, duplicate.lname) == (existing.fname, existing.lname)
    assert duplicate.dob != existing.dob


# -- the fixture ------------------------------------------------------------


class _Runner:
    """Records fixture commands; answers ``ps`` and ``exec`` as told."""

    def __init__(self, running: bool = True, healthy: bool = True) -> None:
        self.running = running
        self.healthy = healthy
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(list(argv))
        out = b""
        code = 0
        if "ps" in argv:
            out = b"openemr\ndb\n" if self.running else b""
        if "readyz" in " ".join(argv) and not self.healthy:
            code = 1
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=b"")


def _bootstrapped(state: Path) -> None:
    state.mkdir(parents=True, mode=0o700)
    (state / "runtime.env").write_text(
        "".join(
            f"{k}={v}\n"
            for k, v in {
                "OPENEMR_IMAGE": json.loads(fx.LOCK_PATH.read_text())["services"][
                    "openemr"
                ],
                "MARIADB_IMAGE": json.loads(fx.LOCK_PATH.read_text())["services"][
                    "mariadb"
                ],
                "OPENEMR_ACTOR_USER": "openadapt_actor",
                "OPENEMR_HTTP_PORT": q.UI_PORT,
                "OPENEMR_HTTPS_PORT": q.API_PORT,
                "MARIADB_ROOT_PASSWORD": "a" * 32,
                "OPENEMR_DB_PASSWORD": "b" * 32,
                "OPENEMR_ACTOR_PASSWORD": "c" * 32,
            }.items()
        )
    )
    for name in ("oauth-clients.json", "baseline.sql", "baseline.sql.sha256"):
        (state / name).write_text("{}")


def _fixture(tmp_path: Path, runner: _Runner, monkeypatch) -> q.QuickstartFixture:
    fixture = q.QuickstartFixture(tmp_path / "state", runner=runner)
    monkeypatch.setattr(fixture, "_compose_path", lambda: tmp_path / "compose.yml")
    return fixture


def test_own_project_and_loopback_ports(tmp_path):
    fixture = q.QuickstartFixture(tmp_path / "state")
    assert (
        fixture.lock["project"]
        == q.PROJECT
        != json.loads(fx.LOCK_PATH.read_text())["project"]
    )
    assert fixture.ui_base_url == f"http://127.0.0.1:{q.UI_PORT}"
    assert fixture.api_base_url == f"https://127.0.0.1:{q.API_PORT}"


def test_a_running_fixture_is_reused_and_reset(tmp_path, monkeypatch):
    runner = _Runner()
    fixture = _fixture(tmp_path, runner, monkeypatch)
    _bootstrapped(fixture.state_dir)
    resets = []
    monkeypatch.setattr(fixture, "reset", lambda: resets.append(1))
    said = []
    assert q.bring_up(fixture, lambda phase, text: said.append(phase)) == "reused"
    assert resets == [1]
    assert not any("up" in call for call in runner.calls), (
        "a running copy isn't restarted"
    )
    assert all(
        "-p" in call and q.PROJECT in call for call in runner.calls if "compose" in call
    )


def test_a_stopped_fixture_is_started_not_rebuilt(tmp_path, monkeypatch):
    runner = _Runner(running=False)
    fixture = _fixture(tmp_path, runner, monkeypatch)
    _bootstrapped(fixture.state_dir)
    monkeypatch.setattr(fixture, "reset", lambda: None)
    monkeypatch.setattr(fixture, "_wait_ready", lambda timeout_s=0: None)
    monkeypatch.setattr(fixture, "bootstrap", lambda: pytest.fail("rebuilt"))
    assert q.bring_up(fixture, lambda *a: None) == "restarted"
    assert any(call[-2:] == ["up", "-d"] for call in runner.calls)


def test_a_new_fixture_is_set_up_once(tmp_path, monkeypatch):
    runner = _Runner()
    fixture = _fixture(tmp_path, runner, monkeypatch)
    order = []
    for name in (
        "runtime_preflight",
        "prepare",
        "image_identity",
        "bootstrap",
        "snapshot",
    ):
        monkeypatch.setattr(fixture, name, lambda name=name: order.append(name))
    monkeypatch.setattr(
        fixture, "_wait_ready", lambda timeout_s=0: order.append("ready")
    )
    monkeypatch.setattr(
        fixture, "_verify_running_source", lambda: order.append("source")
    )
    monkeypatch.setattr(fixture, "_validate_runtime_env", lambda: None)
    monkeypatch.setattr(
        q, "pull", lambda image, label, say: order.append(f"pull {label}")
    )
    assert q.bring_up(fixture, lambda *a: None) == "created"
    assert order == [
        "runtime_preflight",
        "prepare",
        "pull OpenEMR 8.0.0.3",
        "pull MariaDB 11.8",
        "image_identity",
        "ready",
        "source",
        "bootstrap",
        "snapshot",
    ]


def test_pull_reports_layers_as_docker_finishes_them(monkeypatch):
    monkeypatch.setattr(q, "_image_present", lambda image: False)

    class _Proc:
        stdout = iter(
            [
                "8.0.0.3: Pulling from openemr/openemr\n",
                "aaaaaaaaaaaa: Pulling fs layer\n",
                "bbbbbbbbbbbb: Already exists\n",
                "aaaaaaaaaaaa: Pull complete\n",
            ]
        )

        def wait(self):
            return 0

    said = []
    q.pull(
        "img",
        "OpenEMR",
        lambda phase, text: said.append(text),
        popen=lambda *a, **k: _Proc(),
    )
    assert said[-1] == "OpenEMR: 2 of 2 layers"
    assert "OpenEMR: 1 of 2 layers" in said


def test_a_present_image_is_not_pulled(monkeypatch):
    monkeypatch.setattr(q, "_image_present", lambda image: True)
    said = []
    q.pull(
        "img",
        "OpenEMR",
        lambda phase, text: said.append(text),
        popen=lambda *a, **k: pytest.fail("pulled"),
    )
    assert said == ["OpenEMR: already downloaded"]


def test_checked_needs_the_engine_and_the_database_to_agree():
    facts = {
        "execution_outcome": "VERIFIED",
        "transaction_outcome": "VERIFIED",
        "effects_required": 11,
        "effects_confirmed": 11,
        "charts": 1,
        "fields_matched": 12,
        "fields_total": 12,
    }
    assert q.checked(facts)
    for change in (
        {"execution_outcome": "COMPLETED_UNVERIFIED"},
        {"effects_confirmed": 10},
        {"charts": 2},
        {"fields_matched": 11},
        {"effects_required": 0, "effects_confirmed": 0},
    ):
        assert not q.checked({**facts, **change}), change
