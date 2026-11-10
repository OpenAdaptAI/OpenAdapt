"""Stage one environment as a self-contained OpenEnv Space directory.

    python environments/openenv/stage.py mockmed --out /tmp/space-mockmed \
        [--wheel dist/openadapt_flow-*.whl] [--flow-requirement 'openadapt-flow[openenv]==X']

The staged directory is what ``openenv validate`` and ``openenv push`` read
and what ``docker build`` builds: the environment's own files (``env.yaml``,
``verify.yaml``, ``openenv.yaml``, the public task pack) plus the shared
``server/`` and ``oracle/`` templates and a README with the Space header.
Staging refuses an environment whose task pack holds anything but the public
split, and never copies an application: copyleft apps are named by upstream
image digest in ``env.yaml`` and pulled by whoever runs them.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SPACE = HERE / "_space"
ENV_FILES = ("env.yaml", "verify.yaml", "openenv.yaml", "README.md")


def stage(name: str, out: Path, *, wheels: list[Path], flow_requirement: str) -> Path:
    sys.path.insert(0, str(HERE.parents[1]))
    from openadapt_flow.rl_env.tasks import load_environment

    source = HERE / name
    env = load_environment(source)  # refuses a non-public task in the pack
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for filename in ENV_FILES:
        if (source / filename).exists():
            shutil.copy2(source / filename, out / filename)
    shutil.copytree(source / "tasks", out / "tasks")
    shutil.copytree(SPACE / "server", out / "server")
    shutil.copytree(SPACE / "oracle", out / "oracle")
    (out / "wheels").mkdir()
    for wheel in wheels:
        shutil.copy2(wheel, out / "wheels" / wheel.name)
    dockerfile = out / "server" / "Dockerfile"
    dockerfile.write_text(
        dockerfile.read_text().replace(
            'ARG FLOW_REQUIREMENT="openadapt-flow[openenv]"',
            f'ARG FLOW_REQUIREMENT="{flow_requirement}"',
        )
    )
    manifest = env.manifest
    readme = out / "README.md"
    body = readme.read_text() if readme.exists() else f"# {manifest.title}\n"
    header = (
        "---\n"
        f"title: {manifest.title}\n"
        "sdk: docker\n"
        "app_port: 8000\n"
        f"license: {manifest.licence.environment.lower()}\n"
        "tags: [openenv, computer-use, effect-verification]\n"
        "---\n\n"
    )
    readme.write_text(header + body)
    (out / ".dockerignore").write_text("**/__pycache__\n")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("name")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, action="append", default=[])
    parser.add_argument("--flow-requirement", default="openadapt-flow[openenv]")
    args = parser.parse_args()
    out = stage(
        args.name, args.out, wheels=args.wheel, flow_requirement=args.flow_requirement
    )
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
