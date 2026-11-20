"""Build a NEW synthetic fixture and pack from a pinned, unchanged base.

This is repository-only test support. It does not qualify a native runtime.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[2]


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_contract() -> dict[str, Any]:
    return json.loads((HERE / "contract.json").read_text())


def canonical_source(path: Path) -> bytes:
    """Windows checkout CRLF is explicit; no other byte normalization."""
    return path.read_bytes().replace(b"\r\n", b"\n")


def save_handler(source: str) -> str:
    return source.split("$saveButton.Add_Click({", 1)[1].split("\n})", 1)[0]


def build(base: bytes, override: dict[str, Any]) -> bytes:
    contract = load_contract()
    if sha256(base) != contract["base_source"]["sha256"]:
        raise ValueError("base source differs from the pinned fixture")
    if override["schema"] != "openadapt.synthetic-source-override/v1":
        raise ValueError("unsupported source override schema")
    text = base.decode("utf-8")
    original_save = save_handler(text)
    for patch in override["patches"]:
        if text.count(patch["before"]) != 1:
            raise ValueError("source override must match exactly once")
        text = text.replace(patch["before"], patch["after"], 1)
    if save_handler(text) != original_save:
        raise ValueError("NEW fixture changed the original save handler")
    return text.encode("utf-8")


def readback_spec(session: dict[str, Any]) -> dict[str, Any]:
    """Supply the caller's owned session; the data pack stores no endpoint."""
    return {"session": session, **load_contract()["readback_template"]}


def validate_database(con: sqlite3.Connection, mapping: dict[str, str]) -> None:
    """Check the declared identity/value mapping against an actual SQLite DB."""
    table = mapping["table"]
    if not isinstance(table, str) or not table:
        raise ValueError("missing table")
    kind = con.execute(
        "SELECT type FROM sqlite_master WHERE name = ?", (table,)
    ).fetchone()
    if kind != ("table",):
        raise ValueError("mapping must name an actual table")
    quoted_table = '"' + table.replace('"', '""') + '"'
    columns = {row[1]: row for row in con.execute(f"PRAGMA table_info({quoted_table})")}
    key = columns.get(mapping["key"])
    value = columns.get(mapping["value"])
    if (
        key is None
        or key[2].upper() != mapping["key_type"]
        or key[2].upper() != "INTEGER"
        or key[5] != 1
        or sum(bool(row[5]) for row in columns.values()) != 1
    ):
        raise ValueError("identity must be the sole INTEGER primary key")
    if value is None or value[2].upper() != mapping["value_type"] or value[2] != "TEXT":
        raise ValueError("note mapping must name a TEXT column")
    if mapping["key"] == mapping["value"]:
        raise ValueError("identity and note mapping must differ")


def validate_source_mapping(source: bytes, contract: dict[str, Any]) -> None:
    """Bind pack data to the pinned CLI's actual lookup and write statements."""
    mapping = contract["database_mapping"]
    table, key, value = (mapping[name] for name in ("table", "key", "value"))
    expected = {
        "get": f"SELECT * FROM {table} WHERE {key} = ?",
        "save": f"UPDATE {table} SET {value} = ? WHERE {key} = ?",
    }
    tree = ast.parse(source)
    for name, sql in expected.items():
        functions = [
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == name
        ]
        if len(functions) != 1:
            raise ValueError("database operation is missing or ambiguous")
        statements = [
            node.args[0].value
            for node in ast.walk(functions[0])
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute"
            and node.args
            and isinstance(node.args[0], ast.Constant)
        ]
        if statements != [sql]:
            raise ValueError("pack mapping differs from actual CLI lookup/write SQL")
    binding = contract["identity_binding"]
    if any(
        binding[name] != key
        for name in ("selected_cell", "loaded_field", "save_target_cell")
    ):
        raise ValueError("selection, load and save mapping must name the same key")
    template = contract["readback_template"]
    if template["identity"]["field"] != key or set(template["fields"]) != {value}:
        raise ValueError("readback pack differs from the database mapping")


def outputs() -> dict[str, bytes]:
    contract = load_contract()
    base = canonical_source(FLOW / contract["base_source"]["path"])
    database = canonical_source(FLOW / contract["database_source"]["path"])
    if sha256(database) != contract["database_source"]["sha256"]:
        raise ValueError("database CLI differs from the pinned source")
    validate_source_mapping(database, contract)
    override_bytes = (HERE / "override.json").read_bytes()
    fixture = build(base, json.loads(override_bytes))
    pack = (json.dumps(contract["readback_template"], indent=2) + "\n").encode()
    manifest = {
        "case_id": contract["case_id"],
        "base_sha256": sha256(base),
        "database_source_sha256": sha256(database),
        "override_sha256": sha256(override_bytes),
        "fixture_sha256": sha256(fixture),
        "readback_template_sha256": sha256(pack),
        "unchanged_save_handler_sha256": sha256(save_handler(base.decode()).encode()),
        "native_validation_performed": False,
    }
    return {
        "patient_notes_v2.ps1": fixture,
        "readback.template.json": pack,
        "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    for name, data in outputs().items():
        path = HERE / name
        if args.check:
            if not path.is_file() or path.read_bytes() != data:
                raise SystemExit(f"generated file differs: {name}")
        else:
            path.write_bytes(data)


if __name__ == "__main__":
    main()
