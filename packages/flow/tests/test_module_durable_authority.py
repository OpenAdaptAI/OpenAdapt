"""The opted-in module fixture isolates setup without replacing test isolation."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from openadapt_flow.runtime.durable.authority import DurableAuthorityBusy
from tests.test_durable_authority_v13 import _fresh_run

FLOW = Path(__file__).resolve().parents[1]
AUTHORITY_ENV = "OPENADAPT_DURABLE_AUTHORITY_DB"


@pytest.mark.parametrize("prior_set", [False, True])
@pytest.mark.parametrize("setup_fails", [False, True])
def test_module_setup_isolated_and_restored(
    tmp_path: Path, prior_set: bool, setup_fails: bool
) -> None:
    """Run real pytest setup and a child authority while another DB is locked."""

    home = tmp_path / "home"
    default = home / ".openadapt" / "durable-authority" / "authority.sqlite3"
    external = tmp_path / "prior" / "authority.sqlite3" if prior_set else default
    external.parent.mkdir(parents=True)
    external.touch(mode=0o600)
    probe = tmp_path / "probe"
    probe.mkdir()
    result_path = probe / "teardown.json"
    (probe / "conftest.py").write_text(
        textwrap.dedent(
            f"""
            import json
            import os
            from pathlib import Path
            from tests.conftest import (
                _isolate_durable_authority,
                module_durable_authority,
            )

            def pytest_sessionfinish(session, exitstatus):
                Path({str(result_path)!r}).write_text(json.dumps({{
                    "restored": os.environ.get({AUTHORITY_ENV!r}),
                    "exitstatus": exitstatus,
                }}))
            """
        )
    )
    (probe / "test_setup.py").write_text(
        textwrap.dedent(
            f"""
            import os
            import subprocess
            import sys
            import uuid
            from pathlib import Path
            import pytest
            from tests.test_durable_authority_v13 import _fresh_run

            @pytest.fixture(scope="module")
            def started(module_durable_authority, tmp_path_factory):
                owned = module_durable_authority
                assert owned != Path({str(external)!r})
                root = tmp_path_factory.mktemp("durable-setup")
                _run, _store, manifest, authority = _fresh_run(root / "parent")
                assert authority.db_path == owned
                assert authority.validate(manifest).phase == "active"
                child_code = (
                    "import sys, uuid; from pathlib import Path; "
                    "from tests.test_durable_authority_v13 import _fresh_run; "
                    "_, _, manifest, authority = _fresh_run("
                    "Path(sys.argv[1]), run_id=str(uuid.uuid4()), "
                    "namespace_id=uuid.uuid4().hex); "
                    "assert authority.db_path == Path(sys.argv[2]); "
                    "assert authority.validate(manifest).phase == 'active'"
                )
                child = subprocess.run(
                    [sys.executable, "-c", child_code,
                     str(root / "child"), str(owned)],
                    env=dict(os.environ), capture_output=True, text=True,
                    timeout=60, check=False,
                )
                assert child.returncode == 0, child.stdout + child.stderr
                Path({str(probe / "setup-complete")!r}).write_text(str(owned))
                if {setup_fails!r}:
                    raise RuntimeError("intentional setup failure")
                return owned

            def test_function_isolation_still_applies(started):
                current = Path(os.environ[{AUTHORITY_ENV!r}])
                assert current != started
                assert current.name == ".durable-authority.sqlite3"
            """
        )
    )
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(FLOW)
    if prior_set:
        env[AUTHORITY_ENV] = str(external)
    else:
        env.pop(AUTHORITY_ENV, None)
    # The parent owns this lock for the entire nested pytest run. Neither
    # setup nor its subprocess can accidentally succeed against this DB.
    with sqlite3.connect(external) as connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO sentinel VALUES ('held')")
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                str(probe),
                "--confcutdir",
                str(probe),
            ],
            cwd=FLOW,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert connection.in_transaction
        assert connection.execute("SELECT value FROM sentinel").fetchall() == [
            ("held",)
        ]
    assert (probe / "setup-complete").is_file(), completed.stdout + completed.stderr
    assert completed.returncode == (1 if setup_fails else 0), (
        completed.stdout + completed.stderr
    )
    if setup_fails:
        assert "intentional setup failure" in completed.stdout
    assert json.loads(result_path.read_text()) == {
        "restored": str(external) if prior_set else None,
        "exitstatus": 1 if setup_fails else 0,
    }


def test_a_locked_owned_authority_still_refuses(tmp_path: Path) -> None:
    """Keep the real SQLite busy refusal, including its production timeout."""

    _run, _store, manifest, authority = _fresh_run(tmp_path)
    with sqlite3.connect(authority.db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(DurableAuthorityBusy, match="unavailable"):
            authority.validate(manifest)
        assert connection.in_transaction
    assert authority.validate(manifest).phase == "active"
