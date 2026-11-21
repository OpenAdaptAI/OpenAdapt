"""The shared record/run/v1 vectors, checked against the Python verifier."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("cryptography")

from openadapt_types import receipt as r  # noqa: E402

VECTORS = Path(__file__).parent / "vectors" / "record-run-v1"
NAMES = sorted(p.stem for p in VECTORS.glob("*.json") if not p.name.startswith("registry"))


def _registry():
    return r.parse_registry(json.loads((VECTORS / "registry.json").read_text()))


def test_the_vector_set_is_complete():
    for required in (
        "valid",
        "tampered",
        "unknown-signer",
        "demo-key",
        "public-redaction",
        "cloud-countersign",
        "embedded-image-in-card",
        "embedded-image-in-record",
    ):
        assert required in NAMES


@pytest.mark.parametrize("name", NAMES)
def test_vector(name):
    vector = json.loads((VECTORS / f"{name}.json").read_text())
    result = r.verify_receipt(vector["document"], _registry(), openings=vector.get("openings"))
    expected = vector["expected"]
    assert result.ok is expected["ok"], result.detail
    assert result.reason == expected["reason"]
    assert list(result.signer_lines) == expected["signer_lines"]
    if expected["ok"]:
        assert result.outcome == expected["outcome"]
        assert result.demo is expected["demo"]
        assert dict(result.disclosed) == expected["disclosed"]
    else:
        assert result.outcome is None


def test_vectors_are_byte_stable(tmp_path, monkeypatch):
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen", VECTORS / "generate.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    for name, vector in gen.build().items():
        on_disk = (VECTORS / f"{name}.json").read_text()
        assert json.dumps(vector, indent=2, sort_keys=True) + "\n" == on_disk, name


def test_unknown_role_refuses_the_registry():
    data = json.loads((VECTORS / "registry-unknown-role.json").read_text())
    with pytest.raises(r.RegistryError) as info:
        r.parse_registry(data)
    assert info.value.reason == "unknown_role"


def test_an_empty_registry_trusts_nothing():
    vector = json.loads((VECTORS / "valid.json").read_text())
    result = r.verify_receipt(vector["document"], [])
    assert (result.ok, result.reason) == (False, "unknown_signer")
    assert result.signer_lines == ("Not verified: unknown signer",)


def test_the_builder_refuses_embedded_content():
    with pytest.raises(ValueError):
        r.make_openings({"program_name": "data:image/png;base64,iVBORw0KGgo"})
    with pytest.raises(ValueError):
        r.make_openings({"program_name": "https://example.test/patient/7"})
    with pytest.raises(ValueError):
        r.make_openings({"page_text": "Jane Doe"})


def test_outcomes_are_never_upgraded():
    assert r.receipt_outcome("VERIFIED") == ("VERIFIED", "verified")
    for run in ("HALTED", "ROLLED_BACK", "COMPLETED_UNVERIFIED", "FAILED", "weird", None, ""):
        assert r.receipt_outcome(run)[0] != "VERIFIED"


def test_public_card_refuses_a_field_off_the_allow_list():
    with pytest.raises(ValueError):
        r.public_card({}, {}, reveal=["run_id"])


def test_allow_list_fields_are_disclosure_fields():
    for version, allowed in r.PUBLIC_DISCLOSURE.items():
        assert set(allowed) <= set(r.DISCLOSURE_FIELDS)
        for source in allowed.values():
            assert source.split(":")[0] in ("public-card/v1", "public-evidence-receipt/v1")
