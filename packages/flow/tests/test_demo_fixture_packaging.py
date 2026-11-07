"""Installed demo DATA and real wrap-loader joins, without an app/model run."""
from __future__ import annotations

import json
import sqlite3

import pytest

from openadapt_flow.integrations.agents import demo_fixture
from openadapt_flow.integrations.agents.recording import load_task


def test_installed_pack_is_available_without_repository_relative_path():
    path = demo_fixture.packaged_pack("kanboard")
    spec, cases = demo_fixture.load_pack(path)
    assert path.name == "kanboard.json" and path.parent.name == "demo_packs"
    assert len(cases) == 20 and spec["task_id"] == "create-task"
    assert all("{" + key + "}" in spec["instruction"] for key in ("title", "description"))


def test_packaged_names_refuse_aliases_and_missing_data():
    for name in ("../kanboard", "/kanboard", "kanboard.json", "kanboard\\alias", "", "missing-owned-fixture"):
        with pytest.raises(ValueError):
            demo_fixture.packaged_pack(name)


def test_native_schema_task_loads_real_factory_into_owned_cache(tmp_path):
    spec, cases = demo_fixture.load_pack(demo_fixture.packaged_pack("kanboard"))
    fixture = demo_fixture.prepare(spec, tmp_path / "owned", project="oawrapdemo-packaging", port=19318)
    with sqlite3.connect(fixture.database) as connection:
        connection.execute("CREATE TABLE projects(id INTEGER PRIMARY KEY, name TEXT)")
        connection.execute("INSERT INTO projects VALUES(7, 'Wrap demo')")
        connection.execute("CREATE TABLE tasks(id INTEGER PRIMARY KEY, project_id INTEGER, title TEXT, description TEXT, date_creation INTEGER, date_modification INTEGER, date_moved INTEGER, position INTEGER)")
    task = demo_fixture.generate_task(spec, cases, fixture)
    setup = load_task(fixture.root / "TASK.json", key_dir=fixture.root / "keys")
    assert setup.parameters == ("title", "description")
    assert setup.instruction == spec["instruction"]
    assert setup.cache.common["cache_dir"] == fixture.root / "cache"
    assert setup.cache.common["expect"] == {"project_id": "7"}
    assert setup.cache.common["on_screen"] == [spec["project_name"]]
    assert setup.cache.canary_params == spec["canary_parameters"]
    assert setup.cache.offer({}) is None and setup.cache.person is None and setup.cache.admitter is None
    compiler, key = setup.cache._compiler(setup.instruction, cases[0]["parameters"])
    assert compiler.dir.is_relative_to(fixture.root / "cache")
    assert key["params"] == ["description", "title"]
    generated = setup.cache.common["verify"]
    assert generated.is_relative_to(fixture.root / "cache")
    assert "effects: []" in generated.read_text()
    assert task["verifier"]["records_drop_fields"] == spec["generated_fields"]
    assert json.loads((fixture.root / "TASK.json").read_text())["cache_dir"] == str(fixture.root / "cache")
    assert not list(fixture.root.rglob("admitted.json"))
