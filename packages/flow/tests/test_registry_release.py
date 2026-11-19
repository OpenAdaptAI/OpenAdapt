"""The registry release: no planned entry, re-stamps, and the scheduled tag.

The tag test runs the release workflow's own tag step in bash, as a scheduled
run would, and checks the tag against the identity ``pull`` pins.
"""

from __future__ import annotations

import json
import os
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from openadapt_flow import registry_attest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/build_registry_index.py"
WORKFLOW = ROOT.parents[1] / ".github/workflows/release-registry.yml"
BASE = "https://github.com/OpenAdaptAI/OpenAdapt/releases/download/"


@pytest.fixture(scope="module")
def script() -> dict:
    return runpy.run_path(str(SCRIPT))


def _build(
    tmp_path: Path, tag: str, *extra: str, root: Path = ROOT
) -> subprocess.CompletedProcess:
    out = tmp_path / f"{tag}.json"
    return subprocess.run(
        [
            sys.executable,
            str(root / "scripts/build_registry_index.py"),
            "--archives",
            str(tmp_path / tag),
            "--asset-base",
            f"{BASE}{tag}/",
            "--out",
            str(out),
            "--stamp",
            *extra,
        ],
        capture_output=True,
        text=True,
    )


def test_a_release_refuses_a_planned_entry(script: dict) -> None:
    index = {
        "workflows": [
            {"id": "a/b", "status": "example"},
            {"id": "c/d", "status": "planned"},
        ]
    }
    assert script["release_problems"](index) == [
        "c/d: status 'planned' has no bundle and is never released"
    ]
    assert script["release_problems"]({"workflows": index["workflows"][:1]}) == []


def test_the_committed_registry_is_releasable() -> None:
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--check", "--release"],
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_a_restamp_changes_only_the_freshness_fields_and_archive_urls(
    tmp_path: Path,
) -> None:
    first = _build(tmp_path, "registry-2026-11-18")
    assert first.returncode == 0, first.stdout + first.stderr
    attested = tmp_path / "registry-2026-11-18.json"
    again = _build(tmp_path, "registry-restamp-20261201", "--restamp-of", str(attested))
    assert again.returncode == 0, again.stdout + again.stderr
    old = json.loads(attested.read_text())
    new = json.loads((tmp_path / "registry-restamp-20261201.json").read_text())
    assert new["serial"] >= old["serial"]
    for before, after in zip(old["workflows"], new["workflows"]):
        assert before["archive"]["sha256"] == after["archive"]["sha256"]
        assert "registry-restamp-20261201/" in after["archive"]["url"]


def test_a_restamp_refuses_an_unattested_index(tmp_path: Path) -> None:
    done = _build(
        tmp_path,
        "registry-restamp-20261201",
        "--restamp-of",
        str(ROOT / "registry/index.json"),
    )
    committed = json.loads((ROOT / "registry/index.json").read_text())
    if isinstance(committed.get("serial"), int):
        pytest.skip("the committed index is attested")
    assert done.returncode == 1
    assert "no attested release" in done.stdout


def test_a_restamp_refuses_a_changed_registry(tmp_path: Path) -> None:
    first = _build(tmp_path, "registry-2026-11-18")
    assert first.returncode == 0, first.stdout + first.stderr
    attested = tmp_path / "registry-2026-11-18.json"

    # A copy of the package whose sources changed after the attested release.
    copy = tmp_path / "flow"
    (copy / "scripts").mkdir(parents=True)
    shutil.copy(SCRIPT, copy / "scripts")
    shutil.copytree(ROOT / "registry", copy / "registry")
    os.symlink(ROOT / "openadapt_flow", copy / "openadapt_flow")
    os.symlink(ROOT.parent / "types", tmp_path / "types")
    sources = json.loads((copy / "registry/sources.json").read_text())
    sources["workflows"][0]["evidence"] += " Edited."
    (copy / "registry/sources.json").write_text(json.dumps(sources))

    done = _build(
        tmp_path, "registry-restamp-20261201", "--restamp-of", str(attested), root=copy
    )
    assert done.returncode == 1
    assert "the registry changed since the attested index" in done.stdout


def _tag_step() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    return next(s for s in jobs["tag"]["steps"] if s.get("id") == "tag")


def _run_tag_step(tmp_path: Path, **env: str) -> tuple[int, str]:
    output = tmp_path / "github_output"
    output.write_text("")
    done = subprocess.run(
        ["bash", "-c", _tag_step()["run"]],
        env={"PATH": os.environ["PATH"], "GITHUB_OUTPUT": str(output), **env},
        capture_output=True,
        text=True,
    )
    return done.returncode, output.read_text() + done.stderr


def test_a_scheduled_run_tags_a_restamp_that_pull_accepts(tmp_path: Path) -> None:
    code, out = _run_tag_step(
        tmp_path, REF="refs/heads/main", EVENT="schedule", SUFFIX="", RESTAMP=""
    )
    assert code == 0, out
    tag = out.strip().removeprefix("tag=")
    assert (
        tag.startswith("registry-restamp-") and len(tag) == len("registry-restamp-") + 8
    )
    san = (
        "https://github.com/OpenAdaptAI/OpenAdapt/.github/workflows/"
        f"release-registry.yml@refs/tags/{tag}"
    )
    assert registry_attest.REGISTRY_IDENTITY.san.match(san)


def test_the_tag_step_refuses_mixed_or_off_main_requests(tmp_path: Path) -> None:
    base = {"REF": "refs/heads/main", "EVENT": "workflow_dispatch"}
    code, out = _run_tag_step(tmp_path, **base, SUFFIX="2026-11-18", RESTAMP="false")
    assert code == 0 and out.strip() == "tag=registry-2026-11-18", out
    code, out = _run_tag_step(tmp_path, **base, SUFFIX="", RESTAMP="true")
    assert code == 0 and out.startswith("tag=registry-restamp-"), out
    for suffix, restamp in (
        ("x", "true"),
        ("restamp-20261118", "false"),
        ("", "false"),
    ):
        code, _ = _run_tag_step(tmp_path, **base, SUFFIX=suffix, RESTAMP=restamp)
        assert code == 1, (suffix, restamp)
    code, _ = _run_tag_step(
        tmp_path, REF="refs/heads/other", EVENT="schedule", SUFFIX="", RESTAMP=""
    )
    assert code == 1


def test_the_schedule_is_off_and_every_guard_stays() -> None:
    text = WORKFLOW.read_text()
    on = yaml.safe_load(text)[True]
    assert set(on) == {"workflow_dispatch", "push"}
    assert '#   - cron: "17 6 1,16 * *"' in text
    jobs = yaml.safe_load(text)["jobs"]
    assert "github.event_name == 'schedule'" in jobs["tag"]["if"]
    assert "--release" in jobs["check"]["steps"][-1]["run"]
    build = next(
        s for s in jobs["attest"]["steps"] if s.get("name", "").startswith("Build")
    )
    assert "--restamp-of packages/flow/registry/index.json" in build["run"]
    assert "registry-restamp-*" in build["run"]
    assert "registry_attest.verify_sigstore" in build["run"]
    assert jobs["alarm"]["permissions"] == {"issues": "write"}
    assert "failure()" in jobs["alarm"]["if"]
