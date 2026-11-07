"""Generated task→normal SQL observer/proposer; no agent or browser execution."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from openadapt_flow.compile_agent.pipeline import check_run_record
from openadapt_flow.integrations.agents.recording import TaskSetupError, load_task
from openadapt_flow.record_sor import load_observer, observer_from_config
from openadapt_flow.verify_run import VerifyRunError, parse_verify_config


@pytest.fixture
def task(tmp_path: Path):
    database = tmp_path / "records.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE entries (id INTEGER PRIMARY KEY, project TEXT, title TEXT, body TEXT)")
    document = {"instruction": "Create {title} with {body}", "parameters": ["title", "body"],
                "url": "http://127.0.0.1:8080/", "verifier": {"kind": "sql", "sqlite_database": str(database), "sql_query": "SELECT id, project, title, body FROM entries"},
                "expect": {"project": "demo"}, "on_screen": ["Demo"],
                "canary_params": [{"title": "canary", "body": "independent"}], "cache_dir": "cache"}
    path = tmp_path / "task.json"
    path.write_text(json.dumps(document))
    return path, document, database


def test_generated_task_uses_normal_observer_proposer_and_closed_loader(task):
    path, _, database = task
    setup = load_task(path)
    read_path = setup.cache.common["verify"]
    assert read_path.read_text().startswith("# Generated setup read; no observed effects or approval.\n")
    assert yaml.safe_load(read_path.read_text())["effects"] == []
    values = {"title": "first", "body": "details"}
    compiler, key = setup.cache._compiler(setup.instruction, values)
    before = compiler.observer.system_of_record
    assert before == []
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO entries(project,title,body) VALUES (?,?,?)", ("demo", "first", "details"))
    after = compiler.observer.system_of_record
    assert check_run_record(before, after, expect={"project": "demo"}, values=values).passed
    proposed = yaml.safe_load(compiler._propose(before, after, values).read_text())
    assert proposed["effects"][0]["needs_operator_confirmation"] is True
    assert proposed["effects"][0]["match"]["title"] == {"param": "title"}
    with pytest.raises(VerifyRunError):
        parse_verify_config(proposed)
    assert setup.cache.offer(compiler.proposed()) is None
    assert setup.parameters == ("title", "body")
    assert setup.cache.key(setup.instruction, {"title": "new", "body": "different"}) == key


def test_mapping_and_file_load_share_real_sql_observer(task):
    path, document, _ = task
    setup = load_task(path)
    direct, raw = observer_from_config(document["verifier"], for_run1=True)
    legacy, legacy_raw = load_observer(setup.cache.common["verify"], for_run1=True)
    assert direct.system_of_record == legacy.system_of_record == []
    assert raw == legacy_raw == document["verifier"]


@pytest.mark.parametrize("key", ["approved", "admitted", "offer", "admitter", "person", "model_key", "effects", "review", "reset"])
def test_operator_json_cannot_supply_authority(task, key):
    path, document, _ = task
    document[key] = True
    path.write_text(json.dumps(document))
    with pytest.raises(TaskSetupError, match="^INVALID_TASK_KEYS$"):
        load_task(path)


@pytest.mark.parametrize("key,value", [("parameters", ["title", "title"]), ("parameters", ["missing"]), ("headless", "false"), ("canary_params", [{}]), ("expect", {}), ("on_screen", []), ("verifier", {"kind": "none"}), ("verifier", {"kind": "sql", "admitted": True})])
def test_malformed_task_refuses_before_materializing(task, key, value):
    path, document, _ = task
    document[key] = value
    path.write_text(json.dumps(document))
    with pytest.raises(TaskSetupError):
        load_task(path)
    assert not (path.parent / "cache").exists()


@pytest.mark.parametrize("data", [b'{"instruction":1,"instruction":2}', b'{"x":NaN}', b'\xff', b' ' * 65537])
def test_bounded_closed_json(task, data):
    path, _, _ = task
    path.write_bytes(data)
    with pytest.raises(TaskSetupError):
        load_task(path)


def test_generated_read_is_stable_and_changed_output_refused(task):
    path, _, _ = task
    first = load_task(path)
    target = first.cache.common["verify"]
    assert load_task(path).cache.common["verify"] == target
    target.write_text("changed")
    with pytest.raises(TaskSetupError, match="^GENERATED_READ_CHANGED$"):
        load_task(path)
