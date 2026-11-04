"""Provision a registered approver key for tests that answer as a person.

A person's answer to a save, and a durable resume approval, count only when a
key on the signed approver roster signs them and the run pins that roster's
root (:mod:`openadapt_flow.approval_mode.approver_roster`). Tests that play the
person use this throwaway key directory: the first key provisioned there is
the roster's root, later ones are added with the root's signature, and the
fixture pins the root in ``OPENADAPT_APPROVER_ROOT``. Agents in real use never
hold these keys.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.approval_mode.human_attestation import (
    PORTAL_KEY_ENV,
    ApproverSigner,
    load_signer,
)
from openadapt_flow.approval_mode.approver_roster import (
    ROOT_ENV,
    add_approver,
    file_key_signer,
    init_root,
)
from openadapt_flow.jit.authority import (
    KEY_PASSPHRASE_ENV,
    generate_encrypted_key,
    load_encrypted_key,
)
from openadapt_flow.jit.lifecycle import KEY_DIR_ENV

PASSPHRASE = "approver-test-passphrase"
_CURRENT: list[ApproverSigner] = []
#: key_dir -> (root principal, root private key, root public hex)
_ROOTS: dict[Path, tuple[str, Any, str]] = {}


def provision(
    key_dir: Path,
    principal_id: str = "front-desk",
    *,
    method: str = "desktop_key",
) -> tuple[Path, ApproverSigner]:
    """Create, register and open an approver key in ``key_dir``."""

    env = {KEY_PASSPHRASE_ENV: PASSPHRASE}
    key_path = key_dir / f"{principal_id.replace(':', '_')}.approver.pem"
    public = generate_encrypted_key(key_path, env=env)
    key = load_encrypted_key(key_path, env=env)
    root = _ROOTS.get(Path(key_dir))
    if root is None:
        init_root(
            key_dir,
            principal=principal_id,
            public_key=public,
            sign=file_key_signer(key),
        )
        _ROOTS[Path(key_dir)] = (principal_id, key, public)
        root_public = public
    else:
        root_principal, root_key, root_public = root
        add_approver(
            key_dir,
            root_public,
            principal=principal_id,
            public_key=public,
            signed_by=root_principal,
            sign=file_key_signer(root_key),
        )
    return key_path, load_signer(  # type: ignore[arg-type]
        key_path, key_dir, method=method, env=env, root=root_public
    )


def approvers_yaml(root: str | None = None) -> str:
    """The deployment config section that pins the approver root.

    A governed run takes its pin only from its deployment config. Without an
    argument, the root the active ``approver`` fixture pinned.
    """

    import os

    pinned = root or os.environ[ROOT_ENV]
    return f"approvers:\n  root_public_key: {pinned}\n"


def approver_config(path: Path, root: str | None = None) -> Path:
    """A deployment config that only pins the approver root."""

    path.write_text(approvers_yaml(root))
    return path


def root_of(key_dir: Path) -> str:
    """The roster root :func:`provision` started in ``key_dir`` (hex)."""

    return _ROOTS[Path(key_dir)][2]


@pytest.fixture
def approver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A registered Desktop approver key in this test's key directory.

    Sets the key directory and passphrase for the CLI and the run, and the
    decision portal's key. Returns ``(key_path, signer)``.
    """

    key_dir = tmp_path / "approver-keys"
    monkeypatch.setenv(KEY_DIR_ENV, str(key_dir))
    monkeypatch.setenv(KEY_PASSPHRASE_ENV, PASSPHRASE)
    key_path, signer = provision(key_dir)
    monkeypatch.setenv(ROOT_ENV, root_of(key_dir))
    portal_path, _portal = provision(key_dir, "decision-portal", method="portal_key")
    monkeypatch.setenv(PORTAL_KEY_ENV, str(portal_path))
    _CURRENT.append(signer)
    yield key_path, signer
    _CURRENT.remove(signer)


def person_signer() -> ApproverSigner:
    """The Desktop approver key the active ``approver`` fixture registered.

    For helpers that play the person deep inside a run (a fake ``sleep``
    that answers each request). Use the fixture in the test or module.
    """

    if not _CURRENT:
        raise RuntimeError("use the approver fixture to answer as a person")
    return _CURRENT[-1]


def answer_as_person(store: Any, request: Any, **fields: Any) -> Any:
    """``make_answer`` signed by the registered Desktop approver key."""

    from openadapt_flow.approval_mode.item_approval import make_answer

    fields.setdefault("policy", store.context())
    fields.setdefault("signer", person_signer())
    return make_answer(request, **fields)
