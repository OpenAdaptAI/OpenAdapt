"""Source/SQLite qualification of a NEW case; no native Windows claims."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_flow.runtime.effects.desktop_readback import DesktopReadbackSpec

HERE = Path(__file__).parent / "fixtures" / "native_record_identity_v2"
spec = importlib.util.spec_from_file_location(
    "identity_fixture_builder", HERE / "build_fixture.py"
)
assert spec and spec.loader
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)
FLOW = HERE.parents[2]
DB_CLI = FLOW / "scripts" / "desktop" / "pn_db.py"


def test_generated_source_and_pack_are_reproducible() -> None:
    for name, data in builder.outputs().items():
        assert (HERE / name).read_bytes() == data
    contract = builder.load_contract()
    base = builder.canonical_source(FLOW / contract["base_source"]["path"])
    generated = (HERE / "patient_notes_v2.ps1").read_text()
    assert builder.save_handler(base.decode()) == builder.save_handler(generated)
    assert "currentRecordId" not in base.decode()
    assert contract["qualification"]["native_value_proof"] is None
    assert contract["qualification"]["database_to_control_binding_proof"] is None
    assert contract["qualification"]["combined_campaign"] is None


@pytest.mark.parametrize("fault", ["base", "anchor", "save"])
def test_source_drift_and_save_changes_are_refused(fault: str) -> None:
    contract = builder.load_contract()
    base = builder.canonical_source(FLOW / contract["base_source"]["path"])
    override = json.loads((HERE / "override.json").read_text())
    if fault == "base":
        base += b"\n# drift\n"
    elif fault == "anchor":
        override["patches"][0]["before"] = "missing source anchor"
    else:
        override["patches"].append(
            {"before": '"Saved note for patient $id"', "after": '"changed"'}
        )
    with pytest.raises(ValueError):
        builder.build(base, override)


@pytest.mark.parametrize(
    "fault", ["table", "key", "value", "loaded_field", "pack_identity"]
)
def test_actual_lookup_and_save_sql_reject_planted_pack_mapping(fault: str) -> None:
    contract = builder.load_contract()
    if fault in {"table", "key", "value"}:
        contract["database_mapping"][fault] = "another_column_or_table"
    elif fault == "loaded_field":
        contract["identity_binding"][fault] = "first"
    else:
        contract["readback_template"]["identity"]["field"] = "first"
    with pytest.raises(ValueError):
        builder.validate_source_mapping(builder.canonical_source(DB_CLI), contract)


def test_pack_names_complete_value_identity_without_virtual_grid_claims() -> None:
    # Validation only. This never creates a backend or opens a session.
    spec = DesktopReadbackSpec.model_validate(
        builder.readback_spec({"kind": "windows", "agent_url": "http://127.0.0.1:1"})
    )
    assert spec.open is None
    assert spec.ready.automation_id == "currentRecordId"
    assert spec.identity.reader.automation_id == "currentRecordId"
    assert spec.identity.field == "id"
    assert spec.identity.value.param == "record_id"
    assert spec.fields["note"].control.automation_id == "noteBox"
    requirements = builder.load_contract()["read_requirements"]
    assert requirements["require_editable"] is False
    assert requirements["value_contract"] == "openadapt.native-value-completeness/v2"
    assert not requirements["runtime_protocol_qualified"]


@pytest.fixture
def database(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    path = tmp_path / "patients.db"
    env = {"PN_DB": str(path), "PATH": os.defpath}
    # Windows subprocesses may require these non-secret OS paths. Never pass
    # the host environment wholesale or return credentials in fixture reprs.
    for name in ("SystemRoot", "WINDIR", "TEMP", "TMP", "PATHEXT"):
        if os.getenv(name) is not None:
            env[name] = os.environ[name]
    subprocess.run(
        [sys.executable, str(DB_CLI), "seed", "--drift", "siblings"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    with sqlite3.connect(path) as con:
        con.executemany(
            "INSERT INTO patients VALUES (?, 'Synthetic', 'Boundary', '2000-01-01', '')",
            [(-(2**63),), (0,), (2**63 - 1,)],
        )
        con.execute("UPDATE patients SET note = 'seed-note-' || id")
    return path, env


def test_real_cli_json_ids_match_independent_sqlite_rows(database: tuple) -> None:
    path, env = database
    mapping = builder.load_contract()["database_mapping"]
    with sqlite3.connect(path) as con:
        builder.validate_database(con, mapping)
        rows = con.execute(
            "SELECT id, first, last, dob, note FROM patients ORDER BY id"
        ).fetchall()
    for row in rows:
        result = subprocess.run(
            [sys.executable, str(DB_CLI), "get", str(row[0])],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        assert json.loads(result.stdout) == dict(
            zip(("id", "first", "last", "dob", "note"), row)
        )


@pytest.mark.parametrize("fault", ["table", "key", "value", "key_type", "value_type"])
def test_planted_mapping_faults_are_refused(database: tuple, fault: str) -> None:
    path, _ = database
    mapping = copy.deepcopy(builder.load_contract()["database_mapping"])
    mapping[fault] = {
        "table": "missing",
        "key": "first",
        "value": "id",
        "key_type": "TEXT",
        "value_type": "INTEGER",
    }[fault]
    with sqlite3.connect(path) as con, pytest.raises(ValueError):
        builder.validate_database(con, mapping)


@pytest.mark.parametrize(
    "schema",
    [
        "id TEXT PRIMARY KEY, note TEXT",
        "id INTEGER, note TEXT",
        "id INTEGER, other INTEGER, note TEXT, PRIMARY KEY(id, other)",
    ],
)
def test_non_integer_or_non_unique_identity_schema_is_refused(schema: str) -> None:
    with sqlite3.connect(":memory:") as con:
        con.execute(f"CREATE TABLE patients ({schema})")
        with pytest.raises(ValueError):
            builder.validate_database(con, builder.load_contract()["database_mapping"])


def test_actual_generated_powershell_lifecycle_without_gui(
    database: tuple, tmp_path: Path
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell unavailable; native qualification is still unperformed")
    path, env = database
    fail_cli = tmp_path / "fail_cli.py"
    fail_cli.write_text("raise SystemExit(3)\n")
    with sqlite3.connect(path) as con:
        before = con.execute("SELECT * FROM patients ORDER BY id").fetchall()
    result = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(HERE / "check_lifecycle.ps1"),
            "-Fixture",
            str(HERE / "patient_notes_v2.ps1"),
            "-Python",
            sys.executable,
            "-DbCli",
            str(DB_CLI),
            "-FailCli",
            str(fail_cli),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["scope"] == "offline-generated-functions-fake-controls-real-sqlite"
    assert report["native_validation"] is False
    assert len(report["cases"]) == report["count"] == 26
    with sqlite3.connect(path) as con:
        assert con.execute("SELECT * FROM patients ORDER BY id").fetchall() == before


def test_lifecycle_checker_detects_omitted_note_clear(
    database: tuple, tmp_path: Path
) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell unavailable; checker-negative cannot execute")
    _, env = database
    source = (HERE / "patient_notes_v2.ps1").read_text()
    assert source.count('    $noteBox.Text = ""\n') == 1
    mutant = tmp_path / "planted_missing_note_clear.ps1"
    mutant.write_text(source.replace('    $noteBox.Text = ""\n', ""))
    fail_cli = tmp_path / "fail_cli.py"
    fail_cli.write_text("raise SystemExit(3)\n")
    result = subprocess.run(
        [
            pwsh,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(HERE / "check_lifecycle.ps1"),
            "-Fixture",
            str(mutant),
            "-Python",
            sys.executable,
            "-DbCli",
            str(DB_CLI),
            "-FailCli",
            str(fail_cli),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode != 0
    assert "Stale identity/value in selection-reset" in result.stderr
