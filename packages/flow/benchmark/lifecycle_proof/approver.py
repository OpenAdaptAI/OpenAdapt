"""A fixture-only approver for the scripted person in the proof and the soak.

A person's approval counts only when a key on the signed approver roster
signs it, and the run pins that roster's root
(:mod:`openadapt_flow.approval_mode.approver_roster`). The scripted person
plays a front-desk approver, so each run gets its own:

- a throwaway Ed25519 key, generated under the run's own directory and never
  a real person's key;
- a one-entry roster that key roots;
- a deployment config that pins the root (``approvers.root_public_key``) and
  the key directory, and whose bytes are pinned by SHA-256, as
  ``--config-sha256`` pins them for the CLI.

The approval stores take their pin only from that config
(``config_only``), never from ``OPENADAPT_APPROVER_ROOT``. The key, its
passphrase and the roster stay in the run directory, which is never
committed. The ledger records the public root and the config digest.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

#: The scripted approver's name on the fixture roster (a person's role name).
PRINCIPAL = "soak-front-desk"
KEY_DIRNAME = "approver-keys"
CONFIG_FILENAME = "deployment.yaml"
_PASSPHRASE_FILENAME = ".passphrase"
ROUTE = "desktop_run_it"


@dataclass(frozen=True)
class FixtureApprover:
    """The run's own approver key, roster and pinned deployment config."""

    key_dir: Path
    root_public_key: str
    config_path: Path
    config_sha256: str
    signer: Any

    @classmethod
    def for_run(
        cls, run_root: Path, *, policy: Optional[str] = None
    ) -> "FixtureApprover":
        """Open the run's approver, creating it on the run's first phase."""

        from openadapt_flow.approval_mode.approver_roster import (
            file_key_signer,
            init_root,
        )
        from openadapt_flow.approval_mode.human_attestation import load_signer
        from openadapt_flow.jit.authority import (
            KEY_PASSPHRASE_ENV,
            generate_encrypted_key,
            load_encrypted_key,
            public_hex,
        )

        key_dir = run_root / KEY_DIRNAME
        key_path = key_dir / f"{PRINCIPAL}.approver.pem"
        secret = key_dir / _PASSPHRASE_FILENAME
        config_path = run_root / CONFIG_FILENAME
        if not key_path.exists():
            key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(secrets.token_hex(24))
        env = {KEY_PASSPHRASE_ENV: secret.read_text().strip()}
        if not key_path.exists():
            public = generate_encrypted_key(key_path, env=env)
            init_root(
                key_dir,
                principal=PRINCIPAL,
                public_key=public,
                sign=file_key_signer(load_encrypted_key(key_path, env=env)),
            )
            lines = ["name: lifecycle-soak-fixture"]
            if policy:
                lines += ["policy:", f"  policy: {policy}"]
            lines += [
                "approvers:",
                f"  root_public_key: {public}",
                f"  key_dir: {key_dir.resolve()}",
            ]
            config_path.write_text("\n".join(lines) + "\n")
        public = public_hex(load_encrypted_key(key_path, env=env))
        digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
        return cls(
            key_dir=key_dir.resolve(),
            root_public_key=public,
            config_path=config_path,
            config_sha256=digest,
            signer=load_signer(
                key_path, key_dir, method="desktop_key", env=env, root=public
            ),
        )

    def deployment(self) -> Any:
        """The run's deployment config, read only if its bytes match the pin."""

        from openadapt_flow.deployment import load_deployment, pin_deployment_bytes

        pin_deployment_bytes(self.config_path, self.config_sha256)
        config = load_deployment(self.config_path)
        approvers = config.approvers
        if approvers is None or approvers.root_public_key != self.root_public_key:
            raise ValueError(
                "the run's deployment config doesn't pin its approver root"
            )
        return config

    def store(self, run_dir: Path) -> Any:
        """An item-approval store pinned only by the run's deployment config."""

        from openadapt_flow.approval_mode.item_approval import ItemApprovalStore

        approvers = self.deployment().approvers
        return ItemApprovalStore(
            run_dir,
            key_dir=Path(approvers.key_dir) if approvers.key_dir else self.key_dir,
            config_root=approvers.root_public_key,
            config_only=True,
        )

    def answer(
        self,
        store: Any,
        request: Any,
        *,
        approve: bool,
        decline_reason: Optional[str] = None,
    ) -> Any:
        """A signed answer from the fixture approver (``make_answer`` checks it)."""

        from openadapt_flow.approval_mode.item_approval import make_answer

        return make_answer(
            request,
            decision="approve" if approve else "decline",
            decline_reason=None if approve else (decline_reason or "wrong_record"),
            decided_by="human",
            route=ROUTE,
            # The signature names the approver; the older sign-in fields stay
            # empty, as for any key-signed answer.
            approver_id=None,
            auth_method="none",
            signer=self.signer,
            policy=store.context(),
        )

    def ledger_fields(self) -> dict[str, str]:
        """What the ledger records: public values only, never the key."""

        return {
            "principal": PRINCIPAL,
            "root_public_key": self.root_public_key,
            "config_sha256": self.config_sha256,
        }
