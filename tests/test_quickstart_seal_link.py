"""The Seal link: the receipt and Seal travel in the URL fragment, never a query."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import pytest
from test_quickstart import RECEIPT, SEAL

from openadapt.quickstart import seal_link
from openadapt.quickstart.seal_link import (
    MAX_FRAGMENT,
    decode_fragment,
    encode_fragment,
    read_card,
    seal_view_url,
)

BASE = "https://example.test/seals/view"


def test_the_seal_rides_in_the_fragment_and_round_trips():
    url = seal_view_url(RECEIPT, SEAL, base=BASE)
    assert url is not None
    parts = urlsplit(url)
    assert parts.query == ""
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == BASE
    assert parts.fragment.startswith("v1.")
    assert len(parts.fragment) <= MAX_FRAGMENT
    decoded = decode_fragment(parts.fragment)
    assert decoded["receipt"]["outcome"] == "VERIFIED"
    assert decoded["receipt"]["effects_confirmed"] == 2
    assert decoded["seal"] == SEAL


def test_only_allow_listed_receipt_fields_travel():
    receipt = dict(RECEIPT, typed_value="Jane Doe", entry_url="https://emr/x")
    decoded = decode_fragment(encode_fragment(receipt, SEAL))
    assert "typed_value" not in decoded["receipt"]
    assert "entry_url" not in decoded["receipt"]
    assert set(decoded["receipt"]) <= set(seal_link.RECEIPT_FIELDS)


def test_the_fragment_is_stable_and_url_safe():
    one = encode_fragment(RECEIPT, SEAL)
    assert one == encode_fragment(dict(reversed(list(RECEIPT.items()))), SEAL)
    assert all(c.isalnum() or c in "-_." for c in one)


def test_no_link_without_a_deployed_viewer(monkeypatch):
    monkeypatch.setattr(seal_link, "ONLINE_VIEWER", None)
    assert seal_view_url(RECEIPT, SEAL) is None


@pytest.mark.parametrize("base", ["", "https://example.test/view#already"])
def test_no_link_for_an_unusable_base(base):
    assert seal_view_url(RECEIPT, SEAL, base=base) is None


def test_no_link_when_it_would_be_too_long(monkeypatch):
    monkeypatch.setattr(seal_link, "MAX_FRAGMENT", 10)
    assert seal_view_url(RECEIPT, SEAL, base=BASE) is None


def test_other_documents_are_refused():
    with pytest.raises(ValueError):
        encode_fragment(dict(RECEIPT, schema_version="other"), SEAL)
    with pytest.raises(ValueError):
        encode_fragment(RECEIPT, dict(SEAL, schema="other"))
    with pytest.raises(ValueError):
        decode_fragment("v2.abc")


def test_read_card_needs_both_files(tmp_path):
    assert read_card(tmp_path) is None
    (tmp_path / "receipt.json").write_text(json.dumps(RECEIPT))
    assert read_card(tmp_path) is None
    (tmp_path / "seal.json").write_text(json.dumps(SEAL))
    card = read_card(tmp_path)
    assert card is not None
    assert (card.steps_ok, card.effects_confirmed, card.model_calls) == (6, 2, 0)
    assert card.short_digest == "6dd4a7f2d1ea"
