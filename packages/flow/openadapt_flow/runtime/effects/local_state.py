"""Read-only local-state :class:`EffectVerifier`: files and OS settings.

Some workflows change state that no application database holds: a document
saved to disk, or an operating-system setting such as the appearance mode.
The screen showing the change proves nothing. This verifier reads that state
back through the operating system's own supported, read-only interfaces, so
a run can be checked the same way a REST or SQL write is checked.

Each **source** is a typed, closed description of one piece of state. There is
no free-form command: the verifier decides how to read each type, and every
read is read-only.

``file``
    A path on this computer. Read with ordinary file I/O, never opened for
    writing.
``macos_defaults``
    A preference ``key`` in a ``domain`` (``NSGlobalDomain`` or a bundle id).
    Read with ``/usr/bin/defaults export <domain> -``, the supported reader of
    the preferences system. It goes through ``cfprefsd``, so it sees the
    current value, not a stale plist on disk.
``windows_registry``
    A value ``name`` under ``hive`` (``HKCU`` or ``HKLM``) and ``path``. Read
    with :mod:`winreg` and ``KEY_READ`` access only.

Each source flattens to one record, keyed by the source's ``id``:

* every source: ``id``, ``type`` and ``present`` (``True`` or ``False``);
* ``file`` adds ``size``, ``sha256`` and ``text`` (the contents decoded as
  UTF-8 when they are at most :data:`TEXT_LIMIT` bytes and valid UTF-8, else
  ``None``);
* ``macos_defaults`` and ``windows_registry`` add ``value`` (the value as
  text: see :func:`value_text`) and ``value_type``.

So an example declares its expected change with the shared effect kinds::

    effects:
      - kind: field_equals
        match: {id: note}
        field: text
        value: {param: note}

A missing file, key or value is a real, readable state (``present: False``):
an expected state of "this setting is off" can be the key's absence. A read
that fails for any other reason (wrong operating system, permission denied,
``defaults`` exiting non-zero, output that does not parse) makes the whole
read unreadable, so the verdict is INDETERMINATE and the run halts. The
verifier never guesses a value it could not read.

The tier is ``INDEPENDENT_SYSTEM``: the read goes to the store the state
lives in (the file system, the preferences system, the registry) through a
separate process or API, not through the screen that showed the change.

No model calls, no network, and nothing here writes.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

from openadapt_flow.runtime.effects.adapter import (
    VerifierAdapterBase,
    poll_until_settled,
)
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    EffectVerdict,
)
from openadapt_flow.verification import VerificationTier

#: A file larger than this many bytes is hashed, but its ``text`` is ``None``.
TEXT_LIMIT = 1 << 20  # 1 MiB

#: The supported macOS preferences reader. An absolute path, so ``PATH``
#: cannot substitute another program.
DEFAULTS_BINARY = "/usr/bin/defaults"

#: A source id: what an effect's ``match: {id: ...}`` names.
_SOURCE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,63}")
#: A preferences domain: ``NSGlobalDomain`` or a reverse-DNS bundle id. A
#: leading ``-`` is refused, so a domain can never become a ``defaults`` flag.
_DEFAULTS_DOMAIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
#: Registry hives a source may read. Per-user settings and machine settings;
#: nothing else is needed for read-back.
REGISTRY_HIVES = ("HKCU", "HKLM")

Runner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]


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


class _Unreadable(Exception):
    """A source could not be read. The whole read is then unreadable."""


def value_text(value: Any) -> Optional[str]:
    """A setting's value as the text an effect compares against.

    Text stays as it is, a boolean is ``"true"`` or ``"false"``, a number is
    its decimal form, bytes are lowercase hex, and anything else (a list or a
    dictionary from a plist, a multi-string from the registry) is compact JSON
    with sorted keys. ``None`` stays ``None``.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float, str)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex()
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise _Unreadable(f"value of type {type(value).__name__} has no text form") from exc


def _plist_type(value: Any) -> str:
    for kind, name in (
        (bool, "boolean"),
        (int, "integer"),
        (float, "float"),
        (str, "string"),
        (bytes, "data"),
        (list, "array"),
        (dict, "dictionary"),
    ):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _run(argv: list[str]) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(argv, capture_output=True, timeout=10, check=False)


