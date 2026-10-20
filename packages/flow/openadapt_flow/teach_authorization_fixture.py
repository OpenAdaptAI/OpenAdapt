"""A throwaway authorizer for tests, demos and evaluations.

A real teach authorization is signed by a person with the SSH key they
publish on GitHub (:mod:`openadapt_flow.teach_authorization`). Tests, the
MockMed demos and the injection evaluation need one without a person at the
keyboard, so :class:`LocalAuthorizer` makes an Ed25519 key in memory, writes
an allowed-signers file that pins it, and signs statements with it. The
pinned file is the trust root, exactly as an operator's would be: anything
signed by this key is trusted only where this file is pinned. Never pin it
in a deployment.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow import ssh_sig
from openadapt_flow import teach_authorization as ta

__all__ = ["LocalAuthorizer"]


class LocalAuthorizer:
    """An in-memory Ed25519 key standing in for a person's GitHub SSH key."""

    def __init__(self, handle: str = "github:synthetic-authorizer") -> None:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        self.handle = handle
        self._key = Ed25519PrivateKey.generate()
        self.raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.fingerprint = ssh_sig.fingerprint(self.raw)

    def pin(self, path: Path) -> Path:
        """Write the allowed-signers file an operator would pin; return it."""

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            ssh_sig.allowed_signers_line(self.handle, ta.NAMESPACE, self.raw) + "\n",
            encoding="utf-8",
        )
        return path

    def sign_file(self, statement_path: Path) -> Path:
        blob = ssh_sig.signed_blob(
            Path(statement_path).read_bytes(), ta.NAMESPACE, "sha512"
        )
        sig = Path(statement_path).with_name(Path(statement_path).name + ".sig")
        sig.write_text(ssh_sig.armor(self.raw, ta.NAMESPACE, self._key.sign(blob)))
        return sig

    def issue(
        self,
        folder: Path,
        spec: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
        valid_for: timedelta = timedelta(days=1),
    ) -> Path:
        """Write and sign a statement for ``spec`` in ``folder``; return the statement path."""

        now = now or datetime.now(timezone.utc)
        statement = ta.TeachAuthorizationV1.model_validate(
            {
                **dict(spec),
                "schema": ta.SCHEMA,
                "authorizer": self.handle,
                "signer_fingerprint": self.fingerprint,
                "issued_at": ta._utc_seconds(now - timedelta(seconds=5)),
                "expires_at": ta._utc_seconds(now + valid_for),
                "nonce": secrets.token_hex(32),
            }
        )
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ta.STATEMENT_FILE
        path.write_text(ta.dumps(statement), encoding="utf-8")
        self.sign_file(path)
        return path

    @staticmethod
    def digest(value: str) -> str:
        return ta.value_sha256(value)

    def __repr__(self) -> str:  # never show key material
        return f"LocalAuthorizer({self.handle}, {self.fingerprint})"


def spec_digest(spec: Mapping[str, Any]) -> str:
    """A stable digest of a spec (for logs that must not hold its values)."""

    import json

    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
