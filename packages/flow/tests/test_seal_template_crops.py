"""Sealing the ``templates/*.png`` image crops inside the encrypted bundle.

Closes the remaining at-rest PHI gap: a compiled bundle's template crops are
PIXELS of the recorded (patient) screen -- image PHI. Before this, only
``workflow.json`` was sealed by ``save(encrypt=True)`` while the crops relied on
operator disk encryption. These tests assert that an ENCRYPTED bundle carries no
cleartext PHI-bearing screenshot on disk, that the crops round-trip identically
through a keyed load, that a wrong key / tampered or missing crop ciphertext
fails LOUD, that the resolver can consume the decrypted crops, and -- crucially
-- that the UNENCRYPTED default is byte-for-byte unchanged (plaintext PNGs).

Import-light: builds IR objects directly (no Playwright / OCR / model deps); the
resolver test injects a fake ``vision`` that records the crop bytes it receives.
"""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from openadapt_flow import crypto
from openadapt_flow.ir import (
    TEMPLATE_AAD,
    ActionKind,
    Anchor,
    Step,
    Workflow,
)
from openadapt_flow.runtime.resolver import resolve

KEY = "correct horse battery staple"
WRONG = "Tr0ub4dor&3"

# Distinctive, PHI-ish crop payloads so a leak would be grep-visible.
BTN_PNG = b"\x89PNG\r\n\x1a\nPATIENT-JANE-DOE-crop-pixels"
EXPECT_PNG = b"\x89PNG\r\n\x1a\nMRN-000123456-expect-crop"


def _workflow() -> Workflow:
    return Workflow(
        name="demo",
        steps=[
            Step(
                id="s1",
                intent="click patient row",
                action=ActionKind.CLICK,
                anchor=Anchor(
                    template="templates/s1.png",
                    region=(100, 100, 50, 20),
                    click_point=(110, 105),
                    ocr_text="Open",
                ),
            ),
            Step(id="s2", intent="submit", action=ActionKind.KEY, key="Enter"),
        ],
    )


def _bundle_dir(tmp_path):
    b = tmp_path / "bundle"
    (b / "templates").mkdir(parents=True)
    (b / "templates" / "s1.png").write_bytes(BTN_PNG)
    # A second crop (an expect/postcondition template) to prove multi-asset
    # sealing, not just the single anchor template.
    (b / "templates" / "s1_expect.png").write_bytes(EXPECT_PNG)
    return b


def _flip_container_ciphertext(container: bytes) -> bytes:
    obj = json.loads(container)
    ct = bytearray(base64.b64decode(obj["ciphertext"]))
    ct[0] ^= 0x01
    obj["ciphertext"] = base64.b64encode(bytes(ct)).decode("ascii")
    return json.dumps(obj).encode("utf-8")


# ---------------------------------------------------------------------------
# no cleartext PHI on disk
# ---------------------------------------------------------------------------


def test_encrypted_bundle_leaves_no_cleartext_png(tmp_path):
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)

    # No plaintext PNG lingers; each crop is present only as a sealed .enc.
    pngs = list((b / "templates").glob("*.png"))
    assert pngs == [], f"cleartext crop(s) left on disk: {pngs}"
    assert (b / "templates" / "s1.png.enc").is_file()
    assert (b / "templates" / "s1_expect.png.enc").is_file()
    assert crypto.is_encrypted((b / "templates" / "s1.png.enc").read_bytes())

    # The distinctive pixel payloads are nowhere grep-visible under the bundle.
    for f in b.rglob("*"):
        if f.is_file():
            data = f.read_bytes()
            assert BTN_PNG not in data
            assert EXPECT_PNG not in data


def test_encrypted_templates_round_trip_identically(tmp_path):
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)

    loaded = Workflow.load(b, key=KEY)
    assert loaded.decrypted_template("templates/s1.png") == BTN_PNG
    assert loaded.decrypted_template("templates/s1_expect.png") == EXPECT_PNG
    assert loaded.decrypted_templates() == {
        "templates/s1.png": BTN_PNG,
        "templates/s1_expect.png": EXPECT_PNG,
    }


def test_template_sealed_under_distinct_domain(tmp_path):
    # A crop ciphertext is sealed under TEMPLATE_AAD, so it must NOT authenticate
    # under the workflow-json domain (BUNDLE_AAD) even with the right key.
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    sealed = (b / "templates" / "s1.png.enc").read_bytes()
    assert crypto.decrypt_bytes(sealed, KEY, aad=TEMPLATE_AAD) == BTN_PNG
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt_bytes(sealed, KEY, aad=crypto.BUNDLE_AAD)


