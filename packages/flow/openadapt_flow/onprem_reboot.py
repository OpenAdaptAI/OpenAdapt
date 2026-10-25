"""What the on-prem service does after the machine restarts.

The service drives applications in a desktop session, so it runs as a login
agent of a dedicated service account and starts only once that account has
a session. The default after a restart is to **wait for a person to sign
in**: the machine does not log in by itself.

``openadapt-flow service boot-check`` runs once per boot, before anyone signs
in (a launchd daemon on macOS that runs as the service account). It:

1. records the restart in ``service/events.jsonl``;
2. waits for the service account to have a session;
3. if no session appears within ``reboot.alert_after_s``, opens a notice in
   ``service/notices/`` and sends an alert through each configured channel;
4. records the sign-in, with how long the service waited, and closes nothing:
   an operator acknowledges the notice (``service notices --ack``).

Notices are always written. The console counts open notices in its
notification, so the OpenAdapt Desktop tray shows them on a local-only
install. Remote alert channels are opt-in (``alerts`` in
``service/service.json``) and carry only a fixed message: the install name,
the event and the time, never workflow data.

A site may opt into automatic login (``reboot.auto_login``, see
``docs/ON_PREM.md``). The same check then alerts if the automatic login has
not produced a session in time.

A mode that runs without any desktop session is not available.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

SETTINGS_SCHEMA = "openadapt.onprem-service-settings/v1"
NOTICE_SCHEMA = "openadapt.onprem-notice/v1"
EVENT_SCHEMA = "openadapt.onprem-event/v1"

REBOOT_NOTICE = "rebooted_awaiting_login"
NOTICE_COPY = {
    REBOOT_NOTICE: (
        "The machine restarted and the service is waiting for a sign-in.",
        "Sign in to the service account on this machine. Queued work resumes "
        "after sign-in; acknowledge this notice once you have checked it.",
    ),
}

#: () -> bool: does the service account have a session now? Injected by tests.
SessionProbe = Callable[[], bool]
#: (channel, payload) -> None. Raises on failure. Injected by tests.
Sender = Callable[[Mapping[str, Any], Mapping[str, Any]], None]


class SettingsError(ValueError):
    """``service/service.json`` is invalid."""


@dataclass(frozen=True)
class RebootPolicy:
    auto_login: bool = False
    alert_after_s: float = 300.0
    give_up_after_s: float = 7 * 24 * 3600.0
    poll_s: float = 15.0


@dataclass(frozen=True)
class ServiceSettings:
    name: str = "openadapt-onprem"
    reboot: RebootPolicy = field(default_factory=RebootPolicy)
    alerts: tuple[Mapping[str, Any], ...] = ()


def _utc(now: Optional[datetime] = None) -> str:
    return (
        (now or datetime.now(timezone.utc))
        .astimezone(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%SZ")
    )


def settings_path(root: Path) -> Path:
    return Path(root) / "service" / "service.json"


def notices_dir(root: Path) -> Path:
    return Path(root) / "service" / "notices"


def events_path(root: Path) -> Path:
    return Path(root) / "service" / "events.jsonl"


def _check_channel(ch: Any) -> Mapping[str, Any]:
    if not isinstance(ch, Mapping):
        raise SettingsError("each alert channel is an object")
    kind = ch.get("kind")
    if kind == "webhook":
        url = ch.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise SettingsError("webhook channel needs an http(s) url")
    elif kind == "command":
        argv = ch.get("argv")
        if not (
            isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
        ):
            raise SettingsError("command channel needs argv, a list of strings")
    else:
        raise SettingsError(f"unknown alert channel kind {kind!r}")
    return dict(ch)


def load_settings(root: Path) -> ServiceSettings:
    """Read ``service/service.json``. A missing file gives the defaults."""
    path = settings_path(root)
    if not path.exists():
        return ServiceSettings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SettingsError(f"{path}: unreadable") from exc
    if not isinstance(data, Mapping) or data.get("schema") != SETTINGS_SCHEMA:
        raise SettingsError(f"{path}: not a {SETTINGS_SCHEMA} file")
    reboot = data.get("reboot") or {}
    if not isinstance(reboot, Mapping):
        raise SettingsError("reboot must be an object")
    unknown = set(reboot) - {"auto_login", "alert_after_s", "give_up_after_s", "poll_s"}
    if unknown:
        raise SettingsError(f"unknown reboot settings: {sorted(unknown)}")
    try:
        policy = RebootPolicy(
            auto_login=bool(reboot.get("auto_login", False)),
            alert_after_s=float(reboot.get("alert_after_s", 300.0)),
            give_up_after_s=float(reboot.get("give_up_after_s", 7 * 24 * 3600.0)),
            poll_s=float(reboot.get("poll_s", 15.0)),
        )
    except (TypeError, ValueError) as exc:
        raise SettingsError("reboot times must be numbers") from exc
    alerts = data.get("alerts") or {}
    channels = alerts.get("channels", []) if isinstance(alerts, Mapping) else None
    if not isinstance(channels, list):
        raise SettingsError("alerts.channels must be a list")
    return ServiceSettings(
        name=str(data.get("name") or "openadapt-onprem"),
        reboot=policy,
        alerts=tuple(_check_channel(c) for c in channels),
    )


def record_event(root: Path, event: str, **detail: Any) -> dict[str, Any]:
    line = {"schema": EVENT_SCHEMA, "event": event, "at": _utc(), **detail}
    path = events_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(line, sort_keys=True) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return line


def open_notice(
    root: Path, kind: str, *, boot_id: str, **detail: Any
) -> dict[str, Any]:
    """Write a notice. Idempotent per (kind, boot)."""
    nid = hashlib.sha256(f"{kind}:{boot_id}".encode()).hexdigest()[:24]
    path = notices_dir(root) / f"{nid}.json"
    if path.exists():
        return dict(json.loads(path.read_text(encoding="utf-8")))
    headline, next_action = NOTICE_COPY[kind]
    notice = {
        "schema": NOTICE_SCHEMA,
        "id": nid,
        "kind": kind,
        "opened_at": _utc(),
        "headline": headline,
        "next_action": next_action,
        "acknowledged_at": None,
        "acknowledged_by": None,
        **detail,
    }
    _write(path, notice)
    return notice


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def list_notices(root: Path, *, include_closed: bool = False) -> list[dict[str, Any]]:
    out = []
    d = notices_dir(root)
    if not d.is_dir():
        return []
    for path in sorted(d.glob("*.json")):
        try:
            notice = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(notice, dict) or notice.get("schema") != NOTICE_SCHEMA:
            continue
        if include_closed or not notice.get("acknowledged_at"):
            out.append(notice)
    return sorted(out, key=lambda n: n.get("opened_at") or "", reverse=True)


def acknowledge(root: Path, notice_id: str, by: str) -> Optional[dict[str, Any]]:
    if not notice_id.isalnum():
        return None
    path = notices_dir(root) / f"{notice_id}.json"
    if not path.is_file():
        return None
    notice = json.loads(path.read_text(encoding="utf-8"))
    if not notice.get("acknowledged_at"):
        notice.update(acknowledged_at=_utc(), acknowledged_by=by)
        _write(path, notice)
        record_event(root, "notice_acknowledged", notice_id=notice_id, by=by)
    return dict(notice)


def alert_payload(settings: ServiceSettings, kind: str, at: str) -> dict[str, Any]:
    """The only content an alert carries. No workflow data."""
    return {
        "source": "openadapt-flow",
        "install": settings.name,
        "event": kind,
        "at": at,
        "message": NOTICE_COPY[kind][0],
    }


def _default_sender(channel: Mapping[str, Any], payload: Mapping[str, Any]) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    if channel["kind"] == "webhook":
        req = urllib.request.Request(
            str(channel["url"]),
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            if resp.status >= 300:
                raise OSError(f"webhook answered {resp.status}")
    else:
        proc = subprocess.run(
            list(channel["argv"]), input=body, capture_output=True, timeout=30
        )
        if proc.returncode != 0:
            raise OSError(f"alert command exited {proc.returncode}")


def send_alerts(
    root: Path,
    settings: ServiceSettings,
    payload: Mapping[str, Any],
    sender: Sender = _default_sender,
) -> list[dict[str, Any]]:
    """Send ``payload`` through every configured channel; record each result.

    In strict no-outbound mode a webhook must be on the allow list, like any
    other peer; a refused send is recorded, never retried by another route.
    """
    results = []
    for ch in settings.alerts:
        label = ch.get("name") or ch["kind"]
        try:
            sender(ch, payload)
            results.append({"channel": label, "sent": True})
        except Exception as exc:  # a failed alert is recorded, never fatal
            results.append(
                {"channel": label, "sent": False, "error": type(exc).__name__}
            )
    record_event(root, "alert", kind=payload.get("event"), results=results)
    return results


def console_session_user(reader: Optional[Callable[[], str]] = None) -> Optional[str]:
    """Who owns the console session (macOS), or None."""
    if reader is None:
        if sys.platform != "darwin":
            return None
        try:
            out = subprocess.run(
                ["stat", "-f", "%Su", "/dev/console"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout
        except OSError:
            return None
    else:
        out = reader()
    user = out.strip()
    return None if user in ("", "root", "loginwindow") else user


def default_session_probe(account: str) -> SessionProbe:
    return lambda: console_session_user() == account


@dataclass
class BootCheckResult:
    boot_id: str
    waited_s: float
    logged_in: bool
    notice: Optional[dict[str, Any]] = None
    alerts: list[dict[str, Any]] = field(default_factory=list)


def boot_check(
    root: Path,
    *,
    boot_id: str,
    probe: SessionProbe,
    settings: Optional[ServiceSettings] = None,
    sender: Sender = _default_sender,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> BootCheckResult:
    """Wait for the service account's session after a restart; alert if late."""
    root = Path(root)
    settings = settings or load_settings(root)
    policy = settings.reboot
    record_event(root, "rebooted", boot_id=boot_id, auto_login=policy.auto_login)
    start = clock()
    notice: Optional[dict[str, Any]] = None
    alerts: list[dict[str, Any]] = []
    while True:
        waited = clock() - start
        if probe():
            record_event(
                root,
                "login_after_reboot",
                boot_id=boot_id,
                waited_s=round(waited, 1),
                notice=notice["id"] if notice else None,
            )
            return BootCheckResult(boot_id, waited, True, notice, alerts)
        if notice is None and waited >= policy.alert_after_s:
            notice = open_notice(
                root,
                REBOOT_NOTICE,
                boot_id=boot_id,
                auto_login=policy.auto_login,
            )
            record_event(root, "awaiting_login", boot_id=boot_id, notice=notice["id"])
            alerts = send_alerts(
                root,
                settings,
                alert_payload(settings, REBOOT_NOTICE, notice["opened_at"]),
                sender,
            )
        if waited >= policy.give_up_after_s:
            record_event(root, "boot_check_gave_up", boot_id=boot_id)
            return BootCheckResult(boot_id, waited, False, notice, alerts)
        sleep(policy.poll_s)


