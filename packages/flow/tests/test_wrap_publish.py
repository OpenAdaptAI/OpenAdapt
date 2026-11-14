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
#: The real host and destination-policy functions, before any fixture patches.
REAL_RESOLVE_HOST = hosted.resolve_host
REAL_DESTINATION_POLICY = hosted.resolve_destination_policy
VALID_CHECKS = (
    "schema_version: openadapt.verify/v1\n"
    "verifier:\n  kind: sql\n  sqlite_database: /srv/app.db\n"
    "effects:\n  - kind: record_written\n    match: {type: note}\n    field: note\n"
)


def _client(http: Any) -> publish.CloudClient:
    destination = hosted.DestinationPolicy("customer-managed", HOST, True, "test")
    return publish.CloudClient(destination=destination, key=SECRET, http=http)


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
        self.common: dict[str, Any] = {
            "url": "https://app.example.com/",
            "policy": "clinical-write",
        }
        self.environment = ""
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
    fake.checks.write_text(VALID_CHECKS)
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


class FakeCloud:
    """The cloud's API-key routes (#430) at the HTTP boundary.

    ``routes`` maps ``(method, path)`` to ``(status, body)``; the definition
    route is the ``FakeHttp`` the tests already tune. Every call is recorded.
    """

    def __init__(self, definition: FakeHttp, fingerprint: str) -> None:
        self.definition = definition
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        base = f"/api/v1/org/{ORG}"
        self.routes: dict[tuple[str, str], tuple[int, Any]] = {
            ("GET", "/api/v1/whoami"): (200, {"org_id": ORG}),
            ("GET", f"{base}/admission-keys"): (200, {"admission_keys": []}),
            ("POST", f"{base}/admission-keys"): (201, {"fingerprint": fingerprint}),
            ("POST", f"{base}/validation-challenges"): (
                201,
                {
                    "challenge_id": "challenge-1",
                    "nonce": "n" * 32,
                    "expires_at": "2999-01-01T00:00:00Z",
                },
            ),
        }

    def _answer(self, method: str, url: str, kwargs: dict[str, Any]) -> httpx.Response:
        assert url.startswith(HOST), url
        path = url[len(HOST) :]
        self.calls.append((method, path, kwargs))
        assert kwargs["headers"]["Authorization"] == f"Bearer {SECRET}"
        assert kwargs["follow_redirects"] is False
        if method == "GET" and path.endswith("/definition"):
            return self.definition.get(url, **kwargs)
        status, body = self.routes.get((method, path), (404, {"error": "not_found"}))
        return httpx.Response(status, json=body)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("GET", url, kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self._answer("POST", url, kwargs)

    def paths(self, method: str | None = None) -> list[str]:
        return [path for m, path, _ in self.calls if method in (None, m)]


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
    "case",
    [
        "no_checks_file",
        "no_verifier",
        "unknown_verifier_field",
        "no_effects",
        "not_yaml",
        "no_save",
        "save_without_effect",
    ],
)
def test_refuses_a_program_without_a_read_back_check(
    cache: FakeCache, case: str
) -> None:
    effects = VALID_CHECKS.split("effects:", 1)[1]
    if case == "no_checks_file":
        cache.checks.unlink()
    elif case == "no_verifier":
        cache.checks.write_text(
            f"schema_version: openadapt.verify/v1\neffects:{effects}"
        )
    elif case == "unknown_verifier_field":
        # Non-empty but not a verifier verify-run accepts.
        cache.checks.write_text(f"verifier: {{foo: bar}}\neffects:{effects}")
    elif case == "no_effects":
        cache.checks.write_text(VALID_CHECKS.split("effects:", 1)[0] + "effects: []\n")
    elif case == "not_yaml":
        cache.checks.write_text("verifier: [unclosed\n")
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
        _client(http),
        org_id=ORG,
        workflow_id=WORKFLOW,
        qualification_id=QUALIFICATION,
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
        _client(FakeHttp(status, body)),
        org_id=ORG,
        workflow_id=WORKFLOW,
        qualification_id=QUALIFICATION,
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
    from openadapt_flow import runtime_validation

    setup = SimpleNamespace(
        cache=cache, instruction="Save {note}", parameters=("note",)
    )
    monkeypatch.setattr(recording, "load_task", lambda *_a, **_k: setup)
    monkeypatch.setattr(hosted, "resolve_host", lambda host=None: HOST)
    monkeypatch.setattr(
        hosted,
        "resolve_destination_policy",
        lambda host, **_k: hosted.DestinationPolicy(
            "customer-managed", host, True, "test allowlist"
        ),
    )
    monkeypatch.setattr(hosted, "_auto_approve_enabled", lambda _explicit: False)

    def no_ingest_token(*_a: Any, **_k: Any) -> str:
        raise AssertionError("publish must not look for an ingest token")

    monkeypatch.setattr(hosted, "resolve_token", no_ingest_token)
    monkeypatch.setattr(runtime_validation, "resolve_host", lambda host=None: HOST)
    monkeypatch.setattr(
        runtime_validation, "resolve_destination_policy", lambda *_a, **_k: None
    )
    monkeypatch.setattr(runtime_validation, "resolve_token", lambda token, **_k: token)
    from openadapt_flow.execute.keys import load_or_create_private_key

    keys = tmp_path / "jit-keys"
    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(keys))
    load_or_create_private_key(keys)  # writes keys/ed25519.pub
    public_key, fingerprint = publish.admission_public_key(None)
    recording_dir = cache.task_dir / "program" / "recording"
    run_dir = cache.task_dir / "runs" / "canary-1"
    state: dict[str, Any] = {
        "pushes": [],
        "approved": set(),
        "attested": [],
        "result": {},
        "evidence": publish.WrapEvidence(
            recording=recording_dir,
            run_dir=run_dir,
            canary={"outcome": "VERIFIED", "environment": "synthetic"},
        ),
        "semantics": "preserved",
        "kind": "web",
    }

    def derive(source: Path, destination: Path, kind: str) -> dict[str, Any]:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "workflow.json").write_text("{}")
        return {
            "kind": kind,
            "execution_semantics": state["semantics"],
            "files": [{"path": "workflow.json", "changed": False}],
            "residual_findings": [],
        }

    def approve(derivatives: Any, args: Any, *, automatic: bool) -> None:
        state["approved"].update(path for path, _source in derivatives)
        state["reviewer"] = publish._reviewer(args)

    def attest(**kwargs: Any) -> dict[str, Any]:
        state["attested"].append(kwargs)
        if state.get("attest_error"):
            raise runtime_validation.RuntimeValidationError(state["attest_error"])
        return {"schema": runtime_validation.V2_SCHEMA}

    def push(path: Any, **kwargs: Any) -> dict[str, Any]:
        state["pushes"].append((Path(path), kwargs))
        if state.get("push_error"):
            raise state["push_error"]
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
            "execute_path": f"/api/v1/org/{ORG}/workflow/{WORKFLOW}",
            "qualification_id": QUALIFICATION,
            **state["result"],
        }

    monkeypatch.setattr(publish, "wrap_evidence", lambda *_a, **_k: state["evidence"])
    monkeypatch.setattr(publish, "_report_kind", lambda _run: state["kind"])
    monkeypatch.setattr(publish, "_derive", derive)
    monkeypatch.setattr(publish, "_approved", lambda path: path in state["approved"])
    monkeypatch.setattr(publish, "_approve", approve)
    monkeypatch.setattr(
        runtime_validation, "create_admitted_program_attestation", attest
    )
    monkeypatch.setattr(hosted, "push", push)
    monkeypatch.setattr(publish.sys.stdin, "isatty", lambda: False, raising=False)
    for name in publish.API_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(publish.ORG_ENV, raising=False)
    monkeypatch.setenv("OPENADAPT_API_KEY", SECRET)
    state["http"] = FakeHttp(200, _definition())
    state["cloud"] = FakeCloud(state["http"], fingerprint)
    state["public_key"], state["fingerprint"] = public_key, fingerprint
    monkeypatch.setattr(httpx, "get", state["cloud"].get)
    monkeypatch.setattr(httpx, "post", state["cloud"].post)
    state["record"] = (
        cache.task_dir.parent / "publish" / cache.task_dir.name / "published.json"
    )
    return state


