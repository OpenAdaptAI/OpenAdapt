"""Opt-in native GI/AT-SPI smoke test in an isolated D-Bus and Xvfb session."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.environ.get("OPENADAPT_TEST_LINUX_ATSPI") != "1",
    reason="requires the isolated native Linux AT-SPI CI session",
)


def test_native_atspi_reads_a_gtk_button() -> None:
    """Catch native build/ABI and GI API failures hidden by fake-runtime tests."""
    import gi

    gi.require_version("GLib", "2.0")
    from gi.repository import GLib

    from openadapt_capture.structural_observer.linux import _ancestry, _fields, _GIAtspiRuntime

    print(f"PyGObject {gi.__version__}; GLib {GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}")
    fixture = """
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import GLib, Gtk
window = Gtk.Window(title='Capture native dependency smoke')
button = Gtk.Button(label='Capture native button')
window.add(button)
window.set_default_size(240, 100)
window.show_all()
GLib.timeout_add_seconds(30, Gtk.main_quit)
Gtk.main()
"""
    process = subprocess.Popen([sys.executable, "-c", fixture])
    try:
        runtime = _GIAtspiRuntime()
        deadline = time.monotonic() + 20
        button = None
        while time.monotonic() < deadline:
            assert process.poll() is None, "synthetic GTK application exited before observation"
            pending = list(runtime.children(runtime.desktop))
            visited = 0
            while pending and visited < 128:
                element = pending.pop()
                visited += 1
                if (
                    runtime.process_id(element) == process.pid
                    and element.get_name() == "Capture native button"
                ):
                    button = element
                    break
                pending.extend(runtime.children(element))
            if button is not None:
                break
            time.sleep(0.1)
        assert button is not None, "AT-SPI did not expose the synthetic GTK button"
        fields = _fields(runtime, button)
        assert fields["name"] == "Capture native button"
        assert fields["role"] == "push button"
        assert "click" in fields["supported_patterns"]
        bounds = fields["bounds"]
        assert bounds is not None
        assert bounds.right > bounds.left and bounds.bottom > bounds.top
        assert runtime.parent(button) is not None
        ancestry = _ancestry(runtime, button, 12)
        assert ancestry is not None
        assert any(ancestor.name == "Capture native dependency smoke" for ancestor in ancestry)
        assert runtime.process_id(button) == process.pid
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
