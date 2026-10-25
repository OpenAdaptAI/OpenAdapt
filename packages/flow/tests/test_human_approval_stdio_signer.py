"""Desktop signs a person's answer over a pipe; Flow never sees the key.

``item-approval approve|decline --approver-signer stdio --approver-public-key
HEX`` writes one sign request line and reads one response line. The process
that started Flow (Desktop, with the key in the macOS Keychain behind Touch
ID) checks what it signs and answers with a signature or a refusal. These
tests play that process.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.approval_mode.human_attestation import (
    ANSWER_DOMAIN,
    SIGN_REQUEST_SCHEMA,
    SIGN_RESPONSE_SCHEMA,
    AttestationRefused,
    StdioSigning,
    stdio_signer,
)
from openadapt_flow.approval_mode.item_approval import ItemApprovalStore
from openadapt_flow.approval_mode.model import ApprovalMode
from openadapt_flow.approval_mode.approver_roster import (
    ROOT_ENV,
    add_approver,
    file_key_signer,
    init_root,
    remove_approver,
)
from openadapt_flow.jit.authority import public_hex
from openadapt_flow.jit.lifecycle import KEY_DIR_ENV

FRAME = b"\x89PNG\r\n\x1a\nmade-up filled form"


def _request(store: ItemApprovalStore):
    return store.open_request(
        run_id="run-0001",
        workflow_name="referral",
        step_id="save",
        step_index=3,
        step_intent="Click 'Save'",
        mode=ApprovalMode.EACH_ITEM,
        bundle_content_digest="a" * 64,
        runtime_inputs_digest="b" * 64,
        frame_png=FRAME,
        form_state=("dom", "sha256:" + "1" * 64),
    )


@pytest.fixture
def held_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The roster's root approver key, held by 'Desktop'; the run pins it."""

    key_dir = tmp_path / "keys"
    monkeypatch.setenv(KEY_DIR_ENV, str(key_dir))
    key = Ed25519PrivateKey.generate()
    init_root(
        key_dir,
        principal="front-desk",
        public_key=public_hex(key),
        sign=file_key_signer(key),
    )
    monkeypatch.setenv(ROOT_ENV, public_hex(key))
    return key_dir, key


def _response(**fields) -> str:
    return json.dumps({"schema": SIGN_RESPONSE_SCHEMA, **fields}) + "\n"


def _args(store, request, public: str, verb: str = "approve") -> list[str]:
    args = [
        "item-approval",
        verb,
        str(store.run_dir),
        "--request-id",
        request.request_id,
        "--request-sha256",
        request.digest(),
        "--decided-by",
        "human",
        "--route",
        "desktop_run_it",
        "--unauthenticated",
        "--approver-signer",
        "stdio",
        "--approver-public-key",
        public,
    ]
    if verb == "decline":
        args += ["--reason", "wrong_values"]
    return args


