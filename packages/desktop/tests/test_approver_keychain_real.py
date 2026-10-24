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
    from openadapt_desktop.approver_keychain import MacEnclave

    real = MacEnclave()
    if not real.available():
        pytest.skip("this Mac has no Secure Enclave")
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

    approvers: dict[str, str] = {}

    def unregister(principal: str, public: str) -> bool:
        if approvers.get(principal) != public:
            return False
        del approvers[principal]
        return True

    keychain = ApproverKeychain(
        enclave,
        account=account,
        require_presence=False,
        register=approvers.__setitem__,
        unregister=unregister,
        lookup=lambda public: next((p for p, v in approvers.items() if v == public), None),
    )

    first = keychain.setup()
    assert keyring.get_password(SERVICE_NAME, account)  # in the login Keychain
    _public, signature = keychain.sign(b"real message", reason="test")
    Ed25519PublicKey.from_public_bytes(bytes.fromhex(first.public_key)).verify(
        bytes.fromhex(signature.removeprefix("ed25519:")), b"real message"
    )

    second = keychain.setup(replace=True)
    assert second.public_key != first.public_key
    assert approvers == {"desktop-approver": second.public_key}

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
