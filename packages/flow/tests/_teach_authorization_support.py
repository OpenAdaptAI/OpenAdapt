"""Give a test's agent-taught draft a real, person-signed teach authorization.

Admission refuses a JIT draft without one
(:func:`openadapt_flow.jit.lifecycle.teach_authorization_gate`). Tests here
use the real pieces: a real approver roster in the test's key directory, a
real approver signature (:class:`LocalAuthorizer`), the real verifier, and
the root pinned in ``OPENADAPT_APPROVER_ROOT`` as an operator would. The
roster's root is shared with ``tests/_approver_keys.py``, so tests that also
answer saves as a person add their approvers to the same roster.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow import teach_authorization as ta
from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from openadapt_flow.teach_authorization_fixture import LocalAuthorizer

AUTHORIZER = "teach-lead"


def authorizer(key_dir: Path, monkeypatch: Any = None) -> LocalAuthorizer:
    """The teach authorizer (an approver the root added) in ``key_dir``; pins the root."""

    key_dir = Path(key_dir)
    found = _AUTHORIZERS.get(key_dir)
    if found is None:
        found = LocalAuthorizer(key_dir, AUTHORIZER, added_by=roster_root(key_dir))
        _AUTHORIZERS[key_dir] = found
    patch = monkeypatch or (_MONKEYPATCH[0] if _MONKEYPATCH else None)
    if patch is not None:
        patch.setenv(ROOT_ENV, found.root)
    return found


_AUTHORIZERS: dict[Path, LocalAuthorizer] = {}


def roster_root(key_dir: Path) -> LocalAuthorizer:
    """The roster's root approver in ``key_dir`` (shared with _approver_keys)."""

    from tests import _approver_keys

    key_dir = Path(key_dir)
    root = _approver_keys._ROOTS.get(key_dir)
    if root is None:
        made = LocalAuthorizer(key_dir, "roster-admin")
        _approver_keys._ROOTS[key_dir] = (made.principal, made._key, made.public_key)
        return made
    # A roster the approver fixture started: act as its root.
    joined = LocalAuthorizer.__new__(LocalAuthorizer)
    joined.key_dir, joined.principal = key_dir, root[0]
    joined._key, joined.public_key, joined.root = root[1], root[2], root[2]
    joined.method = "person_key"
    return joined


#: The running test's monkeypatch (tests/conftest.py sets it), so the root is
#: pinned for that test only.
_MONKEYPATCH: list[Any] = []


def spec(
    task_id: str,
    *,
    origins: Sequence[str] = ("http://127.0.0.1:9",),
    record: Optional[Mapping[str, str]] = None,
    fields: Optional[Mapping[str, str]] = None,
    inputs: Optional[Mapping[str, str]] = None,
    constants: Sequence[str] = (),
    record_kind: str = "note",
    purposes: Sequence[str] = ("teach", "repair"),
) -> dict[str, Any]:
    """A statement spec with spelled-out values."""

    return {
        "task": {"task_id": task_id, "summary": f"Synthetic task {task_id}"},
        "purposes": list(purposes),
        "origins": list(origins),
        "inputs": {k: {"value": v} for k, v in (inputs or {}).items()},
        "constants": [{"value": c} for c in constants],
        "writes": {
            "record_kind": record_kind,
            "identity": {
                k: {"value": v} for k, v in (record or {"type": "note"}).items()
            },
            "fields": {k: {"value": v} for k, v in (fields or {}).items()},
        },
    }


def issue(
    key_dir: Path, folder: Path, task_id: str, monkeypatch: Any = None, **kw: Any
) -> Path:
    """A signed statement for ``task_id`` in ``folder``; return its path."""

    return authorizer(key_dir, monkeypatch).issue(folder, spec(task_id, **kw))


def authority(
    key_dir: Path, folder: Path, task_id: str, monkeypatch: Any = None, **kw: Any
) -> ta.TeachAuthority:
    """A :class:`TeachAuthority` for compile_agent, the pipeline or MCP teach."""

    existing = Path(folder) / ta.STATEMENT_FILE
    statement = (
        existing
        if existing.is_file()
        else issue(key_dir, folder, task_id, monkeypatch, **kw)
    )
    return authorizer(key_dir, monkeypatch).authority(statement)


