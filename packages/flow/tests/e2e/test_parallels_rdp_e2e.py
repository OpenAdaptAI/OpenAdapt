"""Snapshot-safe, opt-in real RDP protocol qualification on Parallels.

This is deliberately separate from the Citrix/remote-window analog. It opens
exactly three independent network RDP sessions through
:class:`AardwolfTransport`, captures a painted framebuffer, sends a unique
command through RDP keyboard input, then verifies the resulting file through
``prlctl exec``. The oracle is outside the protocol/client surface under test.

Safety:

* skipped unless ``OAFLOW_PARALLELS_RDP_E2E=1``;
* requires the preserved base snapshot to be current before any mutation;
* snapshots before resume/account/firewall changes;
* creates only a random qualification account and Public Documents directory;
* switches back to the preserved base and deletes only the exact
  harness-owned snapshot id, never a pre-existing snapshot or user asset;
* binds the result to exact base/candidate commits and exactly three trials;
* reports silent-incorrect, over-halt, and model-call counts explicitly.

macOS Local Network permission is executable-specific. Run this with a Python
3.10-3.12 interpreter that can connect to the Parallels shared subnet and has
``openadapt-flow[rdp]`` installed.
"""

from __future__ import annotations

import io
import json
import os
import re
import secrets
import socket
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import version as package_version
from pathlib import Path

import pytest
from PIL import Image, ImageChops

RUN = os.environ.get("OAFLOW_PARALLELS_RDP_E2E") == "1"
TRIALS = int(os.environ.get("OAFLOW_RDP_QUAL_TRIALS", "3"))
CANDIDATE_ENV = "OAFLOW_RDP_QUAL_CANDIDATE"
BASE_COMMIT_ENV = "OAFLOW_RDP_QUAL_BASE_COMMIT"
BASE_SNAPSHOT_ENV = "OAFLOW_PARALLELS_BASE_SNAPSHOT_ID"
HOST_STORAGE_PATH_ENV = "OAFLOW_PARALLELS_STORAGE_PATH"
# Fixed-VM evidence: the real Welcome -> Windows desktop transition changed
# 0.124912 of framebuffer pixels. Keep a measured margin while requiring the
# separate taskbar predicate, exact session/Explorer proof, and stable frames.
_QUALIFICATION_TRANSITION_FRACTION = 0.10
# The clean-base proof first recognized the desktop at 35 seconds and reached
# three stable frames at 45 seconds. The counted path gets the full observed
# 75-second diagnostic window rather than the rejected 30-second default.
_COUNTED_DESKTOP_READINESS_TIMEOUT_S = 75.0
_QUALIFICATION_STABLE_FRAMES = 3
_QUALIFICATION_STABLE_CHANGE_FRACTION = 0.02
_QUALIFICATION_TASKBAR_BOTTOM_FRACTION = 0.08
_QUALIFICATION_TASKBAR_MIN_LUMA = 161
_QUALIFICATION_TASKBAR_BRIGHT_FRACTION = 0.50
_QUALIFICATION_READINESS_DESCRIPTION = (
    "one exact active target-account RDP session with Explorer in the same "
    "session id; fixed-VM Windows 11 light taskbar in the bottom 8% with at "
    "least 50% of pixels at luma >=161; framebuffer transition >=0.10 from "
    "the login baseline unless that baseline is already desktop-ready; three "
    "consecutive ready frames with <=0.02 change; 75-second counted timeout"
)

pytestmark = [
    pytest.mark.skipif(
        not RUN,
        reason="opt-in live RDP qualification; set OAFLOW_PARALLELS_RDP_E2E=1",
    ),
    pytest.mark.timeout(1200),
]


