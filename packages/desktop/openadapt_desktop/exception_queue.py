"""Exception cards from any agent's run, in the stop queue.

Flow's exception registry (``openadapt-flow exceptions``) keeps one folder per
run under ``$OPENADAPT_HOME/exceptions`` (``~/.openadapt/exceptions`` by
default). A run that hits a second-factor prompt, an unexpected dialog, a
sign-in page mid-task, a stall, a possible duplicate, a record it can't tell
apart, or a task it can't finish writes a ``card-<id>.json`` and waits for an
``answer-<id>.json``. Cards come from any run on this computer: a CLI guard,
``openadapt wrap``, a flight recorder, a hook, a Desktop-launched run.

Desktop pins an exact Flow release, so this module reads the files as plain
JSON and checks them itself: the schema id, the closed kind and answer sets,
the card id (the digest of the card), a regular 0600-style file under the
size cap. A card that fails any check isn't shown.

An answer is the closed answer signed with the approver key Desktop already
uses for the stop queue (Touch ID on a Mac, the portal's key for a phone).
The waiting run checks the signature against its pinned approver roster, so
an answer Desktop writes counts only when that key is on the roster. The
answer file records ``opened_at`` and ``decided_at``: how long a person held
the run.

Nothing here is sent anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CARD_SCHEMA = "openadapt.exception-card/v1"
ANSWER_SCHEMA = "openadapt.exception-answer/v1"
#: The bytes an approver key signs before the canonical answer. Flow's
#: ``openadapt_types.exception_card.EXCEPTION_ANSWER_DOMAIN``.
ANSWER_DOMAIN = b"openadapt.exception-answer/v1\x00"
KINDS = frozenset(
    {
        "second_factor",
        "unexpected_dialog",
        "session_expired",
        "timeout_or_stall",
        "possible_duplicate",
        "ambiguous_record",
        "cannot_complete",
    }
)
ANSWERS = frozenset({"continue", "skip", "decline", "done_by_person", "abort"})
#: How many cards one scan reads at most.
MAX_CARDS = 200
_MAX_BYTES = 256 * 1024
_RUN_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CARD_FILE = re.compile(r"^card-([0-9a-f]{64})\.json$")
_SUMMARY_MAX = 200


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def registry_root() -> Path:
    home = os.environ.get("OPENADAPT_HOME")
    return (Path(home) if home else Path.home() / ".openadapt") / "exceptions"


def _read(path: Path) -> bytes | None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_BYTES:
            return None
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return None
        return os.read(fd, _MAX_BYTES + 1)[:_MAX_BYTES]
    finally:
        os.close(fd)


def _time(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def valid_card(raw: Any, *, run_ref: str, file_hex: str) -> dict[str, Any] | None:
    """The card when every field checks out, else ``None``."""

    if not isinstance(raw, dict) or raw.get("schema") != CARD_SCHEMA:
        return None
    if raw.get("kind") not in KINDS or raw.get("run_ref") != run_ref:
        return None
    body = {k: v for k, v in raw.items() if k != "card_id"}
    digest = "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()
    if raw.get("card_id") != digest or digest != f"sha256:{file_hex}":
        return None
    if not (_time(raw.get("opened_at")) and _time(raw.get("deadline_at"))):
        return None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary or len(summary) > _SUMMARY_MAX:
        return None
    choices = raw.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    ids = [c.get("id") if isinstance(c, dict) else None for c in choices]
    if any(i not in ANSWERS for i in ids) or len(set(ids)) != len(ids):
        return None
    task_id = raw.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or len(task_id) > 128):
        return None
    return raw


def open_cards(root: Path | None = None) -> list[dict[str, Any]]:
    """Every valid card with no answer yet, oldest first."""

    base = root if root is not None else registry_root()
    try:
        folders = sorted(p for p in base.iterdir() if _RUN_REF.fullmatch(p.name))
    except OSError:
        return []
    cards: list[dict[str, Any]] = []
    for folder in folders:
        try:
            if stat.S_ISLNK(os.lstat(folder).st_mode) or not folder.is_dir():
                continue
            names = sorted(os.listdir(folder))
        except OSError:
            continue
        for name in names:
            match = _CARD_FILE.fullmatch(name)
            if match is None:
                continue
            if (folder / f"answer-{match.group(1)}.json").exists():
                continue
            data = _read(folder / name)
            if data is None:
                continue
            try:
                raw = json.loads(data)
            except ValueError:
                continue
            card = valid_card(raw, run_ref=folder.name, file_hex=match.group(1))
            if card is not None:
                cards.append(card)
            if len(cards) >= MAX_CARDS:
                break
    return sorted(cards, key=lambda c: str(c.get("opened_at")))


def find_card(card_id: str, run_ref: str, root: Path | None = None) -> dict[str, Any] | None:
    """The card, still unanswered and unchanged, or ``None``."""

    return next(
        (
            c
            for c in open_cards(root)
            if c.get("card_id") == card_id and c.get("run_ref") == run_ref
        ),
        None,
    )


def unsigned_answer(card: dict[str, Any], answer: str, *, route: str, decided_at: str) -> dict:
    """The answer the approver key signs (Flow's ``ExceptionAnswerV1`` fields)."""

    if answer not in ANSWERS or route not in {"desktop", "portal"}:
        raise ValueError("not an answer")
    return {
        "schema": ANSWER_SCHEMA,
        "card_id": card["card_id"],
        "kind": card["kind"],
        "opened_at": card["opened_at"],
        "decided_at": decided_at,
        "answer": answer,
        "approver_kind": "person",
        "route": route,
        "reason": None,
    }


def message(unsigned: dict[str, Any]) -> bytes:
    """The exact bytes the approver key signs."""

    return ANSWER_DOMAIN + _canonical(unsigned)


def _write_exclusive(path: Path, payload: bytes) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def answer(
    source: dict[str, Any],
    choice: str,
    *,
    sign: Callable[..., dict],
    route: str,
    root: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, Any]:
    """Sign and write one card's answer. Returns ``{"ok", ...}``.

    ``source`` is the queue card's ``source`` (the Flow card id and run).
    The Flow card is read again before signing and again before writing: one
    answered, changed or gone in between changes nothing. Raises the
    signer's own error (``ApproverKeychainError``) for the caller to report.
    """

    base = root if root is not None else registry_root()
    card_id, run_ref = str(source.get("id") or ""), str(source.get("run_ref") or "")
    card = find_card(card_id, run_ref, base)
    if card is None:
        return {"ok": False, "code": "card_changed", "error": "The stop was answered or is gone."}
    if choice not in {c.get("id") for c in card["choices"]}:
        return {
            "ok": False,
            "code": "choice_not_offered",
            "error": "That choice isn't on this stop.",
        }
    decided = now().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if _after(decided, card["deadline_at"]):
        return {"ok": False, "code": "card_expired", "error": "The stop expired."}
    unsigned = unsigned_answer(card, choice, route=route, decided_at=decided)
    signature = sign(message(unsigned), count=1)
    if find_card(card_id, run_ref, base) is None:
        return {"ok": False, "code": "card_changed", "error": "The stop was answered or is gone."}
    record = {
        **unsigned,
        "public_key": signature["public_key"],
        "signature": signature["signature"],
    }
    path = base / run_ref / f"answer-{card_id.removeprefix('sha256:')}.json"
    try:
        _write_exclusive(path, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode())
    except FileExistsError:
        return {"ok": False, "code": "card_changed", "error": "The stop was answered first."}
    except OSError:
        return {
            "ok": False,
            "code": "registry_unwritable",
            "error": "Desktop couldn't save the answer.",
        }
    return {
        "ok": True,
        "answer": choice,
        "decided_at": decided,
        "opened_at": card["opened_at"],
        "key": signature.get("key"),
        "public_key": signature["public_key"],
        "signature": signature["signature"],
    }


def _after(a: str, b: str) -> bool:
    try:
        return datetime.fromisoformat(a.replace("Z", "+00:00")) > datetime.fromisoformat(
            str(b).replace("Z", "+00:00")
        )
    except ValueError:
        return True


__all__ = [
    "ANSWERS",
    "ANSWER_DOMAIN",
    "ANSWER_SCHEMA",
    "CARD_SCHEMA",
    "KINDS",
    "answer",
    "find_card",
    "message",
    "open_cards",
    "registry_root",
    "unsigned_answer",
    "valid_card",
]
