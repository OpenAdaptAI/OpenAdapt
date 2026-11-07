"""Throwaway lab identities using the ordinary admission and approver APIs.

Call only in an explicitly authorized synthetic lab. Never use its trust root
in a deployment or retain the generated directory in source or evidence.
This helper creates identities, not a confirmation, approval or admission.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

from openadapt_flow.jit import authority
from openadapt_flow.teach_authorization_fixture import LocalAuthorizer


class LabAuthorityError(ValueError):
    """The caller did not supply a fresh private lab directory."""


@dataclass(frozen=True)
class LabAuthorities:
    key_dir: Path
    admitter: authority.Principal = field(repr=False)
    operator: LocalAuthorizer = field(repr=False)


def create_lab_authorities(lab_root: Path) -> LabAuthorities:
    """Create a fresh ``lab-demo`` operator and registered admitter.

    The caller supplies an absolute new directory under a private owned lab
    parent, outside any Git checkout. The encrypted admitter key stays there;
    its random passphrase is used only in this process. The returned ordinary
    handles can be passed to ``load_task`` by the trusted lab driver. They do
    not answer its confirmation callback or authorize a repair themselves.
    """
    root = Path(lab_root)
    try:
        if not root.is_absolute() or root.exists() or root.is_symlink():
            raise ValueError("not fresh")
        for ancestor in [root.parent, *root.parent.parents]:
            if ancestor.is_symlink() or (ancestor / ".git").exists():
                raise ValueError("not an independent lab")
        parent = root.parent.stat()
        if root.parent.resolve(strict=True) != root.parent or not root.parent.is_dir() or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("not a private owned parent")
        root.mkdir(mode=0o700)
        key_dir = root / "authority"
        key_dir.mkdir(mode=0o700)
        operator = LocalAuthorizer(key_dir, "lab-demo")
        env = {authority.KEY_PASSPHRASE_ENV: secrets.token_urlsafe(48)}
        path = key_dir / "admitter.pem"
        public = authority.generate_encrypted_key(path, env=env)
        authority.register_principal(
            key_dir, role="admitter_key", principal_id="lab-demo:admitter", public_key_hex=public
        )
        admitter = authority.principal_from_key(
            authority.load_encrypted_key(path, env=env), key_dir
        )
        return LabAuthorities(key_dir, admitter, operator)
    except Exception:
        # Keep any partial lab directory for the owner. Never retry or reuse it.
        raise LabAuthorityError("LAB_AUTHORITY_UNAVAILABLE") from None
