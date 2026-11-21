"""``openadapt login`` through the browser (RFC 8252 loopback with S256 PKCE).

The "browser" here is a real HTTP request to the real loopback listener, made
from ``show_url`` after parsing the printed sign-in URL. Cloud's claim,
validation and confirmation calls are faked at ``httpx``; the keychain is
faked at the ``hosted`` seam, as in ``test_hosted.py``.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import threading
import urllib.error
import urllib.parse
import urllib.request

import httpx
import pytest

from openadapt_flow import __main__ as cli
from openadapt_flow import browser_login as bl
from openadapt_flow import hosted

PAIRING = "oap_" + "A" * 43
PAIRING_ID = "11111111-1111-4111-8111-111111111111"
TOKEN = "oai_ingest_" + "b" * 40
HOST = "https://app.openadapt.ai"  # httpx is faked; nothing leaves the test


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path))
    monkeypatch.delenv(hosted.TOKEN_ENV, raising=False)
    store: dict[str, str | None] = {"canonical": None}
    monkeypatch.setattr(hosted, "_keyring_available", lambda: True)
    monkeypatch.setattr(hosted, "_keyring_token", lambda host: store["canonical"])
    monkeypatch.setattr(
        hosted, "_snapshot_keyring_token", lambda host: (True, store["canonical"])
    )
    monkeypatch.setattr(hosted, "_store_staged_keyring_token", lambda h, p, t: True)
    monkeypatch.setattr(hosted, "_delete_staged_keyring_token", lambda h, p: True)

    def keep(host, token):
        store["canonical"] = token
        return True

    monkeypatch.setattr(hosted, "_store_keyring_token", keep)
    return store


@pytest.fixture()
def cloud(monkeypatch):
    """Fake Cloud: records claim bodies and enforces the PKCE binding."""

    seen: dict = {"claims": [], "challenge": None}

    def post(url, **kwargs):
        if url.endswith("/pairings/claim"):
            body = kwargs["json"]
            seen["claims"].append(body)
            verifier = body.get("code_verifier")
            if verifier is None:
                return httpx.Response(410, json={"error": "proof required"})
            digest = hashlib.sha256(verifier.encode()).digest()
            if (
                base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                != seen["challenge"]
            ):
                return httpx.Response(410, json={"error": "proof mismatch"})
            return httpx.Response(
                201,
                json={"paired": True, "pairing_id": PAIRING_ID, "ingest_token": TOKEN},
            )
        if url.endswith("/pairings/confirm"):
            return httpx.Response(200, json={"connected": True})
        raise AssertionError(f"unexpected POST {url}")

    def get(url, **kwargs):
        assert kwargs["headers"]["Authorization"] == f"Bearer {TOKEN}"
        return httpx.Response(200, json={"count": 0})

    monkeypatch.setattr(httpx, "post", post)
    monkeypatch.setattr(httpx, "get", get)
    return seen


def _browser(cloud, *, reply=None, state_override=None):
    """A ``show_url`` that plays the browser: sign in, approve, redirect."""

    printed: list[str] = []

    def show(url: str) -> None:
        printed.append(url)
        parsed = urllib.parse.urlparse(url)
        query = dict(urllib.parse.parse_qsl(parsed.query))
        cloud["challenge"] = query["code_challenge"]
        params = reply if reply is not None else {"code": PAIRING}
        params = {**params, "state": state_override or query["state"]}
        target = f"{query['redirect_to']}?{urllib.parse.urlencode(params)}"

        def visit() -> None:
            with urllib.request.urlopen(target, timeout=5) as response:  # noqa: S310
                response.read()

        threading.Thread(target=visit, daemon=True).start()

    return show, printed


def test_signs_in_and_keeps_the_credential_in_the_keychain(cloud, _isolated):
    show, printed = _browser(cloud)
    result = bl.browser_login(HOST, show_url=show, timeout=5)

    assert result["paired"] is True and result["token_storage"] == "keyring"
    assert _isolated["canonical"] == TOKEN
    (url,) = printed
    parsed = urllib.parse.urlparse(url)
    query = dict(urllib.parse.parse_qsl(parsed.query))
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == f"{HOST}/login"
    assert set(query) == {
        "redirect_to",
        "code_challenge",
        "code_challenge_method",
        "state",
    }
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_to"].startswith("http://127.0.0.1:")
    assert query["redirect_to"].endswith("/callback")
    # The verifier went only to the claim; nothing secret was in the URL.
    (claim,) = cloud["claims"]
    assert claim["pairing_secret"] == PAIRING
    assert claim["code_verifier"] not in url
    assert TOKEN not in url


def test_opens_a_browser_only_when_asked(cloud):
    opened: list[str] = []
    show, printed = _browser(cloud)
    bl.browser_login(HOST, show_url=show, timeout=5)
    assert opened == []

    show, printed = _browser(cloud)
    bl.browser_login(HOST, show_url=show, open_browser=opened.append, timeout=5)
    assert opened == printed


def test_a_mismatched_state_spends_nothing(cloud):
    show, _ = _browser(cloud, state_override="someone-else")
    with pytest.raises(bl.BrowserLoginError, match="did not match"):
        bl.browser_login(HOST, show_url=show, timeout=5)
    assert cloud["claims"] == []


def test_a_refusal_is_reported_and_nothing_is_stored(cloud, _isolated):
    show, _ = _browser(
        cloud, reply={"error": "access_denied", "error_description": "Not approved"}
    )
    with pytest.raises(bl.BrowserLoginError, match="not approved"):
        bl.browser_login(HOST, show_url=show, timeout=5)
    assert cloud["claims"] == [] and _isolated["canonical"] is None


def test_a_malformed_code_is_refused_before_any_claim(cloud):
    show, _ = _browser(cloud, reply={"code": "not-a-pairing"})
    with pytest.raises(bl.BrowserLoginError, match="malformed"):
        bl.browser_login(HOST, show_url=show, timeout=5)
    assert cloud["claims"] == []


def test_times_out_plainly_when_no_one_signs_in(cloud):
    with pytest.raises(bl.BrowserLoginError, match="No sign-in arrived"):
        bl.browser_login(HOST, show_url=lambda url: None, timeout=0.2)
    assert cloud["claims"] == []


def test_the_listener_serves_only_the_callback_on_loopback():
    with bl.LoopbackReceiver() as receiver:
        receiver.start()
        assert receiver.redirect_uri.startswith("http://127.0.0.1:")
        other = receiver.redirect_uri.replace("/callback", "/anything")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(other, timeout=5)  # noqa: S310
        assert caught.value.code == 404
        assert receiver.code is None


def test_connect_sends_the_verifier_only_when_given(cloud):
    verifier, challenge = bl.pkce_pair()
    cloud["challenge"] = challenge
    hosted.connect(PAIRING, host=HOST, code_verifier=verifier)
    assert cloud["claims"][-1]["code_verifier"] == verifier
    with pytest.raises(hosted.HostedError, match="proof is malformed"):
        hosted.connect(PAIRING, host=HOST, code_verifier="short")


def _login_args(**overrides):
    values = dict(
        token=None,
        host=HOST,
        destination_kind=None,
        trusted_host=None,
        no_save=False,
        allow_plaintext_token=False,
        browser=None,
        open=False,
        timeout=None,
    )
    values.update(overrides)
    return argparse.Namespace(**values)


def test_login_picks_the_browser_only_without_a_token(monkeypatch, _isolated):
    assert bl.uses_browser(_login_args()) is True
    assert bl.uses_browser(_login_args(token=TOKEN)) is False
    assert bl.uses_browser(_login_args(browser=False)) is False
    monkeypatch.setenv(hosted.TOKEN_ENV, TOKEN)
    assert bl.uses_browser(_login_args()) is False
    assert bl.uses_browser(_login_args(browser=True)) is True
    monkeypatch.delenv(hosted.TOKEN_ENV)
    _isolated["canonical"] = TOKEN
    # A stored token is re-validated, as before this change.
    assert bl.uses_browser(_login_args()) is False


def test_login_command_prints_the_link_and_never_the_secret(cloud, monkeypatch, capsys):
    def fake_login(host, *, show_url, open_browser, timeout, **kwargs):
        assert open_browser is None and timeout == bl.DEFAULT_TIMEOUT_S
        show_url(f"{HOST}/login?state=x")
        return {"host": HOST, "device_name": "laptop", "settings_url": f"{HOST}/s"}

    monkeypatch.setattr(bl, "browser_login", fake_login)
    assert cli._cmd_login(_login_args()) == 0
    out = capsys.readouterr().out
    assert f"{HOST}/login?state=x" in out
    assert "Add --open" in out
    assert "Signed in to" in out and TOKEN not in out


def test_login_command_reports_a_failure_in_one_line(monkeypatch, capsys):
    def fail(host, **kwargs):
        raise bl.BrowserLoginError("Sign-in was not approved: Not approved")

    monkeypatch.setattr(bl, "browser_login", fail)
    assert cli._cmd_login(_login_args()) == 1
    assert capsys.readouterr().out.strip() == (
        "login failed: Sign-in was not approved: Not approved"
    )
