"""Where the shadow buffer lives, pausing it, and clearing it on exit."""

from __future__ import annotations

import json
import random

import pytest

from openadapt_capture.shadow import (
    CollectingNotifier,
    ShadowConfig,
    ShadowEvent,
    ShadowObserver,
    ShadowStorageRefused,
    SuppressionList,
    app_data_dir,
    require_unsynced,
    storage,
)
from openadapt_capture.shadow.consent import NOTICE_VERSION, Acknowledgement, notice_sha256
from openadapt_capture.shadow.keys import ACTIVE_ACCOUNT, MemoryKeyStore
from tests.shadow_support.streams import repeated_task_stream

ACK = Acknowledgement(NOTICE_VERSION, notice_sha256(), 0.0)
WORK = frozenset({"erp.example.com"})


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for env in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer", "APPDATA"):
        monkeypatch.delenv(env, raising=False)
    return home


def test_app_data_is_application_support_or_local_app_data(home, monkeypatch):
    monkeypatch.setattr(storage.sys, "platform", "darwin")
    assert app_data_dir("ai.x") == home / "Library" / "Application Support" / "ai.x"
    monkeypatch.setattr(storage.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
    assert app_data_dir("ai.x") == home / "AppData" / "Local" / "ai.x"
    monkeypatch.setattr(storage.sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert app_data_dir("ai.x") == home / ".local" / "share" / "ai.x"


def test_app_data_folders_are_not_sync_roots(home, monkeypatch):
    for platform in ("darwin", "win32", "linux"):
        monkeypatch.setattr(storage.sys, "platform", platform)
        monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))
        path = app_data_dir("ai.openadapt.desktop") / "shadow" / "buffer.db"
        assert require_unsynced(path) == path.resolve()


@pytest.mark.parametrize(
    "relative",
    [
        "Documents/OpenAdapt/buffer.db",
        "Desktop/buffer.db",
        "Library/Mobile Documents/com~apple~CloudDocs/buffer.db",
        "Library/CloudStorage/OneDrive-Personal/buffer.db",
        "Library/CloudStorage/GoogleDrive-me/My Drive/buffer.db",
        "Dropbox/buffer.db",
        "OneDrive - Example Corp/buffer.db",
        "iCloudDrive/buffer.db",
        "Box/buffer.db",
    ],
)
def test_known_sync_roots_are_refused(home, relative):
    with pytest.raises(ShadowStorageRefused):
        require_unsynced(home / relative)


def test_a_symlink_into_a_synced_folder_is_refused(home):
    synced = home / "Dropbox" / "shadow"
    synced.mkdir(parents=True)
    support = home / "Library" / "Application Support"
    support.mkdir(parents=True)
    (support / "ai.openadapt.desktop").symlink_to(synced, target_is_directory=True)
    with pytest.raises(ShadowStorageRefused):
        require_unsynced(support / "ai.openadapt.desktop" / "buffer.db")


def test_sync_roots_named_by_onedrive_and_dropbox_are_refused(home, tmp_path, monkeypatch):
    onedrive = tmp_path / "corp-files"
    monkeypatch.setenv("OneDriveCommercial", str(onedrive))
    with pytest.raises(ShadowStorageRefused):
        require_unsynced(onedrive / "buffer.db")
    elsewhere = tmp_path / "team-share"
    (home / ".dropbox").mkdir()
    (home / ".dropbox" / "info.json").write_text(json.dumps({"business": {"path": str(elsewhere)}}))
    with pytest.raises(ShadowStorageRefused):
        require_unsynced(elsewhere / "buffer.db")


def test_the_observer_refuses_a_buffer_in_a_synced_folder(home):
    store = MemoryKeyStore()
    with pytest.raises(ShadowStorageRefused):
        ShadowObserver(
            ShadowConfig(enabled=True, store_path=home / "Documents" / "buffer.db"),
            acknowledgement=ACK,
            key_store=store,
        )
    # It refused before making a key or a file.
    assert store._values == {}
    assert not (home / "Documents").exists()


def _observer(clock, **kw):
    cfg = {"enabled": True, "work_domains": WORK, **kw.pop("cfg", {})}
    return ShadowObserver(ShadowConfig(**cfg), acknowledgement=ACK, clock=clock, **kw)


def test_pause_drops_events_and_typing_in_progress(tmp_path):
    clock = Clock(10_000.0)
    obs = _observer(clock, key_store=MemoryKeyStore())
    obs.observe(ShadowEvent(clock.now, "focus", app="Excel"))
    obs.observe(
        ShadowEvent(clock.now, "type", app="Excel", role="textfield", label="Name", text="ab")
    )
    obs.pause()
    assert obs.paused
    obs.observe(ShadowEvent(clock.now + 1, "focus", app="Patient chart"))
    obs.observe(
        ShadowEvent(clock.now + 2, "click", app="Patient chart", role="button", label="Open")
    )
    kinds = [symbol.token for symbol in obs.symbols()]
    assert not any("Patient" in token or "type" in token for token in kinds), kinds
    obs.unpause()
    obs.observe(ShadowEvent(clock.now + 3, "focus", app="Mail"))
    assert any("Mail" in symbol.token for symbol in obs.symbols())


def test_shutdown_clears_the_buffer_and_key_but_keeps_the_ignore_list(tmp_path):
    store = MemoryKeyStore()
    clock = Clock(10_000.0)
    suppression = SuppressionList(tmp_path / "ignored.json")
    obs = _observer(
        clock,
        key_store=store,
        suppression=suppression,
        notifier=CollectingNotifier(),
        cfg={"store_path": tmp_path / "ring.db"},
    )
    events, end = repeated_task_stream(random.Random(7), 4, start=clock.now)
    clock.now = end
    obs.observe_all(events)
    (prompt,) = obs.scan()
    obs.decide(prompt.motif_id, "ignore")
    assert obs.shutdown() is True
    assert not obs.enabled
    assert not list(tmp_path.glob("ring.db*"))
    assert store.get(ACTIVE_ACCOUNT) is None
    assert json.loads((tmp_path / "ignored.json").read_text())["ids"]
