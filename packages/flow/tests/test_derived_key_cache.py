"""Synthetic cache lifecycle checks; retained keys are not memory erasure."""

from __future__ import annotations

import base64
import hmac
import json
from collections import OrderedDict

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from hypothesis import example, given, settings
from hypothesis import strategies as st

from openadapt_flow import crypto

PASSPHRASES = (
    "synthetic-short",
    "synthetic-" + "x" * 1000,
    "synthetic-é-😀",
    "synthetic-cache-" + "q" * 16,
)
METHODS = ("data", "set", "encrypt", "decrypt", "clear")
CACHE_MAC_KEY = b"c" * 32
CACHE_DOMAIN = b"openadapt-flow/derived-key-cache/v1\0"


def _synthetic_key(secret, salt, n, r, p):
    # An inexpensive independent KDF stand-in lets generated operation sequences
    # check cache behavior. The separate real-scrypt test keeps the real crypto.
    return hmac.digest(
        b"k" * 32,
        b"synthetic-kdf-oracle/v1\0" + salt + f"{n}:{r}:{p}:".encode() + secret,
        "sha256",
    )


def _exercise(method, passphrase, salt, params):
    n, r, p = params
    if method == "encrypt":
        n, r, p = crypto._SCRYPT_N, crypto._SCRYPT_R, crypto._SCRYPT_P
    expected = _synthetic_key(passphrase.encode(), salt, n, r, p)
    plain, aad = b"synthetic payload", crypto.BUNDLE_AAD
    if method == "data":
        assert crypto.derive_data_key(passphrase, salt, n=n, r=r, p=p) == expected
    elif method == "set":
        key = crypto.SetKey.derive(passphrase, salt, n=n, r=r, p=p)
        body = key.seal(plain, aad=aad)[len(crypto.SET_FILE_MAGIC) :]
        assert AESGCM(expected).decrypt(body[:12], body[12:], aad) == plain
    elif method == "encrypt":
        obj = json.loads(crypto.encrypt_bytes(plain, passphrase, aad=aad, salt=salt))
        assert (
            AESGCM(expected).decrypt(
                base64.b64decode(obj["nonce"]), base64.b64decode(obj["ciphertext"]), aad
            )
            == plain
        )
    else:
        nonce = b"n" * 12
        obj = {
            "format": "openadapt-flow-enc",
            "kdf_n": n,
            "kdf_r": r,
            "kdf_p": p,
            "salt": base64.b64encode(salt).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(
                AESGCM(expected).encrypt(nonce, plain, aad)
            ).decode(),
        }
        assert (
            crypto.decrypt_bytes(json.dumps(obj).encode(), passphrase, aad=aad) == plain
        )
    return (salt, n, r, p)


