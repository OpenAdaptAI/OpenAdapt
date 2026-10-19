"""A browser page shown to Flow as a pixel-only remote-desktop window.

:class:`PageWindowClient` is a
:class:`~openadapt_flow.backends.remote_display.WindowClient` over a
Playwright page. Flow's real ``RemoteDisplayBackend`` (``rdp`` with a local
client window) and ``CitrixWorkspaceBackend`` (``citrix``) drive it exactly as
they drive an RDP or Citrix client: they capture the window's pixels and send
pointer and key input at window coordinates. They never see the page's DOM.

This is the same stand-in the Citrix Workspace benchmark uses
(``benchmark/citrix_workspace/``), without Docker or noVNC, so it runs in the
unit suite. It is not ICA/HDX or a real RDP session.
"""

from __future__ import annotations

import io
from typing import Any, Optional

from PIL import Image

from openadapt_flow.backends.remote_display import WindowInfo

#: Named keys the client can send, in the order of their fake key codes.
_KEYS = (
    "Enter",
    "Tab",
    "Escape",
    "Backspace",
    "Delete",
    "ArrowUp",
    "ArrowDown",
    "ArrowLeft",
    "ArrowRight",
    "Home",
    "End",
    "PageUp",
    "PageDown",
    "Space",
)
_KEY_TOKENS = {name.lower(): i for i, name in enumerate(_KEYS)}
_KEY_TOKENS.update({"return": 0, "esc": 2, "up": 5, "down": 6, "left": 7, "right": 8})
_FLAGS = {"shift": "Shift", "control": "Control", "ctrl": "Control", "alt": "Alt"}


class PageWindowClient:
    """One fixed window, owner ``owner`` and title ``title``, over ``page``."""

    def __init__(
        self, page: Any, *, owner: str, title: str, width: int = 1280, height: int = 800
    ) -> None:
        self.page = page
        self._w, self._h = width, height
        self.window = WindowInfo(
            window_id=7,
            owner=owner,
            title=title,
            pid=4343,
            bounds=(0.0, 0.0, float(width), float(height)),
            on_screen=True,
        )
        self.inputs = 0

    # -- WindowClient --------------------------------------------------------

    def input_trusted(self) -> bool:
        return True

    def frontmost_pid(self) -> Optional[int]:
        return self.window.pid

    def find_windows(self, owner: str, title: Optional[str]) -> list[WindowInfo]:
        if owner.lower() not in self.window.owner.lower():
            return []
        if title is not None and title not in self.window.title:
            return []
        return [self.window]

    def key_window_id(self, pid: int) -> Optional[int]:
        return self.window.window_id if pid == self.window.pid else None

    def window_at_point(self, x: float, y: float) -> Optional[int]:
        return self.window.window_id

    def capture(self, window_id: int) -> tuple[bytes, int, int]:
        raw = self.page.screenshot(
            clip={"x": 0, "y": 0, "width": self._w, "height": self._h}
        )
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue(), img.width, img.height

    def activate(self, pid: int) -> None:
        return None

    def mouse_move(self, x: float, y: float) -> None:
        self.page.mouse.move(x, y)

    def mouse(
        self, x: float, y: float, *, button: str, down: bool, click_count: int = 1
    ) -> None:
        self.inputs += 1
        self.page.mouse.move(x, y)
        if down:
            self.page.mouse.down(button=button, click_count=click_count)
        else:
            self.page.mouse.up(button=button, click_count=click_count)

    def type_chars(self, text: str) -> None:
        self.inputs += 1
        self.page.keyboard.type(text)

    def key(self, keycode: int, *, down: bool, flags: list[str]) -> None:
        self.inputs += 1
        mods = [_FLAGS[f] for f in flags if f in _FLAGS]
        name = _KEYS[keycode]
        if down:
            for mod in mods:
                self.page.keyboard.down(mod)
            self.page.keyboard.down(name)
        else:
            self.page.keyboard.up(name)
            for mod in reversed(mods):
                self.page.keyboard.up(mod)

    def scroll(self, dx: int, dy: int) -> None:
        self.inputs += 1
        self.page.mouse.wheel(dx, dy)

    def resolve_key(self, token: str) -> Optional[tuple[int, bool]]:
        code = _KEY_TOKENS.get(token.strip().lower())
        return None if code is None else (code, False)
