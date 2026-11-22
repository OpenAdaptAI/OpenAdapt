"""Exception cards: the closed question a run asks a person, and the answer.

Any agent (a compiled program, Claude computer use, a CUA harness, a person's
own script) can stop at a moment it can't get past alone: a second-factor
prompt, a dialog it didn't expect, a sign-in page mid-task, a stall, a
possible duplicate, a record it can't tell apart from another, or a task it
can't finish. It opens one :class:`ExceptionCardV1`, and a person answers it
with one :class:`ExceptionAnswerV1`.

Both sets are closed. ``kind`` is one of :data:`EXCEPTION_KINDS` and
``answer`` is one of :data:`EXCEPTION_ANSWERS`; nothing else validates. Only
``continue`` and ``done_by_person`` let a run go on
(:data:`PROCEED_ANSWERS`). Every other answer, an expired card and an answer
that can't be read or verified mean the run stops before any write.

The card carries no raw values. ``summary`` is scrubbed text of at most
:data:`SUMMARY_MAX` characters, and ``screenshot_ref`` names a local, scrubbed
copy, never the original frame. ``card_id`` is the digest of everything else
on the card (:func:`card_digest`), so an answer names exactly the card a
person saw.

``opened_at`` and ``decided_at`` on the answer are the intervention interval:
how long a person held the run. An expiry answer has ``decided_at`` ``None``,
``approver_kind`` ``expiry``, ``answer`` ``decline`` and ``reason``
``expired``.

This module is imported by its submodule path
(``openadapt_types.exception_card``); it is not re-exported from the package.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Optional, get_args

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EXCEPTION_CARD_SCHEMA = "openadapt.exception-card/v1"
EXCEPTION_ANSWER_SCHEMA = "openadapt.exception-answer/v1"
#: What an approver key signs for an answer: these bytes, then the canonical
#: JSON of :func:`answer_signing_payload`.
EXCEPTION_ANSWER_DOMAIN = b"openadapt.exception-answer/v1\x00"

ExceptionKind = Literal[
    "second_factor",
    "unexpected_dialog",
    "session_expired",
    "timeout_or_stall",
    "possible_duplicate",
    "ambiguous_record",
    "cannot_complete",
]
EXCEPTION_KINDS: tuple[str, ...] = get_args(ExceptionKind)

ExceptionAnswerValue = Literal["continue", "skip", "decline", "done_by_person", "abort"]
EXCEPTION_ANSWERS: tuple[str, ...] = get_args(ExceptionAnswerValue)
#: The only answers that let a run go on. Anything else stops before a write.
PROCEED_ANSWERS = frozenset({"continue", "done_by_person"})

ApproverKind = Literal["person", "expiry"]
#: Where a person's answer came from.
AnswerRoute = Literal["cli", "desktop", "portal", "registry"]

SUMMARY_MAX = 200
_DIGEST = r"^sha256:[0-9a-f]{64}$"
#: A run reference is opaque: it becomes a directory name.
RUN_REF_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_TIME = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$"
_SIGNATURE = r"^ed25519:[0-9a-f]{128}$"
_PUBLIC_KEY = r"^[0-9a-f]{64}$"


def canonical(value: Any) -> bytes:
    """The canonical JSON bytes every digest and signature here covers."""

    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


class ExceptionChoice(BaseModel):
    """One button: the closed answer, its label, and what it does."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    id: ExceptionAnswerValue
    label: str = Field(min_length=1, max_length=80)
    consequence: str = Field(min_length=1, max_length=200)


