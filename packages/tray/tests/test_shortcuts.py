"""Tests for hotkey/shortcut handling."""

import pytest
from unittest.mock import patch, MagicMock
import threading
import time

from openadapt_tray.shortcuts import HotkeyManager, HotkeyConfig


class TestHotkeyManager:
    """Tests for HotkeyManager class."""

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
