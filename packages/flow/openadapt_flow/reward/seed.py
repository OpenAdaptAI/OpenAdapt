"""Synthetic MockMed reward fixtures for ``serve-reward --seed-mockmed``.

Two bundles and two stores:

* ``contracts/mockmed`` reads ``mockmed/records.db`` through the ``sqlite``
  recipe (channel ``db``, tier 2) and carries a self-signed certificate
  issued at policy update 0. The database starts with two rows for the
  duplicate patient and nothing for anybody else, so an episode has to write
  something before it can be verified.
* ``contracts/mockmed-tier0`` reads ``mockmed/screen.json`` through the
  ``screen_dump`` recipe (channel ``ocr``, tier 0). Its receipts are
  ``development_only`` and never certified, whatever the dump says.

Read the two together and the fixture makes its point: write the banner into
the screen dump and nothing into the database, and the tier-0 worker says
``verified`` while the tier-2 worker says ``wrong_effect`` about the same
episode.

The tier-2 store is a SQLite database rather than a JSON document because the
tier has to rest on something the worker can check. Both JSON recipe kinds
build the same reader over the same kind of bytes, so neither can claim a
record channel; ``openadapt_flow.reward.oracles.assert_sqlite_database``
refuses a ``sqlite`` recipe whose file is not a real database.

Nothing here is a production recipe. Both bundles are synthetic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openadapt_flow.reward.bundle import (
    SYNTHETIC_ENVIRONMENT_ID,
    SYNTHETIC_TASK_ID,
    _write,
    write_bundle,
)

MOCKMED_TASK_ID = SYNTHETIC_TASK_ID
MOCKMED_ENVIRONMENT_ID = SYNTHETIC_ENVIRONMENT_ID
MOCKMED_CONTRACT_ID = "reward_contract_mockmed"
MOCKMED_TIER0_CONTRACT_ID = "reward_contract_mockmed_tier0"
MOCKMED_HONEST_PATIENT = "patient-honest-0001"
MOCKMED_LIE_PATIENT = "patient-lie-0002"
MOCKMED_DUPLICATE_PATIENT = "patient-dup-0003"
MOCKMED_CHECKPOINT = "policy_checkpoint_mockmed_0"

#: The tier-2 store's one table and the read-only SELECT the oracle runs.
MOCKMED_TABLE = "encounters"
MOCKMED_QUERY = f"SELECT id, patient_id, type, status FROM {MOCKMED_TABLE}"

#: What the database holds before any episode runs. The honest patient and
#: the banner-lie patient have no row: a required effect now asserts a change,
#: so a row that was already there earns nothing.
_ROWS: list[dict[str, Any]] = [
    {"id": 2, "patient_id": MOCKMED_DUPLICATE_PATIENT, "type": "Triage"},
    {"id": 3, "patient_id": MOCKMED_DUPLICATE_PATIENT, "type": "Triage"},
]

#: What the screen shows before any episode runs.
_SCREEN: list[dict[str, Any]] = [
    {"id": 2, "patient_id": MOCKMED_DUPLICATE_PATIENT, "type": "Triage"},
]


def seed_mockmed(
    data_dir: Path, key: Ed25519PrivateKey, issuer_key_id: str
) -> dict[str, Path]:
    """Write both bundles and both stores. Returns the bundle paths."""

    data_dir = Path(data_dir)
    store = data_dir / "mockmed"
    store.mkdir(parents=True, exist_ok=True)
    database = store / "records.db"
    write_mockmed_database(database, _ROWS)
    _write(store / "screen.json", {"records": _SCREEN})

    tier2 = data_dir / "contracts" / "mockmed"
    write_bundle(
        tier2,
        contract_id=MOCKMED_CONTRACT_ID,
        oracle={
            "kind": "sqlite",
            "path": str(database),
            "query": MOCKMED_QUERY,
        },
        channel="db",
        key=key,
        issuer_key_id=issuer_key_id,
        certify=True,
    )
    tier0 = data_dir / "contracts" / "mockmed-tier0"
    write_bundle(
        tier0,
        contract_id=MOCKMED_TIER0_CONTRACT_ID,
        oracle={
            "kind": "screen_dump",
            "path": str(store / "screen.json"),
            "records_key": "records",
        },
        channel="ocr",
        key=key,
        issuer_key_id=issuer_key_id,
        certify=False,
    )
    return {"tier2": tier2, "tier0": tier0}


def write_mockmed_database(path: Path, rows: list[dict[str, Any]]) -> Path:
    """Create the synthetic tier-2 store as a real SQLite database."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            f"CREATE TABLE {MOCKMED_TABLE} ("
            "id INTEGER PRIMARY KEY, patient_id TEXT NOT NULL, "
            "type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'saved')"
        )
        connection.executemany(
            f"INSERT INTO {MOCKMED_TABLE} (id, patient_id, type, status) "
            "VALUES (:id, :patient_id, :type, :status)",
            [{"status": "saved", **row} for row in rows],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def write_mockmed_encounter(
    database: Path, patient_id: str, *, type_: str = "Triage"
) -> None:
    """Add one encounter row, standing in for what an episode would write."""

    connection = sqlite3.connect(database)
    try:
        connection.execute(
            f"INSERT INTO {MOCKMED_TABLE} (patient_id, type, status) "
            "VALUES (?, ?, 'saved')",
            (patient_id, type_),
        )
        connection.commit()
    finally:
        connection.close()


def write_mockmed_banner(screen: Path, patient_id: str) -> None:
    """Add one row to the screen dump, standing in for a save banner."""

    body = json.loads(screen.read_text(encoding="utf-8"))
    records = list(body.get("records") or [])
    records.append(
        {
            "id": 100 + len(records),
            "patient_id": patient_id,
            "type": "Triage",
            "status": "saved",
        }
    )
    _write(screen, {"records": records})


def mockmed_episode(
    patient_id: str,
    *,
    episode_id: str,
    contract_digest: str,
    policy_update: int = 0,
    runtime_signal: str = "completed",
) -> dict[str, Any]:
    """The wire payload for one seeded episode, in the trainer client's shape."""

    return {
        "episode_id": episode_id,
        "policy_checkpoint_id": MOCKMED_CHECKPOINT,
        "policy_update": policy_update,
        "reward_contract_digest": contract_digest,
        "task_id": MOCKMED_TASK_ID,
        "environment_id": MOCKMED_ENVIRONMENT_ID,
        "metadata": {
            "oracle_identity": {"patient_id": patient_id},
            "runtime_signal": runtime_signal,
        },
    }
