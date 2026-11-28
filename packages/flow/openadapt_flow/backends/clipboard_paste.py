"""Safe clipboard paste-and-verify helper with guaranteed clipboard scrub.

Remote and desktop surfaces (VDI, Citrix, RDP, noVNC canvas, native macOS,
Windows, and Linux apps) often lose characters or mangle unicode/punctuation
under synthetic keystroke injection. Pasting through the host clipboard avoids
dropped characters, but leaving sensitive values (such as PHI or credentials)
on the host clipboard after paste is a data leak.

:func:`paste_and_verify` enforces a strict lifecycle:
1. Snapshot the current host clipboard text.
2. Write the payload to the host clipboard and deliver the paste chord.
3. Verify that the exact payload landed via Select-All+Copy readback or an
   exact OCR reader.
4. In a ``finally`` block (which runs even on exception, crash, or halt),
   zero the host clipboard and restore the prior snapshot.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable

__all__ = [
    "ClipboardAdapter",
    "HostClipboard",
    "PasteVerifyResult",
    "paste_and_verify",
]


@runtime_checkable
class ClipboardAdapter(Protocol):
    """Minimal text clipboard interface for host clipboard snapshotting."""

    def read_text(self) -> Optional[str]:
        """Return the current clipboard text, or None if unavailable."""
        ...

    def write_text(self, text: str) -> None:
        """Write ``text`` to the clipboard."""
        ...

    def clear(self) -> None:
        """Overwrite the clipboard with an empty payload."""
        ...


class HostClipboard:
    """Best-effort host OS clipboard adapter (macOS, Linux, Windows)."""

    def __init__(
        self, *, platform: Optional[str] = None, timeout_s: float = 5.0
    ) -> None:
        self._platform = platform or sys.platform
        self._timeout_s = timeout_s

    def read_text(self) -> Optional[str]:
        if self._platform == "darwin":
            res = subprocess.run(
                ["pbpaste"],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
            return res.stdout if res.returncode == 0 else None
        if self._platform == "win32":
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
            return res.stdout if res.returncode == 0 else None
        for cmd in (
            ["xclip", "-selection", "clipboard", "-o"],
            ["xsel", "--clipboard", "--output"],
        ):
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout_s,
                    check=False,
                )
            except FileNotFoundError:
                continue
            if res.returncode == 0:
                return res.stdout
        return None

    def write_text(self, text: str) -> None:
        if self._platform == "darwin":
            res = subprocess.run(
                ["pbcopy"],
                input=text,
                text=True,
                capture_output=True,
                timeout=self._timeout_s,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"pbcopy failed with exit code {res.returncode}")
            return
        if self._platform == "win32":
            res = subprocess.run(
                ["clip"],
                input=text,
                text=True,
                capture_output=True,
                timeout=self._timeout_s,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(f"clip failed with exit code {res.returncode}")
            return
        for cmd in (
            ["xclip", "-selection", "clipboard", "-i"],
            ["xsel", "--clipboard", "--input"],
        ):
            try:
                res = subprocess.run(
                    cmd,
                    input=text,
                    text=True,
                    capture_output=True,
                    timeout=self._timeout_s,
                    check=False,
                )
            except FileNotFoundError:
                continue
            if res.returncode == 0:
                return
            raise RuntimeError(f"{cmd[0]} failed with exit code {res.returncode}")
        raise RuntimeError("no supported host clipboard utility found (xclip/xsel)")

    def clear(self) -> None:
        self.write_text("")


@dataclass(frozen=True)
class PasteVerifyResult:
    """Outcome of :func:`paste_and_verify`."""

    verified: bool
    method: str
    expected: str
    observed: Optional[str]
    clipboard_zeroed: bool
    clipboard_restored: bool
    error: Optional[str] = None


def _default_ocr_text(png: bytes, region: Optional[tuple[int, int, int, int]]) -> str:
    from openadapt_flow import vision

    lines = vision.ocr(png, region=region)
    return " ".join(
        line.text for line in lines or [] if getattr(line, "text", "")
    ).strip()


def paste_and_verify(
    backend: Any,
    text: str,
    *,
    clipboard: Optional[ClipboardAdapter] = None,
    paste_chord: str = "ControlOrMeta+v",
    select_all_chord: str = "ControlOrMeta+a",
    copy_chord: str = "ControlOrMeta+c",
    verify_mode: str = "select_all_copy",
    ocr_reader: Optional[
        Callable[[bytes, Optional[tuple[int, int, int, int]]], str]
    ] = None,
    region: Optional[tuple[int, int, int, int]] = None,
    settle_s: float = 0.0,
) -> PasteVerifyResult:
    """Paste ``text`` through the host clipboard, verify exact landing, and scrub.

    The host clipboard is snapshotted before pasting, zeroed in a ``finally``
    block, and restored to its prior text even if delivery or verification
    raises an exception.

    Args:
        backend: A backend exposing ``press(chord)`` (and ``screenshot()`` when
            ``verify_mode == "ocr"``).
        text: The exact string to paste and verify.
        clipboard: Optional :class:`ClipboardAdapter` (defaults to
            :class:`HostClipboard`).
        paste_chord: Key chord used to paste (e.g. ``"Control+v"`` or
            ``"ControlOrMeta+v"``).
        select_all_chord: Key chord used to select the field contents before
            copy readback.
        copy_chord: Key chord used to copy the field contents back to the
            clipboard for verification.
        verify_mode: ``"select_all_copy"`` (default) or ``"ocr"``.
        ocr_reader: Optional callable ``(png_bytes, region) -> str`` for OCR
            verification. When omitted in ``"ocr"`` mode, uses
            :func:`openadapt_flow.vision.ocr`.
        region: Optional ``(x, y, w, h)`` crop region for OCR verification.
        settle_s: Optional post-paste settle delay in seconds.

    Returns:
        :class:`PasteVerifyResult` recording exact match and clipboard cleanup.
    """
    if verify_mode not in ("select_all_copy", "ocr"):
        raise ValueError(
            f"unsupported verify_mode {verify_mode!r}; expected 'select_all_copy' or 'ocr'"
        )

    clip = clipboard if clipboard is not None else HostClipboard()
    snapshot: Optional[str] = clip.read_text()
    clipboard_zeroed = False
    clipboard_restored = False

    try:
        clip.write_text(text)
        backend.press(paste_chord)
        if settle_s > 0:
            time.sleep(settle_s)

        if verify_mode == "select_all_copy":
            # Zero the clipboard before copying back so a no-op Ctrl+C cannot
            # read the stale payload we just put on the clipboard.
            clip.clear()
            backend.press(select_all_chord)
            backend.press(copy_chord)
            if settle_s > 0:
                time.sleep(settle_s)
            observed = clip.read_text()
            verified = observed == text
            error = (
                None
                if verified
                else "clipboard readback did not match pasted value exactly"
            )
        else:
            clip.clear()
            png = backend.screenshot()
            reader = ocr_reader if ocr_reader is not None else _default_ocr_text
            observed = reader(png, region)
            verified = observed == text
            error = (
                None if verified else "exact OCR readback did not match pasted value"
            )
    finally:
        # Always zero first so sensitive text is overwritten even if restore fails.
        try:
            clip.clear()
            clipboard_zeroed = True
        finally:
            if snapshot is not None:
                try:
                    clip.write_text(snapshot)
                    clipboard_restored = True
                except (OSError, RuntimeError, ValueError):
                    clipboard_restored = False
            else:
                clipboard_restored = True

    return PasteVerifyResult(
        verified=verified,
        method=verify_mode,
        expected=text,
        observed=observed,
        clipboard_zeroed=clipboard_zeroed,
        clipboard_restored=clipboard_restored,
        error=error,
    )
