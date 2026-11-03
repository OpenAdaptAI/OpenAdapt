"""The approver key on a real Mac: the Secure Enclave and the login Keychain.

Opt-in, because it writes a throwaway item to the login Keychain and makes
Secure Enclave keys. It never shows a Touch ID prompt:

* the full setup, sign, rotate and remove cycle uses a Secure Enclave key
  without the presence flag (``require_presence=False``);
* the presence flag is checked by unsealing with a context that may not show
  a prompt, which the Secure Enclave must refuse.

Run it on a Mac with Touch ID or a T2 chip (Apple silicon or a T2 Intel Mac;
a macOS virtual machine, such as a hosted CI runner, has no Secure Enclave
and skips)::

    cd packages/desktop
    OPENADAPT_REAL_KEYCHAIN_TESTS=1 uv run pytest tests/test_approver_keychain_real.py -v

It removes its Keychain item when it finishes. To check by hand that a real
approval prompts, set up the key from Settings > Approver key in the app.
"""

from __future__ import annotations

import os
import secrets
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

pytestmark = [
    pytest.mark.skipif(sys.platform != "darwin", reason="macOS only"),
    pytest.mark.skipif(
        os.environ.get("OPENADAPT_REAL_KEYCHAIN_TESTS") != "1",
        reason="set OPENADAPT_REAL_KEYCHAIN_TESTS=1 to use the real Keychain",
    ),
]


@pytest.fixture
def enclave():
    from openadapt_desktop.approver_keychain import ApproverKeychainError, MacEnclave

    real = MacEnclave()
    if not real.available():
        pytest.skip("this Mac has no Secure Enclave")
    try:
        real.create(require_presence=False)
    except ApproverKeychainError as exc:
        if exc.code == "approver_presence_refused":
            pytest.skip("this Mac is locked; unlock it to run the real Secure Enclave tests")
        raise
    return real


@pytest.fixture
def account():
    import keyring

    from openadapt_desktop.auth.store import SERVICE_NAME

    name = f"approver-keychain-test-{secrets.token_hex(6)}"
    yield name
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except Exception:
        pass


def test_setup_sign_rotate_and_remove_with_the_real_keychain(enclave, account):
    import keyring

    from openadapt_desktop.approver_keychain import ApproverKeychain, ApproverKeychainError
    from openadapt_desktop.auth.store import SERVICE_NAME
    from tests.test_engine.test_approver_keychain import FakeRegistry

    # The Secure Enclave and the login Keychain are real; the roster is the
    # tests' signature-checking stand-in (Flow's own is tested in Flow).
    roster = FakeRegistry()
    keychain = ApproverKeychain(
        enclave, account=account, require_presence=False, roster=roster
    )

    first = keychain.setup()
    assert keyring.get_password(SERVICE_NAME, account)  # in the login Keychain
    _public, signature = keychain.sign(b"real message", reason="test")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(first.public_key)).verify(
        bytes.fromhex(signature.removeprefix("ed25519:")), b"real message"
    )

    second = keychain.setup(replace=True)
    assert second.public_key != first.public_key
    assert roster.approvers[second.principal] == second.public_key

    assert keychain.remove() is True
    assert keyring.get_password(SERVICE_NAME, account) is None
    with pytest.raises(ApproverKeychainError):
        keychain.sign(b"real message", reason="test")


def test_the_secure_enclave_refuses_a_presence_key_without_a_prompt(enclave):
    from openadapt_desktop.approver_keychain import ApproverKeychainError

    handle, public = enclave.create(require_presence=True)
    sealed = enclave.seal(public, b"\x07" * 32)

    with pytest.raises(ApproverKeychainError) as raised:
        enclave.open(handle, sealed, reason="test", interactive=False)

    assert raised.value.code == "approver_presence_refused"


def test_a_sealed_key_opens_only_with_its_own_enclave_key(enclave):
    from openadapt_desktop.approver_keychain import ApproverKeychainError

    handle, public = enclave.create(require_presence=False)
    other, _other_public = enclave.create(require_presence=False)
    sealed = enclave.seal(public, b"\x07" * 32)

    assert bytes(enclave.open(handle, sealed, reason="test")) == b"\x07" * 32
    with pytest.raises(ApproverKeychainError):
        enclave.open(other, sealed, reason="test")


def test_a_pin_another_program_wrote_reads_as_no_pin():
    """Attack: a same-user process deletes Desktop's Keychain pin and plants
    its own (readable by any app), or rewrites Desktop's in place. Read with
    Keychain prompts off, neither may come back as a root, and the read must
    not wait on a prompt someone could allow."""

    import subprocess
    import uuid

    from openadapt_desktop.auth.store import SERVICE_NAME

    account = f"desktop-approver-root-probe-{uuid.uuid4().hex[:12]}"
    read = (
        "from openadapt_desktop.approver_keychain import _no_keychain_ui\n"
        "from openadapt_desktop.auth import store\n"
        "with _no_keychain_ui():\n"
        f"    print(store._kr_get(store._keyring(), {account!r}))\n"
    )
    security = "/usr/bin/security"

    def desktop_reads() -> str:
        done = subprocess.run(
            [sys.executable, "-c", read], capture_output=True, text=True, timeout=30
        )
        assert done.returncode == 0, done.stderr
        return done.stdout.strip().splitlines()[-1]

    try:
        import keyring

        keyring.set_password(SERVICE_NAME, account, "a" * 64)
        assert desktop_reads() == "a" * 64

        # Rewritten in place by another program.
        subprocess.run(
            [security, "add-generic-password", "-U", "-s", SERVICE_NAME, "-a", account,
             "-w", "b" * 64],
            check=True, timeout=15,
        )
        assert desktop_reads() == "None"

        # Deleted and planted fresh, readable by any app.
        subprocess.run(
            [security, "delete-generic-password", "-s", SERVICE_NAME, "-a", account],
            check=True, capture_output=True, timeout=15,
        )
        subprocess.run(
            [security, "add-generic-password", "-A", "-s", SERVICE_NAME, "-a", account,
             "-w", "c" * 64],
            check=True, timeout=15,
        )
        assert desktop_reads() == "None"
    finally:
        subprocess.run(
            [security, "delete-generic-password", "-s", SERVICE_NAME, "-a", account],
            capture_output=True, timeout=15,
        )
