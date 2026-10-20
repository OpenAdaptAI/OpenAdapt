"""``local-seal authorize sign``: show what is being authorized, then sign.

A synthetic bundle (made-up fields, no patient data), a stand-in for
GitHub's ``.keys`` answer, and throwaway SSH keys. Signing tests use the real
``ssh-keygen`` and are skipped where it isn't installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from openadapt_flow import ssh_sig
from openadapt_flow.authorization_summary import (
    SignRefused,
    bundle_digest,
    prepare,
    qualification_line,
    sign,
)
from openadapt_flow.ir import Effect, Postcondition, Step, Workflow
from openadapt_flow.run_authorization import SIGNATURE_FILE, create, verify

HAS_SSH_KEYGEN = shutil.which("ssh-keygen") is not None
QUALIFICATION = """# Qualification result

- Verdict: PASS, certified under `clinical-write`

| Case | Trial | Expected | Observed | Read-back | Model calls | Engine ms |
|---|---|---|---|---|---|---|
| representative | 1 | VERIFIED | VERIFIED | exact_record | 0 | 1 |
| fault-ambiguity | 1 | HALTED | HALTED | no_mutation | 0 | 1 |

## Real-duplicate check

| Trial | Execution | Transaction | Halted at | New charts | Changed charts | Pass |
|---|---|---|---|---|---|---|
| 1 | HALTED | HALTED_BEFORE_EFFECT | step_005 | 0 | 0 | yes |
"""


def _bundle(root: Path) -> Path:
    bundle = root / "qual" / "bundle"
    bundle.mkdir(parents=True)
    steps = [
        Step(
            id="step_000",
            intent="click 'New Patient'",
            action="click",
            identity_armed=True,
        ),
        Step(
            id="step_001",
            intent="type <fname>",
            action="type",
            param="fname",
            field_label="First Name",
        ),
        Step(
            id="step_002",
            intent="click 'Last Name'",
            action="click",
            identity_armed=True,
        ),
        Step(
            id="step_003",
            intent="type <lname>",
            action="type",
            param="lname",
            field_label="form_lname",
        ),
        Step(
            id="step_005",
            intent="click 'Create New Patient'",
            action="click",
            identity_armed=True,
            expect=[Postcondition(kind="text_present", text="No matches were found.")],
        ),
        Step(
            id="step_006",
            intent="click 'Confirm Create New Patient'",
            action="click",
            identity_armed=True,
            risk="irreversible",
            effects=[Effect(kind="record_written"), Effect(kind="field_equals")],
        ),
    ]
    wf = Workflow(name="clinic-intake-referral", steps=steps)
    (bundle / "workflow.json").write_text(wf.model_dump_json(), encoding="utf-8")
    (bundle.parent / "QUALIFICATION.md").write_text(QUALIFICATION, encoding="utf-8")
    return bundle


def _ssh_key(path: Path) -> bytes:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "t", "-f", str(path)],
        check=True,
    )
    return ssh_sig.parse_key_line(path.with_suffix(".pub").read_text())


def _write_pub_only(path: Path) -> bytes:
    """A public key file without its private half: enough to render the summary."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw = (
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    )
    Path(str(path) + ".pub").write_text(ssh_sig.key_line(raw) + "\n")
    return raw


def _statement(tmp_path: Path, bundle: Path, github_raw: bytes) -> Path:
    return create(
        tmp_path / "auth",
        bundle_content_digest=bundle_digest(bundle),
        items=["r3", "r4"],
        profile="standard",
        authorizer="github:synthetic-operator",
        seal_key_ids=["seal-ed25519-0123456789abcdef"],
        fetch_keys=lambda user: [ssh_sig.key_line(github_raw)],
    )


