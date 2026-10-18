"""After a restart: wait for a sign-in, open a notice, send opt-in alerts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openadapt_flow import onprem_reboot as rb
from openadapt_flow.console.attention import notification
from openadapt_flow.onprem_service import health


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def _settings(root: Path, **body) -> None:
    path = rb.settings_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema": rb.SETTINGS_SCHEMA, **body}))


def _events(root: Path) -> list[dict]:
    return [json.loads(x) for x in rb.events_path(root).read_text().splitlines()]


def test_defaults_are_no_auto_login_and_no_remote_channels(tmp_path):
    s = rb.load_settings(tmp_path)
    assert s.reboot.auto_login is False
    assert s.alerts == ()


def test_quick_sign_in_records_the_wait_and_opens_no_notice(tmp_path):
    clock = Clock()
    logins = iter([False, False, True])
    result = rb.boot_check(
        tmp_path,
        boot_id="b1",
        probe=lambda: next(logins),
        clock=clock,
        sleep=clock.sleep,
    )
    assert result.logged_in and result.notice is None and result.alerts == []
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["rebooted", "login_after_reboot"]
    assert rb.list_notices(tmp_path) == []


def test_late_sign_in_opens_one_notice_and_alerts_each_channel_once(tmp_path):
    _settings(
        tmp_path,
        name="records-host-2",
        reboot={"alert_after_s": 60, "poll_s": 15},
        alerts={
            "channels": [
                {"kind": "webhook", "url": "http://10.20.0.9:8080/hook", "name": "ops"},
                {"kind": "command", "argv": ["/usr/local/bin/page-ops"]},
            ]
        },
    )
    clock = Clock()
    sent: list = []
    result = rb.boot_check(
        tmp_path,
        boot_id="b2",
        probe=lambda: clock.t >= 300,
        sender=lambda ch, payload: sent.append((ch["kind"], payload)),
        clock=clock,
        sleep=clock.sleep,
    )
    assert result.logged_in and result.waited_s == 300
    assert [k for k, _ in sent] == ["webhook", "command"]
    payload = sent[0][1]
    assert payload["install"] == "records-host-2"
    assert payload["event"] == rb.REBOOT_NOTICE
    assert set(payload) == {"source", "install", "event", "at", "message"}
    notices = rb.list_notices(tmp_path)
    assert len(notices) == 1 and notices[0]["kind"] == rb.REBOOT_NOTICE
    events = [e["event"] for e in _events(tmp_path)]
    assert events == ["rebooted", "awaiting_login", "alert", "login_after_reboot"]


def test_local_only_install_still_logs_and_counts_the_notice(tmp_path):
    clock = Clock()
    rb.boot_check(
        tmp_path,
        boot_id="b3",
        probe=lambda: clock.t >= 600,
        clock=clock,
        sleep=clock.sleep,
    )
    alert = [e for e in _events(tmp_path) if e["event"] == "alert"][0]
    assert alert["results"] == []
    assert rb.open_notice_count(tmp_path) == 1
    assert notification([], rb.open_notice_count(tmp_path)).open_count == 1


def test_failed_alert_is_recorded_not_fatal(tmp_path):
    _settings(
        tmp_path,
        reboot={"alert_after_s": 0},
        alerts={"channels": [{"kind": "webhook", "url": "http://10.0.0.9/x"}]},
    )
    clock = Clock()

    def boom(ch, payload):
        raise PermissionError("no-outbound mode refused")

    result = rb.boot_check(
        tmp_path,
        boot_id="b4",
        probe=lambda: clock.t > 0,
        sender=boom,
        clock=clock,
        sleep=clock.sleep,
    )
    assert result.alerts == [
        {"channel": "webhook", "sent": False, "error": "PermissionError"}
    ]


def test_notice_is_once_per_boot_and_acknowledged_by_a_person(tmp_path):
    a = rb.open_notice(tmp_path, rb.REBOOT_NOTICE, boot_id="b5")
    b = rb.open_notice(tmp_path, rb.REBOOT_NOTICE, boot_id="b5")
    assert a["id"] == b["id"]
    acked = rb.acknowledge(tmp_path, a["id"], by="operator-1")
    assert acked and acked["acknowledged_by"] == "operator-1"
    assert rb.list_notices(tmp_path) == []
    assert len(rb.list_notices(tmp_path, include_closed=True)) == 1
    assert rb.acknowledge(tmp_path, "../../etc", by="x") is None


def test_gives_up_after_the_configured_limit(tmp_path):
    _settings(tmp_path, reboot={"alert_after_s": 10, "give_up_after_s": 100})
    clock = Clock()
    result = rb.boot_check(
        tmp_path, boot_id="b6", probe=lambda: False, clock=clock, sleep=clock.sleep
    )
    assert not result.logged_in and result.notice is not None
    assert _events(tmp_path)[-1]["event"] == "boot_check_gave_up"


def test_auto_login_opt_in_is_read_and_recorded(tmp_path):
    _settings(tmp_path, reboot={"auto_login": True})
    clock = Clock()
    rb.boot_check(
        tmp_path, boot_id="b7", probe=lambda: True, clock=clock, sleep=clock.sleep
    )
    assert _events(tmp_path)[0]["auto_login"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"reboot": {"auto_logn": True}},
        {"reboot": {"alert_after_s": "soon"}},
        {"alerts": {"channels": [{"kind": "sms"}]}},
        {"alerts": {"channels": [{"kind": "webhook", "url": "ftp://x"}]}},
        {"alerts": {"channels": [{"kind": "command", "argv": "page-ops"}]}},
    ],
)
def test_bad_settings_are_refused(tmp_path, body):
    _settings(tmp_path, **body)
    with pytest.raises(rb.SettingsError):
        rb.load_settings(tmp_path)


def test_console_session_user_parsing():
    assert rb.console_session_user(lambda: "svc-records\n") == "svc-records"
    assert rb.console_session_user(lambda: "root\n") is None


def test_health_warns_on_open_notice(tmp_path):
    rb.open_notice(tmp_path, rb.REBOOT_NOTICE, boot_id="b8")
    checks = {c["name"]: c for c in health(tmp_path)["checks"]}
    assert checks["service_notices"]["status"] == "warn"
    assert rb.REBOOT_NOTICE in checks["service_notices"]["detail"]


def test_service_root_for_runs(tmp_path):
    (tmp_path / "service").mkdir()
    (tmp_path / "runs").mkdir()
    assert rb.service_root_for(tmp_path / "runs", env={}) == tmp_path.resolve()
    assert rb.service_root_for(
        tmp_path / "runs", env={"OPENADAPT_ONPREM_ROOT": "/x"}
    ) == Path("/x")
