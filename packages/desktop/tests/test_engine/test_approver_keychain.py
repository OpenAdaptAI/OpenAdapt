"""This Mac's approver key behind Touch ID, with a fake Secure Enclave.

The fake keeps the unsealed key inside itself, as the Secure Enclave keeps
its own key, and answers each presence prompt the way the test says: confirm,
cancel or fail. The real Secure Enclave and Keychain are exercised by
tests/test_approver_keychain_real.py.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from openadapt_desktop.approver_keychain import (
    ACCOUNT,
    ANSWER_DOMAIN,
    ApproverKeychain,
    ApproverKeychainError,
)
from openadapt_desktop.auth.store import SERVICE_NAME
from openadapt_desktop.flow_bridge import FlowBridge

REQUEST_ID = "0" * 31 + "1"
REQUEST_SHA = "sha256:" + "a" * 64


class FakeEnclave:
    def __init__(self, *, available: bool = True) -> None:
        self._available = available
        self._keys: dict[bytes, bool] = {}  # handle -> requires presence
        self._sealed: dict[bytes, tuple[bytes, bytes]] = {}  # sealed -> (handle, plain)
        self.prompts: list[str] = []
        self.answer = "confirm"  # or "cancel", "error"

    def available(self) -> bool:
        return self._available

    def create(self, *, require_presence: bool) -> tuple[bytes, bytes]:
        handle = secrets.token_bytes(16)
        self._keys[handle] = require_presence
        return handle, b"\x04public:" + handle

    def seal(self, public_key: bytes, plaintext: bytes) -> bytes:
        handle = public_key.removeprefix(b"\x04public:")
        sealed = secrets.token_bytes(24)
        self._sealed[sealed] = (handle, bytes(plaintext))
        return sealed

    def open(self, handle, sealed, *, reason, interactive=True) -> bytearray:
        if self._keys.get(handle):
            self.prompts.append(reason)
            if not interactive or self.answer == "cancel":
                raise ApproverKeychainError(
                    "approver_presence_refused",
                    "Touch ID or your password wasn't confirmed, so nothing was approved.",
                )
        if self.answer == "error":
            raise ApproverKeychainError(
                "approver_key_unusable", "The Secure Enclave refused (error -25300)."
            )
        owner, plain = self._sealed[sealed]
        if owner != handle:
            raise ApproverKeychainError("approver_key_unusable", "wrong key")
        return bytearray(plain)


class FakeRegistry:
    """Flow's signed approver roster: a self-signed root, then changes signed
    by an approver already on it. Every signature is checked."""

    def __init__(self) -> None:
        self.approvers: dict[str, str] = {}
        self.root_key: str | None = None
        self.fail = False
        self.pin: str | None = None

    @staticmethod
    def _check(public: str, message: bytes, signature: str) -> None:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public)).verify(
            bytes.fromhex(signature.removeprefix("ed25519:")), message
        )

    def root(self) -> str | None:
        return self.pin

    def is_empty(self) -> bool:
        return self.root_key is None

    def lookup(self, public: str) -> str | None:
        if self.pin is None or self.pin != self.root_key:
            return None
        return next((p for p, v in self.approvers.items() if v == public), None)

    def init_root(self, principal, public, sign) -> None:
        if self.fail:
            raise ApproverKeychainError("approver_key_unusable", "Flow is too old")
        message = b"root:" + principal.encode() + public.encode()
        self._check(public, message, sign(message))
        self.root_key = public
        self.pin = public
        self.approvers = {principal: public}

    def add(self, principal, public, signed_by, sign) -> None:
        if self.fail:
            raise ApproverKeychainError("approver_key_unusable", "Flow refused")
        signer = self.approvers[signed_by]
        message = b"add:" + principal.encode() + public.encode()
        self._check(signer, message, sign(message))
        self.approvers[principal] = public

    def remove(self, principal, public, signed_by, sign) -> None:
        signer = self.approvers[signed_by]
        message = b"remove:" + principal.encode() + public.encode()
        self._check(signer, message, sign(message))
        if public == self.root_key:
            raise ApproverKeychainError("approver_key_unusable", "the root stays")
        del self.approvers[principal]


@pytest.fixture
def enclave() -> FakeEnclave:
    return FakeEnclave()


@pytest.fixture
def registry() -> FakeRegistry:
    return FakeRegistry()


@pytest.fixture
def keychain(enclave, registry) -> ApproverKeychain:
    return ApproverKeychain(
        enclave,
        roster=registry,
    )


def _verify(public_hex: str, message: bytes, signature: str) -> None:
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex)).verify(
        bytes.fromhex(signature.removeprefix("ed25519:")), message
    )


# -- setup -------------------------------------------------------------------------


def test_setup_is_one_prompt_and_registers_the_public_key(
    keychain, enclave, registry, fake_keyring
):
    stored = keychain.setup()

    assert enclave.prompts == ["set up your approver key"]
    assert registry.approvers == {"desktop-approver": stored.public_key}
    assert keychain.status() == {
        "supported": True,
        "set_up": True,
        "stored": True,
        "registered": True,
        "principal": "desktop-approver",
        "fingerprint": stored.public_key[:16],
    }
    item = json.loads(fake_keyring.get_password(SERVICE_NAME, ACCOUNT))
    assert item["presence"] is True
    # Only sealed material is stored: nothing in the item opens without the
    # (fake) Secure Enclave.
    assert set(item) == {
        "schema",
        "public_key",
        "principal",
        "enclave_key",
        "sealed_key",
        "presence",
        "created_at",
    }
    (plain,) = [plain for _handle, plain in enclave._sealed.values()]
    assert plain.hex() not in json.dumps(item)


def test_setup_again_returns_the_same_key_without_a_prompt(keychain, enclave):
    first = keychain.setup()
    enclave.prompts.clear()

    assert keychain.setup().public_key == first.public_key
    assert enclave.prompts == []


def test_a_cancelled_setup_prompt_stores_and_registers_nothing(
    keychain, enclave, registry, fake_keyring
):
    enclave.answer = "cancel"

    with pytest.raises(ApproverKeychainError) as raised:
        keychain.setup()

    assert raised.value.code == "approver_presence_refused"
    assert fake_keyring.get_password(SERVICE_NAME, ACCOUNT) is None
    assert registry.approvers == {}
    assert keychain.status()["set_up"] is False


def test_a_keychain_that_wont_save_registers_nothing(keychain, registry, fake_keyring):
    def refuse(*_args):
        raise RuntimeError("keychain locked")

    fake_keyring.set_password = refuse  # type: ignore[method-assign]

    with pytest.raises(ApproverKeychainError) as raised:
        keychain.setup()

    assert raised.value.code == "approver_key_unusable"
    assert registry.approvers == {}


def test_a_failed_registration_leaves_no_key_behind(keychain, registry, fake_keyring):
    registry.fail = True

    with pytest.raises(ApproverKeychainError):
        keychain.setup()

    assert fake_keyring.get_password(SERVICE_NAME, ACCOUNT) is None


def test_a_mac_without_a_secure_enclave_is_sent_to_the_terminal(registry):
    keychain = ApproverKeychain(FakeEnclave(available=False), roster=registry)

    with pytest.raises(ApproverKeychainError) as raised:
        keychain.setup()

    assert raised.value.code == "approver_keychain_unsupported"
    assert keychain.status()["supported"] is False


def _wipe_roster(registry):
    registry.approvers.clear()
    registry.root_key = None
    registry.pin = None


def test_a_stored_key_off_the_roster_starts_it_again_only_behind_a_prompt(
    keychain, enclave, registry
):
    stored = keychain.setup()
    _wipe_roster(registry)  # the roster file was removed
    enclave.prompts.clear()

    assert keychain.status()["set_up"] is False
    keychain.setup()
    assert registry.approvers == {"desktop-approver": stored.public_key}
    assert registry.pin == stored.public_key
    # The root signature is made behind the one prompt: that is the proof.
    assert enclave.prompts == ["set up your approver key"]


def test_a_planted_keychain_item_is_never_registered_without_presence(
    keychain, enclave, registry, fake_keyring
):
    """Something that can write the Keychain item plants its own key."""

    from openadapt_desktop.approver_keychain import StoredKey

    keychain.setup()
    _wipe_roster(registry)
    planted = StoredKey.from_json(fake_keyring.get_password(SERVICE_NAME, ACCOUNT))
    # The planted item claims Touch ID but the person doesn't confirm.
    enclave.answer = "cancel"

    with pytest.raises(ApproverKeychainError) as raised:
        keychain.setup()
    assert raised.value.code == "approver_presence_refused"
    assert registry.approvers == {} and registry.pin is None

    # An item that admits it has no presence check is refused outright.
    fake_keyring.set_password(
        SERVICE_NAME,
        ACCOUNT,
        StoredKey(**{**planted.__dict__, "presence": False}).to_json(),
    )
    enclave.answer = "confirm"
    with pytest.raises(ApproverKeychainError) as raised:
        keychain.setup()
    assert raised.value.code == "approver_key_unusable"
    assert registry.approvers == {}


def test_a_roster_this_mac_didnt_start_needs_an_approver_to_add_the_key(
    keychain, enclave, registry, fake_keyring
):
    """Desktop never adopts the root of a roster it finds."""

    import secrets as _secrets

    other = _secrets.token_hex(32)
    registry.root_key = other
    registry.pin = other
    registry.approvers = {"front-desk": other}

    with pytest.raises(ApproverKeychainError) as raised:
        keychain.setup()

    assert raised.value.code == "approver_needs_adding"
    assert "openadapt-flow jit approver add" in str(raised.value)
    assert set(registry.approvers) == {"front-desk"}
    # The key is kept, so the setup finishes once an approver adds it.
    assert fake_keyring.get_password(SERVICE_NAME, ACCOUNT) is not None
    assert keychain.status()["set_up"] is False


# -- signing -----------------------------------------------------------------------


def test_each_signature_asks_for_presence(keychain, enclave):
    stored = keychain.setup()
    enclave.prompts.clear()

    for _ in range(2):
        public, signature = keychain.sign(b"message", reason="approve this save")
        assert public == stored.public_key
        _verify(public, b"message", signature)

    assert enclave.prompts == ["approve this save", "approve this save"]


@pytest.mark.parametrize(
    "answer, code",
    [("cancel", "approver_presence_refused"), ("error", "approver_key_unusable")],
)
def test_no_presence_or_a_keychain_error_means_no_signature(keychain, enclave, answer, code):
    keychain.setup()
    enclave.answer = answer

    with pytest.raises(ApproverKeychainError) as raised:
        keychain.sign(b"message", reason="approve this save")

    assert raised.value.code == code


def test_no_key_means_no_signature(keychain):
    with pytest.raises(ApproverKeychainError) as raised:
        keychain.sign(b"message", reason="approve this save")
    assert raised.value.code == "approver_key_missing"


def test_a_key_stored_without_presence_isnt_used(enclave, registry, fake_keyring):
    ApproverKeychain(enclave, require_presence=False, roster=registry).setup()

    with pytest.raises(ApproverKeychainError) as raised:
        ApproverKeychain(enclave, roster=registry).sign(b"m", reason="r")

    assert raised.value.code == "approver_key_unusable"


# -- rotation and removal ----------------------------------------------------------


def test_rotation_adds_the_new_key_with_the_old_keys_signature(
    keychain, enclave, registry
):
    old = keychain.setup()
    enclave.prompts.clear()

    new = keychain.setup(replace=True)

    assert new.public_key != old.public_key
    assert registry.approvers[new.principal] == new.public_key
    # The root stays on the roster; it is no longer used here.
    assert registry.approvers["desktop-approver"] == old.public_key
    assert enclave.prompts == [
        "set up your new approver key",
        "approve your new approver key",
    ]
    _public, signature = keychain.sign(b"m", reason="approve this save")
    _verify(new.public_key, b"m", signature)


def test_rotating_again_retires_the_previous_non_root_key(keychain, registry):
    keychain.setup()
    second = keychain.setup(replace=True)

    third = keychain.setup(replace=True)

    assert second.principal not in registry.approvers
    assert registry.approvers[third.principal] == third.public_key


def test_a_failed_rotation_keeps_the_old_key_working(keychain, registry):
    old = keychain.setup()
    registry.fail = True

    with pytest.raises(ApproverKeychainError):
        keychain.setup(replace=True)

    assert keychain.stored().public_key == old.public_key
    assert registry.approvers == {"desktop-approver": old.public_key}


def test_removing_a_non_root_key_takes_it_off_the_roster(keychain, enclave, registry, fake_keyring):
    keychain.setup()
    second = keychain.setup(replace=True)
    enclave.prompts.clear()

    assert keychain.remove() is True

    assert second.principal not in registry.approvers
    assert enclave.prompts == ["remove your approver key"]
    assert fake_keyring.get_password(SERVICE_NAME, ACCOUNT) is None
    assert keychain.remove() is False
    with pytest.raises(ApproverKeychainError) as raised:
        keychain.sign(b"m", reason="r")
    assert raised.value.code == "approver_key_missing"


def test_removing_the_root_key_deletes_it_here_and_keeps_the_root(
    keychain, registry, fake_keyring
):
    root = keychain.setup()

    assert keychain.remove() is True

    assert registry.approvers == {"desktop-approver": root.public_key}
    assert fake_keyring.get_password(SERVICE_NAME, ACCOUNT) is None


# -- Run it: Flow asks Desktop to sign over a pipe ---------------------------------


class FakeFlow:
    """``item-approval ... --approver-signer stdio`` as Flow runs it."""

    def __init__(self, cmd, *, payload: dict, registry: FakeRegistry, **kwargs) -> None:
        self.cmd = cmd
        self.env = kwargs.get("env") or {}
        self.public = cmd[cmd.index("--approver-public-key") + 1]
        self.message = (
            ANSWER_DOMAIN + json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        )
        self.registry = registry
        self.response: dict | None = None
        flow = self

        class Out:
            def readline(self):
                return (
                    json.dumps(
                        {
                            "schema": "openadapt-flow.approver-sign-request/v1",
                            "public_key": flow.public,
                            "message_hex": flow.message.hex(),
                        }
                    )
                    + "\n"
                )

            def read(self):
                return json.dumps(flow.result()) + "\n"

        class In:
            def write(self, text):
                flow.response = json.loads(text)

            def flush(self):
                pass

            def close(self):
                pass

        self.stdout, self.stdin = Out(), In()

    def result(self) -> dict:
        response = self.response or {}
        if "signature" not in response:
            return {
                "ok": False,
                "code": response.get("refused", "approver_signature_unavailable"),
                "message": response.get("message", ""),
            }
        _verify(self.public, self.message, response["signature"])
        self.returncode = 0
        return {
            "ok": True,
            "request_id": REQUEST_ID,
            "decision": "approve",
            "answer_sha256": "sha256:" + "c" * 64,
            "resume_required": False,
        }

    def wait(self, timeout=None):
        return 0 if (self.response or {}).get("signature") else 2

    def kill(self):
        pass


def _payload(**overrides) -> dict:
    return {
        "request_id": REQUEST_ID,
        "request_sha256": REQUEST_SHA,
        "decision": "approve",
        "decided_by": "human",
        "route": "desktop_run_it",
        "approver_kind": "unauthenticated",
        "auth_method": "none",
        "answered_at": "2026-10-24T12:00:00Z",
        **overrides,
    }


def _bridge(monkeypatch, keychain, registry, payload, launched):
    monkeypatch.setattr(
        "openadapt_desktop.flow_bridge._flow_command", lambda _bin: ["openadapt-flow"]
    )

    def popen(cmd, **kwargs):
        flow = FakeFlow(cmd, payload=payload, registry=registry, **kwargs)
        launched.append(flow)
        return flow

    def no_run(*_args, **_kwargs):
        raise AssertionError("the in-app key never takes the terminal path")

    return FlowBridge(runner=no_run, popen=popen, approver_keychain=keychain)


def test_run_it_signs_in_app_after_touch_id(monkeypatch, tmp_path, keychain, enclave, registry):
    stored = keychain.setup()
    enclave.prompts.clear()
    launched: list = []
    bridge = _bridge(monkeypatch, keychain, registry, _payload(), launched)

    answer = bridge.item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )

    assert answer["ok"] is True
    (flow,) = launched
    assert flow.cmd[flow.cmd.index("--approver-signer") + 1] == "stdio"
    assert flow.public == stored.public_key
    assert "--approver-key" not in flow.cmd
    assert "OPENADAPT_JIT_KEY_PASSPHRASE" not in flow.env
    assert enclave.prompts == ["approve this save"]


@pytest.mark.parametrize(
    "payload",
    [
        _payload(request_id="0" * 31 + "2"),
        _payload(decision="decline"),
        _payload(decided_by="automation"),
        _payload(route="mcp"),
    ],
)
def test_desktop_signs_only_the_answer_the_person_chose(
    monkeypatch, tmp_path, keychain, enclave, registry, payload
):
    keychain.setup()
    enclave.prompts.clear()
    launched: list = []
    bridge = _bridge(monkeypatch, keychain, registry, payload, launched)

    answer = bridge.item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )

    assert answer == {
        "ok": False,
        "code": "approver_payload_refused",
        "message": (
            "Flow asked Desktop to sign something other than your answer, so nothing was signed."
        ),
    }
    assert enclave.prompts == []  # never asked the person to approve it


def test_a_cancelled_prompt_sends_flow_a_refusal(
    monkeypatch, tmp_path, keychain, enclave, registry
):
    keychain.setup()
    enclave.answer = "cancel"
    launched: list = []
    bridge = _bridge(monkeypatch, keychain, registry, _payload(), launched)

    answer = bridge.item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )

    assert answer["ok"] is False
    assert answer["code"] == "approver_presence_refused"
    assert launched[0].response["refused"] == "approver_presence_refused"


def test_no_key_on_a_mac_that_can_set_one_up_sends_nothing(
    monkeypatch, tmp_path, keychain, registry
):
    launched: list = []
    bridge = _bridge(monkeypatch, keychain, registry, _payload(), launched)

    answer = bridge.item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )

    assert answer["ok"] is False
    assert answer["code"] == "approver_key_missing"
    assert answer["setup"] == "in_app"
    assert launched == []


# -- the window's commands ---------------------------------------------------------


def _dispatcher(tmp_path: Path, keychain):
    from openadapt_desktop.config import EngineConfig
    from openadapt_desktop.dispatch import EngineDispatcher, EngineServices

    class Audit:
        def __init__(self):
            self.events: list = []

        def log(self, event, **fields):
            self.events.append((event, fields))

    audit = Audit()
    config = EngineConfig(data_dir=tmp_path / "data")
    services = EngineServices(
        config, flow_bridge=FlowBridge(approver_keychain=keychain), audit=audit
    )
    return EngineDispatcher(config, services=services), audit


def test_the_window_sets_up_rotates_and_removes_the_key(tmp_path, keychain, registry):
    dispatcher, audit = _dispatcher(tmp_path, keychain)

    assert dispatcher.dispatch("get_approver_key", {})["set_up"] is False
    first = dispatcher.dispatch("set_up_approver_key", {})
    assert first["ok"] is True and first["set_up"] is True
    rotated = dispatcher.dispatch("set_up_approver_key", {"replace": True})
    assert rotated["fingerprint"] != first["fingerprint"]
    removed = dispatcher.dispatch("remove_approver_key", {})
    assert removed["removed"] is True and removed["set_up"] is False
    # The rotated-in key is off the roster; the root stays.
    assert set(registry.approvers) == {"desktop-approver"}
    assert [event for event, _ in audit.events] == [
        "approver_key_setup",
        "approver_key_setup",
        "approver_key_removed",
    ]
    # The audit names a fingerprint, never the key.
    for _event, fields in audit.events:
        assert all(len(str(value)) <= 16 for value in fields.values())


def test_a_cancelled_setup_reaches_the_window(tmp_path, keychain, enclave):
    enclave.answer = "cancel"
    dispatcher, _audit = _dispatcher(tmp_path, keychain)

    result = dispatcher.dispatch("set_up_approver_key", {})

    assert result["ok"] is False
    assert result["code"] == "approver_presence_refused"


def test_another_computer_gets_the_terminal_steps(tmp_path, registry):
    keychain = ApproverKeychain(FakeEnclave(available=False), roster=registry)
    dispatcher, _audit = _dispatcher(tmp_path, keychain)

    status = dispatcher.dispatch("get_approver_key", {})

    assert status["supported"] is False
    assert "openadapt-flow jit approver init" in status["instructions"]


def test_the_key_commands_answer_while_a_run_waits_and_stay_off_the_agent_socket():
    from openadapt_desktop.ipc import CONCURRENT_COMMANDS
    from openadapt_desktop.socket_server import _SOCKET_COMMANDS

    keys = {"get_approver_key", "set_up_approver_key", "remove_approver_key"}
    assert keys <= CONCURRENT_COMMANDS
    assert not keys & _SOCKET_COMMANDS


def test_a_mac_that_can_use_touch_id_never_falls_back_to_a_file_key(
    monkeypatch, tmp_path, keychain, registry
):
    """A terminal-made key asks for no presence; on such a Mac it doesn't count."""

    from openadapt_desktop import approver_key

    monkeypatch.setattr(approver_key, "KEY_DIR", tmp_path / "approver")
    assert approver_key.setup_approver_key().created is True
    assert approver_key.approver_key_environment() is not None
    launched: list = []
    bridge = _bridge(monkeypatch, keychain, registry, _payload(), launched)

    answer = bridge.item_approval_answer(
        tmp_path, decision="approve", request_id=REQUEST_ID, request_sha256=REQUEST_SHA
    )

    assert answer["ok"] is False
    assert answer["code"] == "approver_key_missing"
    assert answer["setup"] == "in_app"
    assert "in OpenAdapt" in answer["message"]
    assert launched == []
