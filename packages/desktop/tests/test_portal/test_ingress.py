"""The portal is loopback-only until a customer explicitly publishes it."""

from __future__ import annotations

import pytest

from engine.portal.ingress import (
    LOOPBACK_HOST,
    WILDCARD_BIND_HOSTS,
    IngressError,
    resolve_ingress,
)


class Config:
    """A minimal stand-in for EngineConfig's portal fields."""

    def __init__(self, **overrides: object) -> None:
        self.portal_ingress_mode = "loopback"
        self.portal_public_origin = ""
        self.portal_bind_host = ""
        self.portal_ingress_acknowledged = False
        self.portal_port = 0
        for key, value in overrides.items():
            setattr(self, key, value)


def test_default_configuration_binds_loopback_and_admits_no_phone() -> None:
    ingress = resolve_ingress(Config(portal_port=8890))
    assert ingress.mode == "loopback"
    assert ingress.bind_host == LOOPBACK_HOST
    assert ingress.loopback_only is True
    # The honest consequence of the safe default: a phone cannot reach it, and
    # the pairing surface says so rather than minting a link that fails.
    assert ingress.reachable_from_phone is False
    assert ingress.public_origin == "http://127.0.0.1:8890"


def test_publishing_beyond_loopback_requires_an_explicit_acknowledgement() -> None:
    config = Config(
        portal_ingress_mode="customer_ingress",
        portal_public_origin="https://openadapt.clinic.example",
    )
    with pytest.raises(IngressError, match="acknowledged"):
        resolve_ingress(config)
    config.portal_ingress_acknowledged = True
    ingress = resolve_ingress(config)
    assert ingress.reachable_from_phone is True
    # Even when published, the socket stays on loopback: the customer's own
    # reverse proxy forwards to it.
    assert ingress.bind_host == LOOPBACK_HOST


def test_customer_ingress_without_an_origin_fails_closed() -> None:
    with pytest.raises(IngressError, match="portal_public_origin"):
        resolve_ingress(
            Config(
                portal_ingress_mode="customer_ingress",
                portal_ingress_acknowledged=True,
            )
        )


@pytest.mark.parametrize(
    "origin",
    [
        "http://openadapt.clinic.example",
        "openadapt.clinic.example",
        "https://openadapt.clinic.example/portal",
        "https://user:pw@openadapt.clinic.example",
        "https://openadapt.clinic.example?x=1",
        "https://openadapt.clinic.example#f",
    ],
)
def test_only_a_bare_https_origin_is_accepted(origin: str) -> None:
    with pytest.raises(IngressError):
        resolve_ingress(
            Config(
                portal_ingress_mode="customer_ingress",
                portal_ingress_acknowledged=True,
                portal_public_origin=origin,
            )
        )


@pytest.mark.parametrize("wildcard", sorted(WILDCARD_BIND_HOSTS - {""}))
def test_a_wildcard_bind_address_is_always_refused(wildcard: str) -> None:
    with pytest.raises(IngressError, match="wildcard|unspecified"):
        resolve_ingress(
            Config(
                portal_ingress_mode="customer_ingress",
                portal_ingress_acknowledged=True,
                portal_public_origin="https://openadapt.clinic.example",
                portal_bind_host=wildcard,
            )
        )


def test_a_specific_bind_address_is_allowed_only_with_published_ingress() -> None:
    ingress = resolve_ingress(
        Config(
            portal_ingress_mode="customer_ingress",
            portal_ingress_acknowledged=True,
            portal_public_origin="https://openadapt.clinic.example",
            portal_bind_host="10.4.2.9",
        )
    )
    assert ingress.bind_host == "10.4.2.9"
    assert ingress.loopback_only is False

    # The same address under the loopback default is a configuration error,
    # not a silent widening.
    with pytest.raises(IngressError, match="loopback"):
        resolve_ingress(Config(portal_bind_host="10.4.2.9"))


def test_a_hostname_is_not_an_acceptable_bind_address() -> None:
    with pytest.raises(IngressError, match="literal IP address"):
        resolve_ingress(
            Config(
                portal_ingress_mode="customer_ingress",
                portal_ingress_acknowledged=True,
                portal_public_origin="https://openadapt.clinic.example",
                portal_bind_host="runner.clinic.example",
            )
        )


def test_an_unknown_mode_fails_closed() -> None:
    with pytest.raises(IngressError, match="Unknown portal ingress mode"):
        resolve_ingress(Config(portal_ingress_mode="lan"))


def test_the_pairing_secret_rides_in_the_url_fragment() -> None:
    ingress = resolve_ingress(
        Config(
            portal_ingress_mode="customer_ingress",
            portal_ingress_acknowledged=True,
            portal_public_origin="https://openadapt.clinic.example",
        )
    )
    url = ingress.pairing_url("oapp_" + "A" * 43)
    # A fragment is never transmitted in an HTTP request, so the one-use secret
    # cannot land in a proxy access log or a referrer header.
    assert url.startswith("https://openadapt.clinic.example/pair#c=")
    assert "?" not in url


def test_describe_never_exposes_a_secret() -> None:
    described = resolve_ingress(Config()).describe()
    assert set(described) == {
        "mode",
        "bind_host",
        "loopback_only",
        "public_origin",
        "reachable_from_phone",
    }
