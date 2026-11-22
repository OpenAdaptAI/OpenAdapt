"""The local exception registry: one folder of small files per run.

``$OPENADAPT_HOME/exceptions/<run_ref>/`` (``~/.openadapt`` by default)
holds, for each card:

- ``card-<id>.json``: the :class:`~openadapt_types.exception_card.ExceptionCardV1`
  the run opened, written by :meth:`ExceptionRegistry.open`;
- ``screenshot-<hash>.png``: the scrubbed copy the card names, when a
  scrubber could redact it;
- ``answer-<id>.json``: the one answer, written by a person's signed answer
  (``openadapt-flow exceptions answer``, Desktop, the paired phone) or by the
  registry itself when the card expires;
- ``consumed-<id>.json``: written once by the waiting run when it acts on
  the answer. An answer is never used twice.

Files are created exclusively (a temporary file, then a hard link), owner
read and write only (0600), and never replaced. It mirrors the item-approval
folder (``approval_mode/item_approval.py``) without sharing its code.

The run re-checks every answer itself (:meth:`ExceptionRegistry.wait`).
``continue`` and ``done_by_person`` let it go on only when the answer is
signed by a key on the pinned approver roster, names this exact card, is one
of the card's choices and was decided before the deadline. Everything else
(decline, skip, abort, expiry, an answer that can't be read, verified or
matched) means stop before any write.

Nothing here is sent anywhere.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_types.exception_card import (
    EXCEPTION_KINDS,
    RUN_REF_PATTERN,
    SUMMARY_MAX,
    ExceptionAnswerV1,
    ExceptionCardV1,
    ExceptionChoice,
    answer_message,
    card_digest,
)

#: How long a card waits for a person by default, and at most.
DEFAULT_DEADLINE_S = 15 * 60
MAX_DEADLINE_S = 24 * 60 * 60
HEARTBEAT_S = 30.0
_MAX_FILE_BYTES = 256 * 1024
_MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_RUN_REF = re.compile(RUN_REF_PATTERN)
_HEX_ID = re.compile(r"^[0-9a-f]{64}$")


def _c(answer: str, label: str, consequence: str) -> ExceptionChoice:
    return ExceptionChoice(id=answer, label=label, consequence=consequence)  # type: ignore[arg-type]


_SKIP = _c(
    "skip", "Skip this item", "Nothing is saved. The run moves to the next item."
)
_ABORT = _c("abort", "Stop the run", "Nothing more is saved. The whole run stops.")

#: Each kind's choices, in the order a card shows them. The first is the
#: usual answer; no card offers a choice that writes on its own.
CHOICES: dict[str, tuple[ExceptionChoice, ...]] = {
    "second_factor": (
        _c(
            "done_by_person",
            "I entered the code in the browser",
            "You type the code in the live browser yourself. The run then goes on "
            "from the same screen. OpenAdapt never sees the code.",
        ),
        _SKIP,
        _ABORT,
    ),
    "unexpected_dialog": (
        _c(
            "done_by_person",
            "I dealt with the dialog",
            "You close or answer the dialog yourself. The run then goes on.",
        ),
        _c(
            "continue",
            "It's harmless, go on",
            "The run goes on with the dialog as it is, and every check still runs.",
        ),
        _SKIP,
        _ABORT,
    ),
    "session_expired": (
        _c(
            "done_by_person",
            "I signed in again",
            "You sign in yourself in the live browser. The run then goes on. "
            "OpenAdapt never sees the password.",
        ),
        _SKIP,
        _ABORT,
    ),
    "timeout_or_stall": (
        _c(
            "continue",
            "Keep waiting",
            "The run tries the step again, with every check.",
        ),
        _SKIP,
        _ABORT,
    ),
    "possible_duplicate": (
        _c(
            "skip",
            "Same record: don't add it",
            "Nothing is saved. The item closes as skipped.",
        ),
        _c(
            "continue",
            "Different record: go on",
            "The run goes on to fill the form. The save still needs every check.",
        ),
        _c(
            "done_by_person",
            "I'll do this one by hand",
            "Nothing is saved by OpenAdapt. The item closes as done by a person.",
        ),
        _ABORT,
    ),
    "ambiguous_record": (
        _c(
            "done_by_person",
            "I opened the right record",
            "You open the right record yourself. The run then goes on from there.",
        ),
        _SKIP,
        _c(
            "decline",
            "This item shouldn't be done",
            "Nothing is saved. The item closes as declined.",
        ),
    ),
    "cannot_complete": (
        _c(
            "done_by_person",
            "I did it by hand",
            "Nothing is saved by OpenAdapt. The item closes as done by a person.",
        ),
        _SKIP,
        _ABORT,
    ),
}

#: What a card says when the agent's own words can't be scrubbed first.
GENERIC_SUMMARIES: dict[str, str] = {
    "second_factor": "The app is asking for a one-time code. Enter it in the browser.",
    "unexpected_dialog": "A dialog the run didn't expect is covering the page.",
    "session_expired": "The app is asking to sign in again partway through the task.",
    "timeout_or_stall": "The run has made no progress for a while.",
    "possible_duplicate": "The app may already have this record.",
    "ambiguous_record": "The run can't tell which record is the right one.",
    "cannot_complete": "The run can't finish this item on its own.",
}

assert set(CHOICES) == set(EXCEPTION_KINDS) == set(GENERIC_SUMMARIES)


class ExceptionRegistryError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# -- time and text ---------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("no time zone")
    return parsed.astimezone(timezone.utc)


_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_DATE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")
#: Any token holding four or more digits: record numbers, phone numbers,
#: account numbers, codes.
_DIGITS = re.compile(r"\S*(?:\d\S*){4,}")


def _pattern_scrub(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _DATE.sub("[date]", text)
    return _DIGITS.sub("[number]", text)


def scrub_summary(kind: str, text: Optional[str]) -> tuple[str, str]:
    """``(summary, source)``: the agent's words scrubbed, or the kind's own.

    The summary can reach a paired phone, so this follows the outbound rule
    (``openadapt_flow.privacy``): without a working scrubber the agent's text
    is dropped and the card says the kind's generic sentence
    (``source="generic"``). Digit runs, dates and email addresses are masked
    on top of the scrubber either way.
    """

    from openadapt_flow import privacy

    generic = GENERIC_SUMMARIES[kind]
    raw = " ".join(str(text or "").split())
    if not raw:
        return generic, "generic"
    scrubber = privacy.get_scrubber()
    if scrubber is None:
        return generic, "generic"
    try:
        scrubbed = scrubber.scrub_text(raw)
    except Exception:  # noqa: BLE001 - a broken provider scrubs nothing
        return generic, "generic"
    cleaned = " ".join(_pattern_scrub(str(scrubbed or "")).split())
    if not cleaned:
        return generic, "generic"
    if len(cleaned) > SUMMARY_MAX:
        cleaned = cleaned[: SUMMARY_MAX - 1].rstrip() + "…"
    return cleaned, "scrubbed"


# -- files -----------------------------------------------------------------------


def _flags(base: int) -> int:
    for name in ("O_NOFOLLOW", "O_CLOEXEC"):
        base |= getattr(os, name, 0)
    return base


def _read_regular(path: Path, *, limit: int = _MAX_FILE_BYTES) -> Optional[bytes]:
    """The file's bytes, ``None`` when it doesn't exist. Raises ``OSError``
    for a link, a non-regular file or one over ``limit``."""

    try:
        fd = os.open(path, _flags(os.O_RDONLY))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise OSError("not a small regular file")
        data = b""
        while len(data) <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > limit:
            raise OSError("file too large")
        return data
    finally:
        os.close(fd)


def _write_exclusive(path: Path, payload: bytes) -> None:
    """Create ``path`` once, 0600. Raises ``FileExistsError`` if it's there."""

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = os.open(tmp, _flags(os.O_WRONLY | os.O_CREAT | os.O_EXCL), 0o600)
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
    try:
        dfd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _json(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def default_root() -> Path:
    home = os.environ.get("OPENADAPT_HOME")
    return (Path(home) if home else Path.home() / ".openadapt") / "exceptions"


def _hex(card_id: str) -> str:
    value = card_id.removeprefix("sha256:")
    if not _HEX_ID.fullmatch(value):
        raise ExceptionRegistryError("card_id_invalid", "that isn't a card id")
    return value


# -- outcome ---------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """What the waiting run does now."""

    card_id: str
    kind: str
    answer: str
    #: True only for a verified ``continue`` or ``done_by_person``.
    proceed: bool
    #: Why the run stops when the answer alone doesn't say: ``expired``, or
    #: a closed refusal code for an answer that couldn't be used.
    reason: Optional[str]
    opened_at: str
    decided_at: Optional[str]

    def wire(self) -> dict[str, Any]:
        return {
            "card_id": self.card_id,
            "kind": self.kind,
            "answer": self.answer,
            "proceed": self.proceed,
            "reason": self.reason,
            "opened_at": self.opened_at,
            "decided_at": self.decided_at,
            "instruction": (
                f"A person answered {self.answer}. Go on from the current screen."
                if self.proceed
                else "Stop now. Make no further writes for this task."
            ),
        }


#: ``verify(answer)`` returns ``None`` for a valid signature by a key on the
#: pinned approver roster, or a closed refusal code.
AnswerVerifier = Callable[[ExceptionAnswerV1], Optional[str]]


def roster_verifier(key_dir: Optional[Path], root: Optional[str]) -> AnswerVerifier:
    """Check answers against Flow's signed approver roster (``jit approver``).

    Without a key directory or a pinned root no person's answer counts.
    """

    def verify(answer: ExceptionAnswerV1) -> Optional[str]:
        from openadapt_flow.approval_mode.human_attestation import (
            AttestationRefused,
            approvers,
        )
        from openadapt_flow.execute.keys import verify_signature
        from openadapt_flow.jit.authority import _public

        if key_dir is None:
            return "human_answer_unverifiable"
        try:
            known = approvers(key_dir, root)
        except AttestationRefused as exc:
            return exc.code
        if not answer.public_key or answer.public_key not in set(known.values()):
            return "human_approver_unregistered"
        try:
            public = _public(answer.public_key)
        except ValueError:
            return "human_answer_signature_invalid"
        if not verify_signature(
            public, answer_message(answer.wire()), str(answer.signature or "")
        ):
            return "human_answer_signature_invalid"
        return None

    return verify


def _stop(card: ExceptionCardV1, reason: str, answer: str = "decline") -> Outcome:
    return Outcome(card.card_id, card.kind, answer, False, reason, card.opened_at, None)


# -- the registry ----------------------------------------------------------------


class ExceptionRegistry:
    """The exceptions folder: open cards, list them, answer them, wait."""

    def __init__(self, root: Optional[Path | str] = None) -> None:
        self.root = Path(root) if root is not None else default_root()

    def run_dir(self, run_ref: str) -> Path:
        if not _RUN_REF.fullmatch(run_ref or ""):
            raise ExceptionRegistryError(
                "run_ref_invalid", "a run reference is letters, digits, '.', '_' or '-'"
            )
        return self.root / run_ref

    def _ensure(self, path: Path) -> None:
        for directory in (self.root, path):
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = os.lstat(directory)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise ExceptionRegistryError(
                    "registry_unsafe", f"{directory} isn't a plain folder"
                )
            if hasattr(os, "getuid") and info.st_uid != os.getuid():
                raise ExceptionRegistryError(
                    "registry_unsafe", f"{directory} belongs to another user"
                )

    # -- opening -----------------------------------------------------------

    def _copy_screenshot(self, folder: Path, source: Optional[str]) -> Optional[str]:
        """A scrubbed copy of the agent's screenshot, or ``None``.

        The path is the agent's: only a regular PNG file under the size cap
        is read, never through a link. Without a scrubber that can redact
        it, no image is kept.
        """

        if not source:
            return None
        from openadapt_flow import privacy

        try:
            data = _read_regular(Path(source), limit=_MAX_SCREENSHOT_BYTES)
        except OSError:
            return None
        if not data or not data.startswith(_PNG):
            return None
        if privacy.get_scrubber() is None:
            return None
        try:
            redacted = privacy.scrub_image_bytes(data, force=True)
        except Exception:  # noqa: BLE001 - no redaction, no image
            return None
        if not redacted.startswith(_PNG):
            return None
        name = f"screenshot-{hashlib.sha256(redacted).hexdigest()[:32]}.png"
        try:
            _write_exclusive(folder / name, redacted)
        except FileExistsError:
            pass
        return name

    def open(
        self,
        run_ref: str,
        kind: str,
        summary: Optional[str],
        *,
        task_id: Optional[str] = None,
        screenshot_path: Optional[str] = None,
        deadline_s: float = DEFAULT_DEADLINE_S,
        now: Optional[datetime] = None,
    ) -> ExceptionCardV1:
        """Write a new card and return it. Raises ``ExceptionRegistryError``."""

        if kind not in CHOICES:
            raise ExceptionRegistryError(
                "kind_unknown", "kind is one of " + ", ".join(EXCEPTION_KINDS)
            )
        folder = self.run_dir(run_ref)
        self._ensure(folder)
        opened = now or _now()
        seconds = max(1.0, min(float(deadline_s), MAX_DEADLINE_S))
        text, _source = scrub_summary(kind, summary)
        if task_id is not None:
            task_id = _pattern_scrub(" ".join(str(task_id).split()))[:128] or None
        body = {
            "schema": "openadapt.exception-card/v1",
            "run_ref": run_ref,
            "task_id": task_id,
            "kind": kind,
            "opened_at": iso(opened),
            "deadline_at": iso(opened + timedelta(seconds=seconds)),
            "screenshot_ref": self._copy_screenshot(folder, screenshot_path),
            "summary": text,
            "choices": [c.model_dump(mode="json") for c in CHOICES[kind]],
        }
        for _attempt in range(3):
            card = ExceptionCardV1.model_validate(
                {**body, "card_id": card_digest(body)}
            )
            try:
                _write_exclusive(
                    folder / f"card-{_hex(card.card_id)}.json", _json(card.wire())
                )
                return card
            except FileExistsError:
                opened = opened + timedelta(microseconds=1)
                body["opened_at"] = iso(opened)
                body["deadline_at"] = iso(opened + timedelta(seconds=seconds))
        raise ExceptionRegistryError("card_exists", "could not open a new card")

    # -- reading -----------------------------------------------------------

    def _load(self, path: Path, model: Any) -> Any:
        data = _read_regular(path)
        if data is None:
            return None
        return model.model_validate(json.loads(data))

    def card(self, card_id: str) -> Optional[tuple[str, ExceptionCardV1]]:
        """``(run_ref, card)`` for an id, searching every run's folder."""

        name = f"card-{_hex(card_id)}.json"
        if not self.root.is_dir():
            return None
        for folder in sorted(self.root.iterdir()):
            if not _RUN_REF.fullmatch(folder.name) or not (folder / name).exists():
                continue
            try:
                card = self._load(folder / name, ExceptionCardV1)
            except (OSError, ValueError):
                return None
            if card is not None and card.run_ref == folder.name:
                return folder.name, card
        return None

    def read_answer(self, card: ExceptionCardV1) -> Optional[ExceptionAnswerV1]:
        """The card's answer, ``None`` if there isn't one. Raises on a bad file."""

        return self._load(
            self.run_dir(card.run_ref) / f"answer-{_hex(card.card_id)}.json",
            ExceptionAnswerV1,
        )

    def open_cards(self, run_ref: Optional[str] = None) -> list[ExceptionCardV1]:
        """Cards with no answer yet, oldest first. Unreadable files are skipped."""

        if not self.root.is_dir():
            return []
        folders = [self.run_dir(run_ref)] if run_ref else sorted(self.root.iterdir())
        cards = []
        for folder in folders:
            if not folder.is_dir() or not _RUN_REF.fullmatch(folder.name):
                continue
            for path in sorted(folder.glob("card-*.json")):
                try:
                    card = self._load(path, ExceptionCardV1)
                except (OSError, ValueError):
                    continue
                if card is None or card.run_ref != folder.name:
                    continue
                if path.name != f"card-{_hex(card.card_id)}.json":
                    continue
                if (folder / f"answer-{_hex(card.card_id)}.json").exists():
                    continue
                cards.append(card)
        return sorted(cards, key=lambda c: c.opened_at)

    # -- answering ---------------------------------------------------------

    def unsigned_answer(
        self,
        card: ExceptionCardV1,
        answer: str,
        *,
        route: str,
        now: Optional[datetime] = None,
    ) -> dict[str, Any]:
        """The answer a person's key signs (no key or signature yet)."""

        if not card.offers(answer):
            raise ExceptionRegistryError(
                "choice_not_offered", "that answer isn't one of the card's choices"
            )
        return {
            "schema": "openadapt.exception-answer/v1",
            "card_id": card.card_id,
            "kind": card.kind,
            "opened_at": card.opened_at,
            "decided_at": iso(now or _now()),
            "answer": answer,
            "approver_kind": "person",
            "route": route,
            "reason": None,
        }

    def write_answer(
        self, card: ExceptionCardV1, answer: Mapping[str, Any]
    ) -> ExceptionAnswerV1:
        """Write the card's one answer. Raises ``ExceptionRegistryError``."""

        model = ExceptionAnswerV1.model_validate(dict(answer))
        if model.card_id != card.card_id or model.kind != card.kind:
            raise ExceptionRegistryError(
                "answer_mismatch", "the answer names another card"
            )
        folder = self.run_dir(card.run_ref)
        self._ensure(folder)
        try:
            _write_exclusive(
                folder / f"answer-{_hex(card.card_id)}.json", _json(model.wire())
            )
        except FileExistsError:
            raise ExceptionRegistryError(
                "already_answered", "this card already has an answer"
            ) from None
        return model

    def _expire(self, card: ExceptionCardV1) -> None:
        record = ExceptionAnswerV1.model_validate(
            {
                "schema": "openadapt.exception-answer/v1",
                "card_id": card.card_id,
                "kind": card.kind,
                "opened_at": card.opened_at,
                "decided_at": None,
                "answer": "decline",
                "approver_kind": "expiry",
                "route": "registry",
                "reason": "expired",
            }
        )
        _write_exclusive(
            self.run_dir(card.run_ref) / f"answer-{_hex(card.card_id)}.json",
            _json(record.wire()),
        )

    def _settle(
        self, card: ExceptionCardV1, answer: ExceptionAnswerV1, verify: AnswerVerifier
    ) -> Outcome:
        if answer.card_id != card.card_id or answer.kind != card.kind:
            return _stop(card, "answer_mismatch")
        if answer.opened_at != card.opened_at:
            return _stop(card, "answer_mismatch")
        if answer.approver_kind == "expiry":
            outcome = _stop(card, "expired")
        else:
            if not card.offers(answer.answer):
                return _stop(card, "choice_not_offered")
            try:
                decided = parse_time(str(answer.decided_at))
            except ValueError:
                return _stop(card, "answer_time_invalid")
            if decided < parse_time(card.opened_at):
                return _stop(card, "answer_time_invalid")
            if decided > parse_time(card.deadline_at):
                return _stop(card, "answer_late")
            refusal = verify(answer)
            if refusal is not None:
                return _stop(card, refusal)
            outcome = Outcome(
                card.card_id,
                card.kind,
                answer.answer,
                answer.proceeds,
                None,
                card.opened_at,
                answer.decided_at,
            )
        consumed = {
            "card_id": card.card_id,
            "answer": outcome.answer,
            "proceed": outcome.proceed,
            "reason": outcome.reason,
            "consumed_at": iso(_now()),
        }
        try:
            _write_exclusive(
                self.run_dir(card.run_ref) / f"consumed-{_hex(card.card_id)}.json",
                _json(consumed),
            )
        except FileExistsError:
            return _stop(card, "answer_already_used", outcome.answer)
        return outcome

    def poll(
        self,
        card: ExceptionCardV1,
        *,
        verify: AnswerVerifier,
        deadline: Optional[datetime] = None,
        clock: Callable[[], datetime] = _now,
        now: Optional[datetime] = None,
    ) -> Optional[Outcome]:
        """One look at the card: what to do once it is answered or expired,
        ``None`` while it is still open. Never blocks; :meth:`wait` loops on
        it, and a caller with its own loop (the browser watch) calls it."""

        end = parse_time(card.deadline_at)
        if deadline is not None:
            end = min(end, deadline.astimezone(timezone.utc))
        for _attempt in range(3):
            try:
                answer = self.read_answer(card)
            except (OSError, ValueError):
                return _stop(card, "answer_unreadable")
            if answer is not None:
                return self._settle(card, answer, verify)
            if (now if now is not None else clock()) < end:
                return None
            try:
                self._expire(card)
            except FileExistsError:
                continue  # a person's answer won the race: read it
            except OSError:
                return _stop(card, "expired")
        return _stop(card, "answer_unreadable")

    def wait(
        self,
        card: ExceptionCardV1,
        *,
        verify: AnswerVerifier,
        deadline: Optional[datetime] = None,
        poll_s: float = 0.5,
        heartbeat_s: float = HEARTBEAT_S,
        heartbeat: Optional[Callable[[str, float], None]] = None,
        clock: Callable[[], datetime] = _now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> Outcome:
        """Block until the card is answered or expires; return what to do.

        ``deadline`` (default the card's ``deadline_at``) can only shorten the
        wait. On expiry the registry writes an ``answer=decline``,
        ``decided_at=null``, ``reason=expired`` record, unless a person's
        answer got there first, which is then checked like any other.
        ``heartbeat(card_id, seconds_left)`` is called every ``heartbeat_s``
        (default: a line on stderr, never stdout).
        """

        end = parse_time(card.deadline_at)
        if deadline is not None:
            end = min(end, deadline.astimezone(timezone.utc))
        beat = heartbeat or _stderr_heartbeat
        next_beat = 0.0
        started = time.monotonic()
        while True:
            try:
                answered = self.read_answer(card) is not None
            except (OSError, ValueError):
                answered = True  # poll() reports the unreadable answer
            now = None if answered else clock()
            outcome = self.poll(card, verify=verify, deadline=deadline, now=now)
            if outcome is not None:
                return outcome
            assert now is not None
            elapsed = time.monotonic() - started
            if elapsed >= next_beat:
                beat(card.card_id, (end - now).total_seconds())
                next_beat = elapsed + heartbeat_s
            sleep(poll_s)


def _stderr_heartbeat(card_id: str, seconds_left: float) -> None:
    print(
        f"exceptions: waiting for a person on {card_id[:19]}… "
        f"({int(seconds_left)} s left)",
        file=sys.stderr,
        flush=True,
    )


__all__ = [
    "CHOICES",
    "DEFAULT_DEADLINE_S",
    "GENERIC_SUMMARIES",
    "AnswerVerifier",
    "ExceptionRegistry",
    "ExceptionRegistryError",
    "Outcome",
    "default_root",
    "iso",
    "parse_time",
    "roster_verifier",
    "scrub_summary",
]
