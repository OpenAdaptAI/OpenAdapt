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

from pydantic import BaseModel, ConfigDict, Field, field_validator

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
    """Read-only check for a macOS Calendar event matching calendar, title, and start."""

    type: Literal["macos_calendar_event"] = "macos_calendar_event"
    calendar_name: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=255)
    #: ISO-8601 start timestamp (e.g. ``2026-11-24T10:00:00``).
    start_iso: str = Field(min_length=10, max_length=64)

    @field_validator("calendar_name", "title")
    @classmethod
    def _check_safe_text(cls, value: str) -> str:
        if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
            raise ValueError(
                "macos_calendar_event text fields must not contain control characters"
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


LocalStateSource = Union[
    FileSource,
    MacosDefaultsSource,
    WindowsRegistrySource,
    MacosNightShiftSource,
    WindowsNightLightSource,
    MacosCalendarEventSource,
]