class ExceptionCardV1(BaseModel):
    """The question one run asks a person."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt.exception-card/v1"] = Field(
        default=EXCEPTION_CARD_SCHEMA, alias="schema"
    )
    card_id: str = Field(pattern=_DIGEST)
    run_ref: str = Field(pattern=RUN_REF_PATTERN)
    task_id: Optional[str] = Field(default=None, max_length=128)
    kind: ExceptionKind
    opened_at: str = Field(pattern=_TIME)
    deadline_at: str = Field(pattern=_TIME)
    #: A file name inside the card's own folder (a scrubbed copy), or None.
    screenshot_ref: Optional[str] = Field(
        default=None, pattern=r"^screenshot-[0-9a-f]{16,64}\.png$"
    )
    summary: str = Field(min_length=1, max_length=SUMMARY_MAX)
    choices: tuple[ExceptionChoice, ...] = Field(min_length=1, max_length=5)

    @field_validator("choices")
    @classmethod
    def _distinct(cls, value: tuple[ExceptionChoice, ...]) -> tuple[ExceptionChoice, ...]:
        ids = [choice.id for choice in value]
        if len(set(ids)) != len(ids):
            raise ValueError("a card offers each answer once")
        return value

    @model_validator(mode="after")
    def _sealed(self) -> "ExceptionCardV1":
        if card_digest(self.body()) != self.card_id:
            raise ValueError("card_id is not the digest of the card")
        return self

    def body(self) -> dict[str, Any]:
        """Everything on the card except its id (what ``card_id`` digests)."""

        data = self.model_dump(mode="json", by_alias=True)
        data.pop("card_id")
        return data

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    def offers(self, answer: str) -> bool:
        return any(choice.id == answer for choice in self.choices)


class ExceptionAnswerV1(BaseModel):
    """A person's answer to one card, or the registry's expiry record."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_: Literal["openadapt.exception-answer/v1"] = Field(
        default=EXCEPTION_ANSWER_SCHEMA, alias="schema"
    )
    card_id: str = Field(pattern=_DIGEST)
    kind: ExceptionKind
    opened_at: str = Field(pattern=_TIME)
    decided_at: Optional[str] = Field(default=None, pattern=_TIME)
    answer: ExceptionAnswerValue
    approver_kind: ApproverKind
    route: AnswerRoute
    #: The closed reason for a registry-written answer.
    reason: Optional[Literal["expired"]] = None
    #: The approver key that signed (hex Ed25519), not part of what it signs.
    public_key: Optional[str] = Field(default=None, pattern=_PUBLIC_KEY)
    signature: Optional[str] = Field(default=None, pattern=_SIGNATURE)

    @model_validator(mode="after")
    def _shape(self) -> "ExceptionAnswerV1":
        if self.approver_kind == "expiry":
            if (
                self.answer != "decline"
                or self.decided_at is not None
                or self.reason != "expired"
                or self.signature is not None
                or self.route != "registry"
            ):
                raise ValueError(
                    "an expiry record is answer=decline, decided_at=null, "
                    "reason=expired, unsigned, written by the registry"
                )
        else:
            if self.decided_at is None or self.signature is None or self.public_key is None:
                raise ValueError("a person's answer is timed and signed")
            if self.reason is not None or self.route == "registry":
                raise ValueError("a person's answer carries no registry reason")
        return self

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)

    @property
    def proceeds(self) -> bool:
        return self.approver_kind == "person" and self.answer in PROCEED_ANSWERS


def card_digest(body: dict[str, Any]) -> str:
    """``sha256:`` of the canonical card body (the card without ``card_id``)."""

    return "sha256:" + hashlib.sha256(canonical(body)).hexdigest()


def answer_signing_payload(answer: dict[str, Any]) -> dict[str, Any]:
    """What an approver key signs: the answer without its key and signature."""

    return {k: v for k, v in answer.items() if k not in {"signature", "public_key"}}


def answer_message(answer: dict[str, Any]) -> bytes:
    """The exact bytes an approver key signs for ``answer``."""

    return EXCEPTION_ANSWER_DOMAIN + canonical(answer_signing_payload(answer))


__all__ = [
    "EXCEPTION_ANSWERS",
    "EXCEPTION_ANSWER_DOMAIN",
    "EXCEPTION_ANSWER_SCHEMA",
    "EXCEPTION_CARD_SCHEMA",
    "EXCEPTION_KINDS",
    "PROCEED_ANSWERS",
    "RUN_REF_PATTERN",
    "SUMMARY_MAX",
    "AnswerRoute",
    "ApproverKind",
    "ExceptionAnswerV1",
    "ExceptionAnswerValue",
    "ExceptionCardV1",
    "ExceptionChoice",
    "ExceptionKind",
    "answer_message",
    "answer_signing_payload",
    "canonical",
    "card_digest",
]
