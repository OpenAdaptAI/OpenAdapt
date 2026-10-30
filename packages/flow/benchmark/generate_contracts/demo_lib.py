"""Shared steps for running the contract generator on a first-study app.

The generator is run the way a new customer would run it: it gets only the
app's read-only database account, the kinds and parameter names from the
app's ``setup_view.yaml``, and demonstration writes made here through the
app's public REST API (one per example). Nothing from the study's tasks,
oracle or faults reaches the generator. ``validate.py`` uses those later,
only to judge the contracts it wrote.

Paths: ``FCG`` is the first study's folder (``apps/<app>/fixture.py``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from openadapt_flow.generate import verify_contract as gen

FCG = Path(
    os.environ.get(
        "FCG_DIR",
        str(
            Path.home()
            / "oa/worktrees/fcg-retest/evidence/first-contact-generalization-2026-10"
        ),
    )
)


def load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def fixture(app: str) -> Any:
    sys.path.insert(0, str(FCG / "apps" / app))
    return load(f"fcg_fixture_{app}", FCG / "apps" / app / "fixture.py")


def setup_view(app: str) -> dict[str, Any]:
    return yaml.safe_load((FCG / "apps" / app / "setup_view.yaml").read_text(encoding="utf-8"))


def write_inputs(work: Path, app: str, read: dict[str, Any]) -> list[str]:
    """``read.yaml`` and ``kinds.yaml`` in ``work``; the CLI's read flags."""

    work.mkdir(parents=True, exist_ok=True)
    (work / "read.yaml").write_text(yaml.safe_dump(read), encoding="utf-8")
    view = setup_view(app)
    kinds = {
        "kinds": [
            {
                "kind": k["kind"],
                "description": k.get("description", ""),
                "parameters": k["parameters"],
                "examples": [{"values": e["values"]} for e in k.get("examples", [])],
            }
            for k in view["kinds"]
        ]
    }
    (work / "kinds.yaml").write_text(yaml.safe_dump(kinds, allow_unicode=True), encoding="utf-8")
    return ["--read", str(work / "read.yaml")]


def demo(work: Path, kind: str, values: dict[str, Any], write: Callable[[], Any]) -> None:
    """Snapshot, make one demonstration write, record the diff."""

    flags = ["--read", str(work / "read.yaml")]
    assert gen.main(["snapshot", *flags, "--out", str(work / "before.json")]) == 0
    write()
    assert (
        gen.main(
            [
                "record",
                *flags,
                "--before",
                str(work / "before.json"),
                "--kind",
                kind,
                "--values",
                json.dumps(values, ensure_ascii=False),
                "--demos",
                str(work / "demos"),
            ]
        )
        == 0
    )


def propose(work: Path, actor: dict[str, str], extra: list[str] = ()) -> dict[str, Any]:
    argv = [
        "propose",
        "--read",
        str(work / "read.yaml"),
        "--kinds",
        str(work / "kinds.yaml"),
        "--demos",
        str(work / "demos"),
        "--out",
        str(work / "out"),
        *[f"--actor-param={k}={v}" for k, v in actor.items()],
        *extra,
    ]
    if (work / "answers.yaml").exists():
        argv += ["--answers", str(work / "answers.yaml")]
    gen.main(argv)
    return json.loads((work / "out" / "report.json").read_text(encoding="utf-8"))