def test_summary_shows_who_what_and_what_is_not_allowed(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    summary, on_github, _ = prepare(statement, key, [bundle])
    assert on_github
    lines = summary.splitlines()
    assert lines[0] == "You are authorizing OpenAdapt to run this program"
    assert "github:synthetic-operator" in summary
    assert ssh_sig.fingerprint(raw) in summary
    assert "✓ listed on github.com/synthetic-operator.keys" in summary
    assert "clinic-intake-referral" in summary
    assert "App          clinic" in summary  # no recorded app: the program's first word
    assert f"{bundle_digest(bundle)[:12]}" in summary and "(6 steps)" in summary
    assert (
        "certified clinical-write · 2/2 cases · real-duplicate case halts (1/1)"
        in summary
    )
    assert "First Name" in summary and "lname" in summary  # form_ labels show the field
    assert (
        "▶ writes: Confirm Create New Patient (step_006) · checked by 2 record read-backs"
        in summary
    )
    assert (
        "stops if: 'No matches were found.' absent · identity unreadable · record not confirmed"
        in summary
    )
    assert "Items        r3, r4" in summary and "Profile      standard" in summary
    assert "seal-ed25519-0123456789abcdef" in summary
    assert "Not allowed  any other bundle, item, profile or Seal key" in summary
    import hashlib

    assert hashlib.sha256(statement.read_bytes()).hexdigest() in summary
    assert str(statement.resolve()) in summary
    assert len(lines) <= 40
    # Plain text only: no terminal escape codes.
    assert "\x1b" not in summary


def test_unknown_bundle_digest_is_refused_before_any_prompt(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    text = (bundle / "workflow.json").read_text()
    (bundle / "workflow.json").write_text(
        text.replace("clinic-intake-referral", "edited")
    )
    with pytest.raises(SignRefused) as exc:
        prepare(statement, key, [bundle])
    assert exc.value.code == "auth_bundle_unknown"
    asked: list[str] = []
    out: list[str] = []
    code = sign(statement, key, [bundle], ask=asked.append, say=out.append)
    assert code == 1 and asked == [] and "auth_bundle_unknown" in out[0]
    assert not (tmp_path / "auth" / SIGNATURE_FILE).exists()


def test_dry_run_and_no_sign_nothing(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    ran: list[list[str]] = []

    def run(cmd, **kwargs):
        ran.append(cmd)
        raise AssertionError("ssh-keygen must not run")

    out: list[str] = []
    assert sign(statement, key, [bundle], dry_run=True, say=out.append, run=run) == 0
    assert "dry run" in out[-1]
    for answer in ("", "n", "no", "N", "maybe"):
        out.clear()
        code = sign(
            statement, key, [bundle], ask=lambda q: answer, say=out.append, run=run
        )
        assert code == 1 and out[-1] == "not signed"
    assert ran == [] and not (tmp_path / "auth" / SIGNATURE_FILE).exists()


@pytest.mark.skipif(not HAS_SSH_KEYGEN, reason="needs ssh-keygen")
def test_yes_signs_with_ssh_keygen_and_verifies(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _ssh_key(key)
    statement = _statement(tmp_path, bundle, raw)
    prompts: list[str] = []
    out: list[str] = []

    def ask(question: str) -> str:
        prompts.append(question)
        return "y"

    assert sign(statement, key, [bundle], ask=ask, say=out.append) == 0
    assert prompts == ["\nSign this authorization? [y/N] "]
    assert verify(statement).ok
    assert out[-1].startswith("signed:") and "verifies" in out[-1]
    # Never re-signs an authorization that already has a signature.
    assert sign(statement, key, [bundle], ask=ask, say=out.append) == 1
    assert "auth_already_signed" in out[-1]


def test_a_key_other_than_the_named_one_is_refused_before_any_prompt(tmp_path):
    bundle = _bundle(tmp_path)
    named = _write_pub_only(
        tmp_path / "named"
    )  # the key GitHub lists and the statement names
    statement = _statement(tmp_path, bundle, named)
    other = tmp_path / "other"
    _write_pub_only(other)
    asked: list[str] = []
    out: list[str] = []
    assert sign(statement, other, [bundle], ask=asked.append, say=out.append) == 1
    assert asked == [] and "auth_signer_mismatch" in out[-1]


@pytest.mark.skipif(not HAS_SSH_KEYGEN, reason="needs ssh-keygen")
def test_key_missing_from_the_stored_copy_warns_and_needs_yes_typed(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _ssh_key(key)
    statement = _statement(tmp_path, bundle, raw)
    # The copy of the GitHub keys next to the statement no longer lists it.
    signers = tmp_path / "auth" / "run-authorization.allowed_signers"
    signers.write_text(
        "# copy\n"
        + ssh_sig.allowed_signers_line(
            "github:synthetic-operator",
            "openadapt-run-authorization",
            _write_pub_only(tmp_path / "x"),
        )
        + "\n"
    )
    summary, on_github, _ = prepare(statement, key, [bundle])
    assert (
        not on_github
        and "✗ NOT listed on github.com/synthetic-operator.keys" in summary
    )
    out: list[str] = []
    prompts: list[str] = []

    def ask(answer: str):
        def inner(question: str) -> str:
            prompts.append(question)
            return answer

        return inner

    assert sign(statement, key, [bundle], ask=ask("y"), say=out.append) == 1
    assert prompts[-1] == 'Type "yes" to sign anyway: '
    assert any("WARNING" in line for line in out)
    assert not (tmp_path / "auth" / SIGNATURE_FILE).exists()
    assert sign(statement, key, [bundle], ask=ask("yes"), say=out.append) == 0
    assert verify(statement).ok


@pytest.mark.skipif(not HAS_SSH_KEYGEN, reason="needs ssh-keygen")
def test_an_edit_during_the_prompt_is_never_signed(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _ssh_key(key)
    statement = _statement(tmp_path, bundle, raw)

    def ask(question: str) -> str:
        # Someone swaps the scope while the person reads the summary.
        statement.write_text(statement.read_text().replace('"r4"', '"r9"'))
        return "y"

    out: list[str] = []
    assert sign(statement, key, [bundle], ask=ask, say=out.append) == 1
    assert "changed while you were reviewing" in out[-1]
    assert not (tmp_path / "auth" / SIGNATURE_FILE).exists()


def test_ssh_keygen_gets_a_private_copy_after_double_dash(tmp_path):
    import stat

    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    shown = statement.read_bytes()
    seen: dict = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        copy = Path(cmd[-1])
        seen["mode"] = stat.S_IMODE(copy.stat().st_mode)
        seen["bytes"] = copy.read_bytes()
        copy.with_name(copy.name + ".sig").write_text("not a signature")

        class Done:
            returncode = 0

        return Done()

    out: list[str] = []
    code = sign(statement, key, [bundle], ask=lambda q: "y", say=out.append, run=run)
    assert seen["cmd"][-2] == "--" and seen["cmd"][-1] != str(statement)
    assert seen["mode"] == 0o600 and seen["bytes"] == shown
    # A signature that does not verify makes sign fail.
    assert code == 1 and "does not verify" in out[-1]


def test_control_characters_never_reach_the_terminal(tmp_path):
    bundle = _bundle(tmp_path)
    wf = Workflow.model_validate_json((bundle / "workflow.json").read_text())
    wf.steps[1].field_label = "First\x1b[2J\x1b]0;owned\x07Name\u202e"
    (bundle / "workflow.json").write_text(wf.model_dump_json())
    (bundle.parent / "QUALIFICATION.md").write_text(
        QUALIFICATION.replace("clinical-write", "clinical\x1b[31m-write")
    )
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    summary, _, _ = prepare(statement, key, [bundle])
    assert "\x1b" not in summary and "\x07" not in summary and "\u202e" not in summary
    assert "FirstName" in summary


def test_qualification_beside_the_bundle_is_marked_unbound(tmp_path):
    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    summary, _, _ = prepare(statement, key, [bundle])
    assert "not covered by its digest" in summary
    assert "Each item    one run only" in summary


def test_qualification_line_without_a_report(tmp_path):
    bundle = tmp_path / "b"
    bundle.mkdir()
    assert qualification_line(bundle) == "no qualification report next to the bundle"


def test_cli_dry_run_prints_the_summary(tmp_path):
    import os
    import sys

    bundle = _bundle(tmp_path)
    key = tmp_path / "id_ed25519"
    raw = _write_pub_only(key)
    statement = _statement(tmp_path, bundle, raw)
    root = Path(__file__).resolve().parents[1]
    env = dict(
        os.environ,
        PYTHONPATH=os.pathsep.join([str(root), os.environ.get("PYTHONPATH", "")]),
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "local-seal",
            "authorize",
            "sign",
            str(statement),
            "--key",
            str(key),
            "--bundle",
            str(bundle),
            "--dry-run",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("You are authorizing OpenAdapt to run this program")
    assert "(dry run: nothing signed)" in proc.stdout
    assert not (tmp_path / "auth" / SIGNATURE_FILE).exists()
