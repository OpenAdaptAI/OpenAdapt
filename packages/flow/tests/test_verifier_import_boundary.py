"""The record-check verifier loads without Flow's vision stack.

openadapt-verify installs Flow's base dependencies only. Since 3.0.0 NumPy,
OpenCV, Pillow and RapidOCR are the ``vision`` extra, so every module on the
verifier path must import, and a SQLite write must verify with a signed
receipt, while those modules are refused. The check runs in a fresh
interpreter that refuses them even where they are installed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
VISION = ("numpy", "cv2", "PIL", "rapidocr_onnxruntime")
REFUSED = VISION + ("onnxruntime", "playwright", "torch", "scipy", "matplotlib")
VISION_DISTRIBUTIONS = {"numpy", "opencv-python", "pillow", "rapidocr-onnxruntime"}

_SCRIPT = textwrap.dedent(
    """
    import json, os, secrets, sqlite3, sys, tempfile
    from pathlib import Path

    REFUSED = set(json.loads(sys.argv[1]))
    attempts = []

    class Refuse:
        def find_spec(self, name, path=None, target=None):
            if name.partition(".")[0] in REFUSED:
                attempts.append(name)
                raise ImportError("refused: " + name)
            return None

    sys.meta_path.insert(0, Refuse())
    root = Path(tempfile.mkdtemp())
    for name in ("SEAL_HOME", "RECEIPT_HOME", "LEDGER_DIR", "VERIFIER_STATE_DIR"):
        os.environ["OPENADAPT_" + name] = str(root / name.lower())
    os.environ["OPENADAPT_FLOW_AT_REST_HOME"] = str(root / "at-rest")
    os.environ["OPENADAPT_DURABLE_AUTHORITY_DB"] = str(root / "authority.sqlite")
    os.environ["OPENADAPT_FRAME_SECRET"] = secrets.token_hex(32)
    os.environ["OPENADAPT_LOCAL_SEAL"] = "on"

    import openadapt_flow.verifier as v
    import openadapt_flow.verify_run, openadapt_flow.write_ledger
    import openadapt_flow.write_journal_authority, openadapt_flow.owned_sqlite_insert
    import openadapt_flow.write_receipt, openadapt_flow.offline_seal
    import openadapt_flow.runtime.effects.sql, openadapt_flow.runtime.effects._mysql_read
    import openadapt_flow.runtime.effects.db_snapshot
    import openadapt_flow.runtime.effects.history
    import openadapt_flow.runtime.effects.rest, openadapt_flow.runtime.effects.fhir
    import openadapt_flow.integrations.agents, openadapt_flow.agent.verify_tools

    db = root / "app.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE item (id INTEGER PRIMARY KEY, code TEXT)")
    contract = v.Contract.from_dict({
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "sql", "sqlite_database": str(db),
                     "sql_query": "SELECT id, code FROM item"},
        "effects": [{"kind": "record_written", "match": {"code": {"param": "code"}},
                     "expected_count": 1, "count_new_only": True, "timeout_s": 0.2}],
        "frame": {"identity_field": "id"},
    })
    pending = v.begin_write(contract, {"code": "a"}, actor=v.Actor("agent", "t"),
                            workdir=root / "w")
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO item (code) VALUES ('a')")
    result = v.verify_write(pending, write_receipt=True)
    loaded = sorted(m for m in sys.modules if m.partition(".")[0] in REFUSED)
    print(json.dumps({"outcome": result.outcome.value, "receipt": result.write_receipt,
                      "attempts": attempts, "loaded": loaded}))
    """
)


def test_verifier_path_runs_with_the_vision_stack_refused(tmp_path: Path) -> None:
    run = subprocess.run(
        [sys.executable, "-c", _SCRIPT, json.dumps(REFUSED)],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=600,
    )
    assert run.returncode == 0, run.stderr[-4000:]
    report = json.loads(run.stdout.strip().splitlines()[-1])
    assert report == {
        "outcome": "CONFIRMED",
        "receipt": "issued",
        "attempts": [],
        "loaded": [],
    }


def test_base_install_declares_no_vision_distribution() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]

    def names(requirements: list[str]) -> set[str]:
        out = set()
        for requirement in requirements:
            name = requirement.split(";")[0].split("[")[0]
            for stop in "<>=!~ ":
                name = name.split(stop)[0]
            out.add(name.strip().lower())
        return out

    assert not names(project["dependencies"]) & VISION_DISTRIBUTIONS
    assert names(project["optional-dependencies"]["vision"]) == VISION_DISTRIBUTIONS


def test_cli_names_the_vision_extra_when_it_is_missing(tmp_path: Path) -> None:
    script = textwrap.dedent(
        """
        import sys

        class Refuse:
            def find_spec(self, name, path=None, target=None):
                if name.partition(".")[0] in {"numpy", "cv2", "PIL"}:
                    raise ModuleNotFoundError(name=name.partition(".")[0])
                return None

        sys.meta_path.insert(0, Refuse())
        from openadapt_flow.__main__ import main

        sys.exit(main(["--help"]))
        """
    )
    run = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        timeout=600,
    )
    assert run.returncode == 2, run.stderr[-2000:]
    assert "pip install 'openadapt-flow[vision]'" in run.stderr
