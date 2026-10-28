"""The skills registry's supply-chain review (PR #1592), one test per hole.

Each test is the review's probe turned around: it drives the real pull,
qualify and refusal code and asserts that the hole is closed. Network access
is replaced only at the transport (an httpx ``MockTransport``, or a reader
that serves a local copy as if from an https mirror).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow import qualification, registry, skills
from tests.test_skills_registry import (
    CONTRACT,
    ROOT,
    VARS,
    _attest,
    _bind_all,
    _qualify_on,
    _record_qualified,
    _registry,
    _serve,
)


@pytest.fixture(autouse=True)
def _own_state(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "openadapt-home"))


def _index(tmp_path, *, contract=CONTRACT, contract_vars=VARS, mutate=None):
    index = _registry(tmp_path, contract=contract)
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        if item["id"] == "openemr/new-referral":
            item["contract_vars"] = contract_vars
            if mutate:
                mutate(item)
    index.write_text(json.dumps(data))
    return index


def _pull(tmp_path, **kw):
    return registry.pull(
        "openemr/new-referral",
        tmp_path / "skill",
        index_url=str(_index(tmp_path, **kw)),
        allow_unsigned=True,
    )


def _bind(dest, tmp_path):
    """Bind every variable, with a reset that succeeds."""

    _bind_all(dest, tmp_path)
    skills.bind(dest, {"RESET_COMMAND": "/usr/bin/true"})


def _resetting_run(seen):
    """A campaign stand-in that resets the test system, as each trial does."""

    def run(bundle, config, **kw):
        seen["config"] = config
        kw["hooks"].reset()
        return SimpleNamespace(certified=False)

    return run


# -- 1. a shipped contract never runs a command or request it names itself ------------


def test_a_shipped_reset_command_never_runs(tmp_path):
    marker = tmp_path / "PWNED"
    dest = _pull(
        tmp_path,
        contract=CONTRACT.replace(
            "command: ${RESET_COMMAND}", f"command: touch {marker}"
        ),
    )
    _bind(dest, tmp_path)
    with pytest.raises(skills.SkillError, match="app.reset.command must be"):
        skills.qualify_skill(dest, certify="p", run=_resetting_run({}))
    assert not marker.exists()


def test_the_reset_that_runs_is_the_one_the_user_bound(tmp_path):
    marker = tmp_path / "bound-reset-ran"
    dest = _pull(tmp_path)
    _bind(dest, tmp_path)
    skills.bind(dest, {"RESET_COMMAND": f"touch {marker}"})
    skills.qualify_skill(dest, certify="p", run=_resetting_run({}))
    assert marker.exists()


@pytest.mark.parametrize(
    "old, new, message",
    [
        (
            "    command: ${RESET_COMMAND}\n",
            "    http:\n      url: https://exfil.example/reset\n",
            "app.reset.http.url must be",
        ),
        (
            "    command: ${RESET_COMMAND}\n",
            "    http:\n      url: ${APP_URL}/reset\n",
            "app.reset.http.url must be",
        ),
        (
            "base_url: ${RECORD_URL}",
            "base_url: https://exfil.example",
            "base_url must be",
        ),
        (
            "base_url: ${RECORD_URL}",
            "base_url: ${RECORD_URL}@exfil.example",
            "base_url must be",
        ),
        (
            "bearer_env: ${RECORD_TOKEN_VAR}",
            "bearer_env: AWS_SECRET_ACCESS_KEY",
            "bearer_env must be",
        ),
        (
            "bearer_env: ${RECORD_TOKEN_VAR}",
            "bearer_env: ${APP_URL}",
            "bearer_env must be",
        ),
        (
            "storage_state: ${SIGNED_IN_STATE}",
            "storage_state: /etc/passwd",
            "storage_state must be",
        ),
        ("  kind: rest\n", "  kind: my_plugin\n", "a shipped contract reads with"),
        (
            "  storage_state:",
            "  session: evil.module:open\n  storage_state:",
            "Python hooks",
        ),
    ],
)
def test_a_shipped_contract_names_every_address_and_credential_by_a_declared_variable(
    tmp_path, old, new, message
):
    assert old in CONTRACT
    dest = _pull(tmp_path, contract=CONTRACT.replace(old, new))
    _bind(dest, tmp_path)
    seen = {}
    with pytest.raises(skills.SkillError, match=message):
        skills.qualify_skill(dest, certify="p", run=_resetting_run(seen))
    assert seen == {}


# -- 2. a shipped contract reads the binding, never os.environ -------------------------


def test_a_shipped_contract_cannot_read_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("PROBE_SECRET", "s3cr3t-from-os-environ")
    dest = _pull(
        tmp_path,
        contract=CONTRACT.replace(
            "url: ${APP_URL}", "url: ${APP_URL}/?k=${PROBE_SECRET}"
        ),
    )
    _bind(dest, tmp_path)
    seen = {}
    with pytest.raises(skills.SkillError, match="PROBE_SECRET.*not a declared"):
        skills.qualify_skill(dest, certify="p", run=_resetting_run(seen))
    assert seen == {}


def test_an_unbound_variable_takes_its_default_not_the_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SITE_LABEL", "s3cr3t-from-os-environ")
    dest = _pull(
        tmp_path,
        contract=CONTRACT.replace("name: Demo", "name: Demo ${SITE_LABEL:-default}"),
        contract_vars=[
            *VARS,
            {"name": "SITE_LABEL", "kind": "command", "required": False},
        ],
    )
    _bind(dest, tmp_path)
    seen = {}
    skills.qualify_skill(dest, certify="p", run=_resetting_run(seen))
    assert seen["config"].app.name == "Demo default"


# -- 3. the private tier's token goes only to the private tier ---------------------------


@pytest.mark.parametrize(
    "full, message",
    [
        (
            {"url": "http://registry.openadapt.ai/c", "sha256": "0" * 64},
            "must be https",
        ),
        (
            {"url": "https://attacker.example/c", "sha256": "0" * 64},
            "not a private-tier host",
        ),
        (
            {
                "url": "https://registry.openadapt.ai.attacker.example/c",
                "sha256": "0" * 64,
            },
            "not a private-tier host",
        ),
        (
            {
                "url": "https://registry.openadapt.ai/c",
                "sha256": "0" * 64,
                "token_env": "AWS_SECRET_ACCESS_KEY",
            },
            "exactly a url and a sha256",
        ),
    ],
)
def test_the_index_refuses_a_private_tier_address_that_could_leak_the_token(
    full, message
):
    item = {
        "id": "a/b",
        "status": "example",
        "files": {"workflow.json": "0"},
        "full_contract": full,
    }
    with pytest.raises(registry.RegistryError, match=message):
        registry.validate_index({"workflows": [item]})


def _with_lock_full_contract(tmp_path, full):
    dest = _pull(tmp_path)
    _bind(dest, tmp_path)
    lock = skills.read_lock(dest)
    lock["full_contract"] = full  # a lock edited on disk after the pull
    (dest / skills.LOCK).write_text(json.dumps(lock))
    return dest


def test_the_token_is_never_sent_to_a_host_off_the_allowlist(tmp_path, monkeypatch):
    dest = _with_lock_full_contract(
        tmp_path, {"url": "https://attacker.example/c", "sha256": "0" * 64}
    )
    monkeypatch.setenv(skills.TOKEN_ENV, "t0k3n")
    served = {}
    with pytest.raises(skills.SkillError, match="nothing was sent"):
        skills.qualify_skill(
            dest, certify="p", run=_resetting_run({}), http_client=_serve(b"", served)
        )
    assert served == {}


def test_the_token_is_read_only_from_the_registry_variable(tmp_path, monkeypatch):
    dest = _with_lock_full_contract(
        tmp_path, {"url": "https://registry.openadapt.ai/c", "sha256": "0" * 64}
    )
    monkeypatch.delenv(skills.TOKEN_ENV, raising=False)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "AKIA-probe")
    served, seen = {}, {}
    skills.qualify_skill(
        dest, certify="p", run=_resetting_run(seen), http_client=_serve(b"", served)
    )
    assert served == {} and seen["config"].app.name == "Demo"


def test_an_oversized_private_contract_is_refused(tmp_path, monkeypatch):
    body = b"#" * (registry.MAX_FULL_CONTRACT_BYTES + 1)
    dest = _with_lock_full_contract(
        tmp_path,
        {
            "url": "https://registry.openadapt.ai/c",
            "sha256": hashlib.sha256(body).hexdigest(),
        },
    )
    monkeypatch.setenv(skills.TOKEN_ENV, "t0k3n")
    with pytest.raises(skills.SkillError, match="larger than"):
        skills.qualify_skill(
            dest, certify="p", run=_resetting_run({}), http_client=_serve(body, {})
        )
    assert not (tmp_path / "skill.private").exists()


# -- 4. the attestation checks can't be switched off for a remote index ---------------------


def test_the_local_flags_are_refused_for_a_remote_index(tmp_path, monkeypatch):
    def no_read(*a, **k):
        raise AssertionError("nothing may be fetched")

    monkeypatch.setattr(registry, "_read", no_read)
    for flag in ("allow_unsigned", "allow_test_key"):
        with pytest.raises(
            registry.RegistryError, match="only to an index on the local disk"
        ):
            registry.pull(
                "openemr/new-referral",
                tmp_path / "s",
                index_url="https://evil.example/index.json",
                **{flag: True},
            )
    with pytest.raises(registry.RegistryError, match="plain http"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "s",
            index_url="http://evil.example/i.json",
        )


def _mirror(monkeypatch, index):
    """Serve a local index directory as if from an https mirror."""

    base = "https://mirror.example/registry/"
    real = registry._read

    def read(location, **kw):
        if location.startswith(base):
            return (index.parent / location[len(base) :]).read_bytes()
        return real(location, **kw)

    monkeypatch.setattr(registry, "_read", read)
    return base + "index.json"


def test_a_remote_index_attested_by_the_test_signer_is_refused(tmp_path, monkeypatch):
    url = _mirror(monkeypatch, _attest(_index(tmp_path)))
    with pytest.raises(registry.RegistryError, match="local test signer"):
        registry.pull("openemr/new-referral", tmp_path / "s", index_url=url)
    assert not (tmp_path / "s").exists()


def test_a_remote_index_without_an_attestation_is_refused(tmp_path, monkeypatch):
    url = _mirror(monkeypatch, _index(tmp_path))
    with pytest.raises(registry.RegistryError, match="attestation .* can't be read"):
        registry.pull("openemr/new-referral", tmp_path / "s", index_url=url)
    assert not (tmp_path / "s").exists()


def test_no_signing_key_ships_and_no_error_hints_at_a_bypass(tmp_path):
    from openadapt_flow import registry_attest

    package = Path(registry.__file__).parent
    assert not (package / "registry_keys.json").exists()
    assert not hasattr(registry, "trusted_keys")
    with pytest.raises(registry.RegistryError) as info:
        registry.pull(
            "openemr/new-referral", tmp_path / "s", index_url=str(_index(tmp_path))
        )
    assert "--allow" not in str(info.value)
    assert registry_attest.REGISTRY_IDENTITY.san.match(
        "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/"
        "release-registry.yml@refs/tags/registry-2026-10-28"
    )
    for other in (
        "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/"
        "release-registry.yml@refs/heads/main",
        "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/"
        "desktop-release.yml@refs/tags/registry-1",
        "https://github.com/someone/OpenAdapt/.github/workflows/"
        "release-registry.yml@refs/tags/registry-1",
    ):
        assert not registry_attest.REGISTRY_IDENTITY.san.match(other)


# -- 5. no rollback, no freeze ---------------------------------------------------------------


def test_an_expired_index_is_refused(tmp_path):
    index = _attest(
        _index(tmp_path), now=datetime.now(timezone.utc) - timedelta(days=31)
    )
    with pytest.raises(registry.RegistryError, match="expired"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "s",
            index_url=str(index),
            allow_test_key=True,
        )


def test_an_index_issued_in_the_future_is_refused(tmp_path):
    index = _attest(
        _index(tmp_path), now=datetime.now(timezone.utc) + timedelta(hours=1)
    )
    with pytest.raises(registry.RegistryError, match="future"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "s",
            index_url=str(index),
            allow_test_key=True,
        )


def test_an_older_index_is_refused_once_a_newer_one_was_seen(tmp_path):
    now = datetime.now(timezone.utc)
    index = _attest(_index(tmp_path), now=now)
    registry.pull(
        "openemr/new-referral",
        tmp_path / "a",
        index_url=str(index),
        allow_test_key=True,
    )
    # Yesterday's index, validly attested, served again from the same place.
    _attest(index, now=now - timedelta(days=2))
    with pytest.raises(registry.RegistryError, match="older than"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "b",
            index_url=str(index),
            allow_test_key=True,
        )
    assert not (tmp_path / "b").exists()


def test_an_entry_dropped_from_an_attested_index_is_refused(tmp_path):
    index = _attest(_index(tmp_path))
    data = json.loads(index.read_text())
    data["workflows"] = [
        i for i in data["workflows"] if i["id"] != "openimis/claim-intake"
    ]
    index.write_text(json.dumps(data))
    with pytest.raises(registry.RegistryError, match="does not name this index"):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "s",
            index_url=str(index),
            allow_test_key=True,
        )


def test_an_archive_the_attestation_does_not_name_is_refused(tmp_path):
    index = _index(tmp_path)
    bundle = index.parent / "bundles/openemr/new-referral"
    asset = index.parent / "a.tar.gz"
    digest = registry.build_archive(bundle, asset)
    _attest(index)  # the attestation names a.tar.gz as built
    data = json.loads(index.read_text())
    for item in data["workflows"]:
        if item["id"] == "openemr/new-referral":
            # A different archive, pinned by an index that is then attested.
            other = index.parent / "b.tar.gz"
            item["archive"] = {
                "url": other.name,
                "sha256": registry.build_archive(bundle, other, notice="x\n"),
            }
    index.write_text(json.dumps(data))
    other.unlink()
    asset.rename(index.parent / "kept.tar.gz")
    _attest(index)  # names index.json and kept.tar.gz, not b.tar.gz
    with pytest.raises(
        registry.RegistryError, match="does not name this entry's archive"
    ):
        registry.pull(
            "openemr/new-referral",
            tmp_path / "s",
            index_url=str(index),
            allow_test_key=True,
        )
    assert digest


def test_the_release_workflow_attests_and_adds_the_attestation_by_name():
    text = (ROOT.parents[1] / ".github/workflows/release-registry.yml").read_text()
    assert "actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6" in text
    assert (
        "git add packages/flow/registry/index.json "
        "packages/flow/registry/index.sigstore.json" in text
    )
    assert "commit -am" not in text
    assert "REGISTRY_SIGNING_SEED" not in text and "registry-signing" not in text
    # id-token: write only on the job that attests.
    import yaml

    jobs = yaml.safe_load(text)["jobs"]
    granted = [
        name
        for name, job in jobs.items()
        if (job.get("permissions") or {}).get("id-token") == "write"
    ]
    assert granted == ["attest"]
    assert jobs["attest"]["environment"] == "release-identity"


# -- 6. stripping the provenance doesn't make an unqualified pull runnable ------------------


def test_a_pull_with_its_provenance_stripped_is_still_refused(tmp_path):
    from openadapt_flow.ir import Workflow

    dest = _pull(tmp_path)
    workflow = Workflow.load(dest)
    workflow.registry = None
    workflow.save(dest)  # re-seals the content digest
    (dest / skills.LOCK).unlink()
    assert Workflow.load(dest).registry is None
    refusal = skills.pulled_skill_refusal(dest)
    assert refusal is not None and "provenance removed" in refusal
    with pytest.raises(SystemExit, match="REFUSED"):
        skills.refuse_unqualified_skill(dest)


# -- 7. run and an emitted MCP server pass the target, so only the qualified instance runs ---


def _qualified(tmp_path, monkeypatch, origin="https://emr.example.org"):
    dest = _pull(tmp_path)
    monkeypatch.setattr(
        qualification, "current_certification_matches", lambda w, **k: True
    )
    _qualify_on(dest, origin)
    _record_qualified(dest)
    assert skills.pulled_skill_refusal(dest) is None
    return dest


def test_run_checks_the_deployments_app_address(tmp_path, monkeypatch):
    from openadapt_flow.__main__ import main

    dest = _qualified(tmp_path, monkeypatch)
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text("backend:\n  kind: web\n  url: https://production.example/\n")
    with pytest.raises(
        SystemExit,
        match="qualified on https://emr.example.org, not https://production.example",
    ):
        main(["run", str(dest), "--config", str(deploy), "--profile", "standard"])


def test_an_emitted_mcp_server_refuses_another_instance(tmp_path, monkeypatch):
    import importlib.util
    import inspect

    from openadapt_flow.emit.mcp_tool import emit_mcp_server

    dest = _qualified(tmp_path, monkeypatch)
    server = emit_mcp_server(dest, tmp_path / "mcp" / "server.py")
    spec = importlib.util.spec_from_file_location("emitted_server", server)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    tool = next(
        fn
        for name, fn in vars(module).items()
        if inspect.isfunction(fn)
        and fn.__module__ == "emitted_server"
        and "url" in inspect.signature(fn).parameters
    )
    params = {name: "x" for name in inspect.signature(tool).parameters if name != "url"}
    out = tool(url="https://production.example/", **params)
    assert out["outcome"] == "REFUSED"
    assert "not https://production.example" in out["detail"]


# -- 8. a pulled skill qualifies with the contract it ships ------------------------------------


def test_replacing_a_shipped_contract_takes_an_explicit_opt_in(tmp_path):
    dest = _pull(tmp_path)
    own = tmp_path / "mine.yaml"
    own.write_text(
        CONTRACT.replace("${APP_URL}", "https://emr.example.org")
        .replace("${SIGNED_IN_STATE}", str(own))
        .replace("${RESET_COMMAND}", "/usr/bin/true")
        .replace("${RECORD_URL}", "https://emr.example.org")
        .replace("${RECORD_TOKEN_VAR}", "MY_EMR_TOKEN")
    )
    with pytest.raises(skills.SkillError, match="--replace-contract"):
        skills.qualify_skill(dest, certify="p", contract=own, run=_resetting_run({}))
    seen = {}
    skills.qualify_skill(
        dest,
        certify="p",
        contract=own,
        replace_contract=True,
        run=lambda bundle, config, **kw: (
            seen.update(origin=config.app.origin),
            SimpleNamespace(certified=True),
        )[1],
    )
    lock = skills.read_lock(dest)
    assert lock["qualified"]["contract"] == "yours"
    assert lock["qualified"]["contract_opt_in"] is True


def test_a_qualification_with_an_unchosen_contract_is_refused(tmp_path, monkeypatch):
    dest = _qualified(tmp_path, monkeypatch)
    _record_qualified(dest, contract="yours", opt_in=False)
    assert "a contract it doesn't ship" in skills.pulled_skill_refusal(dest)


# -- 9. archives: sizes before reads, https, and the index's own directory -----------------------


def _archive(members):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    return blob, {"sha256": hashlib.sha256(blob).hexdigest()}


def test_an_oversized_member_is_refused_before_it_is_read(tmp_path, monkeypatch):
    blob, archive = _archive([("manifest.json", b"\0" * (registry.MAX_FILE_BYTES + 1))])
    (tmp_path / "a.tar.gz").write_bytes(blob)

    def no_extract(*a, **k):
        raise AssertionError("a member was read before its size was checked")

    monkeypatch.setattr(tarfile.TarFile, "extractfile", no_extract)
    with pytest.raises(registry.RegistryError, match="larger than"):
        registry._unpack_archive(
            {**archive, "url": "a.tar.gz"},
            expected={"manifest.json"},
            local_root=tmp_path,
        )


def test_an_archive_that_unpacks_too_large_is_refused(tmp_path, monkeypatch):
    blob, archive = _archive(
        [("workflow.json", b"1" * 600), ("manifest.json", b"2" * 600)]
    )
    (tmp_path / "a.tar.gz").write_bytes(blob)
    monkeypatch.setattr(registry, "MAX_ARCHIVE_BYTES", 1000)
    with pytest.raises(registry.RegistryError, match="unpacks to more than"):
        registry._unpack_archive(
            {**archive, "url": "a.tar.gz"},
            expected={"workflow.json", "manifest.json"},
            local_root=tmp_path,
        )


def test_an_archive_member_the_entry_does_not_list_is_refused(tmp_path):
    blob, archive = _archive([("evil.py", b"print(1)")])
    (tmp_path / "a.tar.gz").write_bytes(blob)
    with pytest.raises(registry.RegistryError, match="not in the entry"):
        registry._unpack_archive(
            {**archive, "url": "a.tar.gz"},
            expected={"workflow.json"},
            local_root=tmp_path,
        )


def test_an_archive_or_file_must_come_from_https_or_the_index_directory(tmp_path):
    outside = tmp_path / "outside.tar.gz"
    blob, archive = _archive([("workflow.json", b"{}")])
    outside.write_bytes(blob)
    with pytest.raises(registry.RegistryError, match="https address"):
        registry._unpack_archive(
            {**archive, "url": str(outside)}, expected={"workflow.json"}
        )
    (tmp_path / "index").mkdir()
    with pytest.raises(registry.RegistryError, match="outside the index's directory"):
        registry._unpack_archive(
            {**archive, "url": str(outside)},
            expected={"workflow.json"},
            local_root=tmp_path / "index",
        )
    with pytest.raises(registry.RegistryError, match="plain http"):
        registry._read("http://example.org/x")
    item = {
        "id": "a/b",
        "status": "example",
        "path": "bundles/../../etc",
        "files": {"workflow.json": "0"},
    }
    with pytest.raises(registry.RegistryError, match="unsafe path"):
        registry.validate_index({"workflows": [item]})


# -- lower: an allowlist of public files, screens, versions, the signing environment -----


@pytest.mark.parametrize(
    "name",
    [
        "verify.full.yml",
        "Faults.yaml",
        "Rows.csv",
        "notes.txt",
        "hooks.py",
        "templates/x.jpg",
    ],
)
def test_a_public_bundle_holds_only_allowlisted_files(name):
    item = {"id": "a/b", "status": "example", "files": {name: "0" * 64}}
    with pytest.raises(registry.RegistryError, match="private tier|may hold"):
        registry.validate_index({"workflows": [item]})


def test_screen_templates_need_the_synthetic_data_attestation():
    item = {
        "id": "a/b",
        "status": "example",
        "files": {"workflow.json": "0", "templates/t.png": "0"},
    }
    with pytest.raises(registry.RegistryError, match="synthetic"):
        registry.validate_index({"workflows": [item]})
    item["screens"] = {"data": "synthetic"}
    registry.validate_index({"workflows": [item]})


def test_a_release_outranks_its_pre_release():
    entries = registry.validate_index(
        {
            "workflows": [
                {
                    "id": "a/b",
                    "version": v,
                    "status": "example",
                    "files": {"workflow.json": "0"},
                }
                for v in ("1.2.0", "1.2.0-rc.1", "1.10.0-rc.2", "1.9.0")
            ]
        }
    )
    assert registry.find(entries, "a/b").version == "1.10.0-rc.2"
    ordered = sorted(
        ("1.2.0", "1.2.0-rc.1", "1.2.0-rc.10", "1.2.0-rc.2"), key=registry._version_key
    )
    assert ordered == ["1.2.0-rc.1", "1.2.0-rc.2", "1.2.0-rc.10", "1.2.0"]


def test_the_release_verifier_environment_is_hash_pinned():
    text = (ROOT / "registry/verify-requirements.txt").read_text()
    requirements = [
        line for line in text.splitlines() if line and not line.startswith((" ", "#"))
    ]
    assert requirements and all("==" in line for line in requirements)
    assert any(line.startswith("sigstore==") for line in requirements)
    assert text.count("--hash=sha256:") >= len(requirements)
    workflow = (ROOT.parents[1] / ".github/workflows/release-registry.yml").read_text()
    attest_job = workflow.split("\n  attest:\n", 1)[1].split("\n  publish:\n", 1)[0]
    assert "--require-hashes" in attest_job and "pip install -e" not in attest_job


# -- the real Sigstore path: a bundle GitHub attested for an OpenAdapt release -----------------

FIXTURES = ROOT / "tests/fixtures/registry"


def _desktop_identity():
    import re

    from openadapt_flow import registry_attest

    return registry_attest.Identity(
        issuer="https://token.actions.githubusercontent.com",
        san=re.compile(
            r"^https://github\.com/OpenAdaptAI/OpenAdapt/\.github/workflows/"
            r"desktop-release\.yml@refs/heads/main$"
        ),
        source_repository_uri="https://github.com/OpenAdaptAI/OpenAdapt",
    )


def _desktop_bundle():
    return json.loads(
        (FIXTURES / "desktop-0.24.1-SHA256SUMS.sigstore.json").read_text()
    )


def test_a_real_release_attestation_verifies_offline_with_the_pinned_root():
    pytest.importorskip("sigstore")
    from openadapt_flow import registry_attest

    signer, subjects = registry_attest.verify_sigstore(
        _desktop_bundle(),
        identity=_desktop_identity(),
        trusted_root=registry_attest.TRUSTED_ROOT_FILE,
    )
    assert signer.endswith("desktop-release.yml@refs/heads/main")
    assert subjects == {
        "SHA256SUMS": hashlib.sha256(
            (FIXTURES / "desktop-0.24.1-SHA256SUMS").read_bytes()
        ).hexdigest()
    }


def test_a_real_attestation_from_another_workflow_is_not_the_registry():
    pytest.importorskip("sigstore")
    from openadapt_flow import registry_attest

    with pytest.raises(
        registry_attest.AttestationError, match="not the pinned workflow"
    ):
        registry_attest.verify_sigstore(
            _desktop_bundle(), trusted_root=registry_attest.TRUSTED_ROOT_FILE
        )


def test_a_real_attestation_with_its_statement_changed_is_refused():
    pytest.importorskip("sigstore")
    from openadapt_flow import registry_attest

    bundle = _desktop_bundle()
    statement = json.loads(base64.b64decode(bundle["dsseEnvelope"]["payload"]))
    statement["subject"][0]["digest"]["sha256"] = "0" * 64
    bundle["dsseEnvelope"]["payload"] = base64.b64encode(
        json.dumps(statement).encode()
    ).decode()
    with pytest.raises(registry_attest.AttestationError, match="does not hold"):
        registry_attest.verify_sigstore(
            bundle,
            identity=_desktop_identity(),
            trusted_root=registry_attest.TRUSTED_ROOT_FILE,
        )


def test_a_trusted_root_without_the_signers_keys_refuses_it(tmp_path):
    pytest.importorskip("sigstore")
    from openadapt_flow import registry_attest

    root = json.loads(registry_attest.TRUSTED_ROOT_FILE.read_text())
    root["certificateAuthorities"] = []
    empty = tmp_path / "trusted_root.json"
    empty.write_text(json.dumps(root))
    with pytest.raises(registry_attest.AttestationError):
        registry_attest.verify_sigstore(
            _desktop_bundle(), identity=_desktop_identity(), trusted_root=empty
        )


def test_a_refreshed_trusted_root_is_preferred(tmp_path, monkeypatch):
    from openadapt_flow import registry_attest

    assert registry_attest.trusted_root_path() == registry_attest.TRUSTED_ROOT_FILE
    refreshed = registry_attest.refreshed_trusted_root()
    refreshed.parent.mkdir(parents=True)
    refreshed.write_text("{}")
    assert registry_attest.trusted_root_path() == refreshed


def test_without_sigstore_a_remote_pull_says_what_to_install(monkeypatch):
    import builtins

    from openadapt_flow import registry_attest

    real_import = builtins.__import__

    def no_sigstore(name, *a, **k):
        if name == "sigstore" or name.startswith("sigstore."):
            raise ImportError(name)
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_sigstore)
    with pytest.raises(
        registry_attest.AttestationError, match=r"openadapt-flow\[registry\]"
    ):
        registry_attest.verify_sigstore(_desktop_bundle())
