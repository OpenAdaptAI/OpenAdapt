"""Tests for hotkey/shortcut handling."""

import contextlib
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest

from openadapt_tray import shortcuts
from openadapt_tray.shortcuts import HotkeyConfig, HotkeyManager, format_hotkey


class TestFormatHotkey:
    """The menu shows a hotkey in the platform's form, not pynput syntax."""

    @pytest.mark.parametrize(
        ("spec", "platform", "expected"),
        [
            ("<ctrl>+<shift>+r", "darwin", "⌃⇧R"),
            ("<ctrl>+<shift>+r", "win32", "Ctrl+Shift+R"),
            ("<ctrl>+<shift>+r", "linux", "Ctrl+Shift+R"),
            ("<cmd>+r", "darwin", "⌘R"),
            ("<cmd>+r", "win32", "Win+R"),
            ("<cmd>+r", "linux", "Super+R"),
            ("<alt>+<ctrl_l>+<shift_r>+x", "darwin", "⌥⌃⇧X"),
            ("<alt>+<ctrl_l>+<shift_r>+x", "win32", "Alt+Ctrl+Shift+X"),
            ("<ctrl>+<f5>", "darwin", "⌃F5"),
            ("<ctrl>+<page_up>", "linux", "Ctrl+Page Up"),
            ("<cmd>+<space>", "darwin", "⌘Space"),
            ("<cmd_r>+<alt_gr>+e", "darwin", "⌘⌥E"),
            ("<alt_gr>+e", "linux", "AltGr+E"),
            ("<ctrl>++", "win32", "Ctrl++"),
        ],
    )
    def test_formats_for_platform(self, spec, platform, expected):
        assert format_hotkey(spec, platform) == expected

    def test_defaults_to_running_platform(self):
        with patch("sys.platform", "darwin"):
            assert format_hotkey("<ctrl>+<shift>+r") == "⌃⇧R"
        with patch("sys.platform", "win32"):
            assert format_hotkey("<ctrl>+<shift>+r") == "Ctrl+Shift+R"

    @pytest.mark.parametrize(
        "spec", ["", "<ctrl>+", "+", "ctrl+shift+r", "<>+r", "<ctrl>+rr"]
    )
    @pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
    def test_malformed_spec_is_shown_unchanged(self, spec, platform):
        # A bad hotkey in tray.json must not stop the menu from rendering, and
        # must not be dressed up as a shortcut pynput would never register.
        assert format_hotkey(spec, platform) == spec

    @pytest.mark.parametrize(("value", "shown"), [(None, "None"), (5, "5")])
    def test_non_string_setting_is_shown_as_text(self, value, shown):
        # tray.json can hold null or a number here; the menu must still build.
        assert format_hotkey(value, "darwin") == shown

    def test_virtual_key_code_is_left_as_written(self):
        # pynput accepts "<65>" as a virtual key code; there is no honest
        # display name for it, so show what the user configured.
        assert format_hotkey("<ctrl>+<65>", "win32") == "Ctrl+<65>"


