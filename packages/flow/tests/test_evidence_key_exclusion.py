"""One filename-family model across the actual evidence publication seams."""

from __future__ import annotations

import base64
import builtins
import hashlib
import importlib.util
import io
import json
import runpy
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

from openadapt_flow import hosted, sanitized_artifact
from openadapt_flow.evidence_files import copy_evidence_tree, is_key_material_path
from openadapt_flow.qualify import campaign
from scripts import export_public_demo_evidence as demo

FLOW = Path(__file__).resolve().parents[1]
REPO = FLOW.parents[1]


def _load_source(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def guard():
    return runpy.run_path(str(REPO / "scripts/check_evidence_keys.py"))


# This is the normative family table, independent of implementation constants.
# No real key/credential or existing artifact payload is used by these tests.
PRIVATE_NAMES = (
    ".attended_capability.key", "signer.KEY", "authority.key~", "authority.pem~",
    "authority.key.bak", "keys.json", "seal-key.json", "site-keys.json",
    "seal-keys.json", "signing-key.json", "hmac-key.json", "password.json",
    "credentials.yaml", "credentials.txt", "secret.toml", "token.yml",
    "webauthn_passkey_double.json", "named_person_binding.json",
    "attended_capability.json", "attended_capability_history.json.enc",
    "id_rsa.bak", "id_ed25519.old", "credentials.backup", ".env", ".env.local",
    "client.p12", "client.pfx", "private.jks", "private.keystore",
    ".ssh/identity", ".aws/config", ".private/config.json",
)


@given(
    name=st.sampled_from(PRIVATE_NAMES),
    parents=st.lists(st.sampled_from(("run", "evidence", "nested")), max_size=4),
    windows=st.booleans(),
)
@settings(max_examples=80, deadline=None)
def test_generated_private_families_are_denied_at_any_depth(name, parents, windows):
    relative = "/".join([*parents, name])
    if windows:
        relative = relative.replace("/", "\\")
    assert is_key_material_path(relative)


@pytest.mark.parametrize(
    "relative",
    ("seal_keys.py", "keys.py", "secret-scan.yml", "tokens.css", "report.json",
     "public.crt", "public.cer", "nested/checkpoint.json"),
)
def test_source_code_and_public_certificate_names_are_distinct(relative):
    assert not is_key_material_path(relative)


def _forbid_payload_reads(monkeypatch, denied: set[Path]) -> None:
    """Observe real file opens, rather than replacing a publication producer."""
    original_builtin = builtins.open
    original_io = io.open

    def checked(opener):
        def open_checked(file, *args, **kwargs):
            if isinstance(file, (str, bytes, Path)):
                candidate = Path(file)
                assert candidate not in denied, "excluded payload was opened"
            return opener(file, *args, **kwargs)
        return open_checked

    monkeypatch.setattr(builtins, "open", checked(original_builtin))
    monkeypatch.setattr(io, "open", checked(original_io))


@pytest.mark.parametrize("name", PRIVATE_NAMES)
def test_actual_export_boundaries_do_not_read_or_copy_private_names(
    tmp_path, monkeypatch, guard, name,
):
    source = tmp_path / "run"
    source.mkdir()
    private = source / name
    private.parent.mkdir(parents=True, exist_ok=True)
    private.write_text("synthetic excluded marker", encoding="utf-8")
    (source / "report.json").write_text("{}", encoding="utf-8")
    _forbid_payload_reads(monkeypatch, {private})

    copied = copy_evidence_tree(source, tmp_path / "derivative")
    assert (copied / "report.json").read_text() == "{}"
    assert not (copied / name).exists()
    assert private.exists()  # Exporting never mutates the original.

    scorer = _load_source(
        "evidence_scorer", FLOW / "benchmark/harness2_scorer/summarize.py"
    )
    assert scorer.copy_raw(source, tmp_path / "raw") == 1
    assert not (tmp_path / "raw" / name).exists()

    from benchmark.harness.harness2 import traces

    trace_root = traces.export(tmp_path / "traces", [], harnesses={"sample": source})
    assert not (trace_root / "candidates/sample/harness" / name).exists()
    assert (trace_root / "candidates/sample/harness/report.json").is_file()

    zipper = _load_source("evidence_zipper", FLOW / "benchmark/rdp_avc_real/harness.py")
    archive = tmp_path / "evidence.zip"
    zipper.zip_dir(source, archive)
    with zipfile.ZipFile(archive) as payload:
        assert payload.namelist() == ["report.json"]

    for operation in (demo._tree_digest, demo.validate_pack):
        with pytest.raises(demo.EvidencePackError, match="live run authority"):
            operation(source)
    with pytest.raises(demo.EvidencePackError, match="live run authority"):
        demo._file_ref(source, private)
    with pytest.raises(hosted.HostedError, match="local key material"):
        hosted._zip_dir(source)
    with pytest.raises(hosted.HostedError, match="local key material"):
        hosted._scrub_recording_tree(source)
    with pytest.raises(sanitized_artifact.SanitizationError, match="local key material"):
        sanitized_artifact.source_tree_sha256(source)
    with pytest.raises(campaign.QualifyError, match="Local key material"):
        campaign._evidence_ref(source, private, "other")
    assert campaign._run_evidence_refs(source, source) == []

    # The ordinary tracked-file refusal does not open even an existing file.
    assert guard["violations"](source, [f"benchmark/{name}"]) == [f"benchmark/{name}"]


def test_run_derivative_strips_all_key_families_without_opening(tmp_path, monkeypatch):
    denied = set()
    for name in PRIVATE_NAMES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic excluded marker")
        denied.add(path)
    (tmp_path / "report.json").write_text("{}")
    _forbid_payload_reads(monkeypatch, denied)
    demo._strip_run_authority(tmp_path)
    assert sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()) == ["report.json"]


