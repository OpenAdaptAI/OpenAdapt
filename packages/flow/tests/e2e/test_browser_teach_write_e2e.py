"""Taught browser writes are refused.

Browser teach refuses taught writes until a person's signed teach
authorization is bound to them (docs/TEACH_AUTHORIZATION.md). Against the
real example app: ``teach_start`` with a write contract is refused before a
browser opens, and a write tool already on disk doesn't run. Nothing is
written to the app.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.agent import browser_teach as bt
from openadapt_flow.agent.bridge import BridgeError
from openadapt_flow.agent.example_app import serve
from openadapt_flow.verify import WriteContract


@pytest.fixture
def app() -> Iterator[str]:
    base, stop = serve(0)
    yield base
    stop()


def _orders(base: str) -> Any:
    with urllib.request.urlopen(f"{base}/api/orders", timeout=5) as response:
        return json.loads(response.read())["records"]


def _contract(base: str) -> WriteContract:
    return WriteContract.from_dict(
        {
            "schema_version": "openadapt.verify/v1",
            "writes": ["POST /login", "POST /orders"],
            "verifier": {
                "kind": "rest",
                "base_url": base,
                "records_path": "/api/orders",
                "records_key": "records",
            },
            "effects": [
                {
                    "kind": "record_written",
                    "match": {"customer": {"param": "customer"}},
                    "expected_count": 1,
                    "count_new_only": True,
                    "timeout_s": 1.0,
                }
            ],
        },
        name="order",
    )


def test_a_taught_write_is_refused_and_writes_nothing(app: str, tmp_path: Path) -> None:
    before = _orders(app)
    bridge = bt.BrowserTeachBridge(
        tmp_path / "tools", tmp_path / "files", contracts={"order": _contract(app)}
    )
    with pytest.raises(BridgeError, match="taught writes are refused"):
        bridge.dispatch(
            "teach_start",
            {
                "name": "add_order",
                "url": f"{app}/login",
                "task": "Add an order",
                "contract": "order",
                "inputs": {"customer": "Acme Ltd"},
            },
        )
    folder = tmp_path / "tools" / "add_order"
    folder.mkdir(parents=True)
    (folder / bt.PROGRAM_FILE).write_text(
        json.dumps(
            {
                "schema": bt.PROGRAM_SCHEMA,
                "name": "add_order",
                "task": "Add an order",
                "url": f"{app}/login",
                "inputs": ["customer"],
                "steps": [],
                "writes": [],
                "result": {"kind": "write", "contract": "order"},
            }
        )
    )
    bridge = bt.BrowserTeachBridge(
        tmp_path / "tools", tmp_path / "files", contracts={"order": _contract(app)}
    )
    result = bridge.dispatch("taught_add_order", {"inputs": {"customer": "Birch"}})
    assert result["outcome"] == "did_not_run", result
    assert _orders(app) == before
