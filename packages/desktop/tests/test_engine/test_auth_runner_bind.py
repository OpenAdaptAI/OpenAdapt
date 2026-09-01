"""Parse-only tests for ``openadapt://runner`` bind grammar."""

from __future__ import annotations

import pytest

from engine.auth import pairing
from engine.auth.runner_bind import (
    RunnerBindError,
    parse_runner_uri,
    valid_bind_token,
    valid_lease_secret,
    valid_pack_id,
)

BIND = "oab_" + "A" * 43
PACK = "p.abcdefghijkl"
ORIGIN = "https://openadapt.ai"
VALID_URI = f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https%3A%2F%2Fopenadapt.ai"
CONNECT_SECRET = "oap_" + "A" * 43
CONNECT_URI = (
    f"openadapt://connect?pairing={CONNECT_SECRET}&host=https%3A%2F%2Fapp.openadapt.ai"
)


def test_parser_accepts_only_the_fixed_runner_action() -> None:
    assert parse_runner_uri(VALID_URI) == {
        "pack": PACK,
        "bind": BIND,
        "origin": ORIGIN,
    }
    for uri in (
        VALID_URI.replace("://runner?", "://run?"),
        VALID_URI.replace("://runner?", "://connect?"),
        VALID_URI.replace("openadapt:", "https:"),
        VALID_URI.replace("runner?", "runner/claim?"),
        VALID_URI + "#fragment",
        f"openadapt://user@runner?pack={PACK}&bind={BIND}&origin={ORIGIN}",
        CONNECT_URI,
    ):
        with pytest.raises(RunnerBindError, match="Invalid OpenAdapt runner link"):
            parse_runner_uri(uri)


def test_connect_parser_rejects_runner_shapes() -> None:
    with pytest.raises(pairing.PairingError, match="Invalid OpenAdapt connect link"):
        pairing.parse_connect_uri(VALID_URI)
    with pytest.raises(pairing.PairingError):
        pairing.parse_connect_uri(
            f"openadapt://connect?pack={PACK}&bind={BIND}&origin={ORIGIN}"
        )


def test_parser_rejects_malformed_missing_duplicate_and_unknown_fields() -> None:
    bad = (
        "",
        "openadapt://runner?pack",
        f"openadapt://runner?pack=short&bind={BIND}&origin={ORIGIN}",
        f"openadapt://runner?pack={PACK}&bind={BIND}",
        f"openadapt://runner?pack={PACK}&bind={BIND}&bind={BIND}&origin={ORIGIN}",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin={ORIGIN}&command=whoami",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https://preview.openadapt.ai",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https://openadapt.ai/",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=https://openadapt.ai:443",
        f"openadapt://runner?pack={PACK}&bind={BIND}&origin=http://openadapt.ai",
    )
    for uri in bad:
        with pytest.raises(RunnerBindError):
            parse_runner_uri(uri)


def test_prefix_parsers_reject_foreign_and_swapped_encodings() -> None:
    oar = "oar_" + "a" * 64
    oap = "oap_" + "A" * 43
    oab_hex = "oab_" + "a" * 64
    oals_b64 = "oals_" + "A" * 43
    oa_prefix = "oa" + "A" * 43
    oab = BIND
    oals = "oals_" + "a" * 64

    assert valid_bind_token(oab) is True
    assert valid_lease_secret(oals) is True
    assert valid_pack_id(PACK) is True
    assert valid_pack_id("v1." + "A" * 48) is True

    for value in (oar, oap, oab_hex, oals_b64, oals, oa_prefix, PACK):
        assert valid_bind_token(value) is False, value
    for value in (oar, oap, oab, oab_hex, oals_b64, oa_prefix, PACK):
        assert valid_lease_secret(value) is False, value
    for value in (oar, oap, oab, oals, oa_prefix, "p.short", "v1.short"):
        assert valid_pack_id(value) is False, value

    for bind in (oar, oap, oab_hex, oals_b64, oa_prefix):
        uri = f"openadapt://runner?pack={PACK}&bind={bind}&origin={ORIGIN}"
        with pytest.raises(RunnerBindError, match="Bind token is malformed"):
            parse_runner_uri(uri)


def test_argument_shaped_values_remain_data_and_cannot_select_an_action() -> None:
    for payload in (
        "--origin=https://evil.example",
        "%2D%2Dorigin%3Dhttps%3A%2F%2Fevil.example",
        "oab_" + "A" * 42 + ";",
    ):
        uri = f"openadapt://runner?pack={PACK}&bind={payload}&origin={ORIGIN}"
        with pytest.raises(RunnerBindError, match="Bind token is malformed"):
            parse_runner_uri(uri)