def _wait_guest(vm, *, timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            result = vm.exec_cmd("echo OAFLOW_RDP_READY", timeout=20)
            if result.returncode == 0 and "OAFLOW_RDP_READY" in result.stdout:
                return
        except Exception:  # noqa: BLE001 - bounded readiness polling
            pass
        time.sleep(3)
    raise AssertionError("Parallels guest tools did not become ready")


def _wait_tcp(host: str, port: int, *, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=3):
                return
        except OSError as exc:
            last_error = exc
            time.sleep(1)
    raise AssertionError(f"RDP listener {host}:{port} unreachable: {last_error}")


@dataclass(frozen=True)
class _UserSession:
    username: str
    session_name: str | None
    session_id: int
    state: str


_QUERY_USER_ROW = re.compile(
    r"^\s*>?(?P<username>\S+)\s+"
    r"(?:(?P<session_name>\S+)\s+)?"
    r"(?P<session_id>\d+)\s+"
    r"(?P<state>Active|Disc|Conn|Listen|Down|Idle)\b",
    re.IGNORECASE,
)
_EXPLORER_IDS_SENTINEL = "OAFLOW_EXPLORER_IDS="


def _parse_query_user(stdout: str) -> list[_UserSession]:
    """Parse ``query user`` without trusting its unreliable exit status."""
    sessions: list[_UserSession] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped or stripped.upper().startswith("USERNAME"):
            continue
        if stripped.casefold().startswith("no user exists"):
            continue
        match = _QUERY_USER_ROW.match(line)
        if match is None:
            raise ValueError(f"unrecognized query-user row: {stripped!r}")
        sessions.append(
            _UserSession(
                username=match.group("username"),
                session_name=match.group("session_name"),
                session_id=int(match.group("session_id")),
                state=match.group("state"),
            )
        )
    return sessions


def _query_user_sessions(vm) -> list[_UserSession]:
    # On this Parallels Windows 11 guest, query.exe returns rc=1 even while
    # emitting a valid table. Parse its stdout and reject unknown row shapes.
    result = vm.exec_cmd("query user", timeout=20)
    return _parse_query_user(result.stdout or "")


def _explorer_session_ids(vm) -> set[int]:
    result = vm.exec_ps(
        "$ids = @(Get-Process explorer -ErrorAction SilentlyContinue | "
        "ForEach-Object { $_.SessionId }); "
        "Write-Output ('OAFLOW_EXPLORER_IDS=' + ($ids -join ',')); exit 0",
        timeout=20,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    lines = [
        line.strip() for line in (result.stdout or "").splitlines() if line.strip()
    ]
    if len(lines) != 1 or not lines[0].startswith(_EXPLORER_IDS_SENTINEL):
        raise AssertionError("missing or ambiguous Explorer session sentinel")
    payload = lines[0][len(_EXPLORER_IDS_SENTINEL) :]
    if not payload:
        return set()
    if re.fullmatch(r"\d+(?:,\d+)*", payload) is None:
        raise AssertionError("could not safely parse Explorer session ids")
    ids = [int(value) for value in payload.split(",")]
    if len(ids) != len(set(ids)):
        raise AssertionError("duplicate Explorer session ids")
    return set(ids)


def _require_no_explorer_sessions(vm) -> None:
    ids = _explorer_session_ids(vm)
    if ids:
        raise AssertionError(f"Explorer still exists in sessions: {sorted(ids)}")


def _logoff_preexisting_interactive_sessions(
    vm, *, timeout_s: float = 30.0
) -> list[_UserSession]:
    """Log off pre-existing console/RDP users inside the owned snapshot only."""
    sessions = _query_user_sessions(vm)
    ids: set[int] = set()
    for session in sessions:
        name = (session.session_name or "").casefold()
        if session.session_id <= 0 or not (
            not name or name == "console" or name.startswith("rdp-")
        ):
            raise AssertionError(
                f"refusing to log off an unrecognized interactive session: {session!r}"
            )
        ids.add(session.session_id)
    for session_id in sorted(ids):
        try:
            # Detach logoff from the Parallels guest-tools command channel.
            # An attached `logoff` can destroy its session while prlctl still
            # waits for command completion, then time out or cancel the guest
            # process. The exact query-user disappearance proof below remains
            # the only success oracle.
            result = vm.exec_cmd(f'start "" /b logoff {session_id}', timeout=20)
        except subprocess.TimeoutExpired:
            # logoff can tear down the interactive session while prlctl is
            # still waiting for its guest command channel. This is an unknown
            # delivery receipt, not success: only the independent query-user
            # disappearance proof below may accept it.
            continue
        if result.returncode != 0:
            raise AssertionError(result.stderr or result.stdout)

    def wait_remaining(target_ids: set[int]) -> set[int]:
        deadline = time.monotonic() + timeout_s
        remaining_ids = target_ids
        while remaining_ids and time.monotonic() < deadline:
            remaining_ids = {
                session.session_id for session in _query_user_sessions(vm)
            } & target_ids
            if remaining_ids:
                time.sleep(1)
        return remaining_ids

    remaining = wait_remaining(ids)
    if remaining:
        # Exact-ID reset is the owned-snapshot-only fallback for a console
        # session whose ordinary logoff remains stuck. Its command receipt is
        # still not the oracle: require the same independent disappearance.
        for session_id in sorted(remaining):
            try:
                result = vm.exec_cmd(f"reset session {session_id}", timeout=20)
            except subprocess.TimeoutExpired:
                continue
            if result.returncode != 0:
                # The exact session can disappear between the preceding poll
                # and reset dispatch, yielding an empty non-zero receipt. Do
                # not convert that receipt into either success or failure;
                # the independent disappearance proof below is authoritative.
                continue
        remaining = wait_remaining(remaining)
    if remaining:
        raise AssertionError(
            f"pre-existing interactive sessions did not log off: {sorted(remaining)}"
        )
    return sessions


def _wait_user_shell(vm, account: str, *, timeout_s: float = 90.0) -> int:
    """Prove one active RDP account session owns an Explorer shell."""
    deadline = time.monotonic() + timeout_s
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        matches = [
            session
            for session in _query_user_sessions(vm)
            if session.username.casefold() == account.casefold()
            and session.state.casefold() == "active"
            and (session.session_name or "").casefold().startswith("rdp-")
        ]
        explorer_ids = _explorer_session_ids(vm)
        last = {
            "account_session_ids": [session.session_id for session in matches],
            "explorer_session_ids": sorted(explorer_ids),
        }
        if len(matches) == 1 and matches[0].session_id in explorer_ids:
            return matches[0].session_id
        time.sleep(2)
    raise AssertionError(
        f"RDP desktop shell did not become ready for {account}: {last}"
    )


def _oracle_read(vm, guest_path: str, *, timeout_s: float = 30.0) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = vm.exec_cmd(f'type "{guest_path}" 2>nul', timeout=20)
        if result.returncode == 0 and (result.stdout or "").strip():
            return (result.stdout or "").strip()
        time.sleep(0.5)
    return None


def _wait_state(vm, expected: str, *, timeout_s: float = 60.0) -> str:
    deadline = time.monotonic() + timeout_s
    observed = vm.status()
    while time.monotonic() < deadline:
        observed = vm.status()
        if observed == expected:
            return observed
        time.sleep(2)
    return observed


def _painted_readiness(png: bytes) -> bool:
    """Protocol-test readiness: valid, nonblank pixels (not app certification)."""
    try:
        if not png.startswith(b"\x89PNG\r\n\x1a\n"):
            return False
        image = Image.open(io.BytesIO(png)).convert("RGB")
        colors = image.getcolors(maxcolors=1 << 24)
        return colors is None or len(colors) > 1
    except Exception:  # noqa: BLE001 - malformed frame means not ready
        return False


def _qualification_desktop_ready(png: bytes) -> bool:
    """Recognize the fixed Windows-11 qualification desktop, not login UI.

    The preserved VM uses its light taskbar on the bottom eight percent of the
    1280x800 desktop. Login, Welcome, disconnect, and account-conflict screens
    are dark there. This is intentionally a deployment-specific readiness
    predicate for this evidence task, not a generic Windows identity claim.
    """
    try:
        image = Image.open(io.BytesIO(png)).convert("L")
        if image.width < 100 or image.height < 100:
            return False
        taskbar = image.crop(
            (
                0,
                int(image.height * (1 - _QUALIFICATION_TASKBAR_BOTTOM_FRACTION)),
                image.width,
                image.height,
            )
        )
        histogram = taskbar.histogram()
        bright_fraction = sum(histogram[_QUALIFICATION_TASKBAR_MIN_LUMA:]) / (
            taskbar.width * taskbar.height
        )
        return bright_fraction >= _QUALIFICATION_TASKBAR_BRIGHT_FRACTION
    except Exception:  # noqa: BLE001 - malformed frame means not ready
        return False


def _frame_change_fraction(before: bytes, after: bytes) -> float:
    """Return the fraction of pixels changed by more than trivial noise."""
    try:
        left = Image.open(io.BytesIO(before)).convert("RGB")
        right = Image.open(io.BytesIO(after)).convert("RGB")
        if left.size != right.size:
            return 1.0
        histogram = ImageChops.difference(left, right).convert("L").histogram()
        changed = sum(histogram[9:])
        return changed / (left.width * left.height)
    except Exception:  # noqa: BLE001 - malformed comparison cannot establish change
        return 0.0


def _wait_qualification_desktop(
    backend,
    baseline: bytes,
    *,
    timeout_s: float = 30.0,
    stable_frames: int = _QUALIFICATION_STABLE_FRAMES,
    settle_s: float = 0.5,
) -> bytes:
    """Wait for a materially transitioned, stable qualification desktop."""
    if stable_frames < 2:
        raise ValueError("stable_frames must be at least 2")
    deadline = time.monotonic() + timeout_s
    baseline_ready = _qualification_desktop_ready(baseline)
    last_ready: bytes | None = None
    stable = 0
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        png = backend.screenshot()
        changed = _frame_change_fraction(baseline, png)
        desktop_ready = _qualification_desktop_ready(png)
        transitioned = baseline_ready or changed >= _QUALIFICATION_TRANSITION_FRACTION
        if transitioned and desktop_ready:
            stable_change = (
                0.0 if last_ready is None else _frame_change_fraction(last_ready, png)
            )
            stable = (
                stable + 1
                if last_ready is None
                or stable_change <= _QUALIFICATION_STABLE_CHANGE_FRACTION
                else 1
            )
            last_ready = png
            if stable >= stable_frames:
                return png
        else:
            stable = 0
            last_ready = None
            stable_change = None
        last = {
            "baseline_change_fraction": round(changed, 6),
            "desktop_ready": desktop_ready,
            "stable_change_fraction": (
                None if stable_change is None else round(stable_change, 6)
            ),
            "stable_frames": stable,
        }
        time.sleep(settle_s)
    raise AssertionError(f"qualification desktop did not become ready: {last}")


def _wait_counted_qualification_desktop(backend, baseline: bytes) -> bytes:
    """Apply the evidence-bound readiness budget used by counted trials."""
    return _wait_qualification_desktop(
        backend,
        baseline,
        timeout_s=_COUNTED_DESKTOP_READINESS_TIMEOUT_S,
        stable_frames=_QUALIFICATION_STABLE_FRAMES,
    )


def _qualification_readiness_config() -> dict[str, object]:
    """Machine-readable binding for every fixed-VM readiness predicate."""
    return {
        "target_session": "one exact active RDP account session",
        "explorer": "same exact session id",
        "taskbar_bottom_fraction": _QUALIFICATION_TASKBAR_BOTTOM_FRACTION,
        "taskbar_min_luma": _QUALIFICATION_TASKBAR_MIN_LUMA,
        "taskbar_min_bright_fraction": _QUALIFICATION_TASKBAR_BRIGHT_FRACTION,
        "transition_fraction": _QUALIFICATION_TRANSITION_FRACTION,
        "baseline_desktop_ready_bypasses_transition": True,
        "stable_frames": _QUALIFICATION_STABLE_FRAMES,
        "max_stable_change_fraction": _QUALIFICATION_STABLE_CHANGE_FRACTION,
        "timeout_s": _COUNTED_DESKTOP_READINESS_TIMEOUT_S,
    }


def _assert_painted_frame(png: bytes, expected_size: tuple[int, int]) -> None:
    assert _painted_readiness(png), "RDP returned an invalid/blank frame"
    assert Image.open(io.BytesIO(png)).size == expected_size


def _write_summary(path: Path, summary: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")


def _git_revision(*args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_real_rdp_three_trial_independent_oracle(tmp_path) -> None:
    from openadapt_flow.backends.parallels_vm import (
        DEFAULT_VM_UUID,
        ParallelsError,
        ParallelsVM,
    )
    from openadapt_flow.backends.rdp_backend import AardwolfTransport, FreeRDPBackend

    vm = ParallelsVM(os.environ.get("OAFLOW_PARALLELS_VM_UUID", DEFAULT_VM_UUID))
    candidate_commit = os.environ.get(CANDIDATE_ENV)
    base_commit = os.environ.get(BASE_COMMIT_ENV)
    base_snapshot_id = os.environ.get(BASE_SNAPSHOT_ENV)
    report_path = Path(
        os.environ.get(
            "OAFLOW_RDP_QUAL_REPORT", str(tmp_path / "rdp_qualification.json")
        )
    ).resolve()
    sha_pattern = r"[0-9a-f]{40}"
    if candidate_commit is None or re.fullmatch(sha_pattern, candidate_commit) is None:
        raise RuntimeError(f"{CANDIDATE_ENV} must name one exact candidate commit")
    if base_commit is None or re.fullmatch(sha_pattern, base_commit) is None:
        raise RuntimeError(f"{BASE_COMMIT_ENV} must name one exact base commit")
    if base_snapshot_id is None:
        raise RuntimeError(f"{BASE_SNAPSHOT_ENV} is required for safe cleanup")
    if TRIALS != 3:
        raise RuntimeError("counted RDP qualification requires exactly 3 trials")
    head_commit = _git_revision("rev-parse", "HEAD")
    if head_commit != candidate_commit:
        raise RuntimeError(
            f"candidate mismatch: environment={candidate_commit}, HEAD={head_commit}"
        )
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_commit, candidate_commit],
        check=False,
    )
    if ancestry.returncode != 0:
        raise RuntimeError("declared base commit is not an ancestor of the candidate")

    initial_state = vm.status()
    if initial_state != "suspended":
        raise RuntimeError(
            f"qualification base must begin suspended (state={initial_state!r})"
        )
    snapshots_before = vm.list_snapshots()
    if not any(
        item.snapshot_id == base_snapshot_id and item.current
        for item in snapshots_before
    ):
        raise RuntimeError("declared preserved base snapshot is not current")
    storage_path = os.environ.get(HOST_STORAGE_PATH_ENV, os.getcwd())
    free_bytes_before = vm.require_host_free_space(storage_path=storage_path)
    aardwolf_version = package_version("aardwolf")
    if aardwolf_version != "0.2.14":
        raise RuntimeError(
            "counted RDP qualification requires exact aardwolf 0.2.14 "
            f"(observed {aardwolf_version})"
        )

    account = f"oaflowq_{secrets.token_hex(4)}"
    # 13 chars; upper/lower/special and usually digits, within legacy net.exe's
    # non-interactive 14-character prompt threshold.
    password = f"Qa!{secrets.token_hex(5)}"
    guest_dir = r"C:\Users\Public\Documents\OpenAdaptRDPQualification"
    width = int(os.environ.get("OPENADAPT_FLOW_RDP_WIDTH", "1280"))
    height = int(os.environ.get("OPENADAPT_FLOW_RDP_HEIGHT", "800"))
    port = int(os.environ.get("OPENADAPT_FLOW_RDP_PORT", "3389"))
    rows: list[dict[str, object]] = []
    snapshot_id: str | None = None
    cleanup: dict[str, object] = {
        "attempted": False,
        "passed": False,
        "base_snapshot_current_after": False,
        "owned_snapshot_deleted": False,
        "restored_state": None,
        "snapshot_ids_after": [],
        "snapshot_inventory_restored": False,
        "error_type": None,
    }
    environment: dict[str, object] = {
        "vm": vm.uuid,
        "base_snapshot_id": base_snapshot_id,
        "initial_state": initial_state,
        "snapshot_ids_before": [item.snapshot_id for item in snapshots_before],
        "framebuffer": [width, height],
        "transport": "aardwolf",
        "aardwolf_version": aardwolf_version,
        "desktop_readiness": _qualification_readiness_config(),
        "host_free_bytes_before": free_bytes_before,
    }
    qualified_session_ids: list[int] = []
    environment["qualified_session_ids"] = qualified_session_ids
    summary_base: dict[str, object] = {
        "schema_version": "openadapt.rdp-qualification.v1",
        "candidate_commit": candidate_commit,
        "base_commit": base_commit,
        "substrate": "real-rdp-aardwolf-parallels-win11",
        "task": "create a unique file through the Windows Run dialog over RDP",
        "environment": environment,
        "oracle": "prlctl exec type <unique file written only via RDP input>",
        "readiness": _QUALIFICATION_READINESS_DESCRIPTION,
        "failure_taxonomy": [
            "connect_or_frame_failure",
            "input_delivery_failure",
            "independent_oracle_mismatch",
            "over_halt_or_timeout",
            "environment_restore_failure",
        ],
        "caveat": "This qualifies RDP transport/input only, not Citrix ICA/HDX.",
    }

    def current_summary() -> dict[str, object]:
        failure_counts: dict[str, int] = {}
        for row in rows:
            label = str(row["failure_class"] or "none")
            failure_counts[label] = failure_counts.get(label, 0) + 1
        successes = sum(bool(row["exact"]) for row in rows)
        silent_incorrect = sum(
            bool(row["input_returned_without_error"])
            and row["observed"] is not None
            and row["observed"] != row["expected"]
            for row in rows
        )
        over_halts = sum(row["failure_class"] == "over_halt_or_timeout" for row in rows)
        model_calls = sum(int(row["model_calls"]) for row in rows)
        return {
            **summary_base,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trials": rows,
            "run_count": len(rows),
            "successes": successes,
            "silent_incorrect_successes": silent_incorrect,
            "over_halts": over_halts,
            "failures": len(rows) - successes,
            "failure_taxonomy_counts": failure_counts,
            "model_calls": model_calls,
            "cleanup": cleanup,
            "accepted": (
                len(rows) == 3
                and successes == 3
                and silent_incorrect == 0
                and over_halts == 0
                and model_calls == 0
                and cleanup["passed"] is True
            ),
        }

    active_error: BaseException | None = None
    try:
        # Snapshot before the first guest mutation, including resume. This id
        # is retained in memory and is the only snapshot cleanup may delete.
        snapshot_id = vm.snapshot(
            f"oaflow-rdp-qual-{int(time.time())}",
            description=f"real RDP qualification; candidate={candidate_commit}",
        )
        summary_base["owned_snapshot_id"] = snapshot_id
        print(
            f"[rdp-qual] owned_snapshot={snapshot_id} "
            f"base={base_snapshot_id} candidate={candidate_commit}"
        )
        vm.ensure_running()
        _wait_guest(vm)
        host = vm.guest_ip()
        computer_name = (vm.exec_cmd("hostname").stdout or "").strip()
        assert computer_name, "could not determine Windows computer name"
        guest_version = (vm.exec_cmd("ver").stdout or "").strip()
        logged_off = _logoff_preexisting_interactive_sessions(vm)
        environment.update(
            {
                "guest_ip": host,
                "computer_name": computer_name,
                "guest_version": guest_version,
                "preexisting_sessions_logged_off": [
                    asdict(session) for session in logged_off
                ],
            }
        )

        create = vm.exec_cmd(
            f'net user {account} "{password}" /add /expires:never '
            f'&& net localgroup "Remote Desktop Users" {account} /add',
            timeout=60,
        )
        assert create.returncode == 0, create.stderr or create.stdout
        mkdir = vm.exec_cmd(f'if not exist "{guest_dir}" mkdir "{guest_dir}"')
        assert mkdir.returncode == 0, mkdir.stderr or mkdir.stdout
        firewall = vm.exec_ps(
            "Enable-NetFirewallRule -Name "
            "RemoteDesktop-UserMode-In-TCP,RemoteDesktop-UserMode-In-UDP"
        )
        assert firewall.returncode == 0, firewall.stderr or firewall.stdout
        _wait_tcp(host, port)

        _write_summary(report_path, current_summary())
        print(f"[rdp-qual] durable_report={report_path}")

        for index in range(1, TRIALS + 1):
            token = f"oaflow-rdp-trial-{index}-{secrets.token_hex(8)}"
            guest_path = rf"{guest_dir}\trial-{index}.txt"
            started = time.monotonic()
            backend = None
            observed: str | None = None
            failure_class: str | None = None
            error_type: str | None = None
            input_returned_without_error = False
            phase = "connect_or_frame_failure"
            try:
                transport = AardwolfTransport.from_credentials(
                    host,
                    account,
                    password,
                    domain=computer_name,
                    port=port,
                    width=width,
                    height=height,
                )
                backend = FreeRDPBackend(
                    transport,
                    max_frame_age_s=5.0,
                    readiness_probe=_qualification_desktop_ready,
                )
                baseline = backend.screenshot()
                session_id = _wait_user_shell(vm, account)
                png = _wait_counted_qualification_desktop(backend, baseline)
                _assert_painted_frame(png, backend.viewport)
                qualified_session_ids.append(session_id)
                # The token reaches the guest only through RDP input. prlctl is
                # used exclusively afterward as an independent read oracle.
                phase = "input_delivery_failure"
                backend.press("Meta+r")
                time.sleep(0.5)
                backend.type_text(f'cmd.exe /d /c echo {token}>"{guest_path}"')
                backend.screenshot()  # refresh lease after potentially slow typing
                backend.press("Enter")
                input_returned_without_error = True
                phase = "over_halt_or_timeout"
                observed = _oracle_read(vm, guest_path)
                if observed is None:
                    failure_class = "over_halt_or_timeout"
                elif observed != token:
                    failure_class = "independent_oracle_mismatch"
            except Exception as exc:  # noqa: BLE001 - classify and continue trials
                failure_class = phase
                error_type = type(exc).__name__
            finally:
                if backend is not None:
                    try:
                        backend.close()
                    except Exception as exc:  # noqa: BLE001 - preserve trial row
                        if failure_class is None:
                            failure_class = "input_delivery_failure"
                            error_type = type(exc).__name__

            exact = observed == token and failure_class is None
            rows.append(
                {
                    "trial": index,
                    "expected": token,
                    "observed": observed,
                    "exact": exact,
                    "input_returned_without_error": input_returned_without_error,
                    "failure_class": failure_class,
                    "error_type": error_type,
                    "latency_s": round(time.monotonic() - started, 3),
                    "model_calls": 0,
                }
            )
            # Persist every row before starting the next trial, including
            # failures, so a later exception cannot erase the evidence.
            _write_summary(report_path, current_summary())

        summary = current_summary()
        print(f"[rdp-qual] {json.dumps(summary, sort_keys=True)}")
        assert len(rows) == 3
        assert summary["successes"] == len(rows)
        assert summary["silent_incorrect_successes"] == 0
        assert summary["over_halts"] == 0
        assert summary["model_calls"] == 0
    except BaseException as exc:
        active_error = exc
        raise
    finally:
        # Restore the explicit preserved base. Only after proving it current
        # may cleanup delete the one exact snapshot id created above.
        cleanup["attempted"] = snapshot_id is not None
        cleanup_error: Exception | None = None
        try:
            if snapshot_id is not None:
                vm.restore_base_and_delete_owned_snapshot(
                    base_snapshot_id=base_snapshot_id,
                    owned_snapshot_id=snapshot_id,
                )
                cleanup["owned_snapshot_deleted"] = True
            restored = _wait_state(vm, initial_state)
            cleanup["restored_state"] = restored
            if restored != initial_state:
                raise ParallelsError(
                    f"preserved base did not restore state {initial_state!r}: {restored!r}"
                )
            snapshots_after = vm.list_snapshots()
            after_ids = [item.snapshot_id for item in snapshots_after]
            cleanup["snapshot_ids_after"] = after_ids
            cleanup["base_snapshot_current_after"] = any(
                item.snapshot_id == base_snapshot_id and item.current
                for item in snapshots_after
            )
            if not cleanup["base_snapshot_current_after"]:
                raise ParallelsError("preserved base is not current after cleanup")
            before_ids = [item.snapshot_id for item in snapshots_before]
            cleanup["snapshot_inventory_restored"] = sorted(after_ids) == sorted(
                before_ids
            )
            if not cleanup["snapshot_inventory_restored"]:
                raise ParallelsError("snapshot inventory differs after cleanup")
            environment["host_free_bytes_after"] = vm.require_host_free_space(
                storage_path=storage_path
            )
            cleanup["passed"] = True
        except Exception as exc:  # noqa: BLE001 - persist cleanup failure evidence
            cleanup_error = exc
            cleanup["error_type"] = type(exc).__name__
        try:
            _write_summary(report_path, current_summary())
        except Exception as exc:  # noqa: BLE001 - report persistence is required
            if cleanup_error is None:
                cleanup_error = exc
        print(
            f"[rdp-qual] cleanup={json.dumps(cleanup, sort_keys=True)} "
            f"durable_report={report_path}"
        )
        if cleanup_error is not None:
            if active_error is not None:
                active_error.add_note(
                    f"RDP qualification cleanup/report error: {cleanup_error!r}"
                )
            else:
                raise RuntimeError(
                    "RDP qualification environment cleanup/report failed"
                ) from cleanup_error

    assert current_summary()["accepted"] is True
