"""Both real fixture scopes must consume additions to the shared policy."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

FLOW = Path(__file__).resolve().parents[1]
KEYS = (
    "OPENADAPT_DURABLE_AUTHORITY_DB",
    "OPENADAPT_SEAL_HOME",
    "OPENADAPT_FRAME_SECRET",
    "OPENADAPT_FRAME_SECRET_FILE",
    "OPENADAPT_LEDGER_DIR",
    "OPENADAPT_FLOW_PLAINTEXT_AT_REST",
    "OPENADAPT_FLOW_AT_REST_HOME",
    "OPENADAPT_FLOW_AT_REST",
    "OPENADAPT_TEST_FUTURE_HOME",
    "OPENADAPT_TEST_FUTURE_FILE",
)


@pytest.mark.parametrize("prior_set", [False, True])
@pytest.mark.parametrize("setup_fails", [False, True])
def test_shared_policy_reaches_both_scopes_and_restores(
    tmp_path: Path, prior_set: bool, setup_fails: bool
) -> None:
    """Exercise pytest setup/undo, including future set/delete policy entries."""

    probe = tmp_path / "probe"
    probe.mkdir()
    home = tmp_path / "synthetic-home"
    home.mkdir()
    observed = probe / "observed.json"
    restored = probe / "restored.json"
    (probe / "conftest.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            from tests import conftest as policy

            original = policy._isolation_env

            def with_future_entries(root):
                values = original(root)
                values["OPENADAPT_TEST_FUTURE_HOME"] = str(root / ".future-home")
                values["OPENADAPT_TEST_FUTURE_FILE"] = None
                return values

            policy._isolation_env = with_future_entries
            from tests.conftest import (
                _isolate_durable_authority,
                module_durable_authority,
            )

            def pytest_sessionfinish(session, exitstatus):
                Path(RESTORED).write_text(json.dumps({
                    "values": {key: os.environ.get(key) for key in KEYS},
                    "exitstatus": exitstatus,
                }))
            """
        )
        .replace("RESTORED", repr(str(restored)))
        .replace("KEYS", repr(KEYS))
    )
    (probe / "test_scopes.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            from pathlib import Path
            import pytest

            def remember(label, root):
                target = Path(OBSERVED)
                data = json.loads(target.read_text()) if target.exists() else {}
                data[label] = {
                    "root": str(root),
                    "values": {key: os.environ.get(key) for key in KEYS},
                }
                target.write_text(json.dumps(data))

            @pytest.fixture(scope="module")
            def started(module_durable_authority):
                root = module_durable_authority.parent
                remember("module", root)
                if SETUP_FAILS:
                    raise RuntimeError("intentional setup failure")
                yield root
                remember("module_after_function", root)

            def test_function_scope(started, tmp_path):
                assert tmp_path != started
                remember("function", tmp_path)
            """
        )
        .replace("OBSERVED", repr(str(observed)))
        .replace("KEYS", repr(KEYS))
        .replace("SETUP_FAILS", repr(setup_fails))
    )
    # Construct this environment from synthetic values only. Do not copy or
    # inspect ambient credential, key, or encryption-selector values.
    env = {
        "PATH": os.defpath,
        "HOME": str(home),
        "PYTHONPATH": os.pathsep.join((str(FLOW), str(FLOW.parent / "types"))),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    prior = {key: "synthetic-prior-" + key for key in KEYS} if prior_set else {}
    env.update(prior)
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            str(probe),
            "--confcutdir",
            str(probe),
            "--basetemp",
            str(probe / "runtime"),
        ],
        cwd=probe,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert child.returncode == (1 if setup_fails else 0), child.stdout + child.stderr
    if setup_fails:
        assert "intentional setup failure" in child.stdout
    assert json.loads(restored.read_text()) == {
        "values": {key: prior.get(key) for key in KEYS},
        "exitstatus": 1 if setup_fails else 0,
    }
    snapshots = json.loads(observed.read_text())
    assert set(snapshots) == (
        {"module"}
        if setup_fails
        else {
            "module",
            "function",
            "module_after_function",
        }
    )
    for scope, snapshot in snapshots.items():
        root = Path(snapshot["root"])
        assert root.is_relative_to(tmp_path)
        database = (
            "authority.sqlite3"
            if scope.startswith("module")
            else ".durable-authority.sqlite3"
        )
        assert snapshot["values"] == {
            "OPENADAPT_DURABLE_AUTHORITY_DB": str(root / database),
            "OPENADAPT_SEAL_HOME": str(root / ".seal-home"),
            "OPENADAPT_FRAME_SECRET": "11" * 32,
            "OPENADAPT_FRAME_SECRET_FILE": None,
            "OPENADAPT_LEDGER_DIR": str(root / ".write-ledger"),
            "OPENADAPT_FLOW_PLAINTEXT_AT_REST": "test suite: synthetic data",
            "OPENADAPT_FLOW_AT_REST_HOME": str(root / ".at-rest-home"),
            "OPENADAPT_FLOW_AT_REST": None,
            "OPENADAPT_TEST_FUTURE_HOME": str(root / ".future-home"),
            "OPENADAPT_TEST_FUTURE_FILE": None,
        }
    if not setup_fails:
        assert snapshots["module_after_function"] == snapshots["module"]
        assert snapshots["function"]["root"] != snapshots["module"]["root"]
    assert not list(home.rglob("*"))
