from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e import test_windows_hosted_campaign as hosted_campaign


class _Window:
    def __init__(self, pid: int, window_id: int) -> None:
        self.pid = pid
        self.window_id = window_id


class _WindowClient:
    windows: list[_Window] = []
    pinned_windows: list[_Window] | None = None
    foreground: int | None = None
    last_instance: _WindowClient | None = None

    def __init__(self) -> None:
        self.activated = []
        type(self).last_instance = self

    def find_windows(self, *, owner: str, title: str | None) -> list[_Window]:
        assert owner == "powershell"
        assert title is None
        return self.windows

    def find_pinned_window(self, *, owner: str, window_id: int) -> list[_Window]:
        assert owner == "powershell"
        windows = self.pinned_windows
        if windows is None:
            windows = self.windows
        return [window for window in windows if window.window_id == window_id]

    def activate(self, pid: int) -> None:
        self.activated.append(pid)

    def key_window_id(self, pid: int) -> int | None:
        return self.foreground if pid == 1234 else None


def _owner(expected_identity: tuple[str, str, str, str], *, app=None):
    if app is None:
        app = SimpleNamespace(pid=1234, poll=lambda: None)
    owner = object.__new__(hosted_campaign._CampaignHost)
    owner.host = SimpleNamespace(app=app)
    owner.expected_identity = expected_identity
    return owner


def _configure(monkeypatch: pytest.MonkeyPatch, client: type[_WindowClient]) -> None:
    monkeypatch.setattr(
        hosted_campaign.shutil, "which", lambda _: "/windows/powershell.exe"
    )
    monkeypatch.setattr(
        "openadapt_flow.backends.win32_window_client.Win32WindowClient", client
    )


def test_activation_requires_exact_owned_foreground_and_pinned_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64)

    class Client(_WindowClient):
        windows = [_Window(1234, 55), _Window(9988, 77)]
        foreground = 55

    _configure(monkeypatch, Client)
    owner = _owner(expected)
    backend = SimpleNamespace(qualification_environment_identity=lambda: expected)

    owner._restore_pinned_app_foreground(backend)
    assert Client.last_instance is not None
    assert Client.last_instance.activated == [1234]


@pytest.mark.parametrize(
    "windows,pinned_pid,foreground,identity,activated",
    [
        ([], None, 55, ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64), []),
        (
            [_Window(1234, 55), _Window(1234, 56)],
            1234,
            55,
            ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64),
            [],
        ),
        (
            [_Window(1234, 55)],
            1234,
            77,
            ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64),
            [1234],
        ),
        (
            [_Window(1234, 55)],
            1234,
            55,
            ("windowsterminal", "sha256:" + "b" * 64, "s" * 64, "e" * 64),
            [1234],
        ),
        (
            [_Window(1234, 55)],
            1234,
            55,
            ("powershell", "sha256:" + "b" * 64, "s" * 64, "e" * 64),
            [1234],
        ),
        (
            [_Window(1234, 55)],
            9988,
            55,
            ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64),
            [],
        ),
    ],
)
def test_activation_refuses_missing_ambiguous_or_mismatching_target(
    monkeypatch: pytest.MonkeyPatch,
    windows: list[_Window],
    pinned_pid: int | None,
    foreground: int,
    identity: tuple[str, str, str, str],
    activated: list[int],
) -> None:
    expected = ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64)

    class Client(_WindowClient):
        pass

    Client.windows = windows
    Client.foreground = foreground
    Client.pinned_windows = (
        None
        if not windows or pinned_pid is None
        else [_Window(pinned_pid, windows[0].window_id)]
    )
    _configure(monkeypatch, Client)
    owner = _owner(expected)
    backend = SimpleNamespace(qualification_environment_identity=lambda: identity)

    with pytest.raises(RuntimeError):
        owner._restore_pinned_app_foreground(backend)
    assert Client.last_instance is not None
    assert Client.last_instance.activated == activated


def test_activation_refuses_if_owned_process_exits_before_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = ("powershell", "sha256:" + "a" * 64, "s" * 64, "e" * 64)

    class Client(_WindowClient):
        windows = [_Window(1234, 55)]
        foreground = 55

    _configure(monkeypatch, Client)
    owner = _owner(
        expected, app=SimpleNamespace(pid=1234, poll=Mock(side_effect=[None, 0]))
    )
    backend = SimpleNamespace(qualification_environment_identity=lambda: expected)

    with pytest.raises(RuntimeError):
        owner._restore_pinned_app_foreground(backend)
    assert Client.last_instance is not None
    assert Client.last_instance.activated == []