def boot_id(reader: Optional[Callable[[], str]] = None) -> str:
    """A stable id for the current boot (kernel boot time)."""
    if reader is not None:
        raw = reader()
    elif sys.platform == "darwin":
        raw = subprocess.run(
            ["sysctl", "-n", "kern.boottime"], capture_output=True, text=True
        ).stdout
    else:
        try:
            raw = Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8")
        except OSError:
            raw = str(int(time.time() - time.monotonic()))
    return hashlib.sha256(raw.strip().encode()).hexdigest()[:16]


def open_notice_count(root: Path) -> int:
    return len(list_notices(root))


def service_root_for(
    runs_root: Path, env: Optional[Mapping[str, str]] = None
) -> Optional[Path]:
    """The on-prem storage root that owns ``runs_root``, if there is one.

    ``OPENADAPT_ONPREM_ROOT`` wins; otherwise the parent of the runs folder
    when it holds a ``service/`` folder (the on-prem layout).
    """
    env = os.environ if env is None else env
    if env.get("OPENADAPT_ONPREM_ROOT"):
        return Path(env["OPENADAPT_ONPREM_ROOT"])
    parent = Path(runs_root).resolve().parent
    return parent if (parent / "service").is_dir() else None


def summarize(notices: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Fields safe to show in the console: no paths, no workflow data."""
    keys = ("id", "kind", "opened_at", "headline", "next_action", "acknowledged_at")
    return [{k: n.get(k) for k in keys} for n in notices]
