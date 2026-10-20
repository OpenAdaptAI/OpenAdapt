"""Run authorizations signed with a GitHub SSH key, and the operator profile.

The person's key is a throwaway Ed25519 key made in each test; GitHub's
``.keys`` answer is a stand-in function; the transparency log is the fake
Rekor of ``tests/test_seal_anchor.py``. Two tests use the real ``ssh-keygen``
when it is installed. Nothing here contacts the network. All data is
synthetic.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.seal import parse_key_registry

from openadapt_flow import seal_keys, ssh_sig
from openadapt_flow.offline_seal import (
    SEAL_FILE,
    LocalSealError,
    seal_run,
    sign_subject,
    signer_for,
    verify_run,
)
from openadapt_flow.operator_profile import build_profile, outcome_category
from openadapt_flow.run_authorization import (
    NAMESPACE,
    SIGNATURE_FILE,
    SIGNERS_FILE,
    AuthorizationError,
    anchor,
    create,
    seal_authorized,
    signing_command,
    verify,
)
from openadapt_flow.seal_anchor import anchor_seal
from tests.test_seal_anchor import FakeRekor

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = "b" * 64
USER = "synthetic-operator"
PATIENT = "Synthia Example"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (seal_keys.ENV_KEY, seal_keys.ENV_KEY_FILE, "OPENADAPT_NO_OUTBOUND"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def rekor() -> FakeRekor:
    return FakeRekor()


class Person:
    """Someone with an Ed25519 SSH key published on (stand-in) GitHub."""

    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        from cryptography.hazmat.primitives import serialization

        self.raw = self.key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )

    def github(self, user: str) -> list[str]:
        assert user == USER
        # GitHub lists every key type; only ssh-ed25519 counts.
        return ["ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQ", ssh_sig.key_line(self.raw)]

    def sign(self, path: Path, namespace: str = NAMESPACE) -> Path:
        blob = ssh_sig.signed_blob(path.read_bytes(), namespace, "sha512")
        sig = path.with_name(path.name + ".sig")
        sig.write_text(ssh_sig.armor(self.raw, namespace, self.key.sign(blob)))
        return sig


def _run(
    tmp_path: Path,
    name: str,
    *,
    outcome: str = "VERIFIED",
    transaction: str = "VERIFIED",
    profile: str = "standard",
    bundle: str = BUNDLE,
) -> Path:
    run = tmp_path / "runs" / name
    run.mkdir(parents=True)
    report = {
        "workflow_name": "openemr-referral-intake",
        "execution_profile": profile,
        "execution_outcome": outcome,
        "transaction_outcome": transaction,
        "bundle_content_digest": bundle,
        "run_id_sha256": "c" * 64,
        "started_at": "2026-10-19T12:00:00+00:00",
        "params": {"fname": "Synthia", "lname": "Example", "record_id": "MRN-0042"},
    }
    (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return run


def _setup(tmp_path: Path, person: Person, **create_kwargs: Any):
    home = tmp_path / "home"
    signer = signer_for(home)
    kwargs = dict(
        bundle_content_digest=BUNDLE,
        items=["r3", "r4", "r5", "r6"],
        profile="standard",
        authorizer=f"github:{USER}",
        seal_key_ids=[signer.key_id],
        fetch_keys=person.github,
    )
    kwargs.update(create_kwargs)
    statement = create(tmp_path / "auth", **kwargs)
    return home, signer, statement, parse_key_registry([signer.registry_entry()])


def _anchor_all(run: Path, auth_dir: Path, signer, rekor: FakeRekor) -> None:
    anchor(auth_dir, signer, log_keys=[rekor.log], submit=rekor.submit, env={})
    anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )


# --- SSH signatures -------------------------------------------------------------


def test_create_writes_statement_and_github_snapshot(tmp_path):
    person = Person()
    _, signer, statement, _ = _setup(tmp_path, person)
    data = json.loads(statement.read_text())
    assert data["schema"] == "openadapt.run-authorization/v1"
    assert data["authorizer"] == f"github:{USER}"
    assert data["scope"] == {
        "items": ["r3", "r4", "r5", "r6"],
        "profile": "standard",
        "seal_key_ids": [signer.key_id],
    }
    assert len(data["nonce"]) == 64 and data["bundle_content_digest"] == BUNDLE
    snapshot = (tmp_path / "auth" / SIGNERS_FILE).read_text()
    assert f"https://github.com/{USER}.keys" in snapshot
    assert ssh_sig.key_line(person.raw) in snapshot and "ssh-rsa" not in snapshot
    assert signing_command(statement).startswith(
        "ssh-keygen -Y sign -n openadapt-run-authorization -f ~/.ssh/id_ed25519 /"
    )
    # Not signed yet.
    assert verify(statement).reason == "auth_unsigned"
    with pytest.raises(AuthorizationError):
        _setup(tmp_path, person)  # never overwrites


def test_signed_authorization_verifies_offline(tmp_path, monkeypatch):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    person.sign(statement)

    def no_network(*a, **k):
        raise AssertionError("offline verification must not fetch")

    monkeypatch.setattr("urllib.request.urlopen", no_network)
    result = verify(tmp_path / "auth")
    assert result.ok, result.reason
    assert result.signer_fingerprint == ssh_sig.fingerprint(person.raw)
    assert result.signers_source == "the stored GitHub snapshot"


def test_wrong_key_is_refused(tmp_path):
    person, stranger = Person(), Person()
    _, _, statement, _ = _setup(tmp_path, person)
    stranger.sign(statement)  # a valid signature, by a key GitHub doesn't list
    result = verify(statement)
    assert result.reason == "auth_unknown_signer"


def test_key_no_longer_on_github_is_refused_live(tmp_path):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    asked: list[str] = []

    def github_now(user: str) -> list[str]:
        asked.append(user)
        return [ssh_sig.key_line(Person().raw)]  # the person rotated their key

    result = verify(statement, live=github_now)
    assert result.reason == "auth_not_on_github" and asked == [USER]
    assert verify(statement, live=person.github).ok

    def down(user: str) -> list[str]:
        raise OSError("unreachable")

    assert verify(statement, live=down).reason == "auth_live_unavailable"


def test_github_user_with_no_ed25519_key_is_refused(tmp_path):
    with pytest.raises(AuthorizationError) as exc:
        _setup(tmp_path, Person(), fetch_keys=lambda user: ["ssh-rsa AAAAB3Nza"])
    assert exc.value.code == "auth_no_ed25519_key"


def test_live_fetch_is_github_only():
    assert ssh_sig.github_keys_url(USER) == f"https://github.com/{USER}.keys"
    for bad in ("evil.example/x", "../x", "a" * 40, "-x", ""):
        with pytest.raises(ssh_sig.SshSigError):
            ssh_sig.github_keys_url(bad)


def test_edited_statement_is_refused(tmp_path):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    statement.write_text(statement.read_text().replace('"r6"', '"r7"'))
    assert verify(statement).reason == "auth_bad_signature"


def test_other_namespace_is_refused(tmp_path):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    person.sign(statement, namespace="file")
    assert verify(statement).reason == "auth_bad_signature"


def test_duplicate_json_key_is_malformed(tmp_path):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    text = statement.read_text()
    statement.write_text(
        text.replace(
            '"profile": "standard"', '"profile": "standard", "profile": "demo"'
        )
    )
    person.sign(statement)  # even signed, a statement two parsers read differently
    assert verify(statement).reason == "auth_malformed"


def test_pinned_allowed_signers_replace_the_snapshot(tmp_path):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    mine = tmp_path / "my-allowed-signers"
    mine.write_text(
        ssh_sig.allowed_signers_line(f"github:{USER}", NAMESPACE, Person().raw) + "\n"
    )
    assert verify(statement, allowed_signers=mine).reason == "auth_unknown_signer"
    mine.write_text(
        ssh_sig.allowed_signers_line(f"github:{USER}", NAMESPACE, person.raw) + "\n"
    )
    ok = verify(statement, allowed_signers=mine)
    assert ok.ok and ok.signers_source == "pinned allowed signers"


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_real_ssh_keygen_signature_verifies(tmp_path):
    key = tmp_path / "id_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)],
        check=True,
    )
    raw = ssh_sig.parse_key_line(key.with_suffix(".pub").read_text())
    statement = create(
        tmp_path / "auth",
        bundle_content_digest=BUNDLE,
        items=["r3"],
        profile="standard",
        authorizer=f"github:{USER}",
        seal_key_ids=["seal-ed25519-0123456789abcdef"],
        fetch_keys=lambda user: [ssh_sig.key_line(raw)],
    )
    command = signing_command(statement).split()
    command[command.index("~/.ssh/id_ed25519")] = str(key)
    subprocess.run(command, check=True, capture_output=True)
    assert verify(statement).ok
    # And ssh-keygen accepts the snapshot as its allowed-signers file.
    proc = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(tmp_path / "auth" / SIGNERS_FILE),
            "-I",
            f"github:{USER}",
            "-n",
            NAMESPACE,
            "-s",
            str(tmp_path / "auth" / SIGNATURE_FILE),
        ],
        input=statement.read_bytes(),
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
def test_ssh_keygen_accepts_our_signature_format(tmp_path):
    person = Person()
    _, _, statement, _ = _setup(tmp_path, person)
    sig = person.sign(statement)
    proc = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(tmp_path / "auth" / SIGNERS_FILE),
            "-I",
            f"github:{USER}",
            "-n",
            NAMESPACE,
            "-s",
            str(sig),
        ],
        input=statement.read_bytes(),
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr


# --- Seals under an authorization ------------------------------------------------


def _authorized_run(tmp_path, person, rekor, item="r3", **run_kwargs):
    home, signer, statement, registry = _setup(tmp_path, person)
    person.sign(statement)
    run = _run(tmp_path, item, **run_kwargs)
    seal = seal_authorized(run, tmp_path / "auth", item, signer, home=home)
    return home, signer, statement, registry, run, seal


def test_authorized_seal_verifies_with_anchors(tmp_path, rekor):
    person = Person()
    _, signer, statement, registry, run, seal = _authorized_run(tmp_path, person, rekor)
    assert seal.scope_item == "r3" and seal.authorization_sha256
    _anchor_all(run, tmp_path / "auth", signer, rekor)
    auth = verify(
        tmp_path / "auth", registry=registry, require_anchor=True, log_keys=[rekor.log]
    )
    assert auth.ok, auth.reason
    result = verify_run(
        run, registry, require_anchor=True, log_keys=[rekor.log], authorization=auth
    )
    assert result.ok, result.reason


@pytest.mark.parametrize(
    ("item", "run_kwargs", "reason"),
    [
        ("r9", {}, "auth_scope_mismatch"),
        ("r3", {"profile": "demo"}, "auth_scope_mismatch"),
        ("r3", {"bundle": "d" * 64}, "auth_bundle_mismatch"),
    ],
)
def test_sealing_outside_the_authorization_is_refused(
    tmp_path, rekor, item, run_kwargs, reason
):
    person = Person()
    home, signer, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    run = _run(tmp_path, "run", **run_kwargs)
    with pytest.raises(LocalSealError) as exc:
        seal_authorized(run, tmp_path / "auth", item, signer, home=home)
    assert exc.value.code == reason
    assert not (run / SEAL_FILE).exists()


def test_seal_key_outside_the_scope_is_refused(tmp_path):
    person = Person()
    home, _, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    other = seal_keys.SoftwareSigner(Ed25519PrivateKey.generate(), "file")
    with pytest.raises(LocalSealError) as exc:
        seal_authorized(_run(tmp_path, "r3"), tmp_path / "auth", "r3", other, home=home)
    assert exc.value.code == "auth_scope_mismatch"


def test_unsigned_authorization_is_refused_at_sealing(tmp_path):
    home, signer, _, _ = _setup(tmp_path, Person())
    with pytest.raises(LocalSealError) as exc:
        seal_authorized(
            _run(tmp_path, "r3"), tmp_path / "auth", "r3", signer, home=home
        )
    assert exc.value.code == "auth_unsigned"


def _direct_seal(run: Path, signer, ref: str | None, item: str | None) -> None:
    """A Seal written around the checks, as a forger with the Seal key could."""
    import hashlib

    digest = hashlib.sha256((run / "report.json").read_bytes()).hexdigest()
    seal = sign_subject(
        "run-report",
        digest,
        run.name,
        outcome="VERIFIED",
        signer=signer,
        authorization_sha256=ref,
        scope_item=item,
    )
    (run / SEAL_FILE).write_text(json.dumps(seal.wire()))


@pytest.mark.parametrize(
    ("ref", "item", "run_kwargs", "reason"),
    [
        (None, None, {}, "auth_not_referenced"),
        ("e" * 64, "r3", {}, "auth_reference_mismatch"),
        ("AUTH", "r9", {}, "auth_scope_mismatch"),
        ("AUTH", "r3", {"profile": "demo"}, "auth_scope_mismatch"),
        ("AUTH", "r3", {"bundle": "d" * 64}, "auth_bundle_mismatch"),
    ],
)
def test_verify_checks_reference_scope_and_bundle(
    tmp_path, ref, item, run_kwargs, reason
):
    person = Person()
    _, signer, statement, registry = _setup(tmp_path, person)
    person.sign(statement)
    auth = verify(statement)
    run = _run(tmp_path, "run", **run_kwargs)
    _direct_seal(run, signer, auth.sha256 if ref == "AUTH" else ref, item)
    assert verify_run(run, registry).ok  # the Seal alone is fine
    assert verify_run(run, registry, authorization=auth).reason == reason


@pytest.mark.parametrize(
    ("started_at", "timing"),
    [
        ("2026-10-19T12:00:00+00:00", "after_run"),  # signed after the run
        ("2999-01-01T00:00:00+00:00", "before_run"),  # signed before the run
    ],
)
def test_timing_is_judged_against_the_runs_own_start(
    tmp_path, rekor, started_at, timing
):
    person = Person()
    home, signer, statement, registry = _setup(tmp_path, person)
    person.sign(statement)
    # Authorization logged first, then the run sealed under it and logged.
    anchor(tmp_path / "auth", signer, log_keys=[rekor.log], submit=rekor.submit, env={})
    run = _run(tmp_path, "r3")
    report = json.loads((run / "report.json").read_text())
    report["started_at"] = started_at
    (run / "report.json").write_text(json.dumps(report))
    seal_authorized(run, tmp_path / "auth", "r3", signer, home=home)
    anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    auth = verify(
        tmp_path / "auth", registry=registry, require_anchor=True, log_keys=[rekor.log]
    )
    result = verify_run(
        run, registry, require_anchor=True, log_keys=[rekor.log], authorization=auth
    )
    # Valid either way; re-sealing after signing never turns "after" into "before".
    assert result.ok and result.authorization_timing == timing
    assert result.authorization_vs_seal == "before_seal"
    # Without the authorization's anchor nothing independent is known.
    plain = verify_run(run, registry, authorization=verify(tmp_path / "auth"))
    assert plain.ok and plain.authorization_timing == "not_anchored"


def test_authorization_anchor_must_be_by_a_pinned_key(tmp_path, rekor):
    person = Person()
    _, signer, _, registry, run, _ = _authorized_run(tmp_path, person, rekor)
    _anchor_all(run, tmp_path / "auth", signer, rekor)
    stranger = parse_key_registry(
        [
            seal_keys.SoftwareSigner(
                Ed25519PrivateKey.generate(), "file"
            ).registry_entry()
        ]
    )
    auth = verify(
        tmp_path / "auth", registry=stranger, require_anchor=True, log_keys=[rekor.log]
    )
    assert auth.reason == "anchor_key_mismatch"
    missing = verify(tmp_path / "auth", registry=registry, require_anchor=True)
    assert (
        missing.reason == "anchor_unknown_log"
    )  # the default log key isn't the fake's


def test_existing_seal_is_replaced_only_when_unanchored_and_asked(tmp_path, rekor):
    person = Person()
    home, signer, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    run = _run(tmp_path, "r3")
    seal_run(run, home=home, signer=signer, enqueue=False)  # sealed by the run hook
    with pytest.raises(LocalSealError) as exc:
        seal_authorized(run, tmp_path / "auth", "r3", signer, home=home)
    assert exc.value.code == "already_sealed"
    seal = seal_authorized(
        run, tmp_path / "auth", "r3", signer, home=home, replace_unanchored=True
    )
    assert seal.scope_item == "r3"
    anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    with pytest.raises(LocalSealError) as exc:
        seal_authorized(
            run, tmp_path / "auth", "r4", signer, home=home, replace_unanchored=True
        )
    assert exc.value.code == "already_anchored"


def test_seals_without_an_authorization_keep_their_bytes(tmp_path):
    run = _run(tmp_path, "plain")
    seal = seal_run(run, home=tmp_path / "home", enqueue=False)
    on_disk = json.loads((run / SEAL_FILE).read_text())
    assert "authorization_sha256" not in on_disk and "scope_item" not in on_disk
    assert seal.wire() == on_disk


# --- operator profile ------------------------------------------------------------


def test_outcome_categories():
    assert outcome_category("VERIFIED", "VERIFIED") == "verified"
    assert outcome_category("HALTED", "HALTED_BEFORE_EFFECT") == "stopped_before_effect"
    assert outcome_category("COMPLETED_UNVERIFIED", None) == "completed_unverified"
    assert outcome_category("FAILED", "FAILED_PLATFORM") == "failed"
    assert (
        outcome_category("HALTED", "RECONCILIATION_REQUIRED")
        == "RECONCILIATION_REQUIRED"
    )
    # A VERIFIED execution whose transaction says otherwise is not a success.
    assert (
        outcome_category("VERIFIED", "RECONCILIATION_REQUIRED")
        == "RECONCILIATION_REQUIRED"
    )


def test_profile_counts_only_anchored_seals_and_shows_every_outcome(tmp_path, rekor):
    person = Person()
    home, signer, statement, registry = _setup(tmp_path, person)
    person.sign(statement)
    runs = []
    for item, outcome, tx, anchored in [
        ("r3", "VERIFIED", "VERIFIED", True),
        ("r4", "HALTED", "HALTED_BEFORE_EFFECT", True),
        ("r5", "COMPLETED_UNVERIFIED", "COMPLETED_UNVERIFIED", True),
        ("r6", "HALTED", "RECONCILIATION_REQUIRED", True),
        ("r3-retry", "VERIFIED", "VERIFIED", False),
    ]:
        run = _run(
            tmp_path,
            f"rundir-{item}-synthia-example",
            outcome=outcome,
            transaction=tx,
        )
        seal_authorized(run, tmp_path / "auth", item.split("-")[0], signer, home=home)
        if anchored:
            anchor_seal(
                run / SEAL_FILE,
                signer,
                log_keys=[rekor.log],
                submit=rekor.submit,
                env={},
            )
        runs.append(run)
    tampered = _run(tmp_path, "tampered")
    seal_authorized(tampered, tmp_path / "auth", "r3", signer, home=home)
    anchor_seal(
        tampered / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    (tampered / "report.json").write_text("{}")
    anchor(tmp_path / "auth", signer, log_keys=[rekor.log], submit=rekor.submit, env={})

    profile = build_profile(runs + [tampered], registry, log_keys=[rekor.log])
    assert profile["excluded"] == {"unanchored": 1, "not_valid": 1}
    [op] = profile["operators"].values()
    assert op["seal_key_id"] == signer.key_id
    assert op["runs"] == 4
    assert op["outcomes"] == {
        "verified": 1,
        "stopped_before_effect": 1,
        "completed_unverified": 1,
        "failed": 0,
    }
    assert op["other_outcomes"] == {"RECONCILIATION_REQUIRED": 1}
    assert op["apps"] == ["openemr"] and op["programs"] == ["openemr-referral-intake"]
    assert len(op["rekor_log_indexes"]) == 4
    # One line per counted Seal, so a reader can recount; nothing private.
    assert [r["scope_item"] for r in op["run_list"]] == ["r3", "r4", "r5", "r6"]
    assert {r["outcome"] for r in op["run_list"]} == {
        "verified",
        "stopped_before_effect",
        "completed_unverified",
        "RECONCILIATION_REQUIRED",
    }
    assert all(r["anchor"]["uuid"] and r["seal_sha256"] for r in op["run_list"])
    assert (
        sorted(r["anchor"]["log_index"] for r in op["run_list"])
        == op["rekor_log_indexes"]
    )
    assert op["first_anchored_at"] <= op["last_anchored_at"]
    assert "linked_identity" not in op  # no authorization passed: pseudonymous

    auth = verify(tmp_path / "auth")
    linked = build_profile(runs, registry, log_keys=[rekor.log], authorizations=[auth])
    [op] = linked["operators"].values()
    assert op["linked_identity"] == [
        {
            "handle": f"github:{USER}",
            "provider": "ssh-github",
            "ssh_key_fingerprint": ssh_sig.fingerprint(person.raw),
            "run_authorization_sha256": auth.sha256,
            "checked_against": "the stored GitHub snapshot",
        }
    ]
    text = json.dumps(linked)
    for private in (
        "Synthia",
        "Example",
        "MRN-0042",
        "synthia-example",
        "c" * 64,
        "rundir-",
    ):
        assert private not in text


def test_identity_needs_a_valid_authorization(tmp_path, rekor):
    person, stranger = Person(), Person()
    home, signer, statement, registry = _setup(tmp_path, person)
    stranger.sign(statement)  # signed by a key GitHub doesn't list for the user
    run = _run(tmp_path, "r3")
    _direct_seal(run, signer, verify(statement).sha256, "r3")
    anchor_seal(
        run / SEAL_FILE, signer, log_keys=[rekor.log], submit=rekor.submit, env={}
    )
    bad = verify(statement)
    assert not bad.ok
    profile = build_profile([run], registry, log_keys=[rekor.log], authorizations=[bad])
    [op] = profile["operators"].values()
    assert "linked_identity" not in op


# --- CLI -------------------------------------------------------------------------


def _cli(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    full = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OPENADAPT_SEAL") and not k.startswith("OPENADAPT_NO_OUT")
    }
    full["PYTHONPATH"] = os.pathsep.join([str(ROOT), full.get("PYTHONPATH", "")])
    full.update(env)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "local-seal",
            "--home",
            str(tmp_path / "home"),
            *args,
        ],
        env=full,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_cli_run_verify_authorize_and_profile(tmp_path, rekor):
    person = Person()
    home, signer, statement, _ = _setup(tmp_path, person)
    person.sign(statement)
    log_pem = tmp_path / "log.pem"
    log_pem.write_bytes(rekor.pem)
    keys = tmp_path / "keys.json"
    keys.write_text(json.dumps({"keys": [signer.registry_entry()]}))
    run = _run(tmp_path, "r3")
    auth_dir = str(tmp_path / "auth")
    sealed = _cli(
        tmp_path, "run", str(run), "--authorization", auth_dir, "--item", "r3"
    )
    assert sealed.returncode == 0, sealed.stderr
    refused = _cli(
        tmp_path,
        "run",
        str(_run(tmp_path, "x")),
        "--authorization",
        auth_dir,
        "--item",
        "r9",
    )
    assert refused.returncode == 1 and "auth_scope_mismatch" in refused.stderr
    _anchor_all(run, tmp_path / "auth", signer, rekor)
    log = ["--log-key", str(log_pem), "--log-origin", "rekor.example"]
    shown = _cli(
        tmp_path,
        "authorize",
        "verify",
        auth_dir,
        "--keys",
        str(keys),
        "--require-anchor",
        *log,
    )
    assert shown.returncode == 0, shown.stderr
    assert f"authorized by github:{USER}" in shown.stdout
    ok = _cli(
        tmp_path,
        "verify",
        str(run),
        "--keys",
        str(keys),
        "--require-anchor",
        "--authorization",
        auth_dir,
        *log,
    )
    assert ok.returncode == 0, ok.stderr
    assert "authorized by github:" in ok.stdout and "r3, r4, r5, r6" in ok.stdout
    out = tmp_path / "operator-profile.json"
    prof = _cli(
        tmp_path,
        "profile",
        str(run),
        "--keys",
        str(keys),
        "--authorization",
        auth_dir,
        "--out",
        str(out),
        *log,
    )
    assert prof.returncode == 0, prof.stderr
    data = json.loads(out.read_text())
    assert data["operators"][signer.key_id]["outcomes"]["verified"] == 1
    assert (
        data["operators"][signer.key_id]["linked_identity"][0]["handle"]
        == f"github:{USER}"
    )
    # --live re-reads github.com, which strict no-outbound mode refuses.
    held = _cli(
        tmp_path, "authorize", "verify", auth_dir, "--live", OPENADAPT_NO_OUTBOUND="1"
    )
    assert held.returncode == 1 and "auth_live_unavailable" in held.stderr
