"""Compositional field-set contract synthesis + planted-fault admission tests (P6).

Proves that ``verify_contract.choose()`` can synthesize a contract for a novel
combination of already-demonstrated fields from admitted manifest variants
(even on a zero-touch manifest with ``field_sets`` stripped), while gating
every composed contract through planted-fault admission and failing closed on
undemonstrated fields, conflicting placements, or weakened checks.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml

from openadapt_flow.generate import verify_contract as gen
from openadapt_flow.generate.compositional import (
    admit_composed_contract,
    compose_field_set_docs,
)
from openadapt_flow.verifier import (
    Actor,
    MemoryLedger,
    Outcome,
    begin_write,
    verify_write,
)

BOT = Actor(kind="agent", id="bot")


@pytest.fixture(autouse=True)
def _ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENADAPT_LEDGER_DIR", str(tmp_path / "ledger"))
    monkeypatch.setenv("OPENADAPT_SEAL_HOME", str(tmp_path / "seal"))


def _create_db(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "app.sqlite"
    con = sqlite3.connect(db)
    con.executescript(
        "CREATE TABLE item ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL,"
        "  notes TEXT,"
        "  category TEXT,"
        "  status TEXT NOT NULL DEFAULT 'Open',"
        "  updated_at TEXT NOT NULL DEFAULT '2026-01-01'"
        ");"
        "INSERT INTO item (name, notes, category, status, updated_at) "
        "VALUES ('Existing', 'keep', 'Tools', 'Open', '2026-01-01');"
        "INSERT INTO item (name, notes, category, status, updated_at) "
        "VALUES ('Sibling', 'untouched', 'Parts', 'Open', '2026-01-01');"
    )
    con.commit()
    con.close()
    return db


def _write_yaml(path: Path, doc: dict[str, Any]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    return str(path)


def _record_contract(
    db: Path,
    *,
    select_cols: list[str],
    match: dict[str, Any],
    count_new_only: bool,
    target_fields: list[str] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": "openadapt.verify/v1",
        "verifier": {
            "kind": "surfaces",
            "surfaces": {
                "record": {
                    "kind": "sql",
                    "sqlite_database": str(db),
                    "sql_query": f"SELECT {', '.join(select_cols)} FROM item t",
                }
            },
        },
        "effects": [
            {
                "kind": "record_written",
                "probe": "surface=record",
                "match": match,
                "expected_count": 1,
                "count_new_only": count_new_only,
            }
        ],
        "frame": {
            "identity_field": "id",
            "target_fields": target_fields if target_fields is not None else [],
            "target_fields_by_surface": (
                {"record": target_fields} if target_fields else {}
            ),
        },
    }
    return doc


def _build_create_manifest(tmp_path: Path, db: Path) -> Path:
    """Zero-touch-style manifest (no ``field_sets``) with two admitted create variants:
    - Variant A: [name, notes] (category pinned empty)
    - Variant B: [name, category] (notes pinned empty)
    """
    out = tmp_path / "contracts"
    base_cols = [
        "t.id AS id",
        "t.name AS name",
        "t.notes AS notes",
        "t.category AS category",
        "t.status AS status",
    ]
    empty_cat = "CASE WHEN t.category IS NULL OR t.category = '' THEN '1' ELSE '0' END AS empty__category"
    empty_notes = "CASE WHEN t.notes IS NULL OR t.notes = '' THEN '1' ELSE '0' END AS empty__notes"

    var_a_create = _record_contract(
        db,
        select_cols=[*base_cols, empty_cat],
        match={
            "name": {"param": "name"},
            "notes": {"param": "notes"},
            "empty__category": {"literal": "1"},
            "status": {"literal": "Open"},
        },
        count_new_only=True,
    )
    var_a_found = _record_contract(
        db,
        select_cols=[*base_cols, empty_cat],
        match={
            "name": {"param": "name"},
            "notes": {"param": "notes"},
            "empty__category": {"literal": "1"},
            "status": {"literal": "Open"},
        },
        count_new_only=False,
    )
    var_b_create = _record_contract(
        db,
        select_cols=[*base_cols, empty_notes],
        match={
            "name": {"param": "name"},
            "category": {"param": "category"},
            "empty__notes": {"literal": "1"},
            "status": {"literal": "Open"},
        },
        count_new_only=True,
    )
    var_b_found = _record_contract(
        db,
        select_cols=[*base_cols, empty_notes],
        match={
            "name": {"param": "name"},
            "category": {"param": "category"},
            "empty__notes": {"literal": "1"},
            "status": {"literal": "Open"},
        },
        count_new_only=False,
    )

    _write_yaml(out / "create_item" / "a.create.yaml", var_a_create)
    _write_yaml(out / "create_item" / "a.found.yaml", var_a_found)
    _write_yaml(out / "create_item" / "b.create.yaml", var_b_create)
    _write_yaml(out / "create_item" / "b.found.yaml", var_b_found)

    manifest = {
        "schema_version": gen.MANIFEST_SCHEMA,
        "zero_touch": {"admitted": ["create_item"]},
        "kinds": {
            "create_item": {
                "mode": "create",
                "fields": {
                    "required": ["name"],
                    "optional": ["notes", "category"],
                    "lists": [],
                },
                "variants": [
                    {
                        "fields": ["name", "notes"],
                        "create": "create_item/a.create.yaml",
                        "found": "create_item/a.found.yaml",
                    },
                    {
                        "fields": ["category", "name"],
                        "create": "create_item/b.create.yaml",
                        "found": "create_item/b.found.yaml",
                    },
                ],
            }
        },
    }
    manifest_path = out / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    return manifest_path


def _build_update_manifest(tmp_path: Path, db: Path) -> Path:
    """Zero-touch-style manifest with two admitted update variants:
    - Variant A: [id, status] (target_fields: [status, updated_at])
    - Variant B: [id, notes] (target_fields: [notes, updated_at])
    """
    out = tmp_path / "contracts_upd"
    base_cols = [
        "t.id AS id",
        "t.name AS name",
        "t.notes AS notes",
        "t.category AS category",
        "t.status AS status",
        "t.updated_at AS updated_at",
    ]
    var_a_upd = _record_contract(
        db,
        select_cols=base_cols,
        match={"id": {"param": "id"}, "status": {"param": "status"}},
        count_new_only=False,
        target_fields=["status", "updated_at"],
    )
    var_a_found = _record_contract(
        db,
        select_cols=base_cols,
        match={"id": {"param": "id"}, "status": {"param": "status"}},
        count_new_only=False,
        target_fields=[],
    )
    var_b_upd = _record_contract(
        db,
        select_cols=base_cols,
        match={"id": {"param": "id"}, "notes": {"param": "notes"}},
        count_new_only=False,
        target_fields=["notes", "updated_at"],
    )
    var_b_found = _record_contract(
        db,
        select_cols=base_cols,
        match={"id": {"param": "id"}, "notes": {"param": "notes"}},
        count_new_only=False,
        target_fields=[],
    )

    _write_yaml(out / "update_item" / "a.update.yaml", var_a_upd)
    _write_yaml(out / "update_item" / "a.found.yaml", var_a_found)
    _write_yaml(out / "update_item" / "b.update.yaml", var_b_upd)
    _write_yaml(out / "update_item" / "b.found.yaml", var_b_found)

    manifest = {
        "schema_version": gen.MANIFEST_SCHEMA,
        "zero_touch": {"admitted": ["update_item"]},
        "kinds": {
            "update_item": {
                "mode": "update",
                "fields": {
                    "identity": ["id"],
                    "changes": ["status", "notes"],
                    "lists": [],
                },
                "variants": [
                    {
                        "fields": ["id", "status"],
                        "update": "update_item/a.update.yaml",
                        "found": "update_item/a.found.yaml",
                    },
                    {
                        "fields": ["id", "notes"],
                        "update": "update_item/b.update.yaml",
                        "found": "update_item/b.found.yaml",
                    },
                ],
            }
        },
    }
    manifest_path = out / "manifest.yaml"
    manifest_path.write_text(
        yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8"
    )
    return manifest_path


def test_compositional_create_union_confirms_and_catches_planted_faults(
    tmp_path: Path,
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_create_manifest(tmp_path, db)
    values = {"name": "Bolt", "notes": "M4", "category": "Hardware"}

    contract, params = gen.choose(manifest, "create_item", values)
    assert set(contract.params) == {"name", "notes", "category"}

    # Correct write confirms.
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO item (name, notes, category, status) VALUES (?, ?, ?, ?)",
        ("Bolt", "M4", "Hardware", "Open"),
    )
    con.commit()
    con.close()
    res = verify_write(pending)
    assert res.outcome is Outcome.CONFIRMED, res.reasons

    # Already-there write chooses composed ``found`` contract and confirms with no-op.
    found_contract, found_params = gen.choose(manifest, "create_item", values)
    pending_found = begin_write(
        found_contract, found_params, actor=BOT, ledger=MemoryLedger()
    )
    res_found = verify_write(pending_found)
    assert res_found.outcome is Outcome.CONFIRMED, res_found.reasons


@pytest.mark.parametrize(
    "bad_row",
    [
        ("Bolt", "M5", "Hardware", "Open"),  # wrong notes
        ("Bolt", "M4", "Software", "Open"),  # wrong category
        ("Bolt", None, "Hardware", "Open"),  # null where value expected
        ("Bolt", "M4", "Hardware", "Closed"),  # violates invariant pin
    ],
)
def test_compositional_create_union_refutes_planted_value_faults(
    tmp_path: Path, bad_row: tuple[Any, ...]
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_create_manifest(tmp_path, db)
    values = {"name": "Bolt", "notes": "M4", "category": "Hardware"}

    contract, params = gen.choose(manifest, "create_item", values)
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO item (name, notes, category, status) VALUES (?, ?, ?, ?)",
        bad_row,
    )
    con.commit()
    con.close()
    res = verify_write(pending)
    assert res.outcome is Outcome.REFUTED, res.reasons


def test_compositional_create_union_refutes_duplicate_and_sibling_edit(
    tmp_path: Path,
) -> None:
    # Duplicate row fault
    db = _create_db(tmp_path / "dup")
    manifest = _build_create_manifest(tmp_path / "dup", db)
    values = {"name": "Bolt", "notes": "M4", "category": "Hardware"}
    contract, params = gen.choose(manifest, "create_item", values)
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    for _ in range(2):
        con.execute(
            "INSERT INTO item (name, notes, category, status) VALUES (?, ?, ?, ?)",
            ("Bolt", "M4", "Hardware", "Open"),
        )
    con.commit()
    con.close()
    res_dup = verify_write(pending)
    assert res_dup.outcome is Outcome.REFUTED, res_dup.reasons

    # Sibling edit fault
    db2 = _create_db(tmp_path / "sib")
    manifest2 = _build_create_manifest(tmp_path / "sib", db2)
    contract2, params2 = gen.choose(manifest2, "create_item", values)
    pending2 = begin_write(contract2, params2, actor=BOT, ledger=MemoryLedger())
    con2 = sqlite3.connect(db2)
    con2.execute(
        "INSERT INTO item (name, notes, category, status) VALUES (?, ?, ?, ?)",
        ("Bolt", "M4", "Hardware", "Open"),
    )
    con2.execute("UPDATE item SET notes = 'tampered' WHERE id = 2")
    con2.commit()
    con2.close()
    res_sib = verify_write(pending2)
    assert res_sib.outcome is Outcome.REFUTED, res_sib.reasons


def test_compositional_create_subset_pins_both_omitted_optional_fields(
    tmp_path: Path,
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_create_manifest(tmp_path, db)
    values = {"name": "Washer"}

    contract, params = gen.choose(manifest, "create_item", values)
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO item (name, notes, category, status) VALUES (?, ?, ?, ?)",
        ("Washer", None, None, "Open"),
    )
    con.commit()
    con.close()
    res = verify_write(pending)
    assert res.outcome is Outcome.CONFIRMED, res.reasons

    # Planted fault: writing a stray omitted field must refute.
    db2 = _create_db(tmp_path / "stray")
    manifest2 = _build_create_manifest(tmp_path / "stray", db2)
    contract2, params2 = gen.choose(manifest2, "create_item", values)
    pending2 = begin_write(contract2, params2, actor=BOT, ledger=MemoryLedger())
    con2 = sqlite3.connect(db2)
    con2.execute(
        "INSERT INTO item (name, notes, category, status) VALUES (?, ?, ?, ?)",
        ("Washer", "stray-note", None, "Open"),
    )
    con2.commit()
    con2.close()
    res2 = verify_write(pending2)
    assert res2.outcome is Outcome.REFUTED, res2.reasons


def test_compositional_update_union_confirms_and_protects_untouched_columns(
    tmp_path: Path,
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_update_manifest(tmp_path, db)
    values = {"id": "1", "status": "Closed", "notes": "Done"}

    contract, params = gen.choose(manifest, "update_item", values)
    pending = begin_write(contract, params, actor=BOT, ledger=MemoryLedger())
    con = sqlite3.connect(db)
    con.execute(
        "UPDATE item SET status = 'Closed', notes = 'Done', updated_at = '2026-01-02' WHERE id = 1"
    )
    con.commit()
    con.close()
    res = verify_write(pending)
    assert res.outcome is Outcome.CONFIRMED, res.reasons

    # Planted fault: mutating an untouched column (category) on the target must refute.
    db2 = _create_db(tmp_path / "untouched")
    manifest2 = _build_update_manifest(tmp_path / "untouched", db2)
    contract2, params2 = gen.choose(manifest2, "update_item", values)
    pending2 = begin_write(contract2, params2, actor=BOT, ledger=MemoryLedger())
    con2 = sqlite3.connect(db2)
    con2.execute(
        "UPDATE item SET status = 'Closed', notes = 'Done', category = 'Hacked' WHERE id = 1"
    )
    con2.commit()
    con2.close()
    res2 = verify_write(pending2)
    assert res2.outcome is Outcome.REFUTED, res2.reasons


def test_compositional_refuses_undemonstrated_or_missing_required_fields(
    tmp_path: Path,
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_create_manifest(tmp_path, db)

    # Undemonstrated field
    with pytest.raises(gen.GenerateError, match="no create_item contract"):
        gen.choose(manifest, "create_item", {"name": "Bolt", "priority": "High"})

    # Missing required field
    with pytest.raises(gen.GenerateError, match="no create_item contract"):
        gen.choose(manifest, "create_item", {"notes": "M4", "category": "Hardware"})


def test_compositional_refuses_conflicting_placements_across_variants(
    tmp_path: Path,
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_create_manifest(tmp_path, db)
    # Tamper variant B so ``name`` maps to a different column expression than variant A.
    b_path = manifest.parent / "create_item" / "b.create.yaml"
    b_doc = yaml.safe_load(b_path.read_text(encoding="utf-8"))
    b_doc["effects"][0]["match"]["name"] = {"param": "name", "stored": "upper"}
    b_path.write_text(yaml.safe_dump(b_doc, sort_keys=False), encoding="utf-8")

    with pytest.raises(gen.GenerateError, match="no create_item contract"):
        gen.choose(
            manifest,
            "create_item",
            {"name": "Bolt", "notes": "M4", "category": "Hardware"},
        )


def test_admit_composed_contract_rejects_weakened_or_unsafe_contracts(
    tmp_path: Path,
) -> None:
    db = _create_db(tmp_path)
    manifest = _build_create_manifest(tmp_path, db)
    m_doc = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    entry = m_doc["kinds"]["create_item"]
    docs = compose_field_set_docs(
        manifest, entry, {"name", "notes", "category"}, {}, ()
    )
    assert docs is not None
    write_doc, found_doc = docs

    # Clean composed contract passes admission with all planted faults stopped.
    ok = admit_composed_contract(
        write_doc,
        found_doc,
        mode="create",
        given={"name", "notes", "category"},
        params={"name": "Bolt", "notes": "M4", "category": "Hardware"},
    )
    assert ok.admitted, ok.reasons
    assert ok.faults_checked >= 8
    assert ok.faults_stopped == ok.faults_checked

    # Dropping a field from match fails admission.
    dropped = yaml.safe_load(yaml.safe_dump(write_doc))
    dropped["effects"][0]["match"].pop("notes")
    bad_drop = admit_composed_contract(
        dropped,
        found_doc,
        mode="create",
        given={"name", "notes", "category"},
        params={"name": "Bolt", "notes": "M4", "category": "Hardware"},
    )
    assert not bad_drop.admitted
    assert any("dropped field" in r for r in bad_drop.reasons)

    # Mutating SQL query fails admission via SQL guard.
    unsafe_sql = yaml.safe_load(yaml.safe_dump(write_doc))
    unsafe_sql["verifier"]["surfaces"]["record"]["sql_query"] = "DELETE FROM item"
    bad_sql = admit_composed_contract(
        unsafe_sql,
        found_doc,
        mode="create",
        given={"name", "notes", "category"},
        params={"name": "Bolt", "notes": "M4", "category": "Hardware"},
    )
    assert not bad_sql.admitted
    assert any("SQL guard" in r for r in bad_sql.reasons)