def _publish(*extra: str) -> int:
    return wrap_cli.main(["publish", "--task", "task.json", *extra])


def test_yes_publishes_with_one_key_and_automatic_validation(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _publish("--yes", "--reviewer", "pat") == 0
    captured = capsys.readouterr()
    # One summary of what will be uploaded, then the endpoint and a call.
    assert "Ready to publish to" in captured.out and "1 saving step" in captured.out
    assert f"{HOST}/api/v1/org/{ORG}/workflow/{WORKFLOW}" in captured.out
    assert "curl -sS -X POST" in captured.out
    assert SECRET not in captured.out + captured.err
    assert command["reviewer"] == "pat"
    # Validation ran inside publish, from the wrap run's own evidence.
    (attested,) = command["attested"]
    assert attested["token"] == SECRET
    assert attested["run_dir"] == command["evidence"].run_dir
    assert attested["recording_source"] == command["evidence"].recording
    assert attested["target_url"] == "https://app.example.com/"
    assert attested["policy_source"] == "clinical-write"
    # The challenge came from the org's API-key route and is passed through.
    assert attested["challenge"]["challenge_id"] == "challenge-1"
    cloud = command["cloud"]
    assert cloud.paths() == [
        "/api/v1/whoami",
        f"/api/v1/org/{ORG}/admission-keys",
        f"/api/v1/org/{ORG}/admission-keys",
        f"/api/v1/org/{ORG}/validation-challenges",
        f"/api/v1/org/{ORG}/workflow/{WORKFLOW}/definition",
    ]
    registered = [kw for m, path, kw in cloud.calls if m == "POST"][0]
    assert registered["json"] == {"public_key": command["public_key"]}
    # The upload is the governed push of the approved sanitized bundle, with
    # the same key, never an ingest token.
    ((path, kwargs),) = command["pushes"]
    assert path.name == "sanitized-bundle" and path in command["approved"]
    assert kwargs["token"] == SECRET and kwargs["kind"] == "bundle"
    assert kwargs["route"] == ("POST", f"/api/v1/org/{ORG}/workflow")
    assert kwargs["validation_attestation"] == {
        "schema": "openadapt.runtime-validation/v2"
    }
    text = command["record"].read_text()
    assert SECRET not in text and json.loads(text)["callable"] is True


def test_without_a_terminal_it_shows_the_summary_and_uploads_nothing(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _publish() == publish.EXIT_WAITING
    out = capsys.readouterr().out
    assert "Ready to publish to" in out and "--yes" in out
    assert "Nothing was uploaded" in out and ORG in out
    assert command["pushes"] == [] and command["attested"] == []
    assert command["approved"] == set()
    # Only the read-only whoami ran before the confirmation.
    assert command["cloud"].paths() == ["/api/v1/whoami"]


@pytest.mark.parametrize("answer,code", [("y", 0), ("n", publish.EXIT_WAITING)])
def test_one_confirm_prompt_at_a_terminal(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answer: str,
    code: int,
) -> None:
    monkeypatch.setattr(publish.sys.stdin, "isatty", lambda: True, raising=False)
    prompts: list[str] = []

    def ask(prompt: str) -> str:
        prompts.append(prompt)
        return answer

    monkeypatch.setattr("builtins.input", ask)
    assert _publish() == code
    assert prompts == ["Upload it? [y/N] "]
    assert len(command["pushes"]) == (1 if answer == "y" else 0)
    # An earlier approval is not consent to the next upload: it asks again.
    if answer == "y":
        prompts.clear()
        assert _publish() == 0
        assert prompts == ["Upload it? [y/N] "]
        assert len(command["pushes"]) == 2


def test_a_prior_approval_never_uploads_without_a_new_confirmation(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _publish("--yes") == 0
    assert len(command["pushes"]) == 1 and command["approved"]
    capsys.readouterr()
    # Another organization, no --yes, no terminal: the summary, nothing sent.
    other = "77777777-7777-4777-8777-777777777777"
    calls = len(command["cloud"].calls)
    assert _publish("--org", other) == publish.EXIT_WAITING
    out = capsys.readouterr().out
    assert (
        "Ready to publish to" in out and other in out and "Nothing was uploaded" in out
    )
    assert len(command["pushes"]) == 1
    assert command["cloud"].calls[calls:] == []
    # --json without --yes refuses to upload even after the earlier approval.
    assert _publish("--json") == publish.EXIT_WAITING
    assert json.loads(capsys.readouterr().out)["step"] == "confirm_upload"
    assert len(command["pushes"]) == 1


@pytest.mark.parametrize(
    "host,env",
    [
        ("http://attacker.example.test", {}),
        ("https://not-allowlisted.example.test", {}),
        (
            "https://not-allowlisted.example.test",
            {hosted.DESTINATION_KIND_ENV: "customer-managed"},
        ),
        (
            "http://cloud.example.test",
            {hosted.DESTINATION_KIND_ENV: "customer-managed"},
        ),
    ],
)
def test_a_refused_destination_never_receives_the_key(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    host: str,
    env: dict[str, str],
) -> None:
    """The real host resolution and destination policy run before any request."""
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    for name in (hosted.DESTINATION_KIND_ENV, hosted.TRUSTED_HOSTS_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hosted, "resolve_host", REAL_RESOLVE_HOST)
    monkeypatch.setattr(hosted, "resolve_destination_policy", REAL_DESTINATION_POLICY)
    sent: list[Any] = []

    def record(*args: Any, **kwargs: Any) -> Any:
        sent.append((args, kwargs))
        raise AssertionError("no request may leave for a refused destination")

    for method in ("get", "post", "put", "request", "stream"):
        monkeypatch.setattr(httpx, method, record)
    for flags in (("--yes",), ("--yes", "--org", ORG), ()):
        assert _publish("--host", host, *flags) == publish.EXIT_SETUP
        captured = capsys.readouterr()
        assert "[DESTINATION_REFUSED]" in captured.err
        assert SECRET not in captured.out + captured.err
    assert sent == [] and command["cloud"].calls == []
    assert command["pushes"] == [] and command["attested"] == []


def test_a_configured_untrusted_host_never_receives_the_key(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[hosted]\nhost = "http://attacker.example.test"\n'
    )
    monkeypatch.setenv("OPENADAPT_HOME", str(home))
    for name in (hosted.DESTINATION_KIND_ENV, hosted.TRUSTED_HOSTS_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(hosted, "resolve_host", REAL_RESOLVE_HOST)
    monkeypatch.setattr(hosted, "resolve_destination_policy", REAL_DESTINATION_POLICY)
    assert _publish("--yes", "--org", ORG) == publish.EXIT_SETUP
    assert "[DESTINATION_REFUSED]" in capsys.readouterr().err
    assert command["cloud"].calls == [] and command["pushes"] == []


def test_every_key_bearing_request_goes_to_the_policy_origin(
    command: dict[str, Any],
) -> None:
    assert _publish("--yes") == 0
    assert command["cloud"].calls  # FakeCloud asserts each URL starts with HOST
    with pytest.raises(publish.PublishError, match="route"):
        _client(FakeHttp(200, {})).request("GET", "//attacker.example.test/x")
    with pytest.raises(publish.PublishError, match="route"):
        _client(FakeHttp(200, {})).request("GET", "/api/../x")


def test_json_needs_yes_and_prints_one_object(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _publish("--json") == publish.EXIT_WAITING
    document = json.loads(capsys.readouterr().out)
    assert document["step"] == "confirm_upload" and document["summary"]
    assert _publish("--json", "--yes") == 0
    document = json.loads(capsys.readouterr().out)
    assert document["status"] == "published" and document["callable"] is True


def test_missing_key_is_one_line_naming_it_before_anything_is_staged(
    command: dict[str, Any],
    cache: FakeCache,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("OPENADAPT_API_KEY")
    assert _publish("--yes") == publish.EXIT_SETUP
    err = capsys.readouterr().err.strip()
    assert len(err.splitlines()) == 1
    assert (
        "OPENADAPT_API_KEY is not set" in err and "/dashboard/settings/api-keys" in err
    )
    assert command["pushes"] == []
    assert not (cache.task_dir.parent / "publish").exists()


def test_local_app_url_is_refused_before_any_upload(
    command: dict[str, Any],
    cache: FakeCache,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cache.common["url"] = "http://localhost:8080/"
    assert _publish("--yes") == publish.EXIT_SETUP
    assert "[TARGET_NOT_PUBLIC]" in capsys.readouterr().err
    assert command["pushes"] == [] and command["attested"] == []


def test_a_scrub_that_changes_the_program_is_refused(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    command["semantics"] = "not-preserved"
    assert _publish("--yes") == publish.EXIT_REFUSED
    assert "[SANITIZATION_CHANGED_PROGRAM]" in capsys.readouterr().err
    assert command["pushes"] == [] and command["approved"] == set()


def test_failed_validation_uploads_nothing(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    command["attest_error"] = "Strict lint failed: 1 error, 0 warn, 0 info"
    assert _publish("--yes") == publish.EXIT_REFUSED
    err = capsys.readouterr().err
    assert "[VALIDATION_FAILED]" in err and "Strict lint failed" in err
    assert command["pushes"] == []


@pytest.mark.parametrize(
    "message,code",
    [
        ("Ingest token was rejected (401).", "API_KEY_REJECTED"),
        (
            'Ingest returned 422 (expected 201): {"code":"runtime_validation_rejected"}',
            "VALIDATION_REJECTED",
        ),
        ("Ingest returned 413 (expected 201): too large", "UPLOAD_REFUSED"),
        (
            'Ingest returned 404 (expected 201): {"error":"not_found"}',
            "WORKFLOW_API_UNAVAILABLE",
        ),
    ],
)
def test_upload_refusals_name_what_is_wrong(
    command: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    message: str,
    code: str,
) -> None:
    command["push_error"] = hosted.HostedError(message)
    assert _publish("--yes") != 0
    err = capsys.readouterr().err
    assert f"[{code}]" in err and SECRET not in err


def test_uncertain_delivery_is_never_retried(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    command["push_error"] = hosted.HostedDeliveryUncertain("timeout", context={})
    assert _publish("--yes") == publish.EXIT_REFUSED
    assert "Do not run publish again" in capsys.readouterr().err


def test_ingest_acknowledgment_names_qualification_and_endpoint(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _publish("--yes") == 0
    assert command["http"].calls[0]["params"] == {"qualification_id": QUALIFICATION}
    other = "66666666-6666-4666-8666-666666666666"
    for bad in (
        {"execute_path": f"/api/v1/org/{ORG}/workflow/{VERSION}"},
        {"execute_path": None},
        {"qualification_id": None},
        {"qualification_id": other},  # differs from the retained version
        {"version": {"id": VERSION, "org_id": other, "workflow_id": WORKFLOW}},
    ):
        command["result"] = bad
        assert _publish("--yes") == publish.EXIT_REFUSED
        assert "[CLOUD_RESPONSE_INVALID]" in capsys.readouterr().err


def test_command_reports_not_callable_yet(
    command: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    command["http"].status = 409
    command["http"].body = {"error": "workflow_not_ready", "code": "first_run_approval"}
    assert _publish("--yes") == publish.EXIT_WAITING
    out = capsys.readouterr().out
    assert "Not callable yet" in out and "first_run_approval" in out


def test_explicit_attestation_skips_the_wrap_evidence(
    command: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_evidence(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("an explicit attestation needs no wrap evidence")

    monkeypatch.setattr(publish, "wrap_evidence", no_evidence)
    assert _publish("--yes", "--validation-attestation", "att.json") == 0
    assert command["attested"] == []
    assert command["pushes"][0][1]["validation_attestation"] == "att.json"


def test_new_version_goes_to_the_definition_route(
    command: dict[str, Any],
) -> None:
    assert _publish("--yes", "--workflow-id", WORKFLOW) == 0
    ((_path, kwargs),) = command["pushes"]
    assert kwargs["route"] == (
        "PUT",
        f"/api/v1/org/{ORG}/workflow/{WORKFLOW}/definition",
    )
    assert kwargs["workflow_id"] == WORKFLOW


def test_org_override_skips_whoami_and_is_validated(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(publish.ORG_ENV, ORG)
    assert _publish("--yes") == 0
    assert "/api/v1/whoami" not in command["cloud"].paths()
    capsys.readouterr()
    assert _publish("--yes", "--org", "not-an-org") == publish.EXIT_SETUP
    assert "[ORG_INVALID]" in capsys.readouterr().err


@pytest.mark.parametrize(
    "status,body,code",
    [
        (404, {"error": "not_found"}, "ORG_UNKNOWN"),
        (200, {"org": "x"}, "ORG_UNKNOWN"),
        (401, {"error": "unauthorized"}, "API_KEY_REJECTED"),
    ],
)
def test_unknown_org_stops_before_anything_is_sent(
    command: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    status: int,
    body: Any,
    code: str,
) -> None:
    command["cloud"].routes[("GET", "/api/v1/whoami")] = (status, body)
    assert _publish("--yes") == publish.EXIT_SETUP
    err = capsys.readouterr().err
    assert f"[{code}]" in err and SECRET not in err
    assert command["pushes"] == [] and command["approved"] == set()
    assert command["cloud"].paths() == ["/api/v1/whoami"]


def test_admission_key_is_registered_once(command: dict[str, Any]) -> None:
    keys = f"/api/v1/org/{ORG}/admission-keys"
    command["cloud"].routes[("GET", keys)] = (
        200,
        {"admission_keys": [{"fingerprint": command["fingerprint"]}]},
    )
    assert _publish("--yes") == 0
    assert keys not in command["cloud"].paths("POST")


@pytest.mark.parametrize(
    "route,status,body,code",
    [
        ("GET", 404, {"error": "not_found"}, "WORKFLOW_API_UNAVAILABLE"),
        ("GET", 503, {"error": "x"}, "ADMISSION_KEY_UNREGISTERED"),
        ("POST", 409, {"error": "admission_key_revoked"}, "ADMISSION_KEY_REVOKED"),
        (
            "POST",
            201,
            {"fingerprint": "sha256:" + "0" * 64},
            "ADMISSION_KEY_UNREGISTERED",
        ),
        ("POST", 401, {"error": "unauthorized"}, "API_KEY_REJECTED"),
    ],
)
def test_admission_key_refusals_upload_nothing(
    command: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    route: str,
    status: int,
    body: Any,
    code: str,
) -> None:
    command["cloud"].routes[(route, f"/api/v1/org/{ORG}/admission-keys")] = (
        status,
        body,
    )
    assert _publish("--yes") != 0
    assert f"[{code}]" in capsys.readouterr().err
    assert command["pushes"] == [] and command["attested"] == []


@pytest.mark.parametrize(
    "status,code", [(401, "API_KEY_REJECTED"), (404, "WORKFLOW_API_UNAVAILABLE")]
)
def test_challenge_refusals_upload_nothing(
    command: dict[str, Any],
    capsys: pytest.CaptureFixture[str],
    status: int,
    code: str,
) -> None:
    command["cloud"].routes[("POST", f"/api/v1/org/{ORG}/validation-challenges")] = (
        status,
        {"error": "x"},
    )
    assert _publish("--yes") == publish.EXIT_SETUP
    assert f"[{code}]" in capsys.readouterr().err
    assert command["pushes"] == [] and command["attested"] == []


def test_missing_admission_public_key_is_refused_locally(
    command: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(tmp_path / "no-keys"))
    assert _publish("--yes") == publish.EXIT_REFUSED
    assert "[ADMISSION_INVALID]" in capsys.readouterr().err
    assert command["cloud"].calls == [] and command["pushes"] == []


def test_real_push_uses_the_given_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(hosted.HostedError, match="route is invalid"):
        hosted.push(tmp_path, kind="bundle", route=("DELETE", "/api/ingest"))
    with pytest.raises(hosted.HostedError, match="route is invalid"):
        hosted.push(tmp_path, kind="bundle", route=("PUT", "/api/../ingest"))


# -- the wrap run's evidence, with real signatures -------------------------------


@pytest.fixture
def evidence_program(
    cache: FakeCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    keys = tmp_path / "keys"
    monkeypatch.setenv(lifecycle.KEY_DIR_ENV, str(keys))
    from openadapt_flow.execute.keys import load_or_create_private_key

    load_or_create_private_key(keys)  # also writes keys/ed25519.pub
    program_dir = cache.task_dir / "program"
    rec = program_dir / "recording" / "rec-1"
    rec.mkdir(parents=True)
    (rec / "events.json").write_text("[]")
    run_dir = cache.task_dir / "runs" / "canary-1"
    run_dir.mkdir(parents=True)
    (run_dir / "report.json").write_text(json.dumps({"execution_target_kind": "web"}))
    digest = "d" * 64
    recorder = {"digest": digest, "task_key": "k"}
    recorder["signature"] = lifecycle._sign_record(recorder, keys)
    (program_dir / lifecycle.CANDIDATE_FILE).write_text(
        json.dumps({"recorder": recorder, "recording": str(rec)})
    )
    canary = {
        "digest": digest,
        "passed": True,
        "outcome": "VERIFIED",
        "ran_at": "2026-11-13T00:00:00Z",
        "run_dir": str(run_dir),
    }
    canary["signature"] = lifecycle._sign_record(canary, keys)
    (program_dir / lifecycle.CANARY_FILE).write_text(json.dumps(canary))
    staged = publish.StagedProgram(
        staged=tmp_path / "staged",
        manifest={},
        record_dir=tmp_path / "record",
        admission={"digest": digest, "canary": {"ran_at": canary["ran_at"]}},
        task_dir=cache.task_dir,
    )
    return {"staged": staged, "keys": keys, "rec": rec, "run_dir": run_dir}


def test_wrap_evidence_finds_the_signed_recording_and_canary(
    evidence_program: dict[str, Any],
) -> None:
    found = publish.wrap_evidence(evidence_program["staged"], key_dir=None)
    assert found.recording == evidence_program["rec"]
    assert found.run_dir == evidence_program["run_dir"]
    assert publish._report_kind(found.run_dir) == "web"


@pytest.mark.parametrize(
    "tamper", ["other_digest", "unsigned_canary", "run_outside", "linked_recording"]
)
def test_wrap_evidence_refuses_unsigned_or_foreign_evidence(
    evidence_program: dict[str, Any], tmp_path: Path, tamper: str
) -> None:
    staged = evidence_program["staged"]
    program_dir = staged.task_dir / "program"
    canary_file = program_dir / lifecycle.CANARY_FILE
    canary = json.loads(canary_file.read_text())
    if tamper == "other_digest":
        staged = publish.StagedProgram(
            **{**staged.__dict__, "admission": {"digest": "e" * 64}}
        )
    elif tamper == "unsigned_canary":
        canary["outcome"] = "HALTED"
        canary_file.write_text(json.dumps(canary))
    elif tamper == "run_outside":
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        (outside / "report.json").write_text("{}")
        canary["run_dir"] = str(outside)
        canary.pop("signature")
        canary["signature"] = lifecycle._sign_record(canary, evidence_program["keys"])
        canary_file.write_text(json.dumps(canary))
    else:
        (evidence_program["rec"] / "linked").symlink_to(tmp_path)
    with pytest.raises(publish.PublishError) as err:
        publish.wrap_evidence(staged, key_dir=None)
    assert err.value.code == "NO_WRAP_EVIDENCE"


# -- the admitted-program attestation keeps its bindings -------------------------


def test_admitted_program_attestation_binds_the_compiled_recording(
    cache: FakeCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import privacy, runtime_validation
    from openadapt_flow.sanitized_artifact import approve_derivative, sanitize_artifact

    class IdentityScrubber:
        def scrub_text(self, text: str, is_separated: bool = False) -> str:
            return text

        def scrub_image(self, image: Any, fill_color: Any = None) -> Any:
            return image

    monkeypatch.setattr(runtime_validation, "resolve_host", lambda host=None: HOST)
    monkeypatch.setattr(
        runtime_validation, "resolve_destination_policy", lambda *_a, **_k: None
    )

    def no_network(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a local refusal must not reach the network")

    monkeypatch.setattr(httpx, "post", no_network)
    compiled = tmp_path / "compiled"
    compiled.mkdir()
    (compiled / "events.json").write_text("[]")
    other = tmp_path / "other"
    other.mkdir()
    (other / "events.json").write_text('[{"i": 1}]')
    privacy.set_text_scrubber(IdentityScrubber())
    try:
        staged = publish.stage(cache, "Save {note}", ["note"])
        bundle = tmp_path / "bundle-d"
        sanitize_artifact(staged.staged, bundle, kind="bundle")
        approve_derivative(bundle, source=staged.staged, reviewer="pat")
        rec = tmp_path / "rec-d"
        sanitize_artifact(other, rec, kind="recording")
        approve_derivative(rec, source=other, reviewer="pat")
        with pytest.raises(runtime_validation.RuntimeValidationError) as err:
            runtime_validation.create_admitted_program_attestation(
                recording_source=compiled,
                recording_derivative=rec,
                bundle_derivative=bundle,
                run_dir=tmp_path,
                policy_source="clinical-write",
                environment="synthetic",
                target_url="https://app.example.com/",
                token="t",
            )
    finally:
        privacy.reset_scrubbers()
    assert "not a copy of the recording" in str(err.value)


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


def test_loopback_publish_needs_the_local_classification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    for name in (hosted.DESTINATION_KIND_ENV, hosted.TRUSTED_HOSTS_ENV):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(publish.PublishError) as refused:
        publish.cloud_client("http://127.0.0.1:3917", "k")
    assert "OPENADAPT_FLOW_DESTINATION_KIND=local" in str(refused.value)
    monkeypatch.setenv(hosted.DESTINATION_KIND_ENV, "local")
    assert publish.cloud_client("http://127.0.0.1:3917", "k").host.startswith(
        "http://127.0.0.1"
    )
    with pytest.raises(publish.PublishError):
        publish.cloud_client("http://notes.example.test", "k")
