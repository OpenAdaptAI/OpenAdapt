"""Sealed record of source ordinals that window-scoped recording dropped.

Every native input reserves a source ordinal when it arrives, before Capture
knows whether the target window received it. Input that went elsewhere is
dropped by policy, so its ordinal holds no event. Capture writes each such
ordinal and its reason code here. The artifact manifest seals the file, and
the verifier accepts a source journal hole only when this file names it. A
hole with no record (a crash, a lost write, or a bug) still fails.

The file holds ordinals and reason codes only: never key identity, text,
points, or window names.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Iterable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

SOURCE_ORDINAL_SKIPS_FILENAME = "source-ordinal-skips.json"
SOURCE_ORDINAL_SKIPS_SCHEMA_VERSION = "openadapt.capture.source-ordinal-skips/v1"

# The release (or drag) of a press that was itself dropped. It continues
# input the target never received, so it is dropped too.
INPUT_CONTINUES_DROPPED_INPUT = "continues_dropped_input"

# Every reason a window-scoped recorder may drop input by policy. These match
# the ``INPUT_*`` reason codes in ``openadapt_capture.window_capture``.
SourceOrdinalSkipReason = Literal[
    "keyboard_focus_elsewhere",
    "keyboard_focus_in_dialog",
    "keyboard_focus_unknown",
    "pointer_over_other_window",
    "pointer_target_unknown",
    "target_not_on_screen",
    "continues_dropped_input",
]
SOURCE_ORDINAL_SKIP_REASONS: frozenset[str] = frozenset(SourceOrdinalSkipReason.__args__)


class SourceOrdinalSkipError(ValueError):
    """The sealed source-ordinal skip record is malformed."""


class SourceOrdinalSkipRun(BaseModel):
    """Consecutive ordinals ``first..last`` dropped for one reason."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    first: int = Field(ge=1)
    last: int = Field(ge=1)
    reason: SourceOrdinalSkipReason

    @model_validator(mode="after")
    def _ordered(self) -> "SourceOrdinalSkipRun":
        if self.last < self.first:
            raise ValueError("a skip run must not end before it starts")
        return self


class SourceOrdinalSkips(BaseModel):
    """Canonical, ordered, non-overlapping runs of dropped ordinals."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["openadapt.capture.source-ordinal-skips/v1"]
    runs: tuple[SourceOrdinalSkipRun, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_runs(self) -> "SourceOrdinalSkips":
        previous: SourceOrdinalSkipRun | None = None
        for run in self.runs:
            if previous is not None:
                if run.first <= previous.last:
                    raise ValueError("skip runs must be ordered and must not overlap")
                if run.first == previous.last + 1 and run.reason == previous.reason:
                    raise ValueError("adjacent skip runs with one reason must be merged")
            previous = run
        return self

    def ordinals(self) -> dict[int, str]:
        """Return every skipped ordinal and its reason."""
        return {
            ordinal: run.reason
            for run in self.runs
            for ordinal in range(run.first, run.last + 1)
        }


def _canonical_bytes(payload: object) -> bytes:
    return (
        json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    ).encode("utf-8")


def build_source_ordinal_skips(
    skips: Iterable[tuple[int, str]],
) -> SourceOrdinalSkips | None:
    """Merge ``(ordinal, reason)`` pairs into canonical runs; ``None`` if empty."""
    ordered = sorted(skips)
    runs: list[dict[str, object]] = []
    for ordinal, reason in ordered:
        if reason not in SOURCE_ORDINAL_SKIP_REASONS:
            raise SourceOrdinalSkipError(f"unknown source-ordinal skip reason: {reason!r}")
        if runs and ordinal <= runs[-1]["last"]:
            raise SourceOrdinalSkipError("a source ordinal was skipped twice")
        if runs and ordinal == runs[-1]["last"] + 1 and reason == runs[-1]["reason"]:
            runs[-1]["last"] = ordinal
        else:
            runs.append({"first": ordinal, "last": ordinal, "reason": reason})
    if not runs:
        return None
    try:
        return SourceOrdinalSkips(
            schema_version=SOURCE_ORDINAL_SKIPS_SCHEMA_VERSION,
            runs=tuple(SourceOrdinalSkipRun(**run) for run in runs),
        )
    except ValidationError as exc:
        raise SourceOrdinalSkipError("invalid source-ordinal skips") from exc


def write_source_ordinal_skips(
    capture_dir: str | os.PathLike[str],
    skips: Iterable[tuple[int, str]],
) -> Path | None:
    """Write the skip record once, durably; write nothing when there are none."""
    record = build_source_ordinal_skips(skips)
    if record is None:
        return None
    directory = Path(capture_dir)
    path = directory / SOURCE_ORDINAL_SKIPS_FILENAME
    if os.path.lexists(path):
        raise SourceOrdinalSkipError("the source-ordinal skip record already exists")
    data = _canonical_bytes(record.model_dump(mode="json"))
    fd, temporary = tempfile.mkstemp(prefix=".source-ordinal-skips-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)  # create-only: never replaces an existing record
    finally:
        os.unlink(temporary)
    return path


def load_source_ordinal_skips(capture_dir: str | os.PathLike[str]) -> dict[int, str]:
    """Return the sealed skipped ordinals and reasons; empty when there is no record."""
    path = Path(capture_dir) / SOURCE_ORDINAL_SKIPS_FILENAME
    if not os.path.lexists(path):
        return {}
    if path.is_symlink() or not path.is_file():
        raise SourceOrdinalSkipError("the source-ordinal skip record is not a regular file")
    raw = path.read_bytes()
    try:
        record = SourceOrdinalSkips.model_validate_json(raw)
    except ValidationError as exc:
        raise SourceOrdinalSkipError("the source-ordinal skip record is malformed") from exc
    if raw != _canonical_bytes(record.model_dump(mode="json")):
        raise SourceOrdinalSkipError("the source-ordinal skip record is not canonical")
    return record.ordinals()


__all__ = [
    "INPUT_CONTINUES_DROPPED_INPUT",
    "SOURCE_ORDINAL_SKIPS_FILENAME",
    "SOURCE_ORDINAL_SKIPS_SCHEMA_VERSION",
    "SOURCE_ORDINAL_SKIP_REASONS",
    "SourceOrdinalSkipError",
    "SourceOrdinalSkipRun",
    "SourceOrdinalSkips",
    "build_source_ordinal_skips",
    "load_source_ordinal_skips",
    "write_source_ordinal_skips",
]
