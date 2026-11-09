"""Unit coverage for the live RDP qualification's refusal/readiness helpers."""

from __future__ import annotations

import io
import subprocess

import pytest
from PIL import Image, ImageDraw

from tests.e2e import test_parallels_rdp_e2e as rdp


def _completed(stdout: str = "", returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _png(*, background: int, taskbar: bool = False, marker: bool = False) -> bytes:
    image = Image.new("RGB", (320, 200), (background,) * 3)
    draw = ImageDraw.Draw(image)
    if taskbar:
        draw.rectangle((0, 184, 319, 199), fill=(235, 235, 235))
    if marker:
        draw.rectangle((120, 50, 200, 130), fill=(245, 245, 245))
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def test_parse_query_user_preserves_exact_account_session_and_state():
    stdout = (
        " USERNAME              SESSIONNAME        ID  STATE   IDLE TIME  LOGON TIME\n"
        " abrichr               console             1  Active      none   7/14/2026\n"
        ">oaflowq_ab12          rdp-tcp#0           3  Active          .  7/17/2026\n"
        " disconnected                             8  Disc         1:02  7/17/2026\n"
    )
    assert rdp._parse_query_user(stdout) == [
        rdp._UserSession("abrichr", "console", 1, "Active"),
        rdp._UserSession("oaflowq_ab12", "rdp-tcp#0", 3, "Active"),
        rdp._UserSession("disconnected", None, 8, "Disc"),
    ]


def test_parse_query_user_refuses_unknown_row_shape():
    with pytest.raises(ValueError, match="unrecognized query-user row"):
        rdp._parse_query_user("unsafe session output without an id\n")


class _SessionVM:
    def __init__(
        self, query_outputs: list[str], explorer_outputs: list[str] | None = None
    ):
        self.query_outputs = iter(query_outputs)
        self.explorer_outputs = iter(explorer_outputs or [])
        self.commands: list[str] = []

    def exec_cmd(self, command: str, timeout: float = 0):
        self.commands.append(command)
        if command == "query user":
            return _completed(next(self.query_outputs), returncode=1)
        if command.startswith('start "" /b logoff '):
            return _completed()
        if command.startswith("reset session "):
            return _completed()
        raise AssertionError(command)

    def exec_ps(self, command: str, timeout: float = 0):
        assert "Get-Process explorer" in command
        return _completed(next(self.explorer_outputs))


def test_explorer_session_query_accepts_explicit_empty_sentinel():
    vm = _SessionVM([], ["OAFLOW_EXPLORER_IDS=\n"])
    assert rdp._explorer_session_ids(vm) == set()
    rdp._require_no_explorer_sessions(_SessionVM([], ["OAFLOW_EXPLORER_IDS=\n"]))


@pytest.mark.parametrize(
    "output",
    [
        "",
        "3\n",
        "OAFLOW_EXPLORER_IDS=3,not-an-id\n",
        "OAFLOW_EXPLORER_IDS=3\nOAFLOW_EXPLORER_IDS=4\n",
        "OAFLOW_EXPLORER_IDS=3,3\n",
    ],
)
def test_explorer_session_query_refuses_missing_or_malformed_sentinel(output):
    vm = _SessionVM([], [output])
    with pytest.raises(AssertionError):
        rdp._explorer_session_ids(vm)


def test_explorer_session_query_refuses_nonzero_receipt():
    class _NonzeroVM(_SessionVM):
        def exec_ps(self, command: str, timeout: float = 0):
            assert "Get-Process explorer" in command
            return _completed("OAFLOW_EXPLORER_IDS=\n", returncode=1)

    with pytest.raises(AssertionError):
        rdp._explorer_session_ids(_NonzeroVM([]))


def test_no_explorer_proof_refuses_nonempty_sentinel():
    vm = _SessionVM([], ["OAFLOW_EXPLORER_IDS=1,3\n"])
    with pytest.raises(AssertionError, match=r"sessions: \[1, 3\]"):
        rdp._require_no_explorer_sessions(vm)


def test_logoff_preexisting_sessions_uses_only_exact_interactive_ids():
    active = " abrichr               console             1  Active      none   now\n"
    vm = _SessionVM([active, "No User exists for *\n"])
    sessions = rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1)
    assert sessions == [rdp._UserSession("abrichr", "console", 1, "Active")]
    assert vm.commands == ["query user", 'start "" /b logoff 1', "query user"]


