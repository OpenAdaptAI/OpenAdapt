"""A throwaway approver for tests, demos and evaluations.

A real teach authorization is signed by a person with their approver key
(:mod:`openadapt_flow.teach_authorization`). Tests, the MockMed demos and the
injection evaluation need one without a person at the keyboard, so
:class:`LocalAuthorizer` makes an Ed25519 approver key in memory, starts (or
joins) a real signed approver roster in a key directory, and signs statements
with the real approver signer. Its root is what a run pins: anything it signs
is trusted only where that root is pinned. Never pin it in a deployment.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow import teach_authorization as ta

__all__ = ["LocalAuthorizer", "demo_authority"]


class LocalAuthorizer:
    """An in-memory approver key on a real approver roster in ``key_dir``.

    The first one in a key directory starts the roster (it is the root);
    pass ``added_by`` to add another approver with that one's signature.
    ``register=False`` makes a key that is on no roster.
    """

    def __init__(
        self,
        key_dir: Path,
        principal: str = "teach-authorizer",
        *,
        added_by: "Optional[LocalAuthorizer]" = None,
        method: str = "person_key",
        register: bool = True,
    ) -> None:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        from openadapt_flow.approval_mode import approver_roster as roster
        from openadapt_flow.jit.authority import public_hex

        self.key_dir = Path(key_dir)
        self.principal = principal
        self.method = method
        self._key = Ed25519PrivateKey.generate()
        self.public_key = public_hex(self._key)
        self.root: str = added_by.root if added_by is not None else self.public_key
        if not register:
            return
        self.key_dir.mkdir(parents=True, exist_ok=True)
        if added_by is None:
            roster.init_root(
                self.key_dir,
                principal=principal,
                public_key=self.public_key,
                sign=roster.file_key_signer(self._key),
            )
        else:
            roster.add_approver(
                self.key_dir,
                self.root,
                principal=principal,
                public_key=self.public_key,
                signed_by=added_by.principal,
                sign=roster.file_key_signer(added_by._key),
            )

    def remove(self, other: "LocalAuthorizer") -> None:
        """Remove (revoke) ``other`` from the roster with this approver's signature."""

        from openadapt_flow.approval_mode import approver_roster as roster

        roster.remove_approver(
            self.key_dir,
            self.root,
            principal=other.principal,
            public_key=other.public_key,
            signed_by=self.principal,
            sign=roster.file_key_signer(self._key),
        )

    def signer(self) -> Any:
        from openadapt_flow.approval_mode.human_attestation import ApproverSigner

        return ApproverSigner(
            principal_id=self.principal,
            method=self.method,  # type: ignore[arg-type]
            signing=self._key,
        )

    def authority(self, statement: Path, **kw: Any) -> ta.TeachAuthority:
        """A :class:`TeachAuthority` over ``statement`` that pins this roster."""

        return ta.TeachAuthority(
            Path(statement), key_dir=self.key_dir, approver_root=self.root, **kw
        )

    def issue(
        self,
        folder: Path,
        spec: Mapping[str, Any],
        *,
        now: Optional[datetime] = None,
        valid_for: timedelta = timedelta(days=1),
        authorizer: Optional[str] = None,
    ) -> Path:
        """Write and sign a statement for ``spec`` in ``folder``; return its path."""

        now = now or datetime.now(timezone.utc)
        statement = ta.TeachAuthorizationV1.model_validate(
            {
                **dict(spec),
                "schema": ta.SCHEMA,
                "authorizer": authorizer or self.principal,
                "authorizer_public_key": self.public_key,
                "issued_at": ta._utc_seconds(now - timedelta(seconds=5)),
                "expires_at": ta._utc_seconds(now + valid_for),
                "nonce": secrets.token_hex(32),
            }
        )
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / ta.STATEMENT_FILE
        path.write_text(ta.dumps(statement), encoding="utf-8")
        self.sign_file(path, as_principal=authorizer)
        return path

    def sign_file(
        self, statement_path: Path, *, as_principal: Optional[str] = None
    ) -> Path:
        """Sign with the real approver signer, as ``teach-authorization sign`` does.

        Writes the attestation directly (not through :func:`ta.sign`, which
        checks the signer first) so a test can sign as a principal the
        roster doesn't hold for this key.
        """

        from openadapt_flow.approval_mode.human_attestation import ApproverSigner
        from openadapt_flow.offline_seal import loads_strict

        data = loads_strict(Path(statement_path).read_bytes())
        signer = ApproverSigner(
            principal_id=as_principal or self.principal,
            method=self.method,  # type: ignore[arg-type]
            signing=self._key,
        )
        attestation = signer.attest(ta.DOMAIN, data)
        sig = Path(statement_path).with_name(Path(statement_path).name + ".sig")
        sig.write_text(attestation.model_dump_json() + "\n", encoding="utf-8")
        return sig

    @staticmethod
    def digest(value: str) -> str:
        return ta.value_sha256(value)

    def __repr__(self) -> str:  # never show key material
        return f"LocalAuthorizer({self.principal}, {self.public_key[:12]})"


def demo_authority(
    work_dir: Path,
    *,
    task_id: str,
    url: str,
    record: Mapping[str, Any],
    inputs: Mapping[str, Any],
    fields: Optional[Mapping[str, Any]] = None,
    constants: Any = (),
    record_kind: str = "record",
) -> ta.TeachAuthority:
    """A teach authorization a throwaway approver signs, for demos and benchmarks.

    Starts a new approver roster in a fresh folder under ``work_dir``, signs
    a statement for exactly this run (the task, the app's origin, the record,
    the inputs' values and any other typeable text), and returns a
    :class:`TeachAuthority` that pins that roster. Admission checks it like
    any other. Never use it where a person must authorize the teaching.
    """

    from openadapt_flow.teach_scope import origin_of

    folder = Path(work_dir) / f"demo-teach-authorization-{secrets.token_hex(6)}"
    person = LocalAuthorizer(folder / "approvers", "demo-authorizer")
    spec = {
        "task": {"task_id": task_id, "summary": f"Demo run of {task_id}"},
        "purposes": ["teach", "repair"],
        "origins": [str(origin_of(url))],
        "inputs": {str(k): {"value": str(v)} for k, v in inputs.items()},
        "constants": [{"value": str(c)} for c in dict.fromkeys(map(str, constants))],
        "writes": {
            "record_kind": record_kind,
            "identity": {str(k): {"value": str(v)} for k, v in record.items()},
            "fields": {str(k): {"value": str(v)} for k, v in (fields or {}).items()},
        },
    }
    return person.authority(person.issue(folder, spec))
