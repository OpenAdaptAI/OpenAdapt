"""The notice a person must acknowledge before shadow mode turns on.

:class:`~openadapt_capture.shadow.observer.ShadowObserver` refuses to start
enabled without an :class:`Acknowledgement` of the current notice. The notice
says what is captured and that it may include personal, confidential, or
health information; why (to suggest draft automations); how long it is kept
(an encrypted local buffer, cleared with its key at least every 24 hours and
when shadow mode stops); how to erase it (turn it off, or delete everything
and turn off) and when to pause it; and who can access it: OpenAdapt never
sends the data off the computer, no one can turn it on or read it through
OpenAdapt, and an administrator, IT, or endpoint security or backup software
could still inspect or copy it.

The acknowledgement is recorded on this computer only, in a JSON file that is
owner-only on macOS and Linux (``0600``): the notice version, a SHA-256 of the notice text, and the time. Turning
shadow mode off with **Delete everything and turn off** records the time it
was revoked; the next opt-in needs a fresh acknowledgement.

Changing the notice text changes its hash, so an acknowledgement of an older
notice no longer counts.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Bump whenever the notice text changes, so a standing acknowledgement of the
# old text stops counting. 2026-10-17.2 dropped the claim that no
# administrator can see the buffer, which isn't true of an OS administrator.
# 2026-10-17.3 added shortcuts and timing to what it records, the drafts and
# ignore list it keeps past 24 hours, and deletion when OpenAdapt was closed.
# 2026-10-17.4 follows counsel's review: it is organized as what, why, how
# long, and how to erase; it says the record may include personal,
# confidential, or health information; it no longer says "nothing leaves
# this computer", since backup, sync, and security tools can copy local
# files; it names IT administrators and endpoint tools on a managed computer;
# and it advises pausing before viewing sensitive material.
NOTICE_VERSION = "2026-10-17.4"
NOTICE_TITLE = "Turn on shadow mode?"
NOTICE_INTRO = (
    "Shadow mode watches how you work across apps to find tasks you repeat. It is "
    "off until you turn it on, and only you can turn it on."
)
NOTICE_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "What it captures",
        (
            "A list of the steps you take: which app you switch to, the kind of "
            "control you use (a button, a text field, a list row), keyboard "
            "shortcuts such as Ctrl+S, keys such as Enter or Tab, and when each "
            "step happened.",
            "What you type is never kept as text. It is reduced to its shape (digits, "
            "a date, an email address) and a keyed fingerprint. Password and PIN "
            "fields are skipped.",
            "Button and menu labels are kept only when they are common words, such as "
            "Save or Send. Any other label, such as a name or a record number, is "
            "replaced with a placeholder.",
            "Web pages count only on the work sites you list: the site name and the "
            "first word of the page address, with record numbers and names "
            "replaced. On any other site, it notes only that you were in a browser.",
            "It never takes screenshots, and never reads window titles or clipboard contents.",
            "Even so, this record may include personal, confidential, or health "
            "information that appears on your screen, such as which apps and work "
            "sites you used, when, and how often.",
        ),
    ),
    (
        "Why",
        (
            "To find tasks you repeat and suggest them as draft automations. It may "
            "show: You did this N times today. You choose Review draft or Ignore.",
            "A draft never runs. To automate the task, you record it and a person "
            "reviews and admits the automation.",
        ),
    ),
    (
        "How long it is kept",
        (
            "In an encrypted buffer in this computer's app-data folder, which isn't "
            "synced. Shadow mode won't start if that folder is inside iCloud, "
            "OneDrive, Dropbox, or a similar synced folder.",
            "The buffer is cleared, and its key destroyed, at least every 24 hours, "
            "and whenever you turn shadow mode off or quit OpenAdapt. If OpenAdapt "
            "stops unexpectedly, the buffer is cleared the next time it starts.",
            "The key is in this computer's keychain, or only in memory if the "
            "keychain can't be used.",
            "If you choose Review draft, the draft (the steps, with no typed text) is "
            "saved on this computer, not encrypted, until you turn shadow mode off. "
            "If you choose Ignore, a fingerprint of the task's steps is kept, so it "
            "isn't offered again.",
        ),
    ),
    (
        "How to erase it",
        (
            "Turning shadow mode off erases the buffer, its key, and any drafts at "
            "once. Delete everything and turn off also erases the ignore list.",
            "Pause stops it recording until you resume. Pause before you view sensitive material.",
        ),
    ),
    (
        "Who can access it",
        (
            "OpenAdapt never sends shadow mode data off this computer. Shadow mode "
            "makes no network connections.",
            "OpenAdapt, your employer, and other apps can't turn shadow mode on or "
            "read the buffer through OpenAdapt. There is no remote or admin setting "
            "for it.",
            "Someone with administrator access to this computer, or software running "
            "under your account, could still inspect it while it's on. On a "
            "work-managed computer, that includes IT administrators, and endpoint "
            "security or backup tools, which could inspect or copy local files or "
            "memory.",
        ),
    ),
)
ACKNOWLEDGE_LABEL = "Turn on shadow mode"
DECLINE_LABEL = "Not now"
PURGE_AND_DISABLE_LABEL = "Delete everything and turn off"


def notice_text() -> str:
    """The full notice as plain text, exactly as it is hashed."""

    lines = [NOTICE_TITLE, "", NOTICE_INTRO]
    for heading, items in NOTICE_SECTIONS:
        lines += ["", heading]
        lines += [f"- {item}" for item in items]
    return "\n".join(lines) + "\n"


def notice_sha256() -> str:
    return hashlib.sha256(notice_text().encode("utf-8")).hexdigest()


def notice() -> dict[str, Any]:
    """The notice as data, for a UI to render."""

    return {
        "version": NOTICE_VERSION,
        "sha256": notice_sha256(),
        "title": NOTICE_TITLE,
        "intro": NOTICE_INTRO,
        "sections": [{"heading": h, "items": list(items)} for h, items in NOTICE_SECTIONS],
        "acknowledge_label": ACKNOWLEDGE_LABEL,
        "decline_label": DECLINE_LABEL,
        "purge_and_disable_label": PURGE_AND_DISABLE_LABEL,
        "text": notice_text(),
    }


class ShadowConsentRequired(PermissionError):
    """Shadow mode can't turn on without an acknowledgement of the current notice."""