@settings(max_examples=30)
@example(
    [
        ("data", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("set", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("encrypt", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("decrypt", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("data", PASSPHRASES[1], 0, (2**15, 8, 1)),
        ("data", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("data", PASSPHRASES[0], 0, (2**14, 8, 1)),
        ("data", PASSPHRASES[0], 1, (2**15, 8, 1)),
        ("data", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("set", PASSPHRASES[0], 0, (2**15, 16, 2)),
        ("data", PASSPHRASES[0], 2, (2**15, 8, 1)),
        ("data", PASSPHRASES[0], 3, (2**15, 8, 1)),
        ("data", PASSPHRASES[0], 1, (2**15, 8, 1)),
        ("clear", PASSPHRASES[0], 0, (2**15, 8, 1)),
        ("decrypt", PASSPHRASES[0], 0, (2**15, 8, 1)),
    ]
)
@given(
    st.lists(
        st.tuples(
            st.sampled_from(METHODS),
            st.sampled_from(PASSPHRASES),
            st.integers(min_value=0, max_value=4),
            st.sampled_from(((2**15, 8, 1), (2**14, 8, 1), (2**15, 16, 2))),
        ),
        min_size=1,
        max_size=40,
    )
)
def test_cache_lifecycle_across_all_callers(operations):
    with pytest.MonkeyPatch.context() as patch:
        cache = OrderedDict()
        patch.setattr(crypto, "_DERIVED", cache)
        patch.setattr(crypto, "_DERIVED_LIMIT", 3)
        patch.setattr(crypto, "derivations", 0)
        patch.setattr(crypto, "_DERIVED_FINGERPRINT_KEY", CACHE_MAC_KEY)
        comparisons = []
        compare = crypto.hmac.compare_digest

        def compare_fixed_size(left, right):
            comparisons.append((left, right))
            assert len(left) == len(right) == 32
            return compare(left, right)

        class SyntheticScrypt:
            def __init__(self, *, salt, length, n, r, p):
                assert length == 32
                self.args = salt, n, r, p

            def derive(self, secret):
                return _synthetic_key(secret, *self.args)

        patch.setattr(crypto.hmac, "compare_digest", compare_fixed_size)
        patch.setattr(crypto, "Scrypt", SyntheticScrypt)
        model = OrderedDict()
        misses = 0
        for method, passphrase, salt_number, params in operations:
            if method == "clear":
                cache.clear()
                model.clear()
                assert crypto.derivations == misses
                continue
            salt = bytes([salt_number]) * 16
            effective = (
                (crypto._SCRYPT_N, crypto._SCRYPT_R, crypto._SCRYPT_P)
                if method == "encrypt"
                else params
            )
            slot = (salt, *effective)
            prior = model.get(slot)
            old_comparisons = len(comparisons)
            _exercise(method, passphrase, salt, params)
            assert len(comparisons) - old_comparisons == int(prior is not None)
            if prior != passphrase:
                misses += 1
            model[slot] = passphrase
            model.move_to_end(slot)
            while len(model) > 3:
                model.popitem(last=False)
            assert crypto.derivations == misses
            assert list(cache) == list(model)
            for cached_slot, (fingerprint, key) in cache.items():
                secret = model[cached_slot].encode()
                assert len(fingerprint) == 32
                assert fingerprint == hmac.digest(
                    CACHE_MAC_KEY, CACHE_DOMAIN + secret, "sha256"
                )
                assert fingerprint != secret
                assert key == _synthetic_key(secret, *cached_slot)


def test_real_scrypt_interoperates_after_wrong_passphrase_and_clear(monkeypatch):
    monkeypatch.setattr(crypto, "_DERIVED", OrderedDict())
    salt = b"s" * 16
    passphrase, wrong = "synthetic-cache-correct", "synthetic-cache-wrong"
    before = crypto.derivations
    expected = crypto.derive_data_key(passphrase, salt)
    set_key = crypto.SetKey.derive(passphrase, salt)
    sealed = set_key.seal(b"payload", aad=crypto.BUNDLE_AAD)
    container = crypto.encrypt_bytes(
        b"payload", passphrase, aad=crypto.BUNDLE_AAD, salt=salt
    )
    assert (
        crypto.decrypt_bytes(container, passphrase, aad=crypto.BUNDLE_AAD) == b"payload"
    )
    assert crypto.derivations == before + 1

    other_key = crypto.SetKey.derive(wrong, salt)
    with pytest.raises(crypto.DecryptionError):
        other_key.open(sealed, aad=crypto.BUNDLE_AAD)
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(container, wrong, aad=crypto.BUNDLE_AAD)
    assert crypto.derivations == before + 2
    assert crypto.derive_data_key(passphrase, salt) == expected
    assert crypto.derivations == before + 3
    crypto._DERIVED.clear()
    assert not crypto._DERIVED
    assert crypto.derivations == before + 3
    assert (
        crypto.decrypt_bytes(container, passphrase, aad=crypto.BUNDLE_AAD) == b"payload"
    )
    assert crypto.derivations == before + 4
    assert all(len(fingerprint) == 32 for fingerprint, _ in crypto._DERIVED.values())


def test_cache_tags_bind_key_and_domain_without_changing_data_keys(monkeypatch):
    monkeypatch.setattr(crypto, "_DERIVED", OrderedDict())
    assert len(crypto._DERIVED_FINGERPRINT_KEY) == 32
    passphrase, salt = "synthetic-cache-domain-check", b"d" * 16
    tags, data_keys = [], []
    scopes = [
        (b"a" * 32, CACHE_DOMAIN),
        (b"b" * 32, CACHE_DOMAIN),
        (b"a" * 32, b"synthetic-other-purpose/v1\0"),
        (b"a" * 32, CACHE_DOMAIN),
    ]
    for mac_key, domain in scopes:
        # Separate cache instances may have different private MAC namespaces;
        # neither that namespace nor a clear changes the scrypt data key.
        crypto._DERIVED.clear()
        monkeypatch.setattr(crypto, "_DERIVED_FINGERPRINT_KEY", mac_key)
        monkeypatch.setattr(crypto, "_DERIVED_FINGERPRINT_DOMAIN", domain)
        data_keys.append(crypto.derive_data_key(passphrase, salt))
        ((tag, _),) = crypto._DERIVED.values()
        assert len(tag) == 32
        assert tag == hmac.digest(mac_key, domain + passphrase.encode(), "sha256")
        tags.append(tag)
    assert len(set(data_keys)) == 1
    assert len(set(tags[:3])) == 3
    assert tags[0] == tags[3]
