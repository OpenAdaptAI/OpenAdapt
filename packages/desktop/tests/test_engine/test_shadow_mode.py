"""Shadow mode in Desktop: opt-in only, local only, one off switch."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import pytest

shadow = pytest.importorskip("openadapt_capture.shadow")
consent = pytest.importorskip("openadapt_capture.shadow.consent")
pytest.importorskip("openadapt_capture.shadow.keys")

from openadapt_capture.shadow.keys import ACTIVE_ACCOUNT, MemoryKeyStore  # noqa: E402

from openadapt_desktop.shadow_mode import (  # noqa: E402
    PROMPT_EVENT,
    STATE_EVENT,
    ShadowModeService,
)


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


# POSIX permission bits don't exist on Windows (NTFS uses ACLs, and
# %LOCALAPPDATA% is private to the user by default), so mode checks run only
# where the bits mean something.
POSIX = os.name != "nt"


def _mode_is(path: Path, mode: str) -> bool:
    return not POSIX or oct(path.stat().st_mode & 0o777) == mode


def _home(monkeypatch, path: Path) -> None:
    """Point the home directory at ``path`` on every platform (Windows reads
    USERPROFILE, not HOME)."""

    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))


def _invoice_events(start: float):
    """Four copies of a spreadsheet-to-ERP task, with noise in between."""

    S = shadow.ShadowEvent
    events = []
    t = start
    for r in range(4):
        for i in range(6):
            t += 7
            events.append(
                S(t, "click", app=("Slack", "Finder")[i % 2], role="button", label=f"N{r}{i}")
            )
        t += 30
        events += [
            S(t, "focus", app="Microsoft Excel"),
            S(t + 1, "click", app="Microsoft Excel", role="AXCell", label=f"INV-{1000 + r}"),
            S(t + 2, "copy", app="Microsoft Excel", key="c", modifiers=("meta",)),
            S(t + 3, "focus", app="Google Chrome"),
            S(t + 4, "navigate", app="Google Chrome", url="https://erp.example.com/invoices/new"),
            S(t + 5, "click", app="Google Chrome", role="textbox", label="Invoice number"),
            S(t + 6, "paste", app="Google Chrome", key="v", modifiers=("meta",)),
            S(t + 7, "click", app="Google Chrome", role="AXButton", label="Submit"),
        ]
        t += 40
    return events, t


@pytest.fixture()
def svc(tmp_path: Path):
    events: list[tuple[str, dict]] = []
    store = MemoryKeyStore()
    service = ShadowModeService(
        tmp_path,
        shadow_dir=tmp_path / "shadow",
        emit=lambda e, d: events.append((e, d)),
        key_store=store,
        clock=Clock(1_000_000.0),
        autostart_scanner=False,
    )
    yield service, events, store, tmp_path
    service.close()


def test_off_by_default_and_nothing_written(svc):
    service, events, _store, root = svc
    status = service.status()
    assert status["available"] is True
    assert status["active"] is False and status["acknowledged_at"] is None
    assert service.live_tap() is None
    assert service.scan() == []
    assert not (root / "shadow" / "buffer.db").exists()
    assert service.resume() is False


def test_enable_needs_the_acknowledged_current_notice(svc):
    service, _events, _store, root = svc
    with pytest.raises(PermissionError):
        service.enable(service.notice()["sha256"], acknowledged=False)
    with pytest.raises(consent.ShadowConsentRequired):
        service.enable("0" * 64, acknowledged=True)
    assert service.status()["active"] is False
    assert not (root / "shadow" / "consent.json").exists()


def test_enable_records_the_acknowledgement_time_locally(svc):
    service, events, _store, root = svc
    status = service.enable(service.notice()["sha256"], acknowledged=True)
    assert status["active"] is True
    assert status["acknowledged_at"] == 1_000_000.0
    assert status["buffer_hours"] == 24
    ledger = json.loads((root / "shadow" / "consent.json").read_text())
    assert ledger["events"][-1]["event"] == "acknowledged"
    assert _mode_is(root / "shadow" / "consent.json", "0o600")
    assert _mode_is(root / "shadow", "0o700")
    assert events[-1][0] == STATE_EVENT and events[-1][1]["active"] is True


def test_prompt_review_draft_and_ignore(svc):
    service, events, _store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    stream, end = _invoice_events(1_000_000.0 - 3000)
    for event in stream:
        service._observer.observe(event)
    prompts = service.scan()
    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt["body"] == "You did this 4 times today."
    assert set(prompt) == {"motif_id", "count", "title", "body", "actions"}
    assert any(e == PROMPT_EVENT for e, _ in events)
    assert service.status()["pending"] == [prompt]
    result = service.decide(prompt["motif_id"], "review_draft")
    draft = Path(result["draft_path"])
    assert draft.parent == root / "shadow" / "drafts"
    export = json.loads(draft.read_text())
    assert export["schema"] == "openadapt.shadow.episodes/v2"
    for raw in ("INV-1000", "INV-1003", "Invoice number"):
        assert raw not in draft.read_text()
    assert _mode_is(draft, "0o600")
    assert service.status()["pending"] == []


def test_non_work_sites_are_not_recorded_until_allowlisted(svc):
    service, _events, _store, _root = svc
    service.enable(service.notice()["sha256"], acknowledged=True)
    S = shadow.ShadowEvent
    service._observer.observe(
        S(999_990.0, "navigate", app="Safari", url="https://erp.example.com/x")
    )
    (sym,) = service._observer.symbols()
    assert sym.token == "focus|non-work-browser"
    with pytest.raises(ValueError):
        service.set_work_domains(["https://erp.example.com/path"])
    status = service.set_work_domains(["ERP.example.com"])
    assert status["work_domains"] == ["erp.example.com"] and status["active"] is True


def test_purge_and_disable_deletes_everything_and_records_revocation(svc):
    service, events, store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    stream, _ = _invoice_events(1_000_000.0 - 3000)
    for event in stream:
        service._observer.observe(event)
    (prompt,) = service.scan()
    service.decide(prompt["motif_id"], "review_draft")
    assert (root / "shadow" / "buffer.db").exists()
    assert store.get(ACTIVE_ACCOUNT) is not None
    status = service.purge_and_disable()
    assert status["active"] is False and status["acknowledged_at"] is None
    shadow_dir = root / "shadow"
    assert not (shadow_dir / "buffer.db").exists()
    assert not (shadow_dir / "drafts").exists()
    assert not (shadow_dir / "ignored.json").exists()
    assert store.get(ACTIVE_ACCOUNT) is None
    ledger = json.loads((shadow_dir / "consent.json").read_text())
    assert [e["event"] for e in ledger["events"]] == ["acknowledged", "revoked"]
    # It stays off across a restart.
    again = ShadowModeService(
        root, shadow_dir=root / "shadow", key_store=store, autostart_scanner=False
    )
    assert again.resume() is False


def test_resume_after_restart_only_with_a_standing_acknowledgement(svc):
    service, _events, store, root = svc
    service.enable(service.notice()["sha256"], acknowledged=True)
    service.close()
    again = ShadowModeService(
        root,
        shadow_dir=root / "shadow",
        key_store=store,
        clock=Clock(1_000_100.0),
        autostart_scanner=False,
    )
    assert again.resume() is True and again.status()["active"] is True
    again.close()
    # A changed notice needs a new acknowledgement.
    data = json.loads((root / "shadow" / "consent.json").read_text())
    data["events"][-1]["notice_sha256"] = "f" * 64
    (root / "shadow" / "consent.json").write_text(json.dumps(data))
    third = ShadowModeService(
        root, shadow_dir=root / "shadow", key_store=store, autostart_scanner=False
    )
    assert third.resume() is False


def _crash(service):
    """Stop a service the way a crash would: nothing cleared."""

    service._stop_scanner()
    service._observer.close()
    service._observer = None


def test_a_resume_that_cant_happen_deletes_the_buffer_and_key(svc):
    service, _events, store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    stream, _ = _invoice_events(1_000_000.0 - 3000)
    for event in stream:
        service._observer.observe(event)
    service._observer.symbols()
    _crash(service)
    assert (root / "shadow" / "buffer.db").exists() and store.get(ACTIVE_ACCOUNT)
    # A new notice version: the old acknowledgement no longer counts, so
    # shadow mode can't come back on. It must not keep the buffer either.
    data = json.loads((root / "shadow" / "consent.json").read_text())
    data["events"][-1]["notice_version"] = "2000-01-01"
    (root / "shadow" / "consent.json").write_text(json.dumps(data))
    again = ShadowModeService(
        root, shadow_dir=root / "shadow", key_store=store, autostart_scanner=False
    )
    assert again.resume() is False
    assert not (root / "shadow" / "buffer.db").exists()
    assert store.get(ACTIVE_ACCOUNT) is None and store._values == {}
    assert json.loads((root / "shadow" / "state.json").read_text()) == {"active": False}


def test_live_tap_follows_the_current_observer(svc):
    service, _events, _store, _root = svc
    service.enable(service.notice()["sha256"], acknowledged=True)
    tap = service.live_tap()
    ax = {
        "element": {"role": "AXTextField", "name": "Amount"},
        "process": {"process_name": "Numbers"},
    }

    def press(t, ch):
        tap.publish(t, "action", {"name": "press", "key_char": ch, "structural_observation": ax})

    press(999_990.0, "1")
    # A work-site change replaces the observer while the recording runs.
    service.set_work_domains(["erp.example.com"])
    press(999_995.0, "2")
    tap.close()
    kinds = [s.kind for s in service._observer.symbols()]
    assert kinds.count("type") >= 1
    # After Delete everything and turn off, the same tap records nothing.
    tap2 = service.live_tap()
    service.purge_and_disable()
    tap2.publish(
        999_996.0, "action", {"name": "press", "key_char": "3", "structural_observation": ax}
    )
    tap2.close()
    assert service._observer is None and not (service.dir / "buffer.db").exists()


class _StubbornStore(MemoryKeyStore):
    def __init__(self) -> None:
        super().__init__()
        self.refuse = False

    def delete(self, account: str) -> bool:
        if self.refuse and account.startswith("shadow-key|") and not account.endswith("retired"):
            return False
        return super().delete(account)


def test_purge_says_so_when_the_keychain_kept_the_key(tmp_path):
    store = _StubbornStore()
    service = ShadowModeService(
        tmp_path, shadow_dir=tmp_path / "shadow", key_store=store, autostart_scanner=False
    )
    service.enable(service.notice()["sha256"], acknowledged=True)
    store.refuse = True
    result = service.purge_and_disable()
    assert result["active"] is False and result["key_deleted"] is False
    assert not (tmp_path / "shadow" / "buffer.db").exists()
    store.refuse = False
    again = ShadowModeService(
        tmp_path, shadow_dir=tmp_path / "shadow", key_store=store, autostart_scanner=False
    )
    assert again.purge_and_disable()["key_deleted"] is True and store._values == {}


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits; Windows uses ACLs")
def test_shadow_files_are_owner_only_from_the_start(svc, monkeypatch):
    import os

    service, _events, _store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    stream, _ = _invoice_events(1_000_000.0 - 3000)
    for event in stream:
        service._observer.observe(event)
    (prompt,) = service.scan()
    opened: list[tuple[str, int]] = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **k):
        opened.append((str(path), mode))
        return real_open(path, flags, mode, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    result = service.decide(prompt["motif_id"], "review_draft")
    monkeypatch.setattr(os, "open", real_open)
    assert opened and all(mode == 0o600 for _p, mode in opened)
    draft = Path(result["draft_path"])
    assert _mode_is(draft, "0o600")
    assert not list(draft.parent.glob(".*.tmp"))


def test_live_tap_feeds_the_miner_while_on(svc):
    service, _events, _store, _root = svc
    service.enable(service.notice()["sha256"], acknowledged=True)
    tap = service.live_tap()
    assert tap is not None
    ax = {
        "element": {"role": "AXTextField", "name": "Amount"},
        "process": {"process_name": "Numbers"},
    }
    for i, ch in enumerate("12"):
        tap.publish(
            999_990.0 + i * 0.1,
            "action",
            {"name": "press", "key_char": ch, "structural_observation": ax},
        )
    tap.close()
    (sym,) = service._observer.symbols()
    assert sym.kind == "type" and sym.value_shape == "digits"


def test_no_network_access(svc, monkeypatch):
    service, _events, _store, _root = svc

    def refuse(*_a, **_k):
        raise AssertionError("shadow mode attempted network access")

    for name in ("connect", "connect_ex", "sendto", "bind"):
        monkeypatch.setattr(socket.socket, name, refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    stream, _ = _invoice_events(1_000_000.0 - 3000)
    for event in stream:
        service._observer.observe(event)
    (prompt,) = service.scan()
    service.decide(prompt["motif_id"], "ignore")
    service.purge_and_disable()


# --------------------------------------------------------------- dispatcher


def test_shadow_commands_are_on_the_window_wire_only():
    from openadapt_desktop import socket_server

    for cmd in (
        "shadow_status",
        "shadow_notice",
        "shadow_enable",
        "shadow_purge_and_disable",
        "shadow_set_work_domains",
        "shadow_decide",
        "shadow_scan",
    ):
        assert cmd not in socket_server._SOCKET_COMMANDS


def test_engine_config_has_no_shadow_switch():
    from openadapt_desktop.config import EngineConfig

    assert not [name for name in EngineConfig.model_fields if "shadow" in name.lower()]


def test_dispatcher_serves_shadow_commands(tmp_path):
    from openadapt_desktop.config import EngineConfig
    from openadapt_desktop.dispatch import EngineDispatcher

    emitted: list[str] = []
    disp = EngineDispatcher(EngineConfig(data_dir=tmp_path), emit=lambda e, d: emitted.append(e))
    disp._shadow = ShadowModeService(
        tmp_path,
        shadow_dir=tmp_path / "shadow",
        emit=lambda e, d: disp.emit(e, d),
        key_store=MemoryKeyStore(),
        autostart_scanner=False,
    )
    assert disp.dispatch("shadow_status", {})["active"] is False
    assert disp.services.live_tap_provider() is None
    sha = disp.dispatch("shadow_notice", {})["sha256"]
    with pytest.raises(PermissionError):
        disp.dispatch("shadow_enable", {"notice_sha256": sha})
    on = disp.dispatch("shadow_enable", {"notice_sha256": sha, "acknowledged": True})
    assert on["active"] is True and STATE_EVENT in emitted
    tap = disp.services.live_tap_provider()
    assert tap is not None
    tap.close()
    assert disp.dispatch("shadow_scan", {}) == {"prompts": []}
    off = disp.dispatch("shadow_purge_and_disable", {})
    assert off["active"] is False
    assert disp.services.live_tap_provider() is None
    disp._shadow.close()


def test_controller_passes_a_tap_only_to_a_recorder_that_accepts_one(tmp_path):
    from openadapt_desktop.controller import RecordingController

    class Tap:
        closed = False

        def close(self):
            Tap.closed = True

    class NewRecorder:
        def __init__(self, path, task_description="", live_event_tap=None):
            pass

    class OldRecorder:
        def __init__(self, path, task_description=""):
            pass

    with_tap = RecordingController(tmp_path, live_tap_provider=lambda: Tap())
    assert "live_event_tap" in with_tap._live_tap_kwargs(NewRecorder)
    with_tap._close_live_tap()
    assert Tap.closed
    assert with_tap._live_tap_kwargs(OldRecorder) == {}
    off = RecordingController(tmp_path, live_tap_provider=lambda: None)
    assert off._live_tap_kwargs(NewRecorder) == {}
    assert RecordingController(tmp_path)._live_tap_kwargs(NewRecorder) == {}


def _fill(service):
    stream, _ = _invoice_events(1_000_000.0 - 3000)
    for event in stream:
        service._observer.observe(event)
    return service._observer.symbols()


def test_a_clean_exit_erases_the_buffer_and_key_and_resumes_empty(svc):
    service, _events, store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    assert _fill(service)
    old_key = store.get(ACTIVE_ACCOUNT)
    service.close()
    assert not list((root / "shadow").glob("buffer.db*"))
    assert store.get(ACTIVE_ACCOUNT) is None
    # It was on, so it comes back on, with a new key and an empty buffer.
    assert json.loads((root / "shadow" / "state.json").read_text())["active"] is True
    again = ShadowModeService(
        root,
        shadow_dir=root / "shadow",
        key_store=store,
        clock=Clock(1_000_000.0),
        autostart_scanner=False,
    )
    assert again.resume() is True
    assert again._observer.symbols() == []
    assert store.get(ACTIVE_ACCOUNT) not in (None, old_key)
    again.close()
    service.close()  # twice is harmless


def test_a_crash_leftover_is_erased_at_the_next_start(svc):
    service, _events, store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    assert _fill(service)
    old_key = store.get(ACTIVE_ACCOUNT)
    _crash(service)
    assert (root / "shadow" / "buffer.db").exists()
    again = ShadowModeService(
        root,
        shadow_dir=root / "shadow",
        key_store=store,
        clock=Clock(1_000_000.0),
        autostart_scanner=False,
    )
    assert again.resume() is True
    assert again._observer.symbols() == []
    assert store.get(ACTIVE_ACCOUNT) != old_key
    again.close()


def test_turn_off_erases_buffer_key_and_drafts_but_keeps_the_ignore_list(svc):
    service, _events, store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    _fill(service)
    (prompt,) = service.scan()
    service.decide(prompt["motif_id"], "review_draft")
    service.decide(prompt["motif_id"], "ignore")
    assert (root / "shadow" / "drafts").exists()
    result = service.turn_off()
    assert result["active"] is False and result["key_deleted"] is True
    assert not list((root / "shadow").glob("buffer.db*"))
    assert not (root / "shadow" / "drafts").exists()
    assert store.get(ACTIVE_ACCOUNT) is None
    assert json.loads((root / "shadow" / "ignored.json").read_text())["ids"]
    assert service.live_tap() is None


def test_pause_records_nothing_and_survives_a_restart(svc):
    service, _events, store, root = svc
    service.set_work_domains(["erp.example.com"])
    service.enable(service.notice()["sha256"], acknowledged=True)
    assert service.pause()["paused"] is True
    assert _fill(service) == []
    service.close()
    again = ShadowModeService(
        root,
        shadow_dir=root / "shadow",
        key_store=store,
        clock=Clock(1_000_000.0),
        autostart_scanner=False,
    )
    assert again.resume() is True and again.status()["paused"] is True
    assert again.unpause()["paused"] is False
    service._observer = again._observer
    assert _fill(service)
    again.close()


def test_default_location_is_unsynced_app_data(tmp_path, monkeypatch):
    from openadapt_desktop import shadow_mode

    _home(monkeypatch, tmp_path)
    monkeypatch.setattr(shadow_mode.sys, "platform", "darwin")
    expected = tmp_path / "Library" / "Application Support" / "ai.openadapt.desktop" / "shadow"
    assert shadow_mode.default_shadow_dir() == expected
    monkeypatch.setattr(shadow_mode.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))
    expected = tmp_path / "AppData" / "Local" / "ai.openadapt.desktop" / "shadow"
    assert shadow_mode.default_shadow_dir() == expected
    # Not under data_dir, which a person may point at a synced folder.
    service = ShadowModeService(tmp_path / "Documents" / "OpenAdapt", autostart_scanner=False)
    assert service.dir == expected


def test_refuses_to_start_in_a_synced_folder(tmp_path, monkeypatch):
    storage = pytest.importorskip("openadapt_capture.shadow.storage")
    _home(monkeypatch, tmp_path)
    store = MemoryKeyStore()
    for synced in (
        tmp_path / "Documents" / "shadow",
        tmp_path / "Library" / "CloudStorage" / "OneDrive-Corp" / "shadow",
        tmp_path / "Dropbox" / "shadow",
    ):
        service = ShadowModeService(
            tmp_path, shadow_dir=synced, key_store=store, autostart_scanner=False
        )
        with pytest.raises(storage.ShadowStorageRefused):
            service.enable(service.notice()["sha256"], acknowledged=True)
        assert not synced.exists() and store._values == {}


def test_engine_shutdown_command_erases_the_buffer_before_exit(tmp_path, monkeypatch):
    import io

    from openadapt_desktop.config import EngineConfig
    from openadapt_desktop.ipc import IPCHandler

    handler = IPCHandler(EngineConfig(data_dir=tmp_path))
    store = MemoryKeyStore()
    handler.dispatcher._shadow = ShadowModeService(
        tmp_path, shadow_dir=tmp_path / "shadow", key_store=store, autostart_scanner=False
    )
    handler.dispatcher._shadow_resumed = True
    handler.dispatcher._shadow.enable(
        handler.dispatcher._shadow.notice()["sha256"], acknowledged=True
    )
    assert (tmp_path / "shadow" / "buffer.db").exists()
    out = io.StringIO()
    monkeypatch.setattr(
        "sys.stdin", io.StringIO('{"id":"shutdown","cmd":"shutdown","params":{}}\n')
    )
    monkeypatch.setattr("sys.stdout", out)
    handler.run()
    assert not list((tmp_path / "shadow").glob("buffer.db*"))
    assert store.get(ACTIVE_ACCOUNT) is None
    replies = [json.loads(line) for line in out.getvalue().splitlines() if line.strip()]
    assert {"id": "shutdown", "status": "ok", "data": {"stopping": True}} in replies


#: Counsel struck this claim: backup, sync, and endpoint security tools can
#: copy local files. Say what OpenAdapt does instead.
STRUCK_CLAIM = __import__("re").compile(
    r"nothing\s+(?:here\s+|it\s+records\s+)?leaves\s+th(?:is|e)\s+computer", 2
)


def test_the_struck_claim_appears_nowhere_in_shadow_copy():
    root = Path(__file__).resolve().parents[2]
    files = [
        root / "README.md",
        root / "docs" / "SHADOW_MODE.md",
        root / "docs" / "COMPLIANCE.md",
        root / "openadapt_desktop" / "shadow_mode.py",
        root / "src" / "lib" / "shadowMode.ts",
        root / "src" / "ui" / "ShadowMode.tsx",
        root / "src" / "ui" / "ShadowMode.test.tsx",
        root / "src-tauri" / "src" / "tray.rs",
    ]
    hits = [
        str(f.relative_to(root)) for f in files if f.exists() and STRUCK_CLAIM.search(f.read_text())
    ]
    assert hits == []
    assert not STRUCK_CLAIM.search(shadow.notice()["text"])
    assert STRUCK_CLAIM.search("Nothing leaves this computer.")
