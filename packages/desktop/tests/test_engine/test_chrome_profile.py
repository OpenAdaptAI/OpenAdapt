"""Desktop-managed Chrome profiles: consent, fail-closed launch, owned close.

No Chrome starts here. Spawning, ``/json/version`` and ``Browser.close`` are
fakes; the real launch runs on the Windows lab and the macOS CI runner
(``tests/test_e2e/test_chrome_profile_real.py``).
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any

import pytest

from openadapt_desktop import chrome_profile as cp
from openadapt_desktop.chrome_profile import ChromeProfileError, ChromeProfileService, notice

GUID = "/devtools/browser/0b5e6c2e-1111-2222-3333-444455556666"


class FakeProcess:
    _next_pid = 4000

    def __init__(self, args: list[str], *, exit_now: bool = False) -> None:
        FakeProcess._next_pid += 1
        self.pid = FakeProcess._next_pid
        self.args = args
        self.returncode: int | None = 0 if exit_now else None
        self.log: list[str] = []

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("chrome", timeout or 0)
        return self.returncode

    def terminate(self) -> None:
        self.log.append("terminate")
        self.returncode = -15

    def kill(self) -> None:
        self.log.append("kill")
        self.returncode = -9


class Harness:
    def __init__(self, tmp_path: Path, **overrides: Any) -> None:
        self.tmp_path = tmp_path
        self.spawned: list[FakeProcess] = []
        self.closed: list[tuple[int, str]] = []
        self.port = 51234
        self.version: dict[str, Any] = {
            "Browser": "Chrome/158.0.8127.36",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}{GUID}",
        }
        self.write_port = True
        self.exit_now = False
        self.close_exits = True
        # Page targets Chrome reports on /json/list; the launch tab takes the
        # launch URL unless a test says otherwise.
        self.launch_tab_url: str | None = None
        self.targets: list[dict[str, Any]] = []
        self.created: list[str] = []
        self.new_tab_url: str | None = None
        chrome = tmp_path / "Google Chrome"
        chrome.write_text("")
        self.chrome = chrome
        kwargs: dict[str, Any] = {
            "platform": "darwin",
            "env": {},
            "find": lambda: self.chrome,
            "spawn": self._spawn,
            "json_version": self._json_version,
            "browser_close": self._browser_close,
            "json_list": self._json_list,
            "json_new": self._json_new,
            "sleep": lambda _s: None,
            "start_timeout": 0.5,
        }
        kwargs.update(overrides)
        self.service = ChromeProfileService(tmp_path / "data", **kwargs)

    def _spawn(self, args: list[str], **kwargs: Any) -> FakeProcess:
        assert kwargs["stdin"] is subprocess.DEVNULL
        process = FakeProcess(args, exit_now=self.exit_now)
        self.spawned.append(process)
        user_data = Path(next(a for a in args if a.startswith("--user-data-dir=")).split("=", 1)[1])
        if self.write_port and not self.exit_now:
            (user_data / "DevToolsActivePort").write_text(f"{self.port}\n{GUID}\n")
        launch = args[-1] if self.launch_tab_url is None else self.launch_tab_url
        self.targets.append({"id": f"T{len(self.targets)}", "type": "page", "url": launch})
        return process

    def _json_list(self, port: int) -> list[dict[str, Any]]:
        assert port == self.port
        return [dict(t) for t in self.targets]

    def _json_new(self, port: int, url: str) -> dict[str, Any]:
        assert port == self.port
        self.created.append(url)
        target = {
            "id": f"N{len(self.created)}",
            "type": "page",
            "url": url if self.new_tab_url is None else self.new_tab_url,
        }
        self.targets.append(target)
        return dict(target)

    def _json_version(self, port: int) -> dict[str, Any]:
        assert port == self.port
        return dict(self.version)

    def _browser_close(self, port: int, path: str) -> None:
        self.closed.append((port, path))
        if self.close_exits:
            for process in self.spawned:
                process.returncode = 0

    def create(self, name: str = "Work portals") -> str:
        n = notice()
        return self.service.create(name, n["sha256"], True)["profile_id"]


@pytest.fixture
def h(tmp_path: Path) -> Harness:
    return Harness(tmp_path)


# ------------------------------------------------------------------ consent


def test_notice_hash_is_stable_and_names_the_rules() -> None:
    n = notice()
    assert n == notice()
    assert re.fullmatch(r"[0-9a-f]{64}", n["sha256"])
    text = n["text"]
    for phrase in ("separate Chrome profile", "never reads, copies, or exports", "keychain"):
        assert phrase in text


@pytest.mark.parametrize(
    ("sha", "ack"), [("0" * 64, True), (None, True), ("current", False), ("current", None)]
)
def test_create_requires_the_current_notice_and_an_explicit_yes(h: Harness, sha, ack) -> None:
    sha = notice()["sha256"] if sha == "current" else sha
    with pytest.raises(ChromeProfileError) as err:
        h.service.create("Work", sha, ack)
    assert err.value.code == "consent_required"
    assert h.service.list()["profiles"] == []


def test_create_records_consent_and_a_private_empty_directory(h: Harness) -> None:
    profile_id = h.create()
    listed = h.service.list()["profiles"]
    assert [p["profile_id"] for p in listed] == [profile_id]
    assert listed[0]["consent_current"] is True
    assert listed[0]["open"] is False
    user_data = h.service.root / profile_id / "user-data"
    assert user_data.is_dir() and list(user_data.iterdir()) == []
    if os.name == "posix":
        assert user_data.stat().st_mode & 0o077 == 0
        assert h.service.root.stat().st_mode & 0o077 == 0


def test_a_changed_notice_refuses_open_until_agreed_again(h: Harness, monkeypatch) -> None:
    profile_id = h.create()
    monkeypatch.setattr(cp, "NOTICE_TEXT", cp.NOTICE_TEXT + "- A new rule.\n")
    assert h.service.list()["profiles"][0]["consent_current"] is False
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "consent_required"
    assert h.spawned == []
    h.service.acknowledge(profile_id, notice()["sha256"], True)
    assert h.service.open(profile_id)["open"] is True


def test_unknown_profile_is_refused(h: Harness) -> None:
    with pytest.raises(ChromeProfileError) as err:
        h.service.open("cp-deadbeef")
    assert err.value.code == "profile_unknown"


# --------------------------------------------------------------------- open


def test_open_launches_official_chrome_on_the_owned_directory(h: Harness) -> None:
    profile_id = h.create()
    result = h.service.open(profile_id, "https://portal.example/start")
    assert result == {
        "profile_id": profile_id,
        "open": True,
        "browser_cdp_endpoint": f"http://127.0.0.1:{h.port}",
    }
    (process,) = h.spawned
    user_data = h.service.root / profile_id / "user-data"
    assert process.args == [
        str(h.chrome),
        f"--user-data-dir={user_data}",
        "--remote-debugging-port=0",
        "--no-first-run",
        "--no-default-browser-check",
        "https://portal.example/start",
    ]
    # Desktop passes nothing that touches the keychain or the password store.
    assert not any("keychain" in a or "password-store" in a for a in process.args)
    assert h.service.list()["profiles"][0]["browser_cdp_endpoint"] == result["browser_cdp_endpoint"]


def test_open_keeps_the_launch_tab_when_it_is_at_the_url(h: Harness) -> None:
    h.service.open(h.create(), "https://portal.example/start")
    assert h.created == []


def test_open_keeps_a_launch_tab_that_redirected_within_the_origin(h: Harness) -> None:
    h.launch_tab_url = "https://portal.example/home"
    h.service.open(h.create(), "https://portal.example/login")
    assert h.created == []


def test_open_opens_a_tab_at_the_url_when_the_launch_tab_stayed_blank(h: Harness) -> None:
    h.launch_tab_url = "about:blank"
    result = h.service.open(h.create(), "https://portal.example/start?next=%2Fhome")
    assert result["open"] is True
    assert h.created == ["https://portal.example/start?next=%2Fhome"]


def test_a_new_tab_redirected_to_another_origin_counts_as_landed(h: Harness) -> None:
    h.launch_tab_url = "chrome://newtab/"
    h.new_tab_url = "https://sso.example/authorize"
    assert h.service.open(h.create(), "https://portal.example/start")["open"] is True
    assert h.created == ["https://portal.example/start"]


def test_a_page_that_never_loads_is_refused_and_the_window_closed(h: Harness) -> None:
    h.launch_tab_url = "about:blank"
    h.new_tab_url = "about:blank"
    profile_id = h.create()
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id, "https://portal.example/start")
    assert err.value.code == "page_not_loaded"
    assert h.created == ["https://portal.example/start"]  # one tab, not one per poll
    assert h.spawned[0].log == ["terminate"]
    assert h.service.list()["profiles"][0]["open"] is False


def test_an_unreachable_target_list_is_refused(tmp_path: Path) -> None:
    def boom(_port: int) -> list:
        raise ConnectionRefusedError

    h = Harness(tmp_path, json_list=boom)
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(h.create(), "https://portal.example/start")
    assert err.value.code == "page_not_loaded"


def test_open_without_a_url_checks_no_page(tmp_path: Path) -> None:
    def never(*_a: Any) -> Any:
        raise AssertionError("no page check without a URL")

    h = Harness(tmp_path, json_list=never, json_new=never)
    assert h.service.open(h.create())["open"] is True


def test_reopening_an_open_window_with_a_url_lands_on_it(h: Harness) -> None:
    profile_id = h.create()
    first = h.service.open(profile_id)
    assert h.service.open(profile_id, "https://portal.example/next") == first
    assert len(h.spawned) == 1
    assert h.created == ["https://portal.example/next"]


def test_reopening_an_open_window_on_another_page_of_the_origin_opens_it(h: Harness) -> None:
    profile_id = h.create()
    h.service.open(profile_id, "https://portal.example/a")
    assert h.created == []
    h.service.open(profile_id, "https://portal.example/b")
    assert h.created == ["https://portal.example/b"]
    h.service.open(profile_id, "https://portal.example/b#top")
    assert h.created == ["https://portal.example/b"]  # already there


def test_an_open_window_is_left_open_when_its_page_does_not_load(h: Harness) -> None:
    profile_id = h.create()
    h.service.open(profile_id)
    h.new_tab_url = "about:blank"
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id, "https://portal.example/next")
    assert err.value.code == "page_not_loaded"
    assert h.closed == []
    assert h.service.list()["profiles"][0]["open"] is True


def test_json_new_puts_the_percent_encoded_url(monkeypatch) -> None:
    import httpx

    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url)))
        return httpx.Response(200, json={"id": "N1", "type": "page", "url": "x"})

    real = httpx.Client

    def client(**kwargs: Any) -> httpx.Client:
        assert kwargs.get("trust_env") is False
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    assert cp.http_json_new(9222, "https://p.example/a?b=1&c=%2F")["id"] == "N1"
    assert seen == [
        ("PUT", "http://127.0.0.1:9222/json/new?https%3A%2F%2Fp.example%2Fa%3Fb%3D1%26c%3D%252F")
    ]


def test_open_twice_reuses_the_owned_window(h: Harness) -> None:
    profile_id = h.create()
    first = h.service.open(profile_id)
    assert h.service.open(profile_id) == first
    assert len(h.spawned) == 1


def test_open_deletes_a_stale_devtools_file_before_launch(h: Harness) -> None:
    profile_id = h.create()
    user_data = h.service.root / profile_id / "user-data"
    (user_data / "DevToolsActivePort").write_text(f"9222\n{GUID}\n")
    h.write_port = False
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "attach_unverified"
    assert h.spawned[0].log == ["terminate"]


def test_open_refuses_a_bad_url(h: Harness) -> None:
    profile_id = h.create()
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id, "file:///etc/passwd")
    assert err.value.code == "url_invalid"
    assert h.spawned == []


@pytest.mark.skipif(os.name != "posix", reason="SingletonLock is a POSIX symlink")
def test_profile_in_use_is_refused_before_launch_mac(h: Harness) -> None:
    profile_id = h.create()
    user_data = h.service.root / profile_id / "user-data"
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", user_data / "SingletonLock")
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "profile_in_use"
    assert h.spawned == []


posix_only = pytest.mark.skipif(os.name != "posix", reason="SingletonLock is a POSIX symlink")


@posix_only
def test_a_stale_lock_from_a_dead_process_is_not_in_use(tmp_path: Path) -> None:
    os.symlink(f"{socket.gethostname()}-999999", tmp_path / "SingletonLock")
    assert cp.profile_in_use(tmp_path, "darwin") is False


@posix_only
def test_a_lock_from_another_host_counts_as_in_use(tmp_path: Path) -> None:
    os.symlink("some-other-host-12", tmp_path / "SingletonLock")
    assert cp.profile_in_use(tmp_path, "darwin") is True


def test_windows_lockfile_held_open_counts_as_in_use(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "lockfile").write_text("")

    def refuse(*_a, **_k):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(cp.os, "open", refuse)
    assert cp.profile_in_use(tmp_path, "win32") is True


def test_windows_without_lockfile_is_free(tmp_path: Path) -> None:
    assert cp.profile_in_use(tmp_path, "win32") is False


def test_chrome_exiting_after_launch_is_a_handoff_and_refused(h: Harness) -> None:
    profile_id = h.create()
    h.exit_now = True
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "profile_in_use"
    assert h.service.list()["profiles"][0]["open"] is False


@pytest.mark.parametrize(
    "version",
    [
        {
            "Browser": "Chrome/158",
            "webSocketDebuggerUrl": "ws://127.0.0.1:51234/devtools/browser/other",
        },
        {"Browser": "Chrome/158", "webSocketDebuggerUrl": f"ws://10.0.0.5:51234{GUID}"},
        {"Browser": "Chrome/158", "webSocketDebuggerUrl": f"ws://127.0.0.1:9999{GUID}"},
        {"Browser": "HeadlessChrome/158", "webSocketDebuggerUrl": f"ws://127.0.0.1:51234{GUID}"},
        {},
    ],
)
def test_an_endpoint_that_isnt_ours_is_refused_and_closed(h: Harness, version) -> None:
    profile_id = h.create()
    h.version = version
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "attach_unverified"
    assert h.spawned[0].log == ["terminate"]
    assert h.closed == []  # never Browser.close an endpoint we couldn't verify


def test_unreachable_endpoint_is_refused(tmp_path: Path) -> None:
    def boom(_port: int) -> dict:
        raise ConnectionRefusedError

    h = Harness(tmp_path, json_version=boom)
    profile_id = h.create()
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "attach_unverified"


def test_default_chrome_directories_are_refused(tmp_path: Path, monkeypatch) -> None:
    h = Harness(tmp_path)
    profile_id = h.create()
    monkeypatch.setattr(
        cp, "default_user_data_dirs", lambda *_a, **_k: [h.service.root / profile_id]
    )
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "default_profile_refused"
    assert h.spawned == []


# ---------------------------------------------------------- find chrome


def test_find_chrome_accepts_only_official_chrome(tmp_path: Path, monkeypatch) -> None:
    cft = tmp_path / "Google Chrome for Testing.app" / "chrome"
    chromium = tmp_path / "Chromium.app" / "chrome"
    official = tmp_path / "Google Chrome.app" / "chrome"
    for path in (cft, chromium, official):
        path.parent.mkdir(parents=True)
        path.write_text("")
    monkeypatch.setattr(cp, "chrome_candidates", lambda *_a: [cft, chromium])
    with pytest.raises(ChromeProfileError) as err:
        cp.find_chrome()
    assert err.value.code == "chrome_not_installed"
    monkeypatch.setattr(cp, "chrome_candidates", lambda *_a: [cft, chromium, official])
    assert cp.find_chrome() == official


def test_chrome_candidates_per_platform() -> None:
    win = cp.chrome_candidates(
        "win32", {"PROGRAMFILES": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    )
    assert [str(p).replace("\\", "/") for p in win] == [
        "C:/Program Files/Google/Chrome/Application/chrome.exe",
        "C:/Users/u/AppData/Local/Google/Chrome/Application/chrome.exe",
    ]
    mac = cp.chrome_candidates("darwin", {})
    assert mac[0].as_posix() == "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def test_default_dirs_per_platform() -> None:
    win = cp.default_user_data_dirs("win32", {"LOCALAPPDATA": r"C:\L"})
    assert str(win[0]).replace("\\", "/") == "C:/L/Google/Chrome/User Data"
    mac = cp.default_user_data_dirs("darwin", {})
    assert mac[0].parts[-3:] == ("Application Support", "Google", "Chrome")


# -------------------------------------------------------------------- close


def test_close_sends_browser_close_to_the_owned_endpoint_only(h: Harness) -> None:
    profile_id = h.create()
    h.service.open(profile_id)
    assert h.service.close(profile_id) == {"profile_id": profile_id, "open": False, "closed": True}
    assert h.closed == [(h.port, GUID)]
    assert h.spawned[0].log == []  # it exited from Browser.close; no terminate
    assert h.service.close(profile_id)["closed"] is False


def test_close_falls_back_to_the_handle_when_browser_close_fails(h: Harness) -> None:
    profile_id = h.create()
    h.service.open(profile_id)
    h.close_exits = False
    h.service.close(profile_id)
    assert h.spawned[0].log == ["terminate"]


def test_close_does_not_send_browser_close_when_the_endpoint_changed(h: Harness) -> None:
    profile_id = h.create()
    h.service.open(profile_id)
    h.version = {
        "Browser": "Chrome/158",
        "webSocketDebuggerUrl": "ws://127.0.0.1:51234/devtools/browser/x",
    }
    h.service.close(profile_id)
    assert h.closed == []
    assert h.spawned[0].log == ["terminate"]


def test_close_of_a_profile_desktop_did_not_open_does_nothing(h: Harness) -> None:
    profile_id = h.create()
    assert h.service.close(profile_id)["closed"] is False
    assert h.spawned == [] and h.closed == []


def test_close_all_closes_every_owned_window(h: Harness) -> None:
    a, b = h.create("A"), h.create("B")
    h.service.open(a)
    h.port = 51235
    h.version["webSocketDebuggerUrl"] = f"ws://127.0.0.1:51235{GUID}"
    h.service.open(b)
    h.service.close_all()
    assert all(p.poll() is not None for p in h.spawned)
    assert all(not p["open"] for p in h.service.list()["profiles"])


# ------------------------------------------------------------------- revoke


def test_revoke_refuses_while_open_and_deletes_on_request(h: Harness) -> None:
    profile_id = h.create()
    h.service.open(profile_id)
    with pytest.raises(ChromeProfileError) as err:
        h.service.revoke(profile_id, delete_data=True)
    assert err.value.code == "profile_open"
    h.service.close(profile_id)
    h.service.revoke(profile_id, delete_data=True)
    assert h.service.list()["profiles"] == []
    assert not (h.service.root / profile_id).exists()


def test_revoke_without_delete_keeps_the_folder(h: Harness) -> None:
    profile_id = h.create()
    h.service.revoke(profile_id)
    assert (h.service.root / profile_id / "user-data").is_dir()
    with pytest.raises(ChromeProfileError):
        h.service.open(profile_id)


# --------------------------------------------------------- static boundary


def test_module_never_reads_cookies_passwords_or_the_keychain() -> None:
    source = Path(cp.__file__).read_text(encoding="utf-8")
    code = re.sub(r'""".*?"""', "", source, flags=re.S)
    code = re.sub(r"#.*", "", code)
    for banned in (
        "keyring",
        "Login Data",
        "Cookies",
        ".cookies(",
        "storage_state",
        "getAllCookies",
        "getCookies",
        "use-mock-keychain",
        "password-store",
        "/usr/bin/security",
        "find-generic-password",
        "Local State",
        "copytree",
    ):
        assert banned not in code, banned


def test_devtools_file_parsing(tmp_path: Path) -> None:
    assert cp.read_devtools_active_port(tmp_path) is None
    (tmp_path / "DevToolsActivePort").write_text("abc\n/x\n")
    assert cp.read_devtools_active_port(tmp_path) is None
    (tmp_path / "DevToolsActivePort").write_text(f"51234\n{GUID}")
    assert cp.read_devtools_active_port(tmp_path) == (51234, GUID)


def test_browser_close_frame_is_a_masked_cdp_message_without_origin() -> None:
    import json
    import threading

    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    port = server.getsockname()[1]
    got: dict[str, bytes] = {}

    def serve() -> None:
        conn, _ = server.accept()
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        got["request"] = data
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
        frame = conn.recv(4096)
        got["frame"] = frame
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    cp.send_browser_close(port, GUID)
    thread.join(5)
    server.close()
    request = got["request"].decode()
    assert request.startswith(f"GET {GUID} HTTP/1.1")
    assert "origin:" not in request.lower()
    frame = got["frame"]
    assert frame[0] == 0x81 and frame[1] & 0x80
    length = frame[1] & 0x7F
    mask, body = frame[2:6], frame[6 : 6 + length]
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(body))
    assert json.loads(payload) == {"id": 1, "method": "Browser.close"}


def test_commands_are_window_only() -> None:
    from openadapt_desktop import socket_server

    reachable = set(socket_server._SOCKET_COMMANDS)
    assert not {c for c in reachable if c.startswith("chrome_profile")}


def test_an_elevated_windows_desktop_refuses_before_launch(tmp_path: Path) -> None:
    h = Harness(tmp_path, platform="win32", elevated=lambda: True)
    profile_id = h.create()
    with pytest.raises(ChromeProfileError) as err:
        h.service.open(profile_id)
    assert err.value.code == "elevated_refused"
    assert h.spawned == []


def test_unknown_elevation_counts_as_elevated(monkeypatch) -> None:
    import ctypes

    monkeypatch.delattr(ctypes, "windll", raising=False)
    assert cp.windows_elevated() is True


def test_a_slow_debugging_server_is_retried_until_it_answers(tmp_path: Path) -> None:
    calls: list[int] = []

    def slow(port: int) -> dict:
        calls.append(port)
        if len(calls) < 3:
            raise TimeoutError("still starting")
        return {
            "Browser": "Chrome/158",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}{GUID}",
        }

    h = Harness(tmp_path, json_version=slow, start_timeout=5)
    assert h.service.open(h.create())["open"] is True
    assert len(calls) == 3


def test_a_foreign_endpoint_is_not_retried(tmp_path: Path) -> None:
    calls: list[int] = []

    def foreign(port: int) -> dict:
        calls.append(port)
        return {"Browser": "Chrome/158", "webSocketDebuggerUrl": f"ws://127.0.0.1:{port}/x"}

    h = Harness(tmp_path, json_version=foreign, start_timeout=5)
    with pytest.raises(ChromeProfileError):
        h.service.open(h.create())
    assert len(calls) == 1
