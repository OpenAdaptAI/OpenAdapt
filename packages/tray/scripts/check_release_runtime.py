#!/usr/bin/env python3
"""Exercise the installed release CLI against disposable local Git history."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from run_semantic_release import release_command, verify_runtime

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    verify_runtime()
    cli = release_command()[0]
    with tempfile.TemporaryDirectory(prefix="tray-release-smoke-") as directory:
        root = Path(directory)
        (root / "src/openadapt_tray").mkdir(parents=True)
        for relative in ("pyproject.toml", "src/openadapt_tray/__init__.py"):
            shutil.copyfile(ROOT / relative, root / relative)
        environment = {
            name: value for name, value in os.environ.items()
            if name in {"PATH", "HOME", "SYSTEMROOT", "TEMP", "TMP"}
        }
        environment.update({
            "GH_TOKEN": "local-smoke-token",
            "GITHUB_ACTIONS": "true",
            "GITHUB_OUTPUT": str(root / "github-output"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            # Refuse Git transports even if the fixture unexpectedly releases.
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "protocol.allow",
            "GIT_CONFIG_VALUE_0": "never",
        })

        def run(*command: str) -> str:
            result = subprocess.run(
                command, cwd=root, env=environment, check=True,
                capture_output=True, text=True,
            )
            return result.stdout

        run("git", "init", "-b", "main")
        run("git", "config", "user.name", "Release smoke")
        run("git", "config", "user.email", "release-smoke@example.invalid")
        run("git", "remote", "add", "origin", "https://github.com/fixture/tray.git")
        run("git", "add", ".")
        run("git", "commit", "-m", "chore: baseline")
        # The fixture uses the exact checked-in release configuration and version.
        from check_release_consistency import release_versions

        version = release_versions(ROOT)["pyproject.toml"]
        run("git", "tag", f"v{version}")
        run("git", "commit", "--allow-empty", "-m", "chore: dependency maintenance")
        before = run("git", "rev-parse", "HEAD")
        run(sys.executable, str(ROOT / "scripts/run_semantic_release.py"))
        output = (root / "github-output").read_text(encoding="utf-8")
        assert "released=false" in output, output
        assert run("git", "rev-parse", "HEAD") == before
        assert run("git", "tag").strip() == f"v{version}"

        run("git", "commit", "--allow-empty", "-m", "feat: synthetic change")
        before = run("git", "rev-parse", "HEAD")
        next_version = run(cli, "--noop", "version", "--print").strip()
        major, minor, _patch = (int(part) for part in version.split("."))
        assert next_version == f"{major}.{minor + 1}.0", next_version
        assert run("git", "rev-parse", "HEAD") == before
        assert run("git", "tag").strip() == f"v{version}"
    print("Locked PSR accepts the configuration, preserves no-release outputs, and calculates the next version.")


if __name__ == "__main__":
    main()