@dataclass(frozen=True)
class Acknowledgement:
    """A person's acknowledgement of one notice, at one time."""

    notice_version: str
    notice_sha256: str
    acknowledged_at: float

    @property
    def current(self) -> bool:
        return self.notice_version == NOTICE_VERSION and self.notice_sha256 == notice_sha256()


def require_current(ack: Acknowledgement | None) -> Acknowledgement:
    if not isinstance(ack, Acknowledgement) or not ack.current:
        raise ShadowConsentRequired(
            "shadow mode needs the person to acknowledge the current notice first"
        )
    return ack


class ConsentLedger:
    """Local record of acknowledgements and revocations."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"events": []}
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"events": []}
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            return {"events": []}
        return data

    def _write(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
        os.replace(tmp, self.path)

    def acknowledge(self, sha256: str, *, now: float | None = None) -> Acknowledgement:
        """Record that the person acknowledged the notice with hash ``sha256``.

        The caller passes back the hash of the notice it showed, so an outdated
        screen can't acknowledge a newer notice.
        """

        if sha256 != notice_sha256():
            raise ShadowConsentRequired("the acknowledged notice is not the current notice")
        ack = Acknowledgement(NOTICE_VERSION, sha256, time.time() if now is None else now)
        data = self._read()
        data["events"].append(
            {
                "event": "acknowledged",
                "at": ack.acknowledged_at,
                "notice_version": ack.notice_version,
                "notice_sha256": ack.notice_sha256,
            }
        )
        self._write(data)
        return ack

    def revoke(self, *, now: float | None = None) -> None:
        data = self._read()
        data["events"].append({"event": "revoked", "at": time.time() if now is None else now})
        self._write(data)

    def current(self) -> Acknowledgement | None:
        """The standing acknowledgement of the current notice, if any."""

        for event in reversed(self._read()["events"]):
            if not isinstance(event, dict):
                continue
            if event.get("event") == "revoked":
                return None
            if event.get("event") == "acknowledged":
                try:
                    ack = Acknowledgement(
                        str(event["notice_version"]),
                        str(event["notice_sha256"]),
                        float(event["at"]),
                    )
                except (KeyError, TypeError, ValueError):
                    return None
                return ack if ack.current else None
        return None

    def history(self) -> list[dict[str, Any]]:
        return [e for e in self._read()["events"] if isinstance(e, dict)]
