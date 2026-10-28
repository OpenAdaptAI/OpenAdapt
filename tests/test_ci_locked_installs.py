"""Every CI install comes from a lockfile, so no test tool floats.

In October 2026 pytest 9.2 was released and every pull request's flow-unit
job picked it up from a fresh resolve and failed. CI now installs from the
packages' uv.lock files (`uv sync --locked`, or scripts/ci/locked_install.py,
which installs `uv export --locked` output with `--require-hashes`) and from
.github/ci-tools/uv.lock for jobs that test no package. These tests read every
workflow's `run` steps and fail on an install that resolves at run time.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

# Installs that resolve on purpose, each with the reason.
ALLOWED = {
    # The launcher's Python-support job checks what a user's pip picks.
    ("main.yml", "pip install --dry-run --quiet --find-links dist"),
    ("main.yml", "pip install dist/*.whl"),
    # Bootstraps uv itself: one exact, dependency-free wheel.
    ("desktop-release.yml", "pip install --disable-pip-version-check uv==0.11.29"),
    # Not on PyPI; pinned to a commit, installed without dependencies.
    ("packages.yml", "uv pip install --system --no-deps 'harness2 @ git+"),
    # Not an install: the test reads the fix the launcher prints.
    ("main.yml", 'grep -q "uv tool install --python 3.12 openadapt"'),
    # npm itself, at an exact version, for trusted publishing.
    ("release-program-graph.yml", "npm install --global npm@11.6.2"),
}


def _commands() -> list[tuple[str, str]]:
    found = []
    for path in WORKFLOWS:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in (workflow.get("jobs") or {}).values():
            for step in job.get("steps") or []:
                run = step.get("run")
                if not run:
                    continue
                joined = re.sub(r"[\\`]\n\s*", " ", run)
                for line in joined.splitlines():
                    line = " ".join(line.split())
                    if line and not line.startswith("#"):
                        found.append((path.name, line))
    return found


COMMANDS = _commands()


def _allowed(workflow: str, line: str) -> bool:
    return any(workflow == name and fragment in line for name, fragment in ALLOWED)


def _offenders(pattern: str, ok: str | None = None) -> list[tuple[str, str]]:
    return [
        (workflow, line)
        for workflow, line in COMMANDS
        if re.search(pattern, line)
        and not (ok and re.search(ok, line))
        and not _allowed(workflow, line)
    ]


def test_the_scan_sees_the_installs() -> None:
    assert len(WORKFLOWS) > 10
    assert any("locked_install.py" in line for _, line in COMMANDS)
    assert any("uv sync --locked" in line for _, line in COMMANDS)


def test_pip_installs_need_hashes() -> None:
    # locked_install.py adds --require-hashes; a direct install must too.
    assert _offenders(r"\bpip install\b", r"--require-hashes") == []


def test_uv_sync_and_run_use_the_lock() -> None:
    assert _offenders(r"\buv sync\b", r"--locked|--frozen") == []
    assert _offenders(r"\buv run\b", r"--locked|--frozen|--no-sync|--no-project") == []
    # `--with` adds packages that resolve at run time.
    assert _offenders(r"\buv run\b.*\s--with\b") == []


def test_no_ad_hoc_tool_environments() -> None:
    assert _offenders(r"(^|\s)uvx\s|\buv tool (run|install)\b|\bpipx\b") == []


def test_npm_and_cargo_installs_are_locked() -> None:
    assert _offenders(r"\bnpm (install|i)\b") == []
    assert _offenders(r"\bcargo install\b", r"--locked") == []


@pytest.mark.parametrize("workflow, fragment", sorted(ALLOWED))
def test_every_exception_is_still_in_use(workflow: str, fragment: str) -> None:
    assert any(name == workflow and fragment in line for name, line in COMMANDS)


def test_every_lock_the_helper_names_exists() -> None:
    specs = [
        spec
        for _, line in COMMANDS
        if "locked_install.py" in line
        for spec in re.findall(r"--lock '?([^' ]+)'?", line)
    ]
    assert specs
    for spec in specs:
        directory = re.split(r"[\[:]", spec)[0]
        assert (ROOT / directory / "uv.lock").is_file(), spec


def _helper():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "locked_install", ROOT / "scripts" / "ci" / "locked_install.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_helper_exports_with_hashes_and_skips_in_tree_packages(
    tmp_path: Path,
) -> None:
    helper = _helper()
    out = tmp_path / "r.txt"
    command = helper._export_command(
        "packages/flow[dev,agent]", ["openadapt-types"], out
    )
    assert command[:3] == ["uv", "export", "--locked"]
    assert "--no-hashes" not in command
    assert command[command.index("--project") + 1] == str(ROOT / "packages" / "flow")
    assert [command[i + 1] for i, part in enumerate(command) if part == "--extra"] == [
        "dev",
        "agent",
    ]
    assert command[command.index("--no-emit-package") + 1] == "openadapt-types"
    groups = helper._export_command(".github/ci-tools:test,build", [], out)
    assert [
        groups[i + 1] for i, part in enumerate(groups) if part == "--only-group"
    ] == [
        "test",
        "build",
    ]
    assert helper._project_name(ROOT / "packages" / "types") == "openadapt-types"
    with pytest.raises(SystemExit):
        helper._export_command("packages/no-such-package", [], out)


def test_pip_check_fails_only_on_what_the_helper_installed() -> None:
    helper = _helper()
    output = "\n".join(
        [
            "Using Python 3.12.10 environment at: C:\\python",
            "Checked 92 packages in 4ms",
            "Found 3 incompatibilities",
            "The package `pipx` requires `packaging>=26.3`, but `26.2` is installed",
            "The package `Openadapt_Capture` requires `sqlalchemy>=2.0.54`, but `2.0.51` is installed",
            "Something new the helper has not seen",
        ]
    )
    ours, others = helper.problems_with(output, {"openadapt-capture", "packaging"})
    assert others == [
        "The package `pipx` requires `packaging>=26.3`, but `26.2` is installed"
    ]
    assert len(ours) == 2 and "Openadapt_Capture" in ours[0]
    assert helper.problems_with("All installed packages are compatible", set()) == (
        [],
        [],
    )