def test_logoff_command_timeout_requires_independent_session_disappearance():
    active = " abrichr               console             1  Active      none   now\n"

    class _TimeoutVM(_SessionVM):
        def exec_cmd(self, command: str, timeout: float = 0):
            if command.startswith('start "" /b logoff '):
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, timeout)
            return super().exec_cmd(command, timeout)

    vm = _TimeoutVM([active, "No User exists for *\n"])
    assert rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1) == [
        rdp._UserSession("abrichr", "console", 1, "Active")
    ]
    assert vm.commands == ["query user", 'start "" /b logoff 1', "query user"]


def test_nonzero_reset_receipt_accepts_only_after_session_disappears(monkeypatch):
    active = " abrichr               console             1  Active      none   now\n"

    class _TimeoutVM(_SessionVM):
        def exec_cmd(self, command: str, timeout: float = 0):
            if command.startswith('start "" /b logoff '):
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, timeout)
            if command.startswith("reset session "):
                self.commands.append(command)
                return _completed(returncode=1)
            return super().exec_cmd(command, timeout)

    vm = _TimeoutVM([active, active, "No User exists for *\n"])
    ticks = iter([0.0, 0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(rdp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rdp.time, "sleep", lambda _seconds: None)
    assert rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1) == [
        rdp._UserSession("abrichr", "console", 1, "Active")
    ]
    assert vm.commands[-1] == "query user"
    assert "reset session 1" in vm.commands


def test_nonzero_reset_receipt_refuses_if_session_remains(monkeypatch):
    active = " abrichr               console             1  Active      none   now\n"

    class _TimeoutVM(_SessionVM):
        def exec_cmd(self, command: str, timeout: float = 0):
            if command.startswith('start "" /b logoff '):
                self.commands.append(command)
                raise subprocess.TimeoutExpired(command, timeout)
            if command.startswith("reset session "):
                self.commands.append(command)
                return _completed(returncode=1)
            return super().exec_cmd(command, timeout)

    vm = _TimeoutVM([active, active, active])
    ticks = iter([0.0, 0.0, 2.0, 2.0, 2.0, 4.0])
    monkeypatch.setattr(rdp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rdp.time, "sleep", lambda _seconds: None)
    with pytest.raises(AssertionError, match=r"did not log off: \[1\]"):
        rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1)


def test_logoff_refuses_malformed_query_before_mutation():
    vm = _SessionVM(["unexpected localized or truncated output\n"])
    with pytest.raises(ValueError, match="unrecognized query-user row"):
        rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1)
    assert vm.commands == ["query user"]


def test_logoff_refuses_unrecognized_session_type_before_mutation():
    vm = _SessionVM(
        [" user                  ica-tcp#1           4  Active  none now\n"]
    )
    with pytest.raises(AssertionError, match="unrecognized interactive session"):
        rdp._logoff_preexisting_interactive_sessions(vm, timeout_s=1)
    assert vm.commands == ["query user"]


def test_wait_user_shell_requires_exact_account_and_explorer_session():
    active = " oaflowq_ab12          rdp-tcp#0           3  Active       . now\n"
    vm = _SessionVM([active], ["OAFLOW_EXPLORER_IDS=3\n"])
    assert rdp._wait_user_shell(vm, "OAFLOWQ_AB12", timeout_s=1) == 3


def test_wait_user_shell_refuses_explorer_from_different_session(monkeypatch):
    active = " oaflowq_ab12          rdp-tcp#0           3  Active       . now\n"
    vm = _SessionVM([active], ["OAFLOW_EXPLORER_IDS=1\n"])
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(rdp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rdp.time, "sleep", lambda _seconds: None)
    with pytest.raises(AssertionError, match="explorer_session_ids"):
        rdp._wait_user_shell(vm, "oaflowq_ab12", timeout_s=1)


