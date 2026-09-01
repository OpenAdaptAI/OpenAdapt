"""Static release-boundary checks for the native `openadapt://` handler."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_registers_only_the_openadapt_scheme() -> None:
    config = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())
    assert config["plugins"]["deep-link"]["desktop"]["schemes"] == ["openadapt"]
    assert config["bundle"]["targets"] == ["dmg", "msi", "nsis", "deb", "appimage"]


def test_single_instance_precedes_deep_link_and_handoff_is_fixed() -> None:
    main = (ROOT / "src-tauri/src/main.rs").read_text()
    pairing = (ROOT / "src-tauri/src/pairing.rs").read_text()
    single = main.index(".plugin(tauri_plugin_single_instance::init")
    deep_link = main.index(".plugin(tauri_plugin_deep_link::init())")
    assert single < deep_link
    assert 'command: "connect_uri"' in pairing
    assert 'command: "claim_runner_uri"' in pairing
    assert 'json!({ "uri": action.uri })' in pairing
    assert "std::process::Command" not in pairing
    assert "open_external" not in pairing
    assert "ShellExt" not in pairing
    assert 'Some("connect") => connect_action' in pairing
    assert 'Some("runner") => runner_action' in pairing
    assert "pack" not in pairing.split("fn connect_action", 1)[1].split("fn runner_action", 1)[0]


def test_python_pairing_action_has_no_shell_or_navigation_escape_hatch() -> None:
    pairing = (ROOT / "engine/auth/pairing.py").read_text()
    store = (ROOT / "engine/auth/store.py").read_text()
    dispatch = (ROOT / "engine/dispatch.py").read_text()
    assert '"connect_uri": self.connect_uri' in dispatch
    assert '"claim_runner_uri": self.claim_runner_uri' in dispatch
    assert "subprocess" not in pairing
    assert "shell=" not in pairing
    assert "webbrowser" not in pairing
    assert "os.system" not in pairing
    assert "follow_redirects=False" in pairing
    assert '_PAIRING_STAGE_ACCOUNT = "__pairing_stage__"' in store
    assert "stage_pairing_credential" in store
    assert "set_password" in store
    assert "write_text" not in pairing + store
    assert "open(" not in pairing + store
    assert "logger." not in pairing


def test_python_runner_bind_is_parse_only() -> None:
    runner_bind = (ROOT / "engine/auth/runner_bind.py").read_text()
    assert "def parse_runner_uri" in runner_bind
    assert "httpx" not in runner_bind
    assert "keyring" not in runner_bind
    assert "subprocess" not in runner_bind
    assert "webbrowser" not in runner_bind
    assert "os.system" not in runner_bind
    assert "wait_seconds" not in runner_bind
    assert "DEFAULT_WAIT_S" not in runner_bind
    assert "oar_" in runner_bind
    assert "oap_" in runner_bind
    assert "oab_" in runner_bind
    assert "oals_" in runner_bind
    assert "BIND_HEX_BODY_RE" in runner_bind
    assert "LEASE_BASE64URL_BODY_RE" in runner_bind
    authoring = (ROOT / "engine/authoring_runner.py").read_text(encoding="utf-8")
    assert 'action not in MAILBOX_ACTIONS' in authoring
    assert '"allow"' in authoring
    assert "win_agent" in authoring
    assert "from openadapt_flow.backends.win_agent" not in authoring
