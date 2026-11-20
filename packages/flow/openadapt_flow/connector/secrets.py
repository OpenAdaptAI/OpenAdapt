"""Local secret store for the connector: only the reference crosses the network.

A dispatch carries ``secrets_ref`` (a short name such as ``emr-reader``). The
connector resolves it here, on this machine, to ``<secrets_dir>/<ref>.env``: a
file of ``NAME=value`` lines the customer's administrator writes and protects.
The values go only into the environment of the one governed child process that
runs that job (where a deployment's ``effects.secret_env`` slots read them),
and into the egress guard's scan list, so a value that somehow reached a
request body is refused before it is sent. They are never logged, written to
the run directory, or returned to the control plane.

On POSIX the file must not be readable by group or others (mode 0600 or
stricter); on Windows the service installer restricts the config directory
ACL to SYSTEM, Administrators and the service account.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Optional

__all__ = ["SecretStoreError", "LocalSecretStore"]

_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
#: Names a secret file may not set: they would redirect the child itself.
_RESERVED = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "PYTHONHOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "OPENADAPT_DURABLE_AUTHORITY_URL",
        "OPENADAPT_RUNNER_TOKEN",
        "OPENADAPT_DURABLE_DISPATCH_SESSION_ID",
        "OPENADAPT_JIT_KEY_DIR",
        "OPENADAPT_QUALIFICATION_SIGNERS_JSON",
        "BYOC_CONNECTOR_TOKEN",
    }
)


class SecretStoreError(RuntimeError):
    """A secret reference cannot be resolved safely. The job is refused."""


class LocalSecretStore:
    def __init__(self, root: Optional[str]) -> None:
        self.root = Path(root) if root else None

    def resolve(self, ref: Optional[str]) -> dict[str, str]:
        """Return the NAME=value pairs for ``ref`` (empty when ref is None)."""
        if ref is None or ref == "":
            return {}
        if not isinstance(ref, str) or not _REF.fullmatch(ref):
            raise SecretStoreError("secrets_ref is not a valid local reference")
        if self.root is None:
            raise SecretStoreError(
                "the job names a secrets_ref but this connector has no secrets_dir"
            )
        path = self.root / f"{ref}.env"
        try:
            info = path.stat()
        except OSError as exc:
            raise SecretStoreError(
                "secrets_ref is not present in the local store"
            ) from exc
        if not stat.S_ISREG(info.st_mode):
            raise SecretStoreError("secrets_ref does not name a regular file")
        if os.name == "posix" and info.st_mode & 0o077:
            raise SecretStoreError(
                "local secret file is readable by group or others; chmod 600 it"
            )
        values: dict[str, str] = {}
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            name, sep, value = stripped.partition("=")
            name = name.strip()
            if not sep or not _ENV_NAME.fullmatch(name) or name in _RESERVED:
                # Say which line, never what it holds.
                raise SecretStoreError(f"local secret file line {number} is invalid")
            values[name] = value.strip()
        return values