class LocalStateVerifier(VerifierAdapterBase):
    """Verify effects against files and OS settings on this computer.

    Args:
        sources: The typed sources to read, each with a distinct ``id``.
        poll_interval_s: Re-read interval while an effect settles (the OS
            may apply a setting a moment after the click).
        runner: Runs the fixed ``defaults`` argv; injectable for tests.
        registry: The :mod:`winreg` module (or a stand-in for tests);
            ``None`` imports it, and its absence makes registry sources
            unreadable.
        platform: ``sys.platform`` override for tests.
        session: Unused; present for protocol symmetry.
    """

    substrate = "local_state"
    verification_tier = VerificationTier.INDEPENDENT_SYSTEM

    def __init__(
        self,
        sources: list[LocalStateSource],
        *,
        poll_interval_s: float = 0.25,
        runner: Optional[Runner] = None,
        registry: Any = None,
        platform: Optional[str] = None,
        session: Any = None,
    ) -> None:
        if not sources:
            raise ValueError("effects.kind 'local-state' needs at least one source")
        ids = [s.id for s in sources]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                "local-state source ids must be distinct: " + ", ".join(duplicates)
            )
        self.sources = list(sources)
        self.poll_interval_s = poll_interval_s
        self._runner = runner or _run
        self._registry = registry
        self._platform = platform or sys.platform
        #: Why the last read was unreadable, for the verdict's detail.
        self.last_error: Optional[str] = None

    # -- reads ---------------------------------------------------------------

    def _read_file(self, source: FileSource) -> dict[str, Any]:
        path = Path(os.path.expandvars(os.path.expanduser(source.path)))
        record: dict[str, Any] = {"id": source.id, "type": source.type}
        try:
            # O_RDONLY only: the read never creates or truncates the file.
            with path.open("rb") as fh:
                data = fh.read(TEXT_LIMIT + 1)
                digest = hashlib.sha256(data)
                size = len(data)
                for block in iter(lambda: fh.read(1 << 16), b""):
                    digest.update(block)
                    size += len(block)
        except FileNotFoundError:
            if not path.parent.is_dir():
                # The folder that should hold the file is gone: that is not
                # "the file was not saved", it is a store we can't see.
                raise _Unreadable(f"{source.id}: folder {path.parent} does not exist")
            record.update(present=False, size=None, sha256=None, text=None)
            return record
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        text: Optional[str] = None
        if size <= TEXT_LIMIT:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = None
        record.update(present=True, size=size, sha256=digest.hexdigest(), text=text)
        return record

    def _read_defaults(
        self, source: MacosDefaultsSource, cache: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        if self._platform != "darwin":
            raise _Unreadable(f"{source.id}: macos_defaults needs macOS")
        domain = cache.get(source.domain)
        if domain is None:
            try:
                done = self._runner([DEFAULTS_BINARY, "export", source.domain, "-"])
            except (OSError, subprocess.SubprocessError) as exc:
                raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
            if done.returncode != 0:
                raise _Unreadable(
                    f"{source.id}: defaults exited {done.returncode}"
                )
            try:
                parsed = plistlib.loads(done.stdout)
            except Exception as exc:  # noqa: BLE001 - any parse failure is unreadable
                raise _Unreadable(f"{source.id}: defaults output is not a plist") from exc
            if not isinstance(parsed, dict):
                raise _Unreadable(f"{source.id}: defaults output is not a dictionary")
            domain = parsed
            cache[source.domain] = domain
        record: dict[str, Any] = {"id": source.id, "type": source.type}
        if source.key not in domain:
            record.update(present=False, value=None, value_type=None)
            return record
        value = domain[source.key]
        record.update(
            present=True, value=value_text(value), value_type=_plist_type(value)
        )
        return record

    def _winreg(self) -> Any:
        if self._registry is not None:
            return self._registry
        if self._platform != "win32":
            raise _Unreadable("windows_registry needs Windows")
        try:
            import winreg  # type: ignore[import-not-found,unused-ignore]
        except ImportError as exc:
            raise _Unreadable("windows_registry needs Windows") from exc
        return winreg

    def _read_registry(self, source: WindowsRegistrySource) -> dict[str, Any]:
        try:
            reg = self._winreg()
        except _Unreadable as exc:
            raise _Unreadable(f"{source.id}: {exc}") from exc
        hive = getattr(reg, "HKEY_CURRENT_USER" if source.hive == "HKCU" else "HKEY_LOCAL_MACHINE")
        record: dict[str, Any] = {"id": source.id, "type": source.type}
        try:
            with reg.OpenKey(hive, source.path, 0, reg.KEY_READ) as key:
                value, kind = reg.QueryValueEx(key, source.name)
        except FileNotFoundError:
            record.update(present=False, value=None, value_type=None)
            return record
        except OSError as exc:
            raise _Unreadable(f"{source.id}: {type(exc).__name__}") from exc
        names = {
            getattr(reg, n): n
            for n in (
                "REG_SZ",
                "REG_EXPAND_SZ",
                "REG_MULTI_SZ",
                "REG_DWORD",
                "REG_QWORD",
                "REG_BINARY",
            )
            if hasattr(reg, n)
        }
        record.update(
            present=True, value=value_text(value), value_type=names.get(kind, str(kind))
        )
        return record

    def read(self) -> Optional[list[dict[str, Any]]]:
        """One read of every source, or ``None`` when any source is unreadable."""
        cache: dict[str, dict[str, Any]] = {}
        records: list[dict[str, Any]] = []
        try:
            for source in self.sources:
                if isinstance(source, FileSource):
                    records.append(self._read_file(source))
                elif isinstance(source, MacosDefaultsSource):
                    records.append(self._read_defaults(source, cache))
                else:
                    records.append(self._read_registry(source))
        except _Unreadable as exc:
            self.last_error = str(exc)
            return None
        self.last_error = None
        return records

    def _detail(self) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "sources": [s.model_dump() for s in self.sources],
        }
        if self.last_error:
            detail["unreadable"] = self.last_error
        return detail

    # -- EffectVerifier protocol --------------------------------------------

    def capture_pre_state(self, context: Any = None) -> EffectState:
        records = self.read()
        return EffectState(
            substrate=self.substrate,
            reachable=records is not None,
            records=records or [],
            detail=self._detail(),
        )

    def verify(
        self, expected: Effect, before: EffectState, context: Any = None
    ) -> EffectVerdict:
        verdict = poll_until_settled(
            self.read,
            expected,
            before,
            substrate=self.substrate,
            poll_interval_s=self.poll_interval_s,
        )
        if self.last_error and not verdict.confirmed:
            verdict = verdict.model_copy(
                update={"reason": f"{verdict.reason} ({self.last_error})"}
            )
        return verdict


__all__ = [
    "DEFAULTS_BINARY",
    "FileSource",
    "LocalStateSource",
    "LocalStateVerifier",
    "MacosDefaultsSource",
    "REGISTRY_HIVES",
    "TEXT_LIMIT",
    "WindowsRegistrySource",
    "value_text",
]