def test_qualification_still_binds_nonsecret_run_evidence(tmp_path):
    (tmp_path / "report.json").write_text("{}")
    (tmp_path / "observed.json").write_text('{"complete":true}')
    refs = campaign._run_evidence_refs(tmp_path, tmp_path)
    assert len(refs) == 1
    assert refs[0].relative_path == "observed.json"
    assert refs[0].sha256 == hashlib.sha256(b'{"complete":true}').hexdigest()


def test_copy_refuses_root_and_descendant_symlinks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "report.json").write_text("{}")
    linked = tmp_path / "linked"
    linked.symlink_to(source, target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        copy_evidence_tree(linked, tmp_path / "first")
    (source / "external.json").symlink_to(source / "report.json")
    with pytest.raises(ValueError, match="symbolic link"):
        copy_evidence_tree(source, tmp_path / "second")
    assert not (tmp_path / "second").exists()


def _public_entry() -> dict[str, str]:
    # Public verification bytes only; no signing/private material is created.
    raw = bytes(range(32))
    return {
        "key_id": "seal-ed25519-" + hashlib.sha256(raw).hexdigest()[:16],
        "public_key": base64.b64encode(raw).decode("ascii"),
        "status": "active",
    }


def test_exact_public_registry_exception_requires_closed_schema(tmp_path, guard):
    relative = sorted(guard["PUBLIC_REGISTRIES"])[0]
    target = tmp_path / relative
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps([_public_entry()]))
    assert guard["violations"](tmp_path, [relative]) == []
    entry = {**_public_entry(), "private_key": "synthetic forbidden extra"}
    target.write_text(json.dumps([entry]))
    assert guard["violations"](tmp_path, [relative]) == [relative]
    assert guard["violations"](tmp_path, ["benchmark/other/seal-key.json"]) == ["benchmark/other/seal-key.json"]
    assert guard["violations"](tmp_path, ["Evidence/KEYS.JSON"]) == ["Evidence/KEYS.JSON"]
    assert guard["violations"](tmp_path, ["root.key", "root.key/data.json"]) == ["root.key", "root.key/data.json"]


def test_public_registry_exception_refuses_indirect_paths_before_open(tmp_path, guard, monkeypatch):
    relative = sorted(guard["PUBLIC_REGISTRIES"])[0]
    outer = tmp_path / "outside"
    outer.mkdir()
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "packages").symlink_to(outer, target_is_directory=True)
    _forbid_payload_reads(monkeypatch, {root / relative})
    assert guard["violations"](root, [relative]) == [relative]


@pytest.mark.parametrize(
    "change",
    ({"private_key": "synthetic"}, {"seed": "synthetic"}, {"status": "unknown"},
     {"public_key": "AA=="}, {"key_id": "wrong"}),
)
def test_public_registry_cannot_hide_unknown_or_private_fields(guard, change):
    assert not guard["is_public_registry"](json.dumps([{**_public_entry(), **change}]))


def test_public_registry_rejects_duplicate_and_outer_fields(guard):
    entry = json.dumps(_public_entry())
    assert not guard["is_public_registry"](f'{{"keys":[{entry}],"secret":"synthetic"}}')
    assert not guard["is_public_registry"](f'{{"keys":[{entry}],"keys":[{entry}]}}')
    assert not guard["is_public_registry"](f'[{entry},{entry}]')
    assert guard["is_public_registry"](f'{{"keys":[{entry}]}}')


def test_public_certificate_exception_is_not_a_private_key_container(guard):
    from datetime import datetime, timedelta, timezone

    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.x509.oid import NameOID

    key = Ed25519PrivateKey.generate()  # Ephemeral test memory only.
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic evidence CA")])
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(1).not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, algorithm=None)
    ).public_bytes(serialization.Encoding.PEM).decode("ascii")
    assert guard["is_public_certificate"](certificate)
    assert not guard["is_public_certificate"]("-----BEGIN PRIVATE KEY-----\nAA==\n-----END PRIVATE KEY-----")
    assert not guard["is_public_certificate"]("-----BEGIN CERTIFICATE-----\nAA==\n-----END CERTIFICATE-----")


def test_git_ignore_and_ci_use_actual_publication_guard(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_bytes((REPO / ".gitignore").read_bytes())
    for name in ("run.key", ".attended_capability.key", "nested/key.KEY", "run.key~"):
        result = subprocess.run(
            ["git", "check-ignore", name], cwd=tmp_path, capture_output=True,
        )
        assert result.returncode == 0
    workflow = (REPO / ".github/workflows/packages.yml").read_text()
    assert "run: python3 scripts/check_evidence_keys.py" in workflow


def test_actual_ci_command_rejects_force_added_synthetic_key_without_values(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    key = tmp_path / "benchmark/run/authority.key"
    key.parent.mkdir(parents=True)
    marker = "synthetic-content-that-must-never-appear-in-diagnostics"
    key.write_text(marker)
    subprocess.run(["git", "add", "-f", "benchmark"], cwd=tmp_path, check=True)
    result = subprocess.run(
        [sys.executable, str(REPO / "scripts/check_evidence_keys.py"), "--root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert result.returncode == 1
    assert "EVIDENCE_KEYS_DENIED" in result.stdout
    assert "benchmark/run/authority.key" in result.stdout
    assert marker not in result.stdout + result.stderr
    abbreviated = subprocess.run(
        [sys.executable, str(REPO / "scripts/check_evidence_keys.py"), "--roo", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert abbreviated.returncode == 2
    assert "unrecognized arguments: --roo" in abbreviated.stderr
    assert marker not in abbreviated.stdout + abbreviated.stderr
