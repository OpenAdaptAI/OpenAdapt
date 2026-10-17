"""The local dispute vault: what a holder keeps so a receipt can be defended.

The vault holds what must not leave the customer by default: the salts and
values behind field commitments, and the notarized document that binds them.
One directory per receipt, named by its ``receipt_sha256``. Directories are
created mode 0700 and files 0600 (POSIX; on Windows rely on the profile ACL).

Default location: ``$OPENADAPT_NOTARY_VAULT`` or ``~/.openadapt/notary-vault``.
Treat it as PHI at rest: back it up the way you back up the system of record,
and encrypt the volume.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.notary.commitments import FieldSecret

__all__ = ["NotaryVault", "VaultError", "default_vault_path", "write_private_bytes"]

_RECEIPT_SHA = re.compile(r"^[0-9a-f]{64}$")
SECRETS_SCHEMA = "openadapt.notary-vault-secrets/v1"


class VaultError(RuntimeError):
    pass


def default_vault_path(env: Optional[Mapping[str, str]] = None) -> Path:
    env = os.environ if env is None else env
    named = env.get("OPENADAPT_NOTARY_VAULT", "").strip()
    return (
        Path(named).expanduser()
        if named
        else Path.home() / ".openadapt" / "notary-vault"
    )


def write_private_bytes(path: Path, data: bytes, *, overwrite: bool = False) -> None:
    """Write ``data`` atomically with mode 0600; refuse to replace unless asked."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists() and not overwrite:
        raise VaultError(
            f"{path.name} already exists in the vault; refusing to replace it"
        )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix)
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class NotaryVault:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root is not None else default_vault_path()

    def _dir(self, receipt_sha256: str) -> Path:
        if not _RECEIPT_SHA.match(receipt_sha256):
            raise VaultError("a vault entry is named by a 64-hex receipt_sha256")
        path = self.root / receipt_sha256
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.root, 0o700)
            os.chmod(path, 0o700)
        except OSError:  # pragma: no cover - non-POSIX filesystems
            pass
        return path

    def put_secrets(self, receipt_sha256: str, kept: list[FieldSecret]) -> Path:
        path = self._dir(receipt_sha256) / "field-secrets.json"
        body = {"schema": SECRETS_SCHEMA, "fields": [s.as_dict() for s in kept]}
        write_private_bytes(path, json.dumps(body, indent=2, sort_keys=True).encode())
        return path

    def get_secrets(self, receipt_sha256: str) -> list[FieldSecret]:
        path = self._dir(receipt_sha256) / "field-secrets.json"
        if not path.is_file():
            raise VaultError("the vault holds no field secrets for this receipt")
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("schema") != SECRETS_SCHEMA:
            raise VaultError("unrecognized vault secrets file")
        return [FieldSecret(**entry) for entry in body["fields"]]

    def put_json(
        self, receipt_sha256: str, name: str, value: Any, *, overwrite: bool = False
    ) -> Path:
        if not re.match(r"^[a-z0-9-]+\.json$", name):
            raise VaultError("vault file names are lowercase .json")
        path = self._dir(receipt_sha256) / name
        write_private_bytes(
            path,
            json.dumps(value, indent=2, sort_keys=True).encode(),
            overwrite=overwrite,
        )
        return path
