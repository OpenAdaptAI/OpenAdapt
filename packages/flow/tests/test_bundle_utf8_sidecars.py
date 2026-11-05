"""Bundle sidecars are read as UTF-8 whatever the platform's default codec.

Windows reads text in its ANSI code page (cp1252) unless told otherwise, so a
sidecar with non-ASCII text would load wrong or fail there. Each reader runs
in a child Python whose default codec is not UTF-8 (``PYTHONUTF8=0`` and the C
locale with no coercion), where an unencoded read of the same bytes fails.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from openadapt_flow.ir import ActionKind, Step, Workflow

NAME = "Zoë Müller – 東京"


def _bundle(tmp_path: Path, *, manifest_name: str = NAME) -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    wf = Workflow(
        name="demo",
        steps=[Step(id="s1", intent="submit", action=ActionKind.KEY, key="Enter")],
    )
    wf.save(bundle)
    # Move the manifest out of workflow.json into the sidecar, as a sealed
    # bundle keeps it, with non-ASCII text in it.
    raw = json.loads((bundle / "workflow.json").read_bytes())
    manifest = raw.pop("manifest", None) or json.loads(
        (bundle / "manifest.json").read_bytes()
    )
    manifest["provenance"]["compiler_version"] = manifest_name
    (bundle / "workflow.json").write_bytes(json.dumps(raw).encode("utf-8"))
    (bundle / "manifest.json").write_bytes(
        json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    )
    (bundle / "api_proposal.json").write_bytes(
        json.dumps({"proposed": False, "reasons": [NAME]}, ensure_ascii=False).encode(
            "utf-8"
        )
    )
    return bundle


def _run(code: str, bundle: Path) -> str:
    env = {
        **os.environ,
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONIOENCODING": "utf-8",
    }
    probe = subprocess.run(
        [
            sys.executable,
            "-c",
            "import locale; print(locale.getpreferredencoding(False))",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    if probe.stdout.strip().lower().replace("-", "") in ("utf8", "utf_8"):
        pytest.skip("this platform's default codec can't be set away from UTF-8")
    done = subprocess.run(
        [sys.executable, "-c", code, str(bundle)],
        env=env,
        capture_output=True,
        encoding="utf-8",
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_manifest_sidecar_loads_as_utf8(tmp_path: Path) -> None:
    out = _run(
        "import sys\n"
        "from openadapt_flow.ir import Workflow\n"
        "wf = Workflow.load(sys.argv[1])\n"
        "print(wf.manifest.provenance.compiler_version)\n",
        _bundle(tmp_path),
    )
    assert out.strip() == NAME


def test_the_bundle_digest_reads_the_sidecar_as_utf8(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    digest = json.loads((bundle / "manifest.json").read_bytes())["content_digest"]
    out = _run(
        "import sys\n"
        "from pathlib import Path\n"
        "from openadapt_flow.cli_config import bundle_digest\n"
        "print(bundle_digest(Path(sys.argv[1])))\n",
        bundle,
    )
    assert out.strip() == digest


def test_api_show_reads_the_proposal_sidecar_as_utf8(tmp_path: Path) -> None:
    out = _run(
        "import argparse, sys\n"
        "from openadapt_flow.cli_api import cmd_api_show\n"
        "cmd_api_show(argparse.Namespace(bundle=sys.argv[1], as_json=True))\n",
        # An ASCII manifest, so only the proposal sidecar has non-ASCII text.
        _bundle(tmp_path, manifest_name="demo"),
    )
    assert json.loads(out)["not_proposed_because"] == [NAME]