class TestHotkeyManager:
    """Tests for HotkeyManager class."""

    @pytest.fixture(autouse=True)
    def _no_real_layout_read(self):
        # These tests mock pynput.keyboard. On macOS, start() would otherwise
        # read the real keyboard layout and patch the real pynput module.
        with patch.object(shortcuts, "_pin_macos_keyboard_layout", return_value=True):
            yield

    def test_initialization(self):
        """Test HotkeyManager initialization."""
        manager = HotkeyManager()
        assert manager.config is not None
        assert manager.is_running() is False

    def test_initialization_with_config(self):
        """Test HotkeyManager initialization with custom config."""
        config = HotkeyConfig(toggle_recording="<cmd>+r")
        manager = HotkeyManager(config)
        assert manager.config.toggle_recording == "<cmd>+r"

    def test_register_handler(self):
        """Test registering a hotkey handler."""
        manager = HotkeyManager()
        handler = MagicMock()

        manager.register("<ctrl>+a", handler)
        assert "<ctrl>+a" in manager._handlers
        assert manager._handlers["<ctrl>+a"] == handler

    def test_unregister_handler(self):
        """Test unregistering a hotkey handler."""
        manager = HotkeyManager()
        handler = MagicMock()

        manager.register("<ctrl>+a", handler)
        manager.unregister("<ctrl>+a")
        assert "<ctrl>+a" not in manager._handlers

    def test_unregister_nonexistent_handler(self):
        """Test unregistering a non-existent handler doesn't raise."""
        manager = HotkeyManager()
        manager.unregister("<ctrl>+nonexistent")  # Should not raise

    def test_start_creates_listener(self):
        """Test that start creates hotkey listener."""
        with patch("pynput.keyboard") as mock_keyboard:
            manager = HotkeyManager()
            handler = MagicMock()
            manager.register("<ctrl>+a", handler)

            mock_listener = MagicMock()
            mock_keyboard.GlobalHotKeys.return_value = mock_listener

            manager.start()

            assert manager.is_running()
            mock_keyboard.GlobalHotKeys.assert_called_once()
            mock_listener.start.assert_called_once()

            manager.stop()

    def test_start_twice_does_nothing(self):
        """Test that starting twice doesn't create multiple listeners."""
        with patch("pynput.keyboard") as mock_keyboard:
            manager = HotkeyManager()
            handler = MagicMock()
            manager.register("<ctrl>+a", handler)

            mock_listener = MagicMock()
            mock_keyboard.GlobalHotKeys.return_value = mock_listener

            manager.start()
            manager.start()  # Second start should be no-op

            assert mock_keyboard.GlobalHotKeys.call_count == 1
            manager.stop()

    def test_stop_stops_listener(self):
        """Test that stop stops the listener."""
        with patch("pynput.keyboard") as mock_keyboard:
            manager = HotkeyManager()
            handler = MagicMock()
            manager.register("<ctrl>+a", handler)

            mock_listener = MagicMock()
            mock_keyboard.GlobalHotKeys.return_value = mock_listener

            manager.start()
            manager.stop()

            assert manager.is_running() is False
            mock_listener.stop.assert_called_once()

    def test_triple_ctrl_not_registered_by_default(self):
        """Test triple ctrl handler setup when not registered."""
        manager = HotkeyManager()
        handler = MagicMock()
        manager.register("<ctrl>+a", handler)  # Regular hotkey, not triple-ctrl

        # Should not have triple-ctrl handler
        assert "<ctrl>+<ctrl>+<ctrl>" not in manager._handlers

    def test_ctrl_count_reset(self):
        """Test that ctrl count resets after timeout."""
        manager = HotkeyManager()
        manager._ctrl_count = 2
        manager._reset_ctrl_count()
        assert manager._ctrl_count == 0

    def test_missing_pynput_handles_gracefully(self):
        """Test graceful handling when pynput is not available."""
        with patch("pynput.keyboard") as mock_keyboard:
            manager = HotkeyManager()
            handler = MagicMock()
            manager.register("<ctrl>+a", handler)
            mock_keyboard.GlobalHotKeys.side_effect = Exception("Mock error")

            manager.start()  # Should not raise
            # Should still mark as running since the attempt was made
            assert manager.is_running() is True

    def test_stop_without_start(self):
        """Test that stop without start doesn't raise."""
        manager = HotkeyManager()
        manager.stop()  # Should not raise


class TestTripleCtrlDetection:
    """Tests for triple-ctrl detection logic."""

    def test_on_ctrl_press_increments_count(self):
        """Test that ctrl press increments counter."""
        manager = HotkeyManager()
        manager._ctrl_count = 0
        manager._on_ctrl_press()
        assert manager._ctrl_count == 1

    def test_triple_ctrl_triggers_handler(self):
        """Test that three ctrl presses trigger the handler."""
        manager = HotkeyManager()
        handler = MagicMock()
        manager.register("<ctrl>+<ctrl>+<ctrl>", handler)

        manager._ctrl_count = 0
        manager._on_ctrl_press()
        manager._on_ctrl_press()
        manager._on_ctrl_press()

        handler.assert_called_once()
        assert manager._ctrl_count == 0  # Reset after trigger

    def test_triple_ctrl_resets_after_trigger(self):
        """Test that count resets after triggering."""
        manager = HotkeyManager()
        handler = MagicMock()
        manager.register("<ctrl>+<ctrl>+<ctrl>", handler)

        for _ in range(3):
            manager._on_ctrl_press()

        assert manager._ctrl_count == 0

    def test_ctrl_timer_created_on_press(self):
        """Test that a timer is created on ctrl press."""
        manager = HotkeyManager()
        handler = MagicMock()
        manager.register("<ctrl>+<ctrl>+<ctrl>", handler)

        manager._on_ctrl_press()
        assert manager._ctrl_timer is not None
        assert manager._ctrl_count == 1

        # Cleanup
        if manager._ctrl_timer:
            manager._ctrl_timer.cancel()

    def test_bad_handler_doesnt_crash(self):
        """Test that a failing handler doesn't crash the manager."""
        manager = HotkeyManager()

        def bad_handler():
            raise ValueError("Intentional error")

        manager.register("<ctrl>+<ctrl>+<ctrl>", bad_handler)

        # Should not raise
        for _ in range(3):
            manager._on_ctrl_press()


