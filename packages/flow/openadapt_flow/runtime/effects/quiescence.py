"""Caller-owned exclusion and completion for one bounded complete read.

Two equal snapshots, a candidate flag or a host lease never grant authority.
Retrospective audits remain pending until the caller seals their exact window.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from collections import Counter
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Literal, Mapping


def read_scope_sha256(read_contract: Mapping[str, Any]) -> str:
    body = json.dumps(
        dict(read_contract),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return (
        "sha256:"
        + hashlib.sha256(b"OpenAdapt quiescent read scope v1\0" + body).hexdigest()
    )


@dataclass(frozen=True)
class QuiescenceBinding:
    scope_sha256: str
    window_ref: str
    generation: int

    def validate(self) -> None:
        if (
            not isinstance(self.scope_sha256, str)
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.scope_sha256) is None
        ):
            raise ValueError("quiescence scope digest is invalid")
        if (
            not isinstance(self.window_ref, str)
            or not self.window_ref
            or len(self.window_ref) > 256
            or any(c.isspace() for c in self.window_ref)
        ):
            raise ValueError("quiescence window reference is invalid")
        if type(self.generation) is not int or not 0 <= self.generation <= 2**63 - 1:
            raise ValueError("quiescence generation is invalid")


@dataclass(frozen=True)
class QuiescenceCompletion:
    state: Literal["PENDING", "VALID", "INVALID"]
    binding: QuiescenceBinding


class QuiescenceLease(ABC):
    @abstractmethod
    def assert_held(self) -> None:
        """Refuse lost ownership, pending writers, or changed target scope."""

    @abstractmethod
    def binding(self) -> QuiescenceBinding:
        """Return the immutable identity of this actual held read window."""


class QuiescenceAuthority(ABC):
    @abstractmethod
    def hold(
        self, read_contract: Mapping[str, Any]
    ) -> AbstractContextManager[QuiescenceLease]:
        """Exclude every writer in the declared read scope until exit."""

    @abstractmethod
    def completion(self) -> QuiescenceCompletion:
        """Report actual completion; holding a retrospective window is pending."""


def two_pass_read(
    authority: QuiescenceAuthority | None,
    read_contract: Mapping[str, Any],
    read_once: Callable[[Callable[[], None]], list[dict[str, Any]] | None],
) -> list[dict[str, Any]]:
    if not isinstance(authority, QuiescenceAuthority):
        raise ValueError("verified quiescence authority is unavailable")
    scope = read_scope_sha256(read_contract)
    with authority.hold(read_contract) as lease:
        if not isinstance(lease, QuiescenceLease):
            raise ValueError("verified quiescence lease is unavailable")
        binding = lease.binding()
        if not isinstance(binding, QuiescenceBinding):
            raise ValueError("quiescence window binding is unavailable")
        binding.validate()
        if binding.scope_sha256 != scope:
            raise ValueError("quiescence window belongs to a different read scope")

        def check() -> None:
            if lease.assert_held() is not None:
                raise ValueError(
                    "quiescence check must assert ownership, not return a flag"
                )
            current = lease.binding()
            if not isinstance(current, QuiescenceBinding):
                raise ValueError("current quiescence window binding is unavailable")
            current.validate()
            if current != binding or read_scope_sha256(read_contract) != scope:
                raise ValueError("the held quiescence scope or window changed")
            completion = authority.completion()
            if not isinstance(completion, QuiescenceCompletion) or not isinstance(
                completion.binding, QuiescenceBinding
            ):
                raise ValueError("quiescence completion is unavailable")
            completion.binding.validate()
            if completion.state != "VALID" or completion.binding != binding:
                raise ValueError(
                    "quiescence completion is pending, invalid or belongs to another window"
                )
            # Recheck after the callback too; it must not replace the active lease.
            if lease.assert_held() is not None or lease.binding() != binding:
                raise ValueError("the held quiescence window changed during completion")

        check()
        first = read_once(check)
        check()
        if first is None:
            raise ValueError("the first quiescent pass was incomplete")
        second = read_once(check)
        check()
        if second is None:
            raise ValueError("a quiescent pass was incomplete")

        def signature(rows: list[dict[str, Any]]) -> Counter[str]:
            return Counter(
                json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False)
                for row in rows
            )

        if signature(first) != signature(second):
            raise ValueError("quiescent complete passes disagree")
        return second