def mockmed_note(
    key_dir: Path,
    folder: Path,
    task_id: str,
    base_url: str,
    *,
    note: str,
    record: Mapping[str, str],
    constants: Sequence[str] = (),
    monkeypatch: Any = None,
) -> ta.TeachAuthority:
    """The authorization a person signs for "add this note to this MockMed patient"."""

    from openadapt_flow.teach_scope import origin_of

    return authority(
        key_dir,
        folder,
        task_id,
        monkeypatch,
        origins=[str(origin_of(base_url))],
        record=record,
        fields={"note": note},
        inputs={"note": note},
        constants=constants,
        record_kind="encounter",
    )


def draft_fields(
    program: Path, key_dir: Path, task_id: str, monkeypatch: Any = None, **kw: Any
) -> dict[str, Any]:
    """Candidate fields for a hand-built draft, as a scoped capture writes them.

    Signs a statement, keeps it beside the draft, and returns the
    ``teach_authorization`` entry and a ``teach_scope`` block whose scope
    names it and whose read-back verified. Add them to the candidate before
    :func:`~openadapt_flow.jit.lifecycle.sign_recorder`.
    """

    from openadapt_flow.jit.lifecycle import TEACH_AUTHORIZATION_DIR

    statement = issue(key_dir, Path(program) / "_issued", task_id, monkeypatch, **kw)
    kept = ta.copy_into(statement, Path(program) / TEACH_AUTHORIZATION_DIR)
    found = authorizer(key_dir)
    record = ta.record(
        ta.require(kept, purpose="teach", key_dir=key_dir, root=found.root)
    )
    return {
        "teach_authorization": record,
        "teach_scope": {
            "scope": {"task_id": task_id, "authorization": record},
            "readback": {"status": "verified"},
        },
    }


def repair_authorization(
    store_root: Path,
    key_dir: Path,
    task_id: str = "dialog-fixture-save",
    monkeypatch: Any = None,
    *,
    purposes: Sequence[str] = ("repair",),
) -> dict[str, Any]:
    """A signed authorization to repair ``task_id``, kept in the repair store.

    Returns the record a calling agent's repair provenance carries. Pins the
    key directory (``OPENADAPT_JIT_KEY_DIR``) and the root, as an operator's
    approval host would.
    """

    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV
    from openadapt_flow.repair import RepairStore

    statement = issue(
        key_dir,
        Path(store_root).parent / "_repair_authorization",
        task_id,
        monkeypatch,
        purposes=purposes,
    )
    patch = monkeypatch or (_MONKEYPATCH[0] if _MONKEYPATCH else None)
    if patch is not None:
        patch.setenv(KEY_DIR_ENV, str(key_dir))
    found = authorizer(key_dir)
    verification = ta.verify(statement, key_dir=key_dir, root=found.root)
    return RepairStore(store_root).keep_teach_authorization(verification, statement)


ORDER_ADAPTER = "openadapt_flow.agent.example_app:OrderWrites"


def order_spec(
    base_url: str,
    values: Mapping[str, str],
    *,
    name: str = "add_order",
    identity: Sequence[str] = ("customer",),
    record_kind: str = "order",
    write_adapter: Optional[str] = ORDER_ADAPTER,
    expected_rows: int = 1,
) -> dict[str, Any]:
    """What a person signs to let an agent teach the order write ``name``."""

    from openadapt_flow.teach_scope import origin_of

    writes: dict[str, Any] = {
        "record_kind": record_kind,
        "identity": {k: {"value": values[k]} for k in identity},
        "fields": {k: {"value": v} for k, v in values.items() if k not in identity},
        "allowed_write_paths": ["/login"],
        "expected_rows": expected_rows,
    }
    if write_adapter is not None:
        writes["write_adapter"] = write_adapter
    return {
        "task": {"task_id": f"taught.{name}", "summary": "Add one order"},
        "purposes": ["teach"],
        "origins": [str(origin_of(base_url))],
        "inputs": {k: {"value": v} for k, v in values.items()},
        "writes": writes,
    }


def order_authority(
    folder: Path,
    base_url: str,
    values: Mapping[str, str],
    *,
    signer: Optional[LocalAuthorizer] = None,
    **kw: Any,
) -> ta.TeachAuthority:
    """A real approver on a real roster signs :func:`order_spec`; pins it."""

    folder = Path(folder)
    person = signer or LocalAuthorizer(folder / "approvers", "clinic-lead")
    admission_key(person.key_dir)
    statement = person.issue(folder / "statement", order_spec(base_url, values, **kw))
    return person.authority(statement)


def admission_key(key_dir: Path) -> None:
    """The local admission key an operator sets up in the key directory."""

    from openadapt_flow.execute.keys import load_or_create_private_key

    load_or_create_private_key(Path(key_dir))