def _run_cli(args: list[str], answer) -> tuple[int, list[dict]]:
    """Run Flow as Desktop does: read its request, write ``answer(request)``."""

    env = {**os.environ}
    proc = subprocess.Popen(
        [sys.executable, "-m", "openadapt_flow", *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    assert proc.stdout is not None and proc.stdin is not None
    lines: list[dict] = []
    first = json.loads(proc.stdout.readline())
    lines.append(first)
    if first.get("schema") == SIGN_REQUEST_SCHEMA:
        proc.stdin.write(answer(first))
        proc.stdin.flush()
        rest = proc.stdout.read()
        lines += [json.loads(line) for line in rest.splitlines() if line.strip()]
    proc.stdin.close()
    proc.wait(timeout=60)
    return proc.returncode, lines


def test_desktop_signs_over_the_pipe_and_the_answer_counts(tmp_path, held_key):
    key_dir, key = held_key
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    request = _request(store)
    seen: list[bytes] = []

    def sign(sign_request: dict) -> str:
        message = bytes.fromhex(sign_request["message_hex"])
        seen.append(message)
        assert sign_request["public_key"] == public_hex(key)
        return _response(signature="ed25519:" + key.sign(message).hex())

    code, lines = _run_cli(_args(store, request, public_hex(key)), sign)

    assert code == 0, lines
    assert lines[-1]["ok"] is True and lines[-1]["decision"] == "approve"
    # What Desktop signed is the domain, then the answer it can read and check.
    assert seen[0].startswith(ANSWER_DOMAIN)
    payload = json.loads(seen[0][len(ANSWER_DOMAIN) :])
    assert payload["request_id"] == request.request_id
    assert payload["decision"] == "approve"
    answer = store.read_answer(request.request_id)
    assert answer is not None and answer.attestation is not None
    assert answer.attestation.signer == "front-desk"
    assert answer.attestation.method == "desktop_key"


def test_a_cancelled_touch_id_prompt_records_nothing(tmp_path, held_key):
    key_dir, key = held_key
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    request = _request(store)

    code, lines = _run_cli(
        _args(store, request, public_hex(key)),
        lambda _req: _response(
            refused="approver_presence_refused", message="Touch ID was cancelled."
        ),
    )

    assert code == 2
    assert lines[-1] == {
        "ok": False,
        "code": "approver_presence_refused",
        "message": "Touch ID was cancelled.",
    }
    assert store.read_answer(request.request_id) is None


def test_a_signature_by_another_key_is_refused(tmp_path, held_key):
    key_dir, key = held_key
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    request = _request(store)
    other = Ed25519PrivateKey.generate()

    code, lines = _run_cli(
        _args(store, request, public_hex(key)),
        lambda req: _response(
            signature="ed25519:" + other.sign(bytes.fromhex(req["message_hex"])).hex()
        ),
    )

    assert code == 2 and lines[-1]["code"] == "human_answer_signature_invalid"
    assert store.read_answer(request.request_id) is None


def test_an_unregistered_public_key_never_asks_for_a_signature(tmp_path, held_key):
    key_dir, _key = held_key
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    request = _request(store)
    stranger = Ed25519PrivateKey.generate()

    code, lines = _run_cli(
        _args(store, request, public_hex(stranger)),
        lambda _req: pytest.fail("Flow asked an unregistered key to sign"),
    )

    assert code == 2 and lines[-1]["code"] == "human_approver_unregistered"


def test_a_removed_key_no_longer_signs(tmp_path, held_key):
    key_dir, root = held_key
    other = Ed25519PrivateKey.generate()
    add_approver(
        key_dir,
        public_hex(root),
        principal="night-desk",
        public_key=public_hex(other),
        signed_by="front-desk",
        sign=file_key_signer(root),
    )
    assert stdio_signer(public_hex(other), key_dir, method="desktop_key")
    remove_approver(
        key_dir,
        public_hex(root),
        principal="night-desk",
        public_key=public_hex(other),
        signed_by="front-desk",
        sign=file_key_signer(root),
    )
    with pytest.raises(AttestationRefused) as raised:
        stdio_signer(public_hex(other), key_dir, method="desktop_key")
    assert raised.value.code == "human_approver_unregistered"


def test_a_key_the_pinned_root_doesnt_vouch_for_never_signs(
    tmp_path, held_key, monkeypatch
):
    key_dir, root = held_key
    monkeypatch.setenv(ROOT_ENV, public_hex(Ed25519PrivateKey.generate()))

    with pytest.raises(AttestationRefused) as raised:
        stdio_signer(public_hex(root), key_dir, method="desktop_key")
    assert raised.value.code == "approver_root_mismatch"


def test_signer_flags_go_together(tmp_path, held_key, capsys):
    from openadapt_flow.__main__ import main

    key_dir, key = held_key
    store = ItemApprovalStore(tmp_path / "run", key_dir=key_dir)
    request = _request(store)
    args = _args(store, request, public_hex(key))
    without_signer = [a for a in args if a not in {"--approver-signer", "stdio"}]

    assert main(without_signer) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "approver_invalid"


@pytest.mark.parametrize(
    "line, code",
    [
        ("", "approver_signature_unavailable"),
        ("not json\n", "approver_signature_unavailable"),
        (json.dumps({"schema": "other"}) + "\n", "approver_signature_unavailable"),
        (_response(refused="made_up_code"), "approver_signature_unavailable"),
        (_response(refused="approver_key_unusable"), "approver_key_unusable"),
        (_response(signature="ed25519:zz"), "human_answer_signature_invalid"),
    ],
)
def test_anything_but_a_valid_signature_is_a_refusal(line, code):
    key = Ed25519PrivateKey.generate()
    out = io.StringIO()
    signing = StdioSigning(public_hex(key), stdin=io.StringIO(line), stdout=out)

    with pytest.raises(AttestationRefused) as raised:
        signing.sign(b"message")

    assert raised.value.code == code
    request = json.loads(out.getvalue())
    assert request["schema"] == SIGN_REQUEST_SCHEMA
    assert request["message_hex"] == b"message".hex()
