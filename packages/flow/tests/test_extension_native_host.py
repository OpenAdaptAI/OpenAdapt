"""The extension's native-messaging host, installed and launched by Chrome.

Part of OpenAdapt#1872. ``install_host.py`` registers the host manifest in a
throwaway Chromium profile (``<profile>/NativeMessagingHosts``). Real headless
Chromium loads the unpacked extension, and the service worker's own
``chrome.runtime.connectNative`` makes Chrome start the installed host. Nothing
on the transport is stubbed: content script → service worker → Chrome native
messaging → ``openadapt_extension_host.py`` → ``extension_evidence.jsonl``.

The installer's own rules are checked without a browser at the top.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

EXT = Path(__file__).resolve().parents[1] / "chrome_extension"
INSTALLER = EXT / "native_host" / "install_host.py"
EVIDENCE = "extension_evidence.jsonl"

# Synthetic value typed into a password field during the test.
LIVE_SECRET = "-".join(["hunter2", "native", "host", "live"])
OTHER_ID = "a" * 32

posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="Windows registers native hosts in the registry"
)


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_host", INSTALLER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install_host = _load_installer()


# --- installer rules (no browser) -------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["", "*", "abc", "A" * 32, "q" * 32, "a" * 31, "a" * 33, f"{'a' * 32}/"],
)
def test_installer_refuses_anything_but_one_real_extension_id(tmp_path, bad):
    with pytest.raises(ValueError):
        install_host.install(tmp_path / "hosts", bad, platform="linux")
    assert not (tmp_path / "hosts").exists()


def test_installer_manifest_allows_exactly_one_origin(tmp_path):
    path = install_host.install(
        tmp_path / "hosts", "b" * 32, recording=tmp_path / "rec", platform="linux"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    assert path.name == "com.openadapt.extension.json"
    assert manifest["name"] == "com.openadapt.extension"
    assert manifest["type"] == "stdio"
    assert manifest["allowed_origins"] == [f"chrome-extension://{'b' * 32}/"]
    launcher = Path(manifest["path"])
    assert launcher.is_absolute() and launcher.parent == path.parent
    text = launcher.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\nexec ")
    assert "openadapt_extension_host.py" in text
    assert "--recording" in text and str((tmp_path / "rec").resolve()) in text
    if sys.platform != "win32":
        assert os.stat(launcher).st_mode & 0o777 == 0o700
        assert os.stat(path).st_mode & 0o777 == 0o600


def test_installer_without_recording_passes_none(tmp_path):
    path = install_host.install(tmp_path, "c" * 32, platform="linux")
    launcher = Path(json.loads(path.read_text(encoding="utf-8"))["path"])
    assert "--recording" not in launcher.read_text(encoding="utf-8")


def test_installer_quotes_paths_with_spaces(tmp_path):
    rec = tmp_path / "my rec; rm -rf x"
    path = install_host.install(tmp_path, "d" * 32, recording=rec, platform="linux")
    launcher = Path(json.loads(path.read_text(encoding="utf-8"))["path"])
    assert f"'{rec.resolve()}'" in launcher.read_text(encoding="utf-8")


def test_installer_refuses_windows_and_relative_python(tmp_path):
    with pytest.raises(RuntimeError):
        install_host.install(tmp_path, "e" * 32, platform="win32")
    with pytest.raises(ValueError):
        install_host.install(tmp_path, "e" * 32, python="python3", platform="linux")
    assert list(tmp_path.iterdir()) == []


def test_user_dirs_and_uninstall(tmp_path):
    home = tmp_path / "home"
    assert (
        install_host.user_manifest_dir("chrome", home=home, platform="darwin")
        == home / "Library/Application Support/Google/Chrome/NativeMessagingHosts"
    )
    assert (
        install_host.user_manifest_dir("chromium", home=home, platform="linux")
        == home / ".config/chromium/NativeMessagingHosts"
    )
    with pytest.raises(ValueError):
        install_host.user_manifest_dir("edge", home=home, platform="linux")
    target = tmp_path / "hosts"
    install_host.install(target, "f" * 32, platform="linux")
    assert len(install_host.uninstall(target)) == 2
    assert list(target.iterdir()) == []


# --- real Chromium, installed host ------------------------------------------

_PAGE = b"""<!doctype html><title>native host</title>
<input id="password" name="note" type="password">
<input id="memo" name="memo">
<button id="go">Go</button>"""


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # quiet
        pass

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(_PAGE)))
        self.end_headers()
        self.wfile.write(_PAGE)


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture
def chrome(tmp_path):
    """Headless Chromium with the unpacked extension and a clean environment."""

    sync = pytest.importorskip("playwright.sync_api")
    profile = tmp_path / "profile"
    env = {k: v for k, v in os.environ.items() if k != "OPENADAPT_EXTENSION_RECORDING"}
    with sync.sync_playwright() as playwright:
        try:
            context = playwright.chromium.launch_persistent_context(
                str(profile),
                channel="chromium",
                headless=True,
                env=env,
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                ],
            )
        except Exception as exc:  # noqa: BLE001 - no browser on this machine
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            workers = context.service_workers
            worker = (
                workers[0]
                if workers
                else context.wait_for_event("serviceworker", timeout=15000)
            )
        except Exception as exc:  # noqa: BLE001
            context.close()
            pytest.skip(f"extension service worker did not start: {exc}")
        extension_id = worker.url.split("/")[2]
        assert install_host.EXTENSION_ID.match(extension_id), worker.url
        yield context, worker, profile, extension_id
        context.close()


def _rows(recording: Path) -> list[dict]:
    path = recording / EVIDENCE
    if not path.is_file():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _wait_rows(page, recording: Path, count: int) -> list[dict]:
    for _ in range(100):
        rows = _rows(recording)
        if len(rows) - 1 >= count:
            return rows
        page.wait_for_timeout(100)
    return _rows(recording)


_DIRECT = """async (msg) => {
  const out = {replies: [], error: null};
  const port = chrome.runtime.connectNative('com.openadapt.extension');
  port.onMessage.addListener((m) => out.replies.push(m));
  const closed = new Promise((resolve) => port.onDisconnect.addListener(() => {
    out.error = chrome.runtime.lastError ? chrome.runtime.lastError.message : null;
    resolve();
  }));
  if (msg) port.postMessage(msg);
  await Promise.race([closed, new Promise((r) => setTimeout(r, 4000))]);
  try { port.disconnect(); } catch (e) {}
  return out;
}"""


@posix_only
def test_typed_password_reaches_installed_host_redacted(chrome, server, tmp_path):
    context, worker, profile, extension_id = chrome
    recording = tmp_path / "rec"
    recording.mkdir()
    install_host.install(
        install_host.profile_manifest_dir(profile), extension_id, recording=recording
    )
    page = context.new_page()
    page.goto(server + "/form")
    worker.evaluate("() => __openadaptExtensionBackground.setRecording(true)")
    page.wait_for_timeout(300)  # SET_RECORDING reaches the content script
    page.type("#password", LIVE_SECRET)
    page.type("#memo", "ok")
    page.click("#go")
    rows = _wait_rows(page, recording, len(LIVE_SECRET) + 3)
    assert rows, "the installed host wrote nothing"
    header, lines = rows[0], rows[1:]
    assert header["schema"] == "openadapt.extension-evidence/1"
    assert header["source"] == "chrome_extension"
    secret = [x for x in lines if x.get("id") == "password" and x["event"] == "input"]
    assert len(secret) == len(LIVE_SECRET)
    assert all(x["secret_field"] is True and x["value"] == "<secret>" for x in secret)
    memo = [x for x in lines if x.get("id") == "memo" and x["event"] == "input"]
    assert memo and memo[-1]["value"] == "ok"
    assert any(x["event"] == "click" and x.get("id") == "go" for x in lines)
    on_disk = (recording / EVIDENCE).read_text(encoding="utf-8")
    for i in range(3, len(LIVE_SECRET) + 1):
        assert LIVE_SECRET[:i] not in on_disk


@posix_only
def test_installed_host_redacts_a_live_value_the_extension_lets_through(
    chrome, tmp_path
):
    """Defense in depth: a regressed content script still cannot store a secret."""

    _context, worker, profile, extension_id = chrome
    recording = tmp_path / "rec"
    recording.mkdir()
    install_host.install(
        install_host.profile_manifest_dir(profile), extension_id, recording=recording
    )
    out = worker.evaluate(
        _DIRECT,
        {
            "type": "EXTENSION_EVIDENCE",
            "schema": "openadapt.extension-evidence/1",
            "evidence": {
                "kind": "target",
                "event": "input",
                "tag": "input",
                "id": "pin",
                "name": "note",
                "value": LIVE_SECRET,
            },
        },
    )
    assert out["replies"] and out["replies"][0] == {"ok": True, "count": 1}, out
    rows = _rows(recording)
    assert rows[1]["value"] == "<secret>" and rows[1]["secret_field"] is True
    assert LIVE_SECRET not in (recording / EVIDENCE).read_text(encoding="utf-8")


@posix_only
def test_installed_host_refuses_evidence_kinds_it_cannot_redact(chrome, tmp_path):
    _context, worker, profile, extension_id = chrome
    recording = tmp_path / "rec"
    recording.mkdir()
    install_host.install(
        install_host.profile_manifest_dir(profile), extension_id, recording=recording
    )
    out = worker.evaluate(
        _DIRECT,
        {
            "type": "EXTENSION_EVIDENCE",
            "evidence": {"kind": "cookie_jar", "value": LIVE_SECRET},
        },
    )
    assert out["replies"] and out["replies"][0]["ok"] is False, out
    assert not (recording / EVIDENCE).exists()


@posix_only
def test_host_registered_for_another_extension_is_refused(chrome, tmp_path):
    _context, worker, profile, extension_id = chrome
    assert extension_id != OTHER_ID
    recording = tmp_path / "rec"
    recording.mkdir()
    install_host.install(
        install_host.profile_manifest_dir(profile), OTHER_ID, recording=recording
    )
    out = worker.evaluate(
        _DIRECT,
        {
            "type": "EXTENSION_EVIDENCE",
            "evidence": {"kind": "target", "event": "click", "id": "go"},
        },
    )
    assert out["replies"] == [] and out["error"], out
    assert "forbidden" in out["error"].lower(), out
    assert not (recording / EVIDENCE).exists()


@posix_only
def test_installed_host_without_recording_dir_fails_closed(chrome, tmp_path):
    _context, worker, profile, extension_id = chrome
    install_host.install(install_host.profile_manifest_dir(profile), extension_id)
    out = worker.evaluate(_DIRECT, None)
    assert out["replies"] == [{"ok": False, "error": "no_recording_dir"}], out
    assert list(tmp_path.rglob(EVIDENCE)) == []


@posix_only
def test_nothing_reaches_the_installed_host_while_idle(chrome, server, tmp_path):
    context, worker, profile, extension_id = chrome
    recording = tmp_path / "rec"
    recording.mkdir()
    install_host.install(
        install_host.profile_manifest_dir(profile), extension_id, recording=recording
    )
    page = context.new_page()
    page.goto(server + "/form")
    page.type("#password", LIVE_SECRET)
    page.click("#go")
    page.wait_for_timeout(500)
    state = worker.evaluate(
        "() => ({active: __openadaptExtensionBackground.recording,"
        " queued: __openadaptExtensionBackground.queued.length})"
    )
    assert state == {"active": False, "queued": 0}
    assert not (recording / EVIDENCE).exists()
