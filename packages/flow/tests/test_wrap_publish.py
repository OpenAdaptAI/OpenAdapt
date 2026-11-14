"""`openadapt wrap publish`: local gates, staging and the cloud boundary.

The bundle, its record check and the staging are real. The admission
signature check and the hosted push are replaced at their boundaries (the
signature check is covered by the lifecycle tests; push by the hosted tests);
the cloud definition API is a mocked HTTP client. No network, browser or model.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from openadapt_flow import hosted
from openadapt_flow.integrations.agents import recording
from openadapt_flow.ir import ActionKind, Step, Workflow
from openadapt_flow.jit import lifecycle
from openadapt_flow.runtime.effects.effect import Effect, EffectKind
from openadapt_flow.wrap import cli as wrap_cli
from openadapt_flow.wrap import publish

ORG = "11111111-1111-4111-8111-111111111111"
WORKFLOW = "22222222-2222-4222-8222-222222222222"
VERSION = "33333333-3333-4333-8333-333333333333"
QUALIFICATION = "44444444-4444-4444-8444-444444444444"
HOST = "https://cloud.example.test"
SECRET = "oa_test_key_value_never_printed_0123456789"


def _bundle(path: Path, *, effects: bool = True, saves: bool = True) -> Path:
    steps = [Step(id="step_000", intent="type", action=ActionKind.TYPE, text="x")]
    if saves:
        steps.append(
            Step(
                id="step_001",
                intent="click Save",
                action=ActionKind.CLICK,
                effects=[
                    Effect(
                        kind=EffectKind.RECORD_WRITTEN,
                        match={"type": "note"},
                        field="note",
                    )
                ]
                if effects
                else [],
            )
        )
    Workflow(name="t", steps=steps).save(path)
    (path / lifecycle.ADMISSION_FILE).write_text("{}")
    return path


class FakeCache:
    def __init__(self, root: Path, *, held: bool = False, admitted: bool = True):
        self.root = root
        self._held = held
        self._admitted = admitted
        self.task_dir = root / "cache" / "skill-abc"
        self.bundle = self.task_dir / "program" / "bundle"
        self.checks = self.task_dir / "verify.yaml"

    def held(self, prompt: str, params: Any) -> bool:
        return self._held

    def admitted_program(self, prompt: str, params: Any) -> dict[str, Path] | None:
        if not self._admitted:
            return None
        return {"bundle": self.bundle, "checks": self.checks, "task_dir": self.task_dir}

    def task_id(self, prompt: str, params: Any) -> str:
        return "skill-abc"


@pytest.fixture
def cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCache:
    fake = FakeCache(tmp_path)
    _bundle(fake.bundle)
    fake.checks.write_text(
        "schema_version: openadapt.verify/v1\n"
        "verifier:\n  kind: sqlite\n  path: /srv/app.db\n"
        "effects: []\n"
    )
    monkeypatch.setattr(lifecycle, "verify_admission", lambda *_a, **_k: {})
    return fake


def _definition(**overrides: Any) -> dict[str, Any]:
    body = {
        "org_id": ORG,
        "workflow_id": WORKFLOW,
        "method": "POST",
        "path": f"/api/v1/org/{ORG}/workflow/{WORKFLOW}",
        "binding": {
            "schema_version": "openadapt.execute-request/v1",
            "qualification_id": QUALIFICATION,
            "workflow_version": VERSION,
            "workflow_digest": "sha256:" + "a" * 64,
            "environment_id": "b" * 64,
            "effect_strength_schema_version": "1",
            "minimum_effect_strength": "independent_readback",
        },
        "parameter_schema": [{"name": "note", "type": "string", "required": True}],
    }
    body.update(overrides)
    return body


class FakeHttp:
    def __init__(self, status: int, body: Any) -> None:
        self.status = status
        self.body = body
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append(dict(url=url, **kwargs))
        return httpx.Response(self.status, json=self.body)


# -- local gates -----------------------------------------------------------------


def test_stage_ships_bundle_with_readback_and_manifest(cache: FakeCache) -> None:
    staged = publish.stage(cache, "Save {note}", ["note"])
    assert (staged.staged / "workflow.json").is_file()
    assert (
        staged.staged / publish.READBACK_PATH
    ).read_text() == cache.checks.read_text()
    manifest = json.loads((staged.staged / publish.MANIFEST_NAME).read_text())
    assert manifest["schema"] == publish.PUBLISH_SCHEMA
    assert manifest["saves"] == 1
    assert manifest["input_schema"]["required"] == ["note"]
    assert len(manifest["record_check_sha256"]) == 64
    # The cached admitted bundle itself is never modified.
    assert not (cache.bundle / publish.MANIFEST_NAME).exists()
    # Staging twice yields the same tree, so the sanitized derivative is reused.
    from openadapt_flow.sanitized_artifact import source_tree_sha256

    first = source_tree_sha256(staged.staged)
    again = publish.stage(cache, "Save {note}", ["note"])
    assert source_tree_sha256(again.staged) == first


def test_refuses_a_held_task(tmp_path: Path) -> None:
    with pytest.raises(publish.PublishError) as err:
        publish.stage(FakeCache(tmp_path, held=True), "Save {note}", ["note"])
    assert err.value.code == "TASK_HELD"


def test_refuses_without_admitted_program_and_says_how(tmp_path: Path) -> None:
    with pytest.raises(publish.PublishError) as err:
        publish.stage(FakeCache(tmp_path, admitted=False), "Save {note}", ["note"])
    assert err.value.code == "NOT_ADMITTED"
    assert "openadapt wrap --task" in err.value.message


def test_refuses_an_admission_that_does_not_verify(
    cache: FakeCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: Any, **_k: Any) -> None:
        raise lifecycle.AdmissionError("bad signature")

    monkeypatch.setattr(lifecycle, "verify_admission", refuse)
    with pytest.raises(publish.PublishError) as err:
        publish.stage(cache, "Save {note}", ["note"])
    assert err.value.code == "ADMISSION_INVALID"
    assert not (cache.task_dir.parent / "publish").exists()


@pytest.mark.parametrize(
    "case", ["no_checks_file", "no_verifier", "no_save", "save_without_effect"]
)
def test_refuses_a_program_without_a_read_back_check(
    cache: FakeCache, case: str
) -> None:
    if case == "no_checks_file":
        cache.checks.unlink()
    elif case == "no_verifier":
        cache.checks.write_text("schema_version: openadapt.verify/v1\neffects: []\n")
    else:
        import shutil

        shutil.rmtree(cache.bundle)
        _bundle(cache.bundle, saves=case != "no_save", effects=False)
    with pytest.raises(publish.PublishError) as err:
        publish.stage(cache, "Save {note}", ["note"])
    assert err.value.code == "NO_READ_BACK"
    assert not (cache.task_dir.parent / "publish").exists()


# -- cloud definition boundary ---------------------------------------------------


def test_definition_is_read_with_bearer_key_and_matched() -> None:
    http = FakeHttp(200, _definition())
    check = publish.fetch_definition(
        HOST,
        SECRET,
        org_id=ORG,
        workflow_id=WORKFLOW,
        qualification_id=QUALIFICATION,
        http=http,
    )
    assert check["callable"] is True
    (call,) = http.calls
    assert call["url"] == f"{HOST}/api/v1/org/{ORG}/workflow/{WORKFLOW}/definition"
    assert call["params"] == {"qualification_id": QUALIFICATION}
    assert call["headers"]["Authorization"] == f"Bearer {SECRET}"
    assert call["follow_redirects"] is False


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, {"error": "unauthorized"}, None),
        (404, {"error": "workflow_not_found"}, None),
        (
            409,
            {"error": "workflow_not_ready", "code": "first_run_approval"},
            "first_run_approval",
        ),
        (409, {"error": "workflow_not_ready", "code": "BAD CODE; rm -rf"}, None),
        (503, {"error": "x"}, None),
        (200, _definition(workflow_id="55555555-5555-4555-8555-555555555555"), None),
    ],
)
def test_definition_refusals_never_claim_callable(
    status: int, body: Any, code: Any
) -> None:
    check = publish.fetch_definition(
        HOST,
        SECRET,
        org_id=ORG,
        workflow_id=WORKFLOW,
        qualification_id=QUALIFICATION,
        http=FakeHttp(status, body),
    )
    assert check["callable"] is False
    assert check["code"] == code


def test_curl_example_uses_variable_not_key_and_refuses_unsafe_values() -> None:
    endpoint = publish.endpoint_url(HOST, ORG, WORKFLOW)
    examples = publish.curl_examples(endpoint, _definition())
    assert examples and all("$OPENADAPT_API_KEY" in line for line in examples)
    payload = json.loads(examples[0].split("-d '", 1)[1].rstrip("'"))
    assert payload["parameters"] == {"note": "<note>"}
    assert payload["qualification_id"] == QUALIFICATION
    unsafe = _definition()
    unsafe["binding"]["workflow_digest"] = "x' ; curl evil '"
    assert publish.curl_examples(endpoint, unsafe) == []
    with pytest.raises(publish.PublishError):
        publish.endpoint_url(HOST, "../other", WORKFLOW)


# -- the command, end to end against mocked boundaries --------------------------


@pytest.fixture
def command(
    cache: FakeCache, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> dict[str, Any]:
    setup = SimpleNamespace(
        cache=cache, instruction="Save {note}", parameters=("note",)
    )
    monkeypatch.setattr(recording, "load_task", lambda *_a, **_k: setup)
    monkeypatch.setattr(hosted, "resolve_host", lambda host=None: HOST)
    monkeypatch.setattr(hosted, "resolve_token", lambda *a, **k: "ingest")
    state: dict[str, Any] = {"pushes": [], "approved": False}
    derivative = tmp_path / "sanitized" / "artifact-1"

    def push(path: Any, **kwargs: Any) -> dict[str, Any]:
        state["pushes"].append((Path(path), kwargs))
        if Path(path) != derivative:
            return {
                "uploaded": False,
                "pending_review": True,
                "sanitized_path": str(derivative),
            }
        return {
            "uploaded": True,
            "status": "accepted",
            "workflow_id": WORKFLOW,
            "dashboard_url": f"{HOST}/dashboard/workflows/{WORKFLOW}",
            "version": {
                "id": VERSION,
                "org_id": ORG,
                "workflow_id": WORKFLOW,
                "runtime_validation_id": QUALIFICATION,
                "version": 1,
            },
        }

    monkeypatch.setattr(hosted, "push", push)
    monkeypatch.setattr(publish, "_approved", lambda _path: state["approved"])
    for name in publish.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    state["derivative"] = derivative
    return state


def test_command_walks_review_then_validation_then_publishes(
    command: dict[str, Any],
    cache: FakeCache,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    task = ["publish", "--task", "task.json"]
    assert wrap_cli.main(task) == publish.EXIT_WAITING
    out = capsys.readouterr().out
    assert "approve-sanitized" in out and "Nothing has been uploaded" in out

    command["approved"] = True
    assert wrap_cli.main(task) == publish.EXIT_WAITING
    assert "validate-hosted" in capsys.readouterr().out
    assert len(command["pushes"]) == 2  # both stopped before any upload

    monkeypatch.setenv("OPENADAPT_API_KEY", SECRET)
    http = FakeHttp(200, _definition())
    monkeypatch.setattr(httpx, "get", http.get)
    code = wrap_cli.main(task + ["--validation-attestation", "att.json"])
    captured = capsys.readouterr()
    assert code == 0
    assert f"{HOST}/api/v1/org/{ORG}/workflow/{WORKFLOW}" in captured.out
    assert "curl -sS -X POST" in captured.out
    assert SECRET not in captured.out + captured.err
    path, kwargs = command["pushes"][-1]
    assert path == command["derivative"]
    assert kwargs["kind"] == "bundle"
    assert kwargs["validation_attestation"] == "att.json"
    record = cache.task_dir.parent / "publish" / cache.task_dir.name / "published.json"
    text = record.read_text()
    assert SECRET not in text
    assert json.loads(text)["callable"] is True


def test_command_without_api_key_publishes_and_says_how_to_check(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    command["approved"] = True
    code = wrap_cli.main(
        ["publish", "--task", "t.json", "--validation-attestation", "a.json", "--json"]
    )
    document = json.loads(capsys.readouterr().out)
    assert code == 0
    assert document["status"] == "published"
    assert document["callable"] is None
    assert "OPENADAPT_API_KEY" in document["reason"]
    assert "$OPENADAPT_API_KEY" in document["examples"][0]


def test_command_reports_not_callable_yet(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command["approved"] = True
    monkeypatch.setenv("OPENADAPT_API_KEY", SECRET)
    monkeypatch.setattr(
        httpx,
        "get",
        FakeHttp(
            409, {"error": "workflow_not_ready", "code": "first_run_approval"}
        ).get,
    )
    code = wrap_cli.main(
        ["publish", "--task", "t.json", "--validation-attestation", "a.json"]
    )
    out = capsys.readouterr().out
    assert code == publish.EXIT_WAITING
    assert "Not callable yet" in out and "first_run_approval" in out


def test_command_refuses_without_ingest_token_before_upload(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def missing(*_a: Any, **_k: Any) -> str:
        raise hosted.HostedError("no token")

    monkeypatch.setattr(hosted, "resolve_token", missing)
    assert wrap_cli.main(["publish", "--task", "t.json"]) == publish.EXIT_SETUP
    assert "OPENADAPT_INGEST_TOKEN" in capsys.readouterr().err
    assert command["pushes"] == []


def test_uncertain_delivery_is_never_retried(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command["approved"] = True
    real = hosted.push

    def uncertain(path: Any, **kwargs: Any) -> Any:
        if Path(path) == command["derivative"]:
            raise hosted.HostedDeliveryUncertain("timeout", context={})
        return real(path, **kwargs)

    monkeypatch.setattr(hosted, "push", uncertain)
    code = wrap_cli.main(
        ["publish", "--task", "t.json", "--validation-attestation", "a.json"]
    )
    assert code == publish.EXIT_REFUSED
    assert "Do not run publish again" in capsys.readouterr().err


def test_wrap_run_parser_is_unchanged_for_non_publish_argv() -> None:
    with pytest.raises(SystemExit):
        wrap_cli.main(["--task", "t.json"])  # a run still requires --request-id


def test_skill_cache_admitted_program_follows_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow.integrations.agents import skills

    compiler = SimpleNamespace(
        dir=tmp_path / "task",
        _jit=lambda: SimpleNamespace(
            bundle_dir=tmp_path / "task" / "program" / "bundle"
        ),
    )
    cache = skills.SkillCache.__new__(skills.SkillCache)
    monkeypatch.setattr(cache, "_compiler", lambda *_a: (compiler, {}), raising=False)
    served: dict[str, Any] = {"server": None}
    monkeypatch.setattr(cache, "_server", lambda *_a: served["server"], raising=False)
    assert cache.admitted_program("Save {note}", {"note": ""}) is None
    served["server"] = tmp_path / "server.py"
    program = cache.admitted_program("Save {note}", {"note": ""})
    assert program == {
        "bundle": tmp_path / "task" / "program" / "bundle",
        "checks": tmp_path / "task" / "verify.yaml",
        "task_dir": tmp_path / "task",
    }


def test_real_governed_push_sanitizes_the_staged_bundle_and_pauses(
    cache: FakeCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real push accepts the staged layout and stops for a person's review.

    A raw bundle is sanitized locally and returns before any connection.
    """
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(hosted.TOKEN_ENV, "ingest-token-for-test-only")
    for name in (hosted.AUTO_APPROVE_ENV, hosted.DEPLOYMENT_KIND_ENV):
        monkeypatch.delenv(name, raising=False)

    def no_network(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a raw bundle must not reach the network")

    monkeypatch.setattr(httpx, "post", no_network)
    monkeypatch.setattr(httpx, "get", no_network)
    from openadapt_flow import privacy

    class IdentityScrubber:
        # Scrubbing quality is the privacy package's job; this checks the path.
        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            return text

        def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
            return image

    privacy.set_text_scrubber(IdentityScrubber())
    try:
        staged = publish.stage(cache, "Save {note}", ["note"])
        result = hosted.push(staged.staged, kind="bundle", name="skill-abc")
    finally:
        privacy.reset_scrubbers()
    assert result["pending_review"] is True
    derivative = Path(result["sanitized_path"])
    assert (derivative / publish.READBACK_PATH).is_file()
    assert (derivative / publish.MANIFEST_NAME).is_file()
    assert publish._approved(derivative) is False
