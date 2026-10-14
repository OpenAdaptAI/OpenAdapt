"""examples/claude-code-demo still builds and runs against this checkout.

The example records an "add expense" form fill against a small SQLite-backed
web app, compiles it with effect mining, swaps in the reviewed SQL contract
from ``effect_contract.yaml``, and certifies it against the example's
``policy.yaml``. This test runs that same build into ``tmp_path``, then runs
the bundle through ``openadapt-flow run`` twice:

- against the normal app: the run succeeds, and the SQL check confirms one
  new row with the values the run was given;
- against the same app with writes dropped (the page still says
  "Expense saved."): the run halts and the table is unchanged.

When ``openadapt-agent`` is installed, it also calls the bundle through the
agent's MCP bridge, the path Claude Code uses.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.timeout(600)

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "claude-code-demo"


def _load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        f"claude_code_demo_{name}", EXAMPLE / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations here
    dont_write = sys.dont_write_bytecode
    sys.dont_write_bytecode = True  # keep __pycache__ out of the checkout
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = dont_write
    return module


app = _load("app")
build = _load("build")


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict]:
    work = tmp_path_factory.mktemp("claude_code_demo")
    db = work / "expenses.db"
    app.init_db(db, reset=True)
    base_url, server = app.serve(db)
    url = f"{base_url}/"
    layout = build.Layout(work)
    try:
        build.build(url, db, layout)
        yield {"server": server, "db": db, "layout": layout, "url": url}
    finally:
        server.shutdown()
        server.server_close()


def _rows(db: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return conn.execute("SELECT description, amount FROM expenses").fetchall()
    finally:
        conn.close()


def _run(built: dict, run_dir: Path, params: dict[str, str]) -> tuple[int, dict]:
    params_file = run_dir.parent / f"{run_dir.name}.params.json"
    params_file.write_text(json.dumps(params))
    layout = built["layout"]
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "run",
            str(layout.bundle),
            "--config",
            str(layout.deployment),
            "--params-file",
            str(params_file),
            "--run-dir",
            str(run_dir),
            "--allow-unencrypted",
        ],
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "OPENADAPT_FLOW_SCRUB": "off"},
        check=False,
    )
    report = json.loads((run_dir / "report.json").read_text())
    return proc.returncode, report


def _effect_results(report: dict) -> list[str]:
    return [line for step in report["results"] for line in step["effect_results"]]


def test_build_attaches_the_reviewed_sql_contract(built: dict) -> None:
    from openadapt_flow.ir import Workflow

    workflow = Workflow.load(built["layout"].bundle)
    assert set(workflow.params) == {"description", "amount"}
    saves = [step for step in workflow.steps if step.risk == "irreversible"]
    assert len(saves) == 1
    (effect,) = saves[0].effects
    assert effect.kind.value == "record_written"
    assert effect.count_new_only is True
    assert effect.match["description"].param == "description"
    assert effect.match["amount"].param == "amount"


def test_run_writes_and_confirms_one_new_row(built: dict, tmp_path: Path) -> None:
    params = {"description": "Parking at the client site", "amount": "18.75"}
    code, report = _run(built, tmp_path / "run", params)

    assert code == 0
    assert report["success"] is True
    assert report["model_calls"] == 0
    assert _rows(built["db"]).count((params["description"], params["amount"])) == 1
    assert any(
        line.startswith("[sql] record_written: CONFIRMED")
        for line in _effect_results(report)
    )


def test_run_halts_when_the_app_drops_the_write(built: dict, tmp_path: Path) -> None:
    params = {"description": "Client dinner", "amount": "96.00"}
    handler = built["server"].RequestHandlerClass
    handler.drop_writes = True
    try:
        before = _rows(built["db"])
        code, report = _run(built, tmp_path / "run", params)
    finally:
        handler.drop_writes = False

    assert code != 0
    assert report["success"] is False
    assert _rows(built["db"]) == before
    assert any(
        line.startswith("[sql] record_written:") and "CONFIRMED" not in line
        for line in _effect_results(report)
    )


def test_agent_bridge_runs_the_bundle(built: dict, tmp_path: Path) -> None:
    pytest.importorskip("openadapt_agent")
    from openadapt_agent.bridge import AgentBridge
    from openadapt_agent.runner import RunnerConfig

    layout = built["layout"]
    bridge = AgentBridge(
        layout.bundles,
        RunnerConfig(
            runs_dir=tmp_path / "runs",
            deployment_config=str(layout.deployment),
            extra_run_args=("--allow-unencrypted",),
        ),
        allow_run=True,
    )
    (tool,) = [
        spec.name for spec in bridge.list_tool_specs() if spec.name.startswith("run_")
    ]
    params = {"description": "Taxi to the airport", "amount": "41.20"}
    result = bridge.dispatch(tool, params)

    assert result["status"] == "success"
    assert result["summary"]["model_calls"] == 0
    assert _rows(built["db"]).count((params["description"], params["amount"])) == 1
