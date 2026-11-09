"""Labels in the menu as people see it, built from the default settings."""

from unittest.mock import MagicMock, patch

import pytest

from openadapt_tray.config import TrayConfig
from openadapt_tray.menu import MenuBuilder
from openadapt_tray.state import AppState, CredentialStatus, TrayState


def _labels(menu):
    """Return every item label in a pystray menu, submenus included."""
    labels = []
    for item in menu.items:
        if item is menu.SEPARATOR:
            continue
        labels.append(str(item.text))
        if item.submenu is not None:
            labels += _labels(item.submenu)
    return labels


@pytest.mark.parametrize(
    "credential",
    [CredentialStatus(), CredentialStatus.signed_out(), CredentialStatus.unknown()],
)
@pytest.mark.parametrize(
    ("platform", "first"),
    [
        ("darwin", "Start Recording (⌃⇧R)"),
        ("win32", "Start Recording (Ctrl+Shift+R)"),
        ("linux", "Start Recording (Ctrl+Shift+R)"),
    ],
)
def test_default_idle_menu_shows_readable_labels(tmp_path, platform, first, credential):
    """The menu from a fresh install shows no pynput syntax and no Login item.

    The tray once showed "Start Recording (<ctrl>+<shift>+r)" and a
    "Login..." item. This builds the whole menu from the real default
    settings, not a mock, and checks every label.
    """
    config = TrayConfig(captures_directory=str(tmp_path / "captures"))
    app = MagicMock()
    app.config = config
    app.state.current = AppState(state=TrayState.IDLE, credential=credential)

    with patch("sys.platform", platform):
        labels = _labels(MenuBuilder(app).build())

    assert labels[0] == first
    assert [label for label in labels if "<" in label or ">" in label] == []
    assert [label for label in labels if label.lower().startswith("login")] == []
    assert any(label.startswith("Account: ") for label in labels)
    # The setting and the hotkey registration keep pynput's syntax.
    assert config.hotkeys.toggle_recording == "<ctrl>+<shift>+r"
