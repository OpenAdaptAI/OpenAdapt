"""The typed, read-only sources a ``kind: local-state`` check reads.

These are configuration models, kept below the runtime layer so that
:mod:`openadapt_flow.deployment` can validate ``effects.local_state`` without
importing the verifier. The verifier is
:mod:`openadapt_flow.runtime.effects.local_state`, which re-exports them.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "FileSource",
    "LocalStateSource",
    "MacosCalendarEventSource",
    "MacosDefaultsSource",
    "MacosNightShiftSource",
    "WindowsNightLightSource",
    "WindowsRegistrySource",
]

#: A source id: what an effect's ``match: {id: ...}`` names.
_SOURCE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
#: A preferences domain: ``NSGlobalDomain`` or a reverse-DNS bundle id. A
#: leading ``-`` is refused, so a domain can never become a ``defaults`` flag.
_DEFAULTS_DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


class _Source(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str

    @field_validator("id")
    @classmethod
    def _check_id(cls, value: str) -> str:
        if not _SOURCE_ID.fullmatch(value):
            raise ValueError(
                "local-state source id must start with a letter and use only "
                "letters, digits, '_', '.' or '-' (at most 64 characters)"
            )
        return value


class FileSource(_Source):
    """A file on this computer: its contents and SHA-256."""

    type: Literal["file"] = "file"
    #: The file's path. ``~`` and environment variables are expanded.
    path: str = Field(min_length=1)


class MacosDefaultsSource(_Source):
    """One key in a macOS preferences domain."""

    type: Literal["macos_defaults"] = "macos_defaults"
    #: ``NSGlobalDomain`` (``-g`` is accepted and means the same) or a bundle id.
    domain: str
    key: str = Field(min_length=1, max_length=255)

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, value: str) -> str:
        if value == "-g":
            return "NSGlobalDomain"
        if not _DEFAULTS_DOMAIN.fullmatch(value):
            raise ValueError(
                "macos_defaults domain must be NSGlobalDomain or a bundle id "
                "(letters, digits, '.', '_' or '-'; not starting with '-')"
            )
        return value


class WindowsRegistrySource(_Source):
    """One value in the Windows registry."""

    type: Literal["windows_registry"] = "windows_registry"
    hive: Literal["HKCU", "HKLM"]
    #: The key path under the hive, with backslashes (no leading backslash).
    path: str = Field(min_length=1, max_length=1024)
    #: The value's name. An empty string is the key's default value.
    name: str = Field(default="", max_length=16383)

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if value.startswith("\\") or "\x00" in value:
            raise ValueError(
                "windows_registry path is relative to the hive and must not "
                "start with a backslash"
            )
        return value


class MacosNightShiftSource(_Source):
    """Current macOS Night Shift enabled state via CoreBrightness (read-only)."""

    type: Literal["macos_night_shift"] = "macos_night_shift"


class WindowsNightLightSource(_Source):
    """Current Windows Night Light enabled state via CloudStore registry Data bytes."""

    type: Literal["windows_night_light"] = "windows_night_light"


class MacosCalendarEventSource(_Source):
    """Read-only check for a macOS EventKit calendar event.

    Matches exact calendar name (rejecting ambiguous duplicates), case-sensitive
    title, local-timezone start/end time to the minute, recurrence cadence, and
    optional ``event_uid``.
    """

    type: Literal["macos_calendar_event"] = "macos_calendar_event"
    calendar_name: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    #: ISO-8601 start timestamp (e.g. ``2026-11-24T10:00:00`` or aware ``...Z``).
    start_iso: str = Field(min_length=10, max_length=64)
    #: Optional ISO-8601 end timestamp; when set, end time must also match to the minute.
    end_iso: str | None = Field(default=None, min_length=10, max_length=64)
    #: Expected recurrence cadence (``none``, ``daily``, ``weekly``, or ``weekdays``).
    recurrence: Literal["none", "daily", "weekly", "weekdays"] = "none"
    #: Optional EventKit event identifier to bind verification to one created event.
    event_uid: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("calendar_name", "title")
    @classmethod
    def _check_safe_text(cls, value: str) -> str:
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError(
                "macos_calendar_event text fields must not contain control characters"
            )
        return value

    @field_validator("event_uid")
    @classmethod
    def _check_event_uid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError(
                "macos_calendar_event event_uid must not contain control characters"
            )
        return value

    @field_validator("start_iso")
    @classmethod
    def _check_start_iso(cls, value: str) -> str:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(
                "macos_calendar_event start_iso must be a valid ISO-8601 timestamp"
            ) from exc
        return value

    @field_validator("end_iso")
    @classmethod
    def _check_end_iso(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(
                "macos_calendar_event end_iso must be a valid ISO-8601 timestamp"
            ) from exc
        return value

    @model_validator(mode="after")
    def _check_end_after_start(self) -> "MacosCalendarEventSource":
        if self.end_iso is not None:
            s_norm = (
                self.start_iso[:-1] + "+00:00"
                if self.start_iso.endswith("Z")
                else self.start_iso
            )
            e_norm = (
                self.end_iso[:-1] + "+00:00"
                if self.end_iso.endswith("Z")
                else self.end_iso
            )
            s_dt = datetime.fromisoformat(s_norm)
            e_dt = datetime.fromisoformat(e_norm)
            if (s_dt.tzinfo is None) != (e_dt.tzinfo is None):
                raise ValueError(
                    "macos_calendar_event start_iso and end_iso must both be naive or both be timezone-aware"
                )
            if e_dt <= s_dt:
                raise ValueError("macos_calendar_event end_iso must be after start_iso")
        return self


LocalStateSource = Union[
    FileSource,
    MacosDefaultsSource,
    WindowsRegistrySource,
    MacosNightShiftSource,
    WindowsNightLightSource,
    MacosCalendarEventSource,
]
