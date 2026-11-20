"""The installed engine finds a certificate bundle when Python has none."""

from __future__ import annotations

import ssl
from pathlib import Path

import pytest

from openadapt_desktop import tls_trust


def _defaults(cafile: str | None, capath: str | None):
    return ssl.DefaultVerifyPaths(
        cafile, capath, "SSL_CERT_FILE", "/build/ssl/cert.pem", "SSL_CERT_DIR", "/build/ssl/certs"
    )


@pytest.fixture
def no_default_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # What a frozen engine sees on a user's Mac: the build machine's paths
    # don't exist, so both come back as None.
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _defaults(None, None))


def test_a_missing_default_store_gets_the_system_bundle(
    tmp_path: Path, no_default_paths: None
) -> None:
    bundle = tmp_path / "cert.pem"
    bundle.write_text("-----BEGIN CERTIFICATE-----\n")
    env: dict[str, str] = {}

    chosen = tls_trust.ensure_tls_trust_store(
        env, platform="darwin", candidates=(str(tmp_path / "absent.pem"), str(bundle))
    )

    assert chosen == str(bundle)
    assert env == {"SSL_CERT_FILE": str(bundle)}


def test_certifi_is_the_fallback_when_the_system_has_no_bundle(
    tmp_path: Path, no_default_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "cacert.pem"
    bundled.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setattr(tls_trust, "_certifi_bundle", lambda: str(bundled))
    env: dict[str, str] = {}

    assert tls_trust.ensure_tls_trust_store(env, platform="linux", candidates=()) == str(bundled)
    assert env["SSL_CERT_FILE"] == str(bundled)


def test_an_empty_or_missing_bundle_is_never_chosen(
    tmp_path: Path, no_default_paths: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty.pem"
    empty.write_text("")
    monkeypatch.setattr(tls_trust, "_certifi_bundle", lambda: None)
    env: dict[str, str] = {}

    assert (
        tls_trust.ensure_tls_trust_store(env, platform="darwin", candidates=(str(empty),)) is None
    )
    assert env == {}


@pytest.mark.parametrize("name", ["SSL_CERT_FILE", "SSL_CERT_DIR"])
def test_an_operator_choice_is_kept(tmp_path: Path, no_default_paths: None, name: str) -> None:
    bundle = tmp_path / "cert.pem"
    bundle.write_text("x")
    env = {name: "/corp/roots"}

    assert (
        tls_trust.ensure_tls_trust_store(env, platform="darwin", candidates=(str(bundle),)) is None
    )
    assert env == {name: "/corp/roots"}


def test_working_defaults_are_left_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = tmp_path / "cert.pem"
    bundle.write_text("x")
    monkeypatch.setattr(
        ssl, "get_default_verify_paths", lambda: _defaults("/usr/lib/ssl/cert.pem", None)
    )
    env: dict[str, str] = {}

    assert (
        tls_trust.ensure_tls_trust_store(env, platform="linux", candidates=(str(bundle),)) is None
    )
    assert env == {}


def test_windows_keeps_its_certificate_store(tmp_path: Path, no_default_paths: None) -> None:
    bundle = tmp_path / "cert.pem"
    bundle.write_text("x")
    env: dict[str, str] = {}

    assert (
        tls_trust.ensure_tls_trust_store(env, platform="win32", candidates=(str(bundle),)) is None
    )
    assert env == {}


def test_an_empty_certificate_directory_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty_dir = tmp_path / "certs"
    empty_dir.mkdir()
    bundle = tmp_path / "cert.pem"
    bundle.write_text("x")
    monkeypatch.setattr(ssl, "get_default_verify_paths", lambda: _defaults(None, str(empty_dir)))
    env: dict[str, str] = {}

    chosen = tls_trust.ensure_tls_trust_store(env, platform="darwin", candidates=(str(bundle),))

    assert chosen == str(bundle)