def test_qualification_desktop_rejects_welcome_and_accepts_light_taskbar():
    welcome = _png(background=18, marker=True)
    desktop = _png(background=0, taskbar=True)
    assert rdp._painted_readiness(welcome) is True
    assert rdp._qualification_desktop_ready(welcome) is False
    assert rdp._qualification_desktop_ready(desktop) is True
    assert (
        rdp._frame_change_fraction(welcome, desktop)
        >= rdp._QUALIFICATION_TRANSITION_FRACTION
    )


class _FrameBackend:
    def __init__(self, frames: list[bytes]):
        self.frames = iter(frames)

    def screenshot(self) -> bytes:
        return next(self.frames)


def test_wait_qualification_desktop_requires_transition_and_stability():
    welcome = _png(background=18, marker=True)
    desktop = _png(background=0, taskbar=True)
    backend = _FrameBackend([welcome, desktop, desktop, desktop])
    assert (
        rdp._wait_qualification_desktop(
            backend,
            welcome,
            timeout_s=1,
            stable_frames=3,
            settle_s=0,
        )
        == desktop
    )


def test_counted_desktop_wait_forwards_exact_evidence_bound_timeout(monkeypatch):
    marker = object()
    calls: list[tuple[object, bytes, float, int]] = []

    def fake_wait(backend, baseline, *, timeout_s, stable_frames, **_kwargs):
        calls.append((backend, baseline, timeout_s, stable_frames))
        return marker

    monkeypatch.setattr(rdp, "_wait_qualification_desktop", fake_wait)
    backend = object()
    baseline = b"baseline"
    assert rdp._wait_counted_qualification_desktop(backend, baseline) is marker
    assert calls == [
        (
            backend,
            baseline,
            rdp._COUNTED_DESKTOP_READINESS_TIMEOUT_S,
            rdp._QUALIFICATION_STABLE_FRAMES,
        )
    ]
    assert rdp._COUNTED_DESKTOP_READINESS_TIMEOUT_S == 75.0


def test_qualification_readiness_report_binding_is_exact():
    assert rdp._qualification_readiness_config() == {
        "target_session": "one exact active RDP account session",
        "explorer": "same exact session id",
        "taskbar_bottom_fraction": 0.08,
        "taskbar_min_luma": 161,
        "taskbar_min_bright_fraction": 0.50,
        "transition_fraction": 0.10,
        "baseline_desktop_ready_bypasses_transition": True,
        "stable_frames": 3,
        "max_stable_change_fraction": 0.02,
        "timeout_s": 75.0,
    }
    assert rdp._QUALIFICATION_READINESS_DESCRIPTION == (
        "one exact active target-account RDP session with Explorer in the same "
        "session id; fixed-VM Windows 11 light taskbar in the bottom 8% with at "
        "least 50% of pixels at luma >=161; framebuffer transition >=0.10 from "
        "the login baseline unless that baseline is already desktop-ready; three "
        "consecutive ready frames with <=0.02 change; 75-second counted timeout"
    )


def test_wait_qualification_desktop_refuses_below_transition_floor(monkeypatch):
    baseline = _png(background=0)
    taskbar_only = _png(background=0, taskbar=True)
    assert rdp._qualification_desktop_ready(taskbar_only) is True
    assert (
        rdp._frame_change_fraction(baseline, taskbar_only)
        < rdp._QUALIFICATION_TRANSITION_FRACTION
    )
    backend = _FrameBackend([taskbar_only])
    ticks = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr(rdp.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(rdp.time, "sleep", lambda _seconds: None)
    with pytest.raises(AssertionError, match="desktop did not become ready"):
        rdp._wait_qualification_desktop(
            backend,
            baseline,
            timeout_s=1,
            stable_frames=3,
            settle_s=0,
        )
