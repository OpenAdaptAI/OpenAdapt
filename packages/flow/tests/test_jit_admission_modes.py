"""The three ways a JIT draft is admitted, and what each refuses.

* an admitter key a person registered (CI);
* a registered agent under an admission policy a person signed;
* a person at a terminal (tests/test_jit_lifecycle.py).

All three write the same signed admission.json. Synthetic bundle, stub canary
runs, real keys and signatures. No browser and no model.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.jit import AdmissionError, BundleState, authority, lifecycle
from openadapt_flow.jit.authority import (
    AdmissionPolicy,
    AuthorityError,
    Principal,
    load_encrypted_key,
    principal_from_key,
    sign_policy,
)
from openadapt_flow.jit.lifecycle import DraftNotAdmitted, ensure_runnable
from tests.test_jit_lifecycle import (  # noqa: F401  (autouse fixture)
    REVIEWER,
    _cli,
    _jit,
    _policy_passes,
    _report,
    _stub_runs,
)

SIGNER = "dana.signer"
CI = "ci:release"
AGENT = "agent:admitter"
RECORDER = "cua-bot-7"  # the draft's agent_principal_id (see _jit)
NOW = datetime.now(timezone.utc)


def _iso(when: datetime) -> str:
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv(authority.KEY_PASSPHRASE_ENV, "test passphrase")
    files = tmp_path / "keyfiles"
    key_dir = tmp_path / "keys"  # the key_dir _jit uses
    out: dict[str, Any] = {"dir": key_dir, "files": files}
    for role, pid, name in (
        ("policy_signer", SIGNER, "signer"),
        ("admitter_key", CI, "ci"),
        ("agent", AGENT, "agent"),
        ("agent", RECORDER, "recorder"),
    ):
        pub = authority.generate_encrypted_key(files / f"{name}.pem")
        authority.register_principal(
            key_dir, role=role, principal_id=pid, public_key_hex=pub
        )
        out[name] = load_encrypted_key(files / f"{name}.pem")
    return out


def _policy(**changes: Any) -> AdmissionPolicy:
    fields: dict[str, Any] = {
        "policy_id": "notes-v1",
        "signer": SIGNER,
        "signed_at": _iso(NOW - timedelta(hours=1)),
        "expires_at": _iso(NOW + timedelta(days=30)),
        "allowed_hosts": ("127.0.0.1:9",),
        "allowed_actions": ("type", "click"),
        "allowed_effects": ("record_written",),
        "min_clean_canaries": 2,
        "agent_principals": (AGENT, RECORDER),
    }
    fields.update(changes)
    return AdmissionPolicy(**fields)


def _agent(keys: dict[str, Any], policy: AdmissionPolicy | None = None, **kw: Any):
    signed = sign_policy(policy or _policy(), keys["signer"])
    return principal_from_key(keys["agent"], keys["dir"], policy=signed, **kw)


# -- (b) an admitter key -----------------------------------------------------------


def test_an_admitter_key_admits_without_a_person(
    tmp_path: Path, monkeypatch, keys, capsys
) -> None:
    jit = _jit(tmp_path)
    ci = principal_from_key(keys["ci"], keys["dir"])
    assert (ci.principal_id, ci.kind) == (CI, "admitter_key")
    calls = _stub_runs(jit, monkeypatch, _report())
    # No review needed: the person who registered the key stands behind it.
    jit.canary(principal_id=ci)
    assert calls[0][0] is ci
    # From a script: no terminal, no --principal, no --yes.
    args = ("admit", str(jit.task_dir), "--key", str(keys["files"] / "ci.pem"))
    assert _cli(*args, "--key-dir", str(keys["dir"]), tty=False) == 0
    assert f"ADMITTED by {CI} (admitter_key)" in capsys.readouterr().out
    manifest = lifecycle.verify_admission(jit.bundle_dir, key_dir=keys["dir"])
    assert manifest["admission_mode"] == "admitter_key"
    assert manifest["admitted_by_principal_id"] == CI
    assert manifest["reviewed_by_principal_id"] is None
    assert jit.state() is BundleState.ADMITTED


def test_an_admitter_key_still_needs_a_passing_canary(
    tmp_path: Path, monkeypatch, keys
) -> None:
    jit = _jit(tmp_path)
    ci = principal_from_key(keys["ci"], keys["dir"])
    with pytest.raises(AdmissionError, match="no passing canary"):
        jit.admit_with_key(ci)
    _stub_runs(jit, monkeypatch, _report(outcome="HALTED"))
    jit.canary(principal_id=ci)
    with pytest.raises(AdmissionError, match="no passing canary"):
        jit.admit_with_key(ci)


def test_an_admitter_key_is_encrypted_and_registered(
    tmp_path: Path, monkeypatch, keys
) -> None:
    plain = tmp_path / "plain.pem"
    plain.write_bytes(
        Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(AuthorityError, match="not encrypted"):
        load_encrypted_key(plain)
    with pytest.raises(AuthorityError, match="passphrase"):
        load_encrypted_key(keys["files"] / "ci.pem", env={})
    with pytest.raises(AuthorityError, match="not registered"):
        principal_from_key(Ed25519PrivateKey.generate(), keys["dir"])
    # A policy signer's key signs policy; it does not admit.
    with pytest.raises(AuthorityError, match="signs admission policy"):
        principal_from_key(keys["signer"], keys["dir"])
    # A name alone, however official it sounds, is not a key.
    jit = _jit(tmp_path)
    with pytest.raises(AdmissionError, match="registered Principal"):
        lifecycle.admit_with_key(jit.program_dir, principal=CI, key_dir=keys["dir"])


# -- (c) an agent under a signed policy ---------------------------------------------


def test_an_agent_under_policy_runs_canaries_and_admits(
    tmp_path: Path, monkeypatch, keys
) -> None:
    jit = _jit(tmp_path)
    agent = _agent(keys)
    # The agent can attend a draft's run: the Replayer check passes it.
    ensure_runnable(jit.bundle_dir, attended_by=agent, key_dir=keys["dir"])
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=agent)
    jit.canary(principal_id=agent)
    decision = jit.admit_with_key(agent)
    assert decision["outcome"] == "admitted"
    manifest = lifecycle.verify_admission(jit.bundle_dir, key_dir=keys["dir"])
    assert manifest["admission_mode"] == "agent_policy"
    assert manifest["admitted_by_principal_id"] == AGENT
    assert manifest["policy_signer"] == SIGNER
    assert manifest["policy_digest"] == _policy().digest()
    assert manifest["clean_canaries"] == 2
    # Still an independent read-back on every write step (tier 1).
    assert all(row["ok"] for row in manifest["readback"])
    # Revoking the agent's key in the registry revokes what it admitted.
    registry = authority.PrincipalRegistry.load(keys["dir"])
    data = registry.model_dump(by_alias=True)
    data["agents"].pop(AGENT)
    authority.PrincipalRegistry.model_validate(data).save(keys["dir"])
    with pytest.raises(AdmissionError, match="not registered"):
        lifecycle.verify_admission(jit.bundle_dir, key_dir=keys["dir"])


def test_too_few_clean_canaries_are_refused(tmp_path: Path, monkeypatch, keys) -> None:
    jit = _jit(tmp_path)
    agent = _agent(keys)
    with pytest.raises(AdmissionError, match="0 clean canaries"):
        jit.admit_with_key(agent)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=agent)
    with pytest.raises(AdmissionError, match="1 clean canaries .* needs 2"):
        jit.admit_with_key(agent)
    assert jit.state() is BundleState.DRAFT


def test_a_failed_canary_sends_the_draft_to_a_person(
    tmp_path: Path, monkeypatch, keys
) -> None:
    jit = _jit(tmp_path)
    agent = _agent(keys)
    _stub_runs(jit, monkeypatch, _report(model_calls=1))
    jit.canary(principal_id=agent)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=agent)
    jit.canary(principal_id=agent)
    decision = jit.admit_with_key(agent)
    assert decision["outcome"] == "needs_person"
    assert "ended VERIFIED" in decision["reasons"][0]  # it called a model
    assert jit.state() is BundleState.DRAFT


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"allowed_actions": ("click",)}, "action 'type' is not allowed"),
        ({"allowed_effects": ("field_equals",)}, "effect 'record_written'"),
        ({"flow_policies": ("read-only",)}, "flow policy 'clinical-write'"),
    ],
)
def test_a_draft_outside_the_policy_goes_to_the_needs_person_queue(
    tmp_path: Path, monkeypatch, keys, capsys, change, reason
) -> None:
    jit = _jit(tmp_path)
    _stub_runs(jit, monkeypatch, _report())
    wide = _agent(keys)
    jit.canary(principal_id=wide)
    jit.canary(principal_id=wide)
    narrow = _agent(keys, _policy(policy_id="narrow", **change))
    decision = jit.admit_with_key(narrow)
    assert decision["outcome"] == "needs_person"
    assert any(reason in r for r in decision["reasons"])
    assert jit.state() is BundleState.DRAFT
    queue = lifecycle.needs_person_queue(jit.cache_dir)
    assert [q["policy_id"] for q in queue] == ["narrow"]
    # jit review lists it, from a script.
    assert _cli("review", "--queue", str(jit.cache_dir), tty=False) == 0
    assert reason in capsys.readouterr().out
    # A person can still admit it the ordinary way.
    jit.review(principal_id=REVIEWER, reviewed_steps=["step_000", "step_001"])
    jit.canary(principal_id=REVIEWER)
    jit.admit(principal_id=REVIEWER, confirm="ok")
    assert lifecycle.needs_person_queue(jit.cache_dir) == []


def test_an_agent_canary_on_an_app_outside_the_policy_does_not_run(
    tmp_path: Path, monkeypatch, keys
) -> None:
    jit = _jit(tmp_path)
    calls = _stub_runs(jit, monkeypatch, _report())
    agent = _agent(keys, _policy(allowed_hosts=("ehr.example.org",)))
    with pytest.raises(AdmissionError, match="queued for a person"):
        jit.canary(principal_id=agent)
    assert calls == []
    assert lifecycle.needs_person_queue(jit.cache_dir)[0]["reasons"]


def test_an_expired_policy_is_refused(tmp_path: Path, monkeypatch, keys) -> None:
    expired = _policy(
        signed_at=_iso(NOW - timedelta(days=10)),
        expires_at=_iso(NOW - timedelta(days=1)),
    )
    with pytest.raises(AuthorityError, match="expired"):
        _agent(keys, expired)
    # Valid when the agent started, expired when it admits: admission
    # re-verifies the expiry itself.
    jit = _jit(tmp_path)
    agent = _agent(keys)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=agent)
    jit.canary(principal_id=agent)
    later = NOW + timedelta(days=31)
    with pytest.raises(AdmissionError, match="expired"):
        lifecycle.admit_with_key(
            jit.program_dir, principal=agent, key_dir=keys["dir"], now=later
        )


def test_an_unsigned_or_tampered_policy_is_refused(tmp_path: Path, keys) -> None:
    signed = sign_policy(_policy(), keys["signer"])
    # Edited after signing: wider hosts.
    tampered = signed.model_copy(
        update={"policy": _policy(allowed_hosts=("127.0.0.1:9", "evil.example"))}
    )
    with pytest.raises(AuthorityError, match="does not verify"):
        principal_from_key(keys["agent"], keys["dir"], policy=tampered)
    # Signed by the agent itself, or by any key not registered to the signer.
    self_signed = sign_policy(_policy(), keys["agent"])
    with pytest.raises(AuthorityError, match="another key"):
        principal_from_key(keys["agent"], keys["dir"], policy=self_signed)
    # Naming a signer who is not registered.
    stranger = sign_policy(_policy(signer="someone.else"), keys["signer"])
    with pytest.raises(AuthorityError, match="not a registered policy signer"):
        principal_from_key(keys["agent"], keys["dir"], policy=stranger)
    # A registered agent with no policy at all.
    with pytest.raises(AuthorityError, match="only under a signed admission"):
        principal_from_key(keys["agent"], keys["dir"])
    # A policy that does not name this agent.
    other = sign_policy(_policy(agent_principals=("agent:other",)), keys["signer"])
    with pytest.raises(AuthorityError, match="does not authorize"):
        principal_from_key(keys["agent"], keys["dir"], policy=other)
    # Only a person signs policy.
    with pytest.raises(AuthorityError, match="names an agent"):
        authority.register_principal(
            keys["dir"],
            role="policy_signer",
            principal_id="agent:signer",
            public_key_hex="ab" * 32,
        )


def test_the_recording_agent_cannot_admit_its_own_draft(
    tmp_path: Path, monkeypatch, keys
) -> None:
    jit = _jit(tmp_path)
    signed = sign_policy(_policy(), keys["signer"])
    recorder = principal_from_key(keys["recorder"], keys["dir"], policy=signed)
    assert recorder.principal_id == RECORDER  # registered and in the policy
    with pytest.raises(AdmissionError, match="made the recording"):
        jit.canary(principal_id=recorder)
    agent = _agent(keys)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=agent)
    jit.canary(principal_id=agent)
    with pytest.raises(AdmissionError, match="made the recording"):
        jit.admit_with_key(recorder)
    with pytest.raises(DraftNotAdmitted, match="made the recording"):
        ensure_runnable(jit.bundle_dir, attended_by=recorder, key_dir=keys["dir"])


@pytest.mark.parametrize("who", [AGENT, "agent:x", "model:gpt", "bot", "", " "])
def test_names_alone_still_cannot_attend_a_draft(
    tmp_path: Path, keys, who: str
) -> None:
    jit = _jit(tmp_path)
    with pytest.raises(DraftNotAdmitted):
        ensure_runnable(jit.bundle_dir, attended_by=who, key_dir=keys["dir"])


def test_a_hand_built_principal_gives_no_authority(tmp_path: Path, keys) -> None:
    jit = _jit(tmp_path)
    real = _agent(keys)
    stolen_id = Principal(AGENT, "agent", real.public_key, real.policy)  # no key
    with pytest.raises(DraftNotAdmitted, match="does not hold"):
        ensure_runnable(jit.bundle_dir, attended_by=stolen_id, key_dir=keys["dir"])
    wrong_key = Principal(
        AGENT, "agent", real.public_key, real.policy, Ed25519PrivateKey.generate()
    )
    with pytest.raises(DraftNotAdmitted, match="does not hold"):
        ensure_runnable(jit.bundle_dir, attended_by=wrong_key, key_dir=keys["dir"])
    as_ci = Principal(CI, "admitter_key", real.public_key, None, keys["agent"])
    with pytest.raises(DraftNotAdmitted, match="not registered"):
        ensure_runnable(jit.bundle_dir, attended_by=as_ci, key_dir=keys["dir"])


def test_the_cli_agent_path_reports_needs_person(
    tmp_path: Path, monkeypatch, keys, capsys
) -> None:
    jit = _jit(tmp_path)
    policy_file = tmp_path / "policy.json"
    sign_policy(_policy(allowed_actions=("click",)), keys["signer"]).save(policy_file)
    _stub_runs(jit, monkeypatch, _report())
    wide = _agent(keys)
    jit.canary(principal_id=wide)
    jit.canary(principal_id=wide)
    args = (
        "admit",
        str(jit.task_dir),
        "--key",
        str(keys["files"] / "agent.pem"),
        "--policy",
        str(policy_file),
        "--key-dir",
        str(keys["dir"]),
    )
    assert _cli(*args, tty=False) == 3
    assert "NEEDS PERSON" in capsys.readouterr().out
    # A person's admission still needs a terminal; there is no --yes.
    person = ("admit", str(jit.task_dir), "--principal", REVIEWER)
    assert _cli(*person, tty=False) == 2
    assert "jit admit --key" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _cli("admit", str(jit.task_dir), "--yes")
    assert json.loads(json.dumps(lifecycle.needs_person_queue(jit.cache_dir)))


def test_canary_evidence_written_by_anything_else_does_not_count(
    tmp_path: Path, monkeypatch, keys
) -> None:
    jit = _jit(tmp_path)
    agent = _agent(keys)
    _stub_runs(jit, monkeypatch, _report())
    jit.canary(principal_id=agent)
    # The agent can write the cache directory: it appends a "clean" canary.
    runs = jit.program_dir / lifecycle.CANARIES_FILE
    real = json.loads(runs.read_text().splitlines()[0])
    forged = {**real, "ran_at": "2026-10-17T00:00:00Z"}
    with runs.open("a") as f:
        f.write(json.dumps(forged) + "\n")
    with pytest.raises(AdmissionError, match="1 clean canaries"):
        jit.admit_with_key(agent)
    # And a hand-written canary.json is refused in every mode.
    canary = jit.program_dir / lifecycle.CANARY_FILE
    canary.write_text(json.dumps({**real, "signature": "ed25519:00"}))
    ci = principal_from_key(keys["ci"], keys["dir"])
    with pytest.raises(AdmissionError, match="not signed by the local admission"):
        jit.admit_with_key(ci)
