"""A small Cocoa app for the live macOS focus check (test_window_input_focus.py).

It opens two document windows and, on request, a panel at the modal-panel
level, the level an NSAlert password prompt uses. Commands arrive one per
line on stdin and each reply is one JSON line on stdout:

    front A | front B   make that window key and the app frontmost
    panel               show the panel in front of window A
    close-panel         close the panel
    quit                exit
"""

from __future__ import annotations

import json
import os
import sys
import threading

import AppKit
from Foundation import NSObject
from PyObjCTools import AppHelper


def _window(title: str, x: float) -> AppKit.NSWindow:
    window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        AppKit.NSMakeRect(x, 200, 420, 300),
        AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable,
        AppKit.NSBackingStoreBuffered,
        False,
    )
    window.setTitle_(title)
    window.setReleasedWhenClosed_(False)
    return window


class Controller(NSObject):
    def setUp(self) -> None:
        self.windows = {"A": _window("OA focus A", 100), "B": _window("OA focus B", 300)}
        for window in self.windows.values():
            window.makeKeyAndOrderFront_(None)
        self.panel = AppKit.NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            AppKit.NSMakeRect(160, 260, 300, 120),
            AppKit.NSWindowStyleMaskTitled,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.panel.setTitle_("OA focus password")
        self.panel.setLevel_(AppKit.NSModalPanelWindowLevel)
        self.panel.setReleasedWhenClosed_(False)

    def handle_(self, line: str) -> None:
        command = line.split()
        app = AppKit.NSApplication.sharedApplication()
        if command[:1] == ["front"]:
            app.activateIgnoringOtherApps_(True)
            self.windows[command[1]].makeKeyAndOrderFront_(None)
        elif command == ["panel"]:
            app.activateIgnoringOtherApps_(True)
            self.panel.makeKeyAndOrderFront_(None)
        elif command == ["close-panel"]:
            self.panel.orderOut_(None)
        elif command == ["quit"]:
            app.terminate_(None)
            return
        reply = {
            "pid": os.getpid(),
            "A": int(self.windows["A"].windowNumber()),
            "B": int(self.windows["B"].windowNumber()),
            "panel": int(self.panel.windowNumber()),
        }
        print(json.dumps(reply), flush=True)


def _read_commands(controller: Controller) -> None:
    for line in sys.stdin:
        AppHelper.callAfter(controller.handle_, line.strip())
    AppHelper.callAfter(controller.handle_, "quit")


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
    controller = Controller.alloc().init()
    controller.setUp()
    threading.Thread(target=_read_commands, args=(controller,), daemon=True).start()
    AppHelper.callAfter(controller.handle_, "front A")
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
