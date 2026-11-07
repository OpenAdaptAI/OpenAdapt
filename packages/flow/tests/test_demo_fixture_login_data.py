"""Source-bound disposable login DATA; no app, browser, provider or model."""

from __future__ import annotations

import json
import sqlite3

import pytest
from openadapt_flow.integrations.agents import demo_fixture as fixture
from openadapt_flow.jit.signature import TaskSignature


def test_disposable_login_is_pack_data_not_a_new_parameter_or_callback():
    spec, rows = fixture.load_pack(fixture.packaged_pack("kanboard"))
    assert spec["constants"] == ["admin"]
    assert spec["source_refs"]["login"] == "https://docs.kanboard.org/v1/admin/installation/"
    assert spec["source_refs"]["login_entry"]["path"] == "/"
    assert spec["source_refs"]["login_entry"]["status"] == "DOCUMENTED_DEFAULTS_UI_FLOW_UNMEASURED"
    assert spec["parameter_columns"] == ["title", "description"]
    assert len(rows) == 20
    assert all(set(row["parameters"]) == {"title", "description"} for row in rows)
    assert "username admin and password admin" in spec["instruction"]
    assert "{title}" in spec["instruction"] and "{description}" in spec["instruction"]
    assert not {"page_setup", "setup_factory", "function", "approved", "admission"}.intersection(spec)


@pytest.mark.parametrize("has_constants", [True, False], ids=["declared", "legacy-absent"])
def test_actual_native_generation_forwards_only_declared_fixed_text(tmp_path, has_constants):
    spec, rows = fixture.load_pack(fixture.packaged_pack("kanboard"))
    if not has_constants:
        spec.pop("constants")
        spec["instruction"] = "Create exactly one task with title {title} and description {description}."
    files = fixture.prepare(spec, tmp_path / "owned", project="oawrapdemo-login-unit", port=19318)
    with sqlite3.connect(files.database) as con:
        con.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT)")
        con.execute("INSERT INTO projects VALUES(7, ?)", (spec["project_name"],))
        con.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, description TEXT, date_creation INTEGER, date_modification INTEGER, date_moved INTEGER, position INTEGER)")
    task = fixture.generate_task(spec, rows, files)
    assert task["constants"] == (["admin"] if has_constants else [])
    assert task["parameters"] == ["title", "description"]
    assert task["url"] == files.url == "http://127.0.0.1:19318"
    assert task["expect"] == {"project_id": "7"}
    assert task["on_screen"] == [spec["project_name"]]
    assert task["cache_dir"] == str(files.root / "cache")
    assert not {"page_setup", "setup_factory", "function", "approved", "admission"}.intersection(task)
    signature = TaskSignature(task_id=task["task_id"], instruction=task["instruction"],
                              params={name: "string" for name in task["parameters"]})
    for row in rows:
        rendered = signature.render_instruction(row["parameters"])
        assert all(value in rendered for value in row["parameters"].values())
    stored = json.loads((files.root / "TASK.json").read_bytes())
    assert stored == task
    task["constants"].append("synthetic-caller-mutation")
    assert spec.get("constants", []) == (["admin"] if has_constants else [])


@pytest.mark.parametrize("value", [True, None, {}, [""], ["admin", "admin"], [{"canary": "synthetic"}]],
                         ids=["boolean", "null", "mapping", "blank", "duplicate", "nested"])
def test_invalid_constant_data_refuses_at_both_entrypoints_before_native_read(tmp_path, value):
    pack = fixture.packaged_pack("kanboard")
    spec = json.loads(pack.read_bytes())
    spec["constants"] = value
    local = tmp_path / "pack.json"
    local.write_text(json.dumps(spec))
    (tmp_path / spec["case_file"]).write_bytes((pack.parent / spec["case_file"]).read_bytes())
    with pytest.raises(ValueError, match="^INVALID_DEMO_CONSTANTS$"):
        fixture.load_pack(local)
    files = fixture.FixtureFiles(tmp_path, "oawrapdemo-login-unit", tmp_path / "compose.json",
                                 tmp_path / "absent.sqlite", "http://127.0.0.1:19318")
    with pytest.raises(ValueError, match="^INVALID_DEMO_CONSTANTS$"):
        fixture.generate_task(spec, [], files)
    assert not files.database.exists() and not (tmp_path / "TASK.json").exists()