class TestMacOSKeyboardLayoutPinning:
    """pynput's listener threads must not call the macOS layout API themselves.

    With an NSApplication in the process, macOS 15 aborts when that API runs
    off the main thread, and ``openadapt-tray`` exited with SIGABRT at startup.
    """

    def _fake_pynput_darwin(self, read_layout):
        @contextlib.contextmanager
        def keycode_context():
            yield read_layout()

        util = types.ModuleType("pynput._util.darwin")
        util.keycode_context = keycode_context
        listener = types.ModuleType("pynput.keyboard._darwin")
        listener.keycode_context = keycode_context
        return {"pynput._util.darwin": util, "pynput.keyboard._darwin": listener}

    def test_listeners_get_the_layout_read_on_the_main_thread(self):
        reads = []

        def read_layout():
            reads.append(threading.current_thread())
            return (91, b"layout")

        modules = self._fake_pynput_darwin(read_layout)
        with patch.dict(sys.modules, modules):
            assert shortcuts._pin_macos_keyboard_layout() is True
            listener_module = sys.modules["pynput.keyboard._darwin"]

            seen = []

            def listener_thread():
                with listener_module.keycode_context() as layout:
                    seen.append(layout)

            thread = threading.Thread(target=listener_thread)
            thread.start()
            thread.join()

        assert reads == [threading.main_thread()]
        assert seen == [(91, b"layout")]

    def test_refuses_off_the_main_thread(self):
        results = []
        thread = threading.Thread(
            target=lambda: results.append(shortcuts._pin_macos_keyboard_layout())
        )
        thread.start()
        thread.join()
        assert results == [False]

    def test_unreadable_layout_refuses(self):
        def read_layout():
            raise OSError("no input source")

        with patch.dict(sys.modules, self._fake_pynput_darwin(read_layout)):
            assert shortcuts._pin_macos_keyboard_layout() is False

    def test_changed_pynput_internals_refuse(self):
        modules = self._fake_pynput_darwin(lambda: (91, b"layout"))
        del modules["pynput.keyboard._darwin"].keycode_context
        with patch.dict(sys.modules, modules):
            assert shortcuts._pin_macos_keyboard_layout() is False

    def test_macos_start_pins_the_layout_before_any_listener(self):
        order = []
        with patch("pynput.keyboard") as keyboard, patch(
            "openadapt_tray.shortcuts.sys.platform", "darwin"
        ), patch.object(
            shortcuts,
            "_pin_macos_keyboard_layout",
            side_effect=lambda: order.append("pin") or True,
        ):
            keyboard.GlobalHotKeys.side_effect = lambda *a: order.append(
                "listener"
            ) or MagicMock()
            manager = HotkeyManager()
            manager.register("<ctrl>+a", MagicMock())
            manager.start()
            manager.stop()
        assert order == ["pin", "listener"]

    def test_macos_start_without_a_pinned_layout_starts_no_listener(self):
        with patch("pynput.keyboard") as keyboard, patch(
            "openadapt_tray.shortcuts.sys.platform", "darwin"
        ), patch.object(shortcuts, "_pin_macos_keyboard_layout", return_value=False):
            manager = HotkeyManager()
            manager.register("<ctrl>+a", MagicMock())
            manager.register("<ctrl>+<ctrl>+<ctrl>", MagicMock())
            manager.start()
        keyboard.GlobalHotKeys.assert_not_called()
        keyboard.Listener.assert_not_called()
