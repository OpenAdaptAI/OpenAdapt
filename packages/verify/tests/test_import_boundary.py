"""The verify path never imports Flow's vision stack or any GUI/ML module.

Each check runs in a fresh interpreter whose import system refuses the heavy
modules outright, so the test fails even where CI has them installed (the
facade job installs Flow's full development environment). A refused import
fails the check whether or not the code caught the ImportError.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

#: Top-level modules the verify path must never import. The first four are
#: Flow's `vision` extra; the rest are GUI, browser and ML stacks.
HEAVY = (
    "numpy",
    "cv2",
    "PIL",
    "rapidocr_onnxruntime",
    "onnxruntime",
    "playwright",
    "torch",
    "torchvision",
    "transformers",
    "scipy",
    "skimage",
    "matplotlib",
    "shapely",
    "pyautogui",
    "pynput",
    "mss",
    "Quartz",
    "AppKit",
    "uiautomation",
)

_BLOCKER = textwrap.dedent(
    """
    import json, sys
    HEAVY = set(json.loads(sys.argv[1]))
    ATTEMPTS = []

    class Refuse:
        def find_spec(self, name, path=None, target=None):
            if name.partition(".")[0] in HEAVY:
                ATTEMPTS.append(name)
                raise ImportError(f"blocked heavy import: {name}")
            return None

    sys.meta_path.insert(0, Refuse())
    """
)

_REPORT = textwrap.dedent(
    """
    loaded = sorted(m for m in sys.modules if m.partition(".")[0] in HEAVY)
    print(json.dumps({"attempts": ATTEMPTS, "loaded": loaded}))
    """
)


def _run_blocked(body: str, tmp_path) -> dict:
    script = _BLOCKER + textwrap.dedent(body) + _REPORT
    run = subprocess.run(
        [sys.executable, "-c", script, json.dumps(HEAVY)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=600,
    )
    assert run.returncode == 0, run.stderr[-4000:] + run.stdout[-2000:]
    return json.loads(run.stdout.strip().splitlines()[-1])


def test_every_verify_module_imports_without_the_vision_stack(tmp_path):
    report = _run_blocked(
        """
        import openadapt_verify, openadapt_verify.cli, openadapt_verify.guard
        import openadapt_verify.quickstart, openadapt_verify.shadow
        import openadapt_verify.mcp
        from openadapt_verify import WriteGuard  # the adapters' shared guard
        # Flow's verifier core, as named in the slim-install contract.
        import openadapt_flow.verifier, openadapt_flow.verify_run
        import openadapt_flow.write_ledger, openadapt_flow.write_journal_authority
        import openadapt_flow.owned_sqlite_insert, openadapt_flow.write_receipt
        import openadapt_flow.offline_seal, openadapt_flow.receipt_log
        import openadapt_flow.runtime.effects.sql
        import openadapt_flow.runtime.effects._mysql_read
        import openadapt_flow.runtime.effects.db_snapshot
        import openadapt_flow.runtime.effects.history
        import openadapt_flow.runtime.effects.rest
        import openadapt_flow.runtime.effects.fhir
        import openadapt_flow.integrations.agents
        """,
        tmp_path,
    )
    assert report == {"attempts": [], "loaded": []}


def test_quickstart_and_one_line_guards_run_without_the_vision_stack(tmp_path):
    """REST, SQLite and FHIR reads, signed receipts and both plain-Python
    helpers, end to end, with every heavy import refused."""

    report = _run_blocked(
        """
        import sqlite3
        from openadapt_verify import checked_write, quickstart, verified

        rows = quickstart.run(("rest", "sqlite", "fhir"))
        assert len(rows) == 9 and all(row.ok for row in rows), [
            row.to_dict() for row in rows
        ]

        with quickstart.isolated_state(__import__("pathlib").Path("state")):
            store = quickstart.SqliteReadPath(__import__("pathlib").Path("db"))
            contract = store.contract()

            @verified(contract, workdir="guard")
            def save(code, quantity):
                store.agent_write({"code": code, "quantity": quantity})
                return "saved"

            reply = save(code="a-1", quantity="1")
            assert reply["outcome"] == "CONFIRMED", reply
            assert reply["seal"] == "sealed" and reply["tool_output"] == "saved"

            with checked_write(contract, {"code": "a-2", "quantity": "2"},
                               workdir="ctx") as write:
                store.agent_write({"code": "a-2", "quantity": "2"})
            assert write.result.outcome.value == "CONFIRMED"
            assert write.result.write_receipt == "issued"
        """,
        tmp_path,
    )
    assert report == {"attempts": [], "loaded": []}
