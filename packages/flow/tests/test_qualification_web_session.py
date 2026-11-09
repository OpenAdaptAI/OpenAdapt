"""The signed-in web session observer identifies real web apps fail-closed."""

from __future__ import annotations

import pytest

from openadapt_flow.qualification_web_session import (
    SIGNED_IN_WEB_SESSION_OBSERVER_ID,
    SignedInWebSessionObserver,
    signed_in_web_session_contract_sha256,
    signed_in_web_session_environment_digest,
    web_origin,
)

SESSION = [{"name": "OpenEMR", "value": "abc123", "domain": "emr.test", "path": "/"}]


class _Context:
    def __init__(self, cookies):
        self.cookies_value = cookies
        self.asked = []

    def cookies(self, urls):
        self.asked.append(urls)
        return self.cookies_value


class _Page:
    def __init__(self, url, cookies, after_url=None):
        self._urls = [url, after_url or url]
        self.context = _Context(cookies)

    @property
    def url(self):
        return self._urls.pop(0) if len(self._urls) > 1 else self._urls[0]


class _Backend:
    def __init__(self, page):
        self.page = page


def _observe(url="https://emr.test/interface/main.php", cookies=SESSION, **kw):
    backend = _Backend(_Page(url, cookies, **kw))
    return SignedInWebSessionObserver("8.0.0.3").observe(backend, "web")


def test_identifies_by_origin_session_and_confirmed_version():
    seen = _observe()
    assert seen.application_identity == "https://emr.test"
    assert seen.application_version == "8.0.0.3"
    assert seen.environment_digest == signed_in_web_session_environment_digest(
        "https://emr.test", "8.0.0.3"
    )
    assert len(seen.session_identity_sha256) == 64


def test_session_digest_changes_with_a_new_login_and_never_holds_the_cookie():
    first = _observe()
    second = _observe(cookies=[dict(SESSION[0], value="other")])
    assert first.session_identity_sha256 != second.session_identity_sha256
    assert "abc123" not in first.model_dump_json()


def test_cookie_order_does_not_change_the_session():
    two = [
        SESSION[0],
        {"name": "lang", "value": "en", "domain": "emr.test", "path": "/"},
    ]
    assert (
        _observe(cookies=two).session_identity_sha256
        == _observe(cookies=list(reversed(two))).session_identity_sha256
    )


@pytest.mark.parametrize(
    "cookies",
    [[], [dict(SESSION[0], value="")], None, [{"name": 1, "value": "x"}]],
)
def test_signed_out_or_malformed_cookies_fail_closed(cookies):
    with pytest.raises(ValueError):
        _observe(cookies=cookies)


def test_navigation_during_observation_fails_closed():
    with pytest.raises(ValueError, match="changed"):
        _observe(after_url="https://other.test/")


@pytest.mark.parametrize("url", ["file:///etc/hosts", "about:blank", None, "https://"])
def test_non_web_addresses_fail_closed(url):
    with pytest.raises(ValueError):
        _observe(url=url)


def test_only_web_targets_and_a_page_are_accepted():
    observer = SignedInWebSessionObserver("1.0")
    with pytest.raises(ValueError):
        observer.observe(_Backend(_Page("https://a.test", SESSION)), "windows")
    with pytest.raises(ValueError):
        observer.observe(object(), "web")


def test_contract_binds_the_confirmed_version():
    a, b = SignedInWebSessionObserver("8.0.0.3"), SignedInWebSessionObserver("8.0.0.4")
    assert a.observer_id == b.observer_id == SIGNED_IN_WEB_SESSION_OBSERVER_ID
    assert a.contract_sha256 != b.contract_sha256
    assert a.contract_sha256 == signed_in_web_session_contract_sha256("8.0.0.3")
    assert signed_in_web_session_environment_digest(
        "https://emr.test", "8.0.0.3"
    ) != signed_in_web_session_environment_digest("https://emr.test", "8.0.0.4")


@pytest.mark.parametrize("version", ["", " 8.0", "x" * 129, None])
def test_invalid_confirmed_versions_are_refused(version):
    with pytest.raises(ValueError):
        SignedInWebSessionObserver(version)


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("HTTPS://EMR.test:443/a?b", "https://emr.test"),
        ("http://emr.test:8080/x", "http://emr.test:8080"),
        ("http://[::1]:3000/", "http://[::1]:3000"),
    ],
)
def test_origin_is_exact(url, origin):
    assert web_origin(url) == origin


def test_environment_digest_needs_an_exact_origin():
    with pytest.raises(ValueError):
        signed_in_web_session_environment_digest("https://emr.test/path", "1")
