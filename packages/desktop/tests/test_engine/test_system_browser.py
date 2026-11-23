"""Default-handler handoff uses no GUI and no fork of the threaded engine."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import webbrowser
from pathlib import Path

import pytest

from openadapt_desktop import system_browser
from openadapt_desktop.auth.browser_pkce import BrowserPkceProvider, _LoopbackReceiver

pytestmark = pytest.mark.usefixtures("python_subprocess_defaults")
URL = "https://example.test/login?state=synthetic&code_challenge=public"


@pytest.fixture(autouse=True)
def refuse_gui(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("stdlib browser controller must not launch a GUI or fork")

    monkeypatch.setattr(webbrowser, "open", refuse)
    monkeypatch.setattr(os, "startfile", refuse, raising=False)


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_default_handler_receives_one_literal_url(platform, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("BROWSER", "sh -c unsafe-command %s")
    monkeypatch.setattr(os, "access", lambda *_: True)
    xdg_open = "/usr/bin/xdg-open"
    monkeypatch.setattr(system_browser.shutil, "which", lambda _: xdg_open)
    calls = []
    monkeypatch.setattr(os, "startfile", lambda *args: calls.append(args), raising=False)
    monkeypatch.setattr(
        system_browser.child_process, "run", lambda *args, **kwargs: calls.append((args, kwargs))
    )
    url = URL + "&literal=$(echo%20no);quoted=%22value%22"
    assert system_browser.is_available()
    system_browser.open_url(url)
    if platform == "win32":
        assert calls == [(url,)]
    else:
        # linux uses os.path.abspath(shutil.which("xdg-open")); abspath of a
        # POSIX path is host-dependent (on Windows, /usr/bin/xdg-open becomes
        # D:\usr\bin\xdg-open). Match that, rather than a hardcoded POSIX path.
        command = ["/usr/bin/open", "--"] if platform == "darwin" else [os.path.abspath(xdg_open)]
        assert calls == [
            (
                ([*command, url],),
                dict(
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10.0,
                    check=True,
                ),
            )
        ]


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "openadapt://pair?code=synthetic",
        "https://user:password@example.test/",
        "https://example.test/\nnext",
        " https://example.test/",
        "https://example.test/has space",
        "https://example.test/\x7f",
        "https://example.test:invalid/",
        "https://",
        "--help",
        "https://example.test\\evil/",
    ],
)
def test_invalid_web_urls_refuse_before_any_handler(url, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("invalid URL reached the handler")

    monkeypatch.setattr(system_browser.child_process, "run", refuse)
    monkeypatch.setattr(os, "startfile", refuse, raising=False)
    with pytest.raises(RuntimeError, match="invalid web URL"):
        system_browser.open_url(url)


@pytest.mark.parametrize("platform", ["darwin", "linux", "freebsd", "win32"])
def test_missing_or_unsupported_handler_is_unavailable(platform, monkeypatch):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(os, "access", lambda *_: False)
    monkeypatch.setattr(system_browser.shutil, "which", lambda _: None)
    monkeypatch.delattr(os, "startfile", raising=False)
    assert not system_browser.is_available()
    with pytest.raises(RuntimeError, match="could not open"):
        system_browser.open_url(URL)


@pytest.mark.parametrize(
    "error",
    [
        OSError(URL),
        subprocess.CalledProcessError(1, [URL]),
        subprocess.TimeoutExpired([URL], 10),
    ],
)
def test_launcher_failure_is_bounded_and_does_not_disclose_url(error, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(system_browser, "_command", lambda: ["/synthetic/handler"])

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(system_browser.child_process, "run", fail)
    with pytest.raises(RuntimeError) as caught:
        system_browser.open_url(URL)
    assert URL not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.skipif(
    os.name != "posix", reason="POSIX spawn proof; Windows API is intercepted above"
)
def test_threaded_pkce_default_handoff_uses_real_child_without_fork_or_gui(
    tmp_path: Path, monkeypatch
):
    # This program is an OS-handler substitute, not a browser. It only sends
    # the synthetic callback to the live loopback listener in the parent.
    handler = tmp_path / "synthetic_handler.py"
    handler.write_text(
        "import sys, urllib.parse, urllib.request\n"
        "q = urllib.parse.parse_qs(urllib.parse.urlsplit(sys.argv[1]).query)\n"
        "callback = q['redirect_to'][0] + '?' + urllib.parse.urlencode({"
        "'code': 'oap_' + 'A' * 43, 'state': q['state'][0]})\n"
        "urllib.request.urlopen(callback, timeout=5).read()\n"
    )
    monkeypatch.setattr(system_browser, "_command", lambda: [sys.executable, str(handler)])
    if os.name == "posix" and getattr(subprocess, "_USE_POSIX_SPAWN", False):

        def refuse_fork(*_args, **_kwargs):
            raise AssertionError("default browser handoff forked the threaded engine")

        monkeypatch.setattr(subprocess, "_fork_exec", refuse_fork)
    with _LoopbackReceiver() as receiver:
        # Serve first so this deliberately exercises an already threaded parent;
        # the real OS handler returns before its browser performs the callback.
        receiver.serve_until_code(0)
        provider = BrowserPkceProvider()
        url = provider._build_login_url(
            receiver.redirect_uri, "public-challenge", "synthetic-state"
        )
        provider._default_open_browser(url)
        assert receiver._event.wait(5)
        assert receiver.code == "oap_" + "A" * 43
        assert receiver.state == "synthetic-state"


def test_shared_url_model_covers_each_python_handler(monkeypatch):
    model = json.loads((Path(__file__).parents[1] / "fixtures/system-url-launch.json").read_text())
    for platform in ("darwin", "linux", "win32"):
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(system_browser, "_command", lambda: ["/synthetic/handler"])
        calls = []
        monkeypatch.setattr(system_browser.child_process, "run", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(os, "startfile", lambda *a: calls.append(a))
        for case in model["cases"]:
            before = len(calls)
            if case["web"]:
                system_browser.open_url(case["url"])
            else:
                with pytest.raises(RuntimeError, match="invalid web URL"):
                    system_browser.open_url(case["url"])
            assert len(calls) - before == int(case["web"])
            if case["web"]:
                assert (
                    calls[-1][0] == case["url"]
                    if platform == "win32"
                    else calls[-1][0][-1] == case["url"]
                )


def test_webview_cannot_bypass_the_validated_native_url_handoff():
    desktop = Path(__file__).resolve().parents[2]
    blocked = {
        "shell:allow-open",
        "opener:allow-open-url",
        "opener:allow-open-path",
        "opener:default",
    }
    capabilities = desktop / "src-tauri/capabilities"
    for path in capabilities.glob("*.json"):
        permissions = json.loads(path.read_text())["permissions"]
        identifiers = {
            item if isinstance(item, str) else item["identifier"] for item in permissions
        }
        assert identifiers.isdisjoint(blocked), path