# ---------------------------------------------------------------------------
# fail-loud
# ---------------------------------------------------------------------------


def test_wrong_key_fails_loud(tmp_path):
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    with pytest.raises(crypto.DecryptionError):
        Workflow.load(b, key=WRONG)


def test_tampered_template_ciphertext_fails_loud(tmp_path):
    # workflow.json stays intact; only a crop's ciphertext is corrupted. The
    # crop AEAD tag must catch it on load (no silent skip of the template).
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    enc = b / "templates" / "s1.png.enc"
    enc.write_bytes(_flip_container_ciphertext(enc.read_bytes()))
    with pytest.raises(crypto.DecryptionError):
        Workflow.load(b, key=KEY)


def test_missing_template_ciphertext_fails_integrity(tmp_path):
    from openadapt_flow.bundle_validation import BundleIntegrityError

    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    (b / "templates" / "s1.png.enc").unlink()
    with pytest.raises(BundleIntegrityError):
        Workflow.load(b, key=KEY)


def test_swapped_template_ciphertext_fails_integrity(tmp_path):
    # Replace one crop's ciphertext with a DIFFERENTLY-keyed-but-valid container
    # for other bytes: it decrypts cleanly (AEAD ok) but its plaintext hash no
    # longer matches the sealed manifest digest -> integrity halts.
    from openadapt_flow.bundle_validation import BundleIntegrityError

    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    forged = crypto.encrypt_bytes(b"\x89PNG\r\n\x1a\nDIFFERENT", KEY, aad=TEMPLATE_AAD)
    (b / "templates" / "s1.png.enc").write_bytes(forged)
    with pytest.raises(BundleIntegrityError):
        Workflow.load(b, key=KEY)


# ---------------------------------------------------------------------------
# unencrypted default unchanged
# ---------------------------------------------------------------------------


def test_unencrypted_default_writes_cleartext_png(tmp_path):
    b = _bundle_dir(tmp_path)
    wf = _workflow()
    path = wf.save(b)

    assert path.name == "workflow.json"
    # Cleartext PNGs on disk exactly as before; no .enc siblings; nothing cached.
    assert (b / "templates" / "s1.png").read_bytes() == BTN_PNG
    assert (b / "templates" / "s1_expect.png").read_bytes() == EXPECT_PNG
    assert list((b / "templates").glob("*.enc")) == []
    assert wf.encrypted is False
    assert wf.decrypted_templates() == {}

    loaded = Workflow.load(b)
    assert loaded.encrypted is False
    # A plaintext bundle exposes no in-memory crops (they are read from disk).
    assert loaded.decrypted_template("templates/s1.png") is None


def test_resave_plaintext_recovers_sealed_crops(tmp_path):
    # Sealing removes the plaintext PNGs from disk; a subsequent plaintext
    # re-save of the same object must restore them (from the in-memory cache)
    # and drop the ciphertext, leaving a clean plaintext bundle.
    b = _bundle_dir(tmp_path)
    wf = _workflow()
    wf.save(b, encrypt=True, key=KEY)
    assert list((b / "templates").glob("*.png")) == []

    wf.save(b)  # plaintext re-save
    assert (b / "templates" / "s1.png").read_bytes() == BTN_PNG
    assert (b / "templates" / "s1_expect.png").read_bytes() == EXPECT_PNG
    assert list((b / "templates").glob("*.enc")) == []
    loaded = Workflow.load(b)
    assert loaded.encrypted is False


# ---------------------------------------------------------------------------
# resolver consumes the decrypted crop
# ---------------------------------------------------------------------------


def test_resolver_consumes_decrypted_template(tmp_path):
    b = _bundle_dir(tmp_path)
    _workflow().save(b, encrypt=True, key=KEY)
    loaded = Workflow.load(b, key=KEY)

    step = loaded.steps[0]
    crop = loaded.decrypted_template(step.anchor.template)
    assert crop == BTN_PNG  # the decrypted-in-memory crop, no disk plaintext

    seen: dict[str, object] = {}

    def find_template(screen_png, template_png, **kwargs):
        # Prove the resolver was handed the DECRYPTED crop bytes.
        seen["template_png"] = template_png
        return SimpleNamespace(region=(100, 100, 50, 20), confidence=0.99)

    vision = SimpleNamespace(find_template=find_template)

    result = resolve(
        step.anchor,
        screen_png=b"\x89PNG\r\n\x1a\nlive-frame",
        vision=vision,
        template_png=crop,
        viewport=(800, 600),
    )
    assert result is not None
    resolution, _region = result
    assert resolution.rung == "template"
    assert seen["template_png"] == BTN_PNG
