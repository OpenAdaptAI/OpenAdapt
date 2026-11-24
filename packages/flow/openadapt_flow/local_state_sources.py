"""The typed, read-only sources a ``kind: local-state`` check reads.

These are configuration models, kept below the runtime layer so that
:mod:`openadapt_flow.deployment` can validate ``effects.local_state`` without
importing the verifier. The verifier is
:mod:`openadapt_flow.runtime.effects.local_state`, which re-exports them.
"""

from __future__ import annotations

import re
from typing import Literal, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "FileSource",
    "LocalStateSource",
    "MacosDefaultsSource",
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


LocalStateSource = Union[FileSource, MacosDefaultsSource, WindowsRegistrySource]
