"""A run step before a job's checkout can't use a working directory inside the repo.

Desktop 0.21.1's first release run failed in its stage job: the release App
identity check ran before actions/checkout with the workflow-wide working
directory packages/desktop, which didn't exist yet.
"""

from __future__ import annotations

from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _inside_repo(directory: str | None) -> bool:
    if not directory:
        return False
    return not directory.startswith("${{") and directory not in {".", "./"}


def test_no_run_step_before_checkout_uses_a_repo_directory() -> None:
    problems = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        default = ((workflow.get("defaults") or {}).get("run") or {}).get("working-directory")
        for name, job in (workflow.get("jobs") or {}).items():
            job_default = ((job.get("defaults") or {}).get("run") or {}).get("working-directory", default)
            checked_out = False
            for step in job.get("steps") or []:
                if "actions/checkout" in str(step.get("uses", "")):
                    checked_out = True
                if "run" in step and not checked_out:
                    directory = step.get("working-directory", job_default)
                    if _inside_repo(directory):
                        problems.append(f"{path.name}:{name}:{step.get('name')} -> {directory}")
    assert problems == [], "\n".join(problems)
