"""``verifiers`` eval that never uploads unless asked.

    python -m openadapt_web_envs.run_eval --env.taskset.base-url http://127.0.0.1:8000 -m <model>

verifiers' ``eval`` uploads every finished run to the Prime Intellect
account logged in on the machine unless ``--no-push`` is given. This wrapper
adds ``--no-push`` and the local subprocess runtime by default. Uploading
needs the explicit ``--allow-upload`` flag.
"""

from __future__ import annotations

import sys

UPLOAD_FLAG = "--allow-upload"
TASKSET = "openadapt-web-envs"


def build_argv(args: list[str]) -> list[str]:
    allow = UPLOAD_FLAG in args
    rest = [a for a in args if a != UPLOAD_FLAG]
    if not allow:
        if any(a == "--push" or a.startswith("--push=") for a in rest):
            raise SystemExit(f"uploading needs {UPLOAD_FLAG}, not --push")
        rest.append("--no-push")
    if not any(a.startswith("--env.player.runtime.type") for a in rest):
        rest += ["--env.player.runtime.type", "subprocess"]
    if not rest or rest[0].startswith("-"):
        rest.insert(0, TASKSET)
    return rest


def main() -> int:
    from verifiers.v1.cli.eval.main import main as vf_eval

    vf_eval(build_argv(sys.argv[1:]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
