"""Verified skills: a pulled skill runs only after it is qualified on your instance.

``pull`` marks the bundle (``skill.lock.json``) and expires any certification
it carries. ``skill bind`` records the user's addresses and the names of the
environment variables that hold credentials, never their values. ``skill
qualify`` runs the bundled contract against the user's instance. ``run``,
``replay``, ``emit-skill`` and ``emit-mcp`` refuse a pulled skill until then.
A local qualification is checked against the bundle's digest, its
certification and the instance it was qualified on.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow import registry, skills

ROOT = Path(__file__).resolve().parents[1]

CONTRACT = """\
schema_version: openadapt.verify/v1
app:
  target: web
  name: Demo
  version: "1"
  url: ${APP_URL}
  storage_state: ${SIGNED_IN_STATE}
  reset:
    command: ${RESET_COMMAND}
verifier:
  kind: rest
  base_url: ${RECORD_URL}
  records_path: /api/patient
  records_key: data
  auth:
    bearer_env: ${RECORD_TOKEN_VAR}
record:
  name: patient
  match: [email]
  fields:
    email: email
"""

VARS = [
    {"name": "APP_URL", "kind": "url"},
    {"name": "RECORD_URL", "kind": "url"},
    {"name": "RECORD_TOKEN_VAR", "kind": "env_var_name"},
    {"name": "SIGNED_IN_STATE", "kind": "path"},
    {"name": "RESET_COMMAND", "kind": "command"},
]


def _registry(tmp_path: Path, *, contract: str = CONTRACT) -> Path:
    """A local registry whose OpenEMR example entry ships a contract."""

    dest = tmp_path / "registry"
    shutil.copytree(ROOT / "registry", dest)
    bundle = dest / "bundles/openemr/new-referral"
    (bundle / "verify.yaml").write_text(contract)
    index = json.loads((dest / "index.json").read_text())
    for item in index["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["files"] = registry.build_entry_files(bundle)
            item.update(
                version="1.0.0",
                contract="verify.yaml",
                contract_vars=VARS,
                app_license="GPL-3.0",
            )
    (dest / "index.json").write_text(json.dumps(index))
    return dest / "index.json"


def _pulled(tmp_path: Path, **kw) -> Path:
    index = _registry(tmp_path, **kw)
    return registry.pull(
        "openemr/new-referral",
        tmp_path / "skill",
        index_url=str(index),
        allow_unsigned=True,
    )


# -- the index ------------------------------------------------------------------


def test_versions_pin_and_newest_wins():
    entries = registry.validate_index(
        {
            "workflows": [
                {
                    "id": "a/b",
                    "version": "1.2.0",
                    "status": "example",
                    "files": {"x": "0"},
                },
                {
                    "id": "a/b",
                    "version": "1.10.0",
                    "status": "example",
                    "files": {"x": "0"},
                },
            ]
        }
    )
    assert registry.find(entries, "a/b").version == "1.10.0"
    assert registry.find(entries, "a/b@1.2.0").version == "1.2.0"
    with pytest.raises(registry.RegistryError, match="no workflow"):
        registry.find(entries, "a/b@9.9.9")


@pytest.mark.parametrize(
    "extra, message",
    [
        ({"contract": "missing.yaml"}, "not one of its files"),
        ({"contract_vars": [{"name": "X", "kind": "secret"}]}, "kind"),
        ({"verifier_score": {"caught": 1}}, "verifier_score needs"),
    ],
)
def test_the_index_rejects_a_malformed_skill(extra, message):
    item = {"id": "a/b", "status": "example", "files": {"x": "0"}, **extra}
    with pytest.raises(registry.RegistryError, match=message):
        registry.validate_index({"workflows": [item]})


# -- pull marks the skill and every entry point refuses it ----------------------


def test_pull_marks_the_skill(tmp_path):
    dest = _pulled(tmp_path)
    lock = skills.read_lock(dest)
    assert lock["id"] == "openemr/new-referral" and lock["version"] == "1.0.0"
    assert lock["qualified"] is None
    assert "no current certification" in skills.pulled_skill_refusal(dest)


def test_every_entry_point_refuses_an_unqualified_skill(tmp_path):
    from openadapt_flow.__main__ import _cmd_emit_mcp, _cmd_emit_skill, _cmd_replay

    dest = _pulled(tmp_path)
    for cmd in (_cmd_replay, _cmd_emit_skill, _cmd_emit_mcp):
        with pytest.raises(SystemExit, match="REFUSED"):
            cmd(SimpleNamespace(bundle=str(dest), out=str(tmp_path / "emitted")))
    with pytest.raises(SystemExit, match="REFUSED"):
        skills.refuse_unqualified_skill(dest)


def test_an_ordinary_bundle_is_not_affected():
    assert (
        skills.pulled_skill_refusal(ROOT / "registry/bundles/openemr/new-referral")
        is None
    )


# -- bind -------------------------------------------------------------------------


def test_bind_records_names_never_secrets(tmp_path):
    dest = _pulled(tmp_path)
    state = tmp_path / "state.json"
    state.write_text("{}")
    skills.bind(
        dest,
        {
            "APP_URL": "https://emr.example.org",
            "RECORD_TOKEN_VAR": "MY_EMR_TOKEN",
            "SIGNED_IN_STATE": str(state),
        },
    )
    assert skills.missing_bindings(dest) == ["RECORD_URL", "RESET_COMMAND"]
    binding = dest / skills.BINDING
    assert binding.stat().st_mode & 0o077 == 0
    for name, value, message in (
        ("RECORD_TOKEN_VAR", "eyJhbGciOiJIUzI1NiJ9.secret", "NAME of an environment"),
        ("APP_URL", "https://user:pw@emr.example.org", "credentials in the URL"),
        ("APP_URL", "emr.example.org", "http"),
        ("UNKNOWN", "x", "not a variable"),
        ("SIGNED_IN_STATE", str(tmp_path / "missing.json"), "does not exist"),
    ):
        with pytest.raises(skills.SkillError, match=message):
            skills.bind(dest, {name: value})


# -- qualify ----------------------------------------------------------------------


def _bind_all(dest, tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{}")
    skills.bind(
        dest,
        {
            "APP_URL": "https://emr.example.org",
            "RECORD_URL": "https://emr.example.org",
            "RECORD_TOKEN_VAR": "MY_EMR_TOKEN",
            "SIGNED_IN_STATE": str(state),
            "RESET_COMMAND": "./reset-test-copy.sh",
        },
    )


def test_qualify_needs_every_binding(tmp_path):
    dest = _pulled(tmp_path)
    with pytest.raises(skills.SkillError, match="bind these first"):
        skills.qualify_skill(dest, certify="clinical-write", run=lambda *a, **k: None)


def test_qualify_refuses_a_contract_that_runs_code(tmp_path):
    hooked = CONTRACT.replace(
        "  reset:\n    command: ${RESET_COMMAND}\n",
        "  session: evil.module:open_session\n  reset:\n    command: ${RESET_COMMAND}\n",
    )
    dest = _pulled(tmp_path, contract=hooked)
    _bind_all(dest, tmp_path)
    with pytest.raises(skills.SkillError, match="Python hooks"):
        skills.qualify_skill(dest, certify="clinical-write", run=lambda *a, **k: None)


def test_qualify_runs_in_place_on_the_bound_instance(tmp_path):
    dest = _pulled(tmp_path)
    _bind_all(dest, tmp_path)
    seen = {}

    def run(bundle, config, **kw):
        seen.update(bundle=bundle, origin=config.app.origin, **kw)
        return SimpleNamespace(certified=False)

    skills.qualify_skill(dest, certify="clinical-write", run=run)
    assert seen["bundle"] == dest and seen["in_place"] is True
    assert seen["origin"] == "https://emr.example.org"
    assert seen["certify"] == "clinical-write"
    # A campaign that didn't certify leaves the skill refused.
    assert skills.read_lock(dest)["qualified"] is None


# -- the refusal after qualification --------------------------------------------------


def _qualify_on(dest, origin):
    """Give the pulled bundle a qualification project on ``origin``."""

    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification import EnvironmentBoundary, init_project

    workflow = Workflow.load(dest)
    init_project(
        workflow,
        environment=EnvironmentBoundary(
            target_kind="web",
            application="Demo",
            application_identity=origin,
            application_version="1",
            environment_digest="e" * 64,
            runtime_version="test",
        ),
    )
    workflow.save(dest)


def test_only_the_qualified_instance_runs(tmp_path, monkeypatch):
    from openadapt_flow import qualification

    dest = _pulled(tmp_path)
    monkeypatch.setattr(
        qualification, "current_certification_matches", lambda w, **k: True
    )
    assert "not qualified on a named instance" in skills.pulled_skill_refusal(dest)
    _qualify_on(dest, "https://emr.example.org")
    assert skills.pulled_skill_refusal(dest) is None
    assert (
        skills.pulled_skill_refusal(dest, target_url="https://emr.example.org/x")
        is None
    )
    assert "qualified on https://emr.example.org, not https://other.example" in (
        skills.pulled_skill_refusal(dest, target_url="https://other.example/")
    )
    monkeypatch.setattr(
        qualification, "current_certification_matches", lambda w, **k: False
    )
    assert "no current certification" in skills.pulled_skill_refusal(dest)


def test_deleting_the_lock_does_not_make_it_runnable(tmp_path):
    from openadapt_flow.ir import Workflow

    dest = _pulled(tmp_path)
    (dest / skills.LOCK).unlink()
    assert Workflow.load(dest).registry.id == "openemr/new-referral"
    assert "no current certification" in skills.pulled_skill_refusal(dest)


def test_a_lock_without_provenance_is_refused(tmp_path):
    shutil.copytree(ROOT / "registry/bundles/openemr/new-referral", tmp_path / "b")
    (tmp_path / "b" / skills.LOCK).write_text(
        json.dumps({"schema": skills.LOCK_SCHEMA, "id": "openemr/new-referral"})
    )
    assert "no registry provenance" in skills.pulled_skill_refusal(tmp_path / "b")


# -- signatures ------------------------------------------------------------------------

SEED = bytes(range(32))


def _signed_registry(tmp_path):
    from openadapt_flow.ed25519ph import public_key

    index = _registry(tmp_path)
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["signature"] = registry.sign_entry(item, SEED, "test-key")
    index.write_text(json.dumps(data))
    return index, {"test-key": public_key(SEED)}


def test_a_signed_entry_pulls_and_records_its_signer(tmp_path):
    from openadapt_flow.ir import Workflow

    index, keys = _signed_registry(tmp_path)
    dest = registry.pull(
        "openemr/new-referral", tmp_path / "s", index_url=str(index), keys=keys
    )
    origin = Workflow.load(dest).registry
    assert origin.signed_by == "test-key" and origin.version == "1.0.0"


def test_an_unsigned_entry_needs_allow_unsigned(tmp_path):
    index = _registry(tmp_path)
    with pytest.raises(registry.RegistryError, match="not signed.*allow-unsigned"):
        registry.pull(
            "openemr/new-referral", tmp_path / "u", index_url=str(index), keys={}
        )
    assert not (tmp_path / "u").exists()


@pytest.mark.parametrize("tamper", ["file_hash", "version", "signature", "unknown_key"])
def test_a_tampered_or_foreign_entry_is_refused_before_anything_is_written(
    tmp_path, tamper
):
    index, keys = _signed_registry(tmp_path)
    data = json.loads(index.read_text())
    item = next(i for i in data["workflows"] if i["id"] == "openemr/new-referral")
    if tamper == "file_hash":
        item["files"]["workflow.json"] = "0" * 64
    elif tamper == "version":
        item["version"] = "9.9.9"
    elif tamper == "signature":
        item["signature"]["ed25519ph"] = registry.sign_entry(
            item, bytes(32), "test-key"
        )["ed25519ph"]
    else:
        item["signature"]["key_id"] = "someone-else"
    index.write_text(json.dumps(data))
    with pytest.raises(
        registry.RegistryError, match="signed by an unknown key|does not match"
    ):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "t",
            index_url=str(index),
            keys=keys,
            allow_unsigned=True,
        )
    assert not (tmp_path / "t").exists()


def test_a_skill_without_a_contract_takes_the_users_own(tmp_path, monkeypatch):
    index = _registry(tmp_path)
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        item.pop("contract", None)
    index.write_text(json.dumps(data))
    dest = registry.pull(
        "openemr/new-referral",
        tmp_path / "skill",
        index_url=str(index),
        allow_unsigned=True,
    )
    with pytest.raises(skills.SkillError, match="pass --verify"):
        skills.qualify_skill(dest, certify="clinical-write", run=lambda *a, **k: None)
    own = tmp_path / "my-verify.yaml"
    own.write_text(
        CONTRACT.replace("${APP_URL}", "https://emr.example.org")
        .replace("${SIGNED_IN_STATE}", str(own))
        .replace("${RESET_COMMAND}", "./reset-test-copy.sh")
        .replace("${RECORD_URL}", "https://emr.example.org")
        .replace("${RECORD_TOKEN_VAR}", "MY_EMR_TOKEN")
        .replace("  reset:", "  session: my_own_hooks:open_session\n  reset:")
    )
    (tmp_path / "my_own_hooks.py").write_text("def open_session(config):\n    pass\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    seen = {}
    skills.qualify_skill(
        dest,
        certify="clinical-write",
        contract=own,
        run=lambda bundle, config, **kw: seen.setdefault("origin", config.app.origin),
    )
    # The user's own contract may name their own hooks.
    assert seen["origin"] == "https://emr.example.org"


# -- the public bundle and the private tier ---------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "verify.full.yaml",
        "check_coverage.json",
        "faults.yaml",
        "heldout2.yaml",
        "SPEC.md",
    ],
)
def test_a_public_bundle_never_lists_a_private_tier_file(name):
    item = {"id": "a/b", "status": "example", "files": {name: "0" * 64}}
    with pytest.raises(registry.RegistryError, match="private tier"):
        registry.validate_index({"workflows": [item]})


def test_no_registry_bundle_in_the_repo_holds_a_private_tier_file():
    found = [
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / "registry").rglob("*")
        if p.is_file() and registry.private_tier_file(p.name)
    ]
    assert found == []


def _with_full_contract(tmp_path, body: bytes):
    import hashlib

    index = _registry(tmp_path)
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["full_contract"] = {
                "url": "https://private.example/registry/openemr/verify.full.yaml",
                "sha256": hashlib.sha256(body).hexdigest(),
                "token_env": "MY_REGISTRY_TOKEN",
            }
    index.write_text(json.dumps(data))
    dest = registry.pull(
        "openemr/new-referral",
        tmp_path / "skill",
        index_url=str(index),
        allow_unsigned=True,
    )
    _bind_all(dest, tmp_path)
    return dest


def _serve(monkeypatch, body: bytes, seen: dict):
    import httpx

    def get(url, headers=None, **kw):
        seen.update(url=url, auth=(headers or {}).get("Authorization"))
        return SimpleNamespace(status_code=200, content=body)

    monkeypatch.setattr(httpx, "get", get)


def test_without_a_token_the_minimal_contract_is_used(tmp_path, monkeypatch):
    full = CONTRACT.replace("name: Demo", "name: Demo full").encode()
    dest = _with_full_contract(tmp_path, full)
    monkeypatch.delenv("MY_REGISTRY_TOKEN", raising=False)
    lines, seen = [], {}
    skills.qualify_skill(
        dest,
        certify="p",
        log=lines.append,
        run=lambda bundle, config, **kw: seen.setdefault("name", config.app.name),
    )
    assert seen["name"] == "Demo"
    assert any("private tier" in line for line in lines)


def test_the_full_contract_is_fetched_checked_and_kept_outside_the_bundle(
    tmp_path, monkeypatch
):
    full = CONTRACT.replace("name: Demo", "name: Demo full").encode()
    dest = _with_full_contract(tmp_path, full)
    monkeypatch.setenv("MY_REGISTRY_TOKEN", "t0k3n")
    served, seen = {}, {}
    _serve(monkeypatch, full, served)
    skills.qualify_skill(
        dest,
        certify="p",
        run=lambda bundle, config, **kw: seen.setdefault("name", config.app.name),
    )
    assert seen["name"] == "Demo full"
    assert served["auth"] == "Bearer t0k3n"
    assert not (dest / "verify.full.yaml").exists()
    stored = tmp_path / "skill.private" / "verify.full.yaml"
    assert stored.read_bytes() == full and stored.stat().st_mode & 0o077 == 0


def test_a_full_contract_that_does_not_match_the_signed_entry_is_refused(
    tmp_path, monkeypatch
):
    dest = _with_full_contract(tmp_path, b"the signed one")
    monkeypatch.setenv("MY_REGISTRY_TOKEN", "t0k3n")
    _serve(monkeypatch, b"something else", {})
    with pytest.raises(skills.SkillError, match="does not match the signed entry"):
        skills.qualify_skill(dest, certify="p", run=lambda *a, **k: None)


def test_a_full_contract_needs_a_url_and_a_digest():
    item = {
        "id": "a/b",
        "status": "example",
        "files": {"x": "0"},
        "full_contract": {"url": "u"},
    }
    with pytest.raises(registry.RegistryError, match="full_contract needs"):
        registry.validate_index({"workflows": [item]})


# -- the TEST key ------------------------------------------------------------------------


def test_the_test_key_needs_allow_test_key(tmp_path):
    index = _registry(tmp_path)
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["signature"] = registry.sign_entry(
                item, registry.TEST_KEY_SEED, registry.TEST_KEY_ID
            )
    index.write_text(json.dumps(data))
    with pytest.raises(registry.RegistryError, match="allow-test-key"):
        registry.pull("openemr/new-referral", tmp_path / "a", index_url=str(index))
    dest = registry.pull(
        "openemr/new-referral",
        tmp_path / "b",
        index_url=str(index),
        allow_test_key=True,
    )
    from openadapt_flow.ir import Workflow

    assert Workflow.load(dest).registry.signed_by == registry.TEST_KEY_ID


# -- bundles as release assets --------------------------------------------------------------


def _with_archive(tmp_path, *, tamper=None):
    index = _registry(tmp_path)
    bundle = index.parent / "bundles/openemr/new-referral"
    asset = tmp_path / "openemr-new-referral-1.0.0.tar.gz"
    digest = registry.build_archive(bundle, asset)
    if tamper == "bytes":
        asset.write_bytes(asset.read_bytes() + b"x")
    shutil.rmtree(bundle)  # nothing left to fetch file by file
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["archive"] = {"url": str(asset), "sha256": digest}
    index.write_text(json.dumps(data))
    return index


def test_a_bundle_can_come_from_a_release_asset(tmp_path):
    index = _with_archive(tmp_path)
    dest = registry.pull(
        "openemr/new-referral",
        tmp_path / "s",
        index_url=str(index),
        allow_unsigned=True,
    )
    assert (dest / "workflow.json").is_file()


def test_a_changed_archive_is_refused(tmp_path):
    index = _with_archive(tmp_path, tamper="bytes")
    with pytest.raises(registry.RegistryError, match="archive's sha256"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "s",
            index_url=str(index),
            allow_unsigned=True,
        )
    assert not (tmp_path / "s").exists()


def test_the_archive_is_reproducible(tmp_path):
    bundle = ROOT / "registry/bundles/openemr/new-referral"
    a = registry.build_archive(bundle, tmp_path / "a.tar.gz")
    b = registry.build_archive(bundle, tmp_path / "b.tar.gz")
    assert a == b
