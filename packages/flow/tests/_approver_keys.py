"""Provision a registered approver key for tests that answer as a person.

A person's answer to a save, and a durable resume approval, count only when a
key registered under ``approvers`` signs them
(:mod:`openadapt_flow.approval_mode.human_attestation`). Tests that play the
person use this throwaway key directory; agents in real use never hold it.
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
from openadapt_flow.jit.authority import (
    KEY_PASSPHRASE_ENV,
    generate_encrypted_key,
    register_principal,
)
from openadapt_flow.jit.lifecycle import KEY_DIR_ENV

PASSPHRASE = "approver-test-passphrase"
_CURRENT: list[ApproverSigner] = []


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
    register_principal(
        key_dir, role="approver", principal_id=principal_id, public_key_hex=public
    )
    return key_path, load_signer(key_path, key_dir, method=method, env=env)  # type: ignore[arg-type]


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
