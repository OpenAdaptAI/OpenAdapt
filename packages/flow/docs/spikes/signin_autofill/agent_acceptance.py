"""Ask Claude Code and Codex to sign in, with the sign-in MCP tool available.

Runs each agent from an empty directory (no project instructions), records
its full output verbatim under runs/agent/, and reads runs/mcp_calls.jsonl to
see whether it called the tool. Signs out between runs.

    python agent_acceptance.py [--agents claude,codex] [--styles approved,bare]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import signin_replay as replay

HERE = Path(__file__).resolve().parent
OUT = HERE / "runs" / "agent"
CALLS = HERE / "runs" / "mcp_calls.jsonl"

ENV: dict[str, str] = {}

PROMPTS = {
    "plain": "Sign in to the test app and open the protected page.",
    "url": "Log in to http://localhost:47811 with my account and open the protected page.",
}


def calls() -> list[dict]:
    if not CALLS.exists():
        return []
    return [json.loads(line) for line in CALLS.read_text().splitlines() if line]


def claude_cmd(prompt: str, style: str, workdir: Path) -> list[str]:
    cfg = {
        "mcpServers": {
            "signin": {
                "command": sys.executable,
                "args": [str(HERE / "signin_mcp.py")],
                "env": {"SPIKE_TOOL_STYLE": style, "PYTHONPATH": str(HERE)},
            }
        }
    }
    path = workdir / "mcp.json"
    path.write_text(json.dumps(cfg))
    return [
        "claude",
        "-p",
        prompt,
        "--mcp-config",
        str(path),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__signin__sign_in_spike_app",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def codex_cmd(prompt: str, style: str, workdir: Path) -> list[str]:
    # The person's ~/.codex/config.toml does not load in this Codex release
    # ("[agents] enabled = true" is rejected), so use a clean CODEX_HOME that
    # links only the existing sign-in file.
    home = workdir / "codex-home"
    home.mkdir()
    (home / "auth.json").symlink_to(Path.home() / ".codex" / "auth.json")
    ENV["CODEX_HOME"] = str(home)
    return [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--json",
        "-c",
        f'mcp_servers.signin.command="{sys.executable}"',
        "-c",
        f'mcp_servers.signin.args=["{HERE / "signin_mcp.py"}"]',
        "-c",
        f'mcp_servers.signin.env={{SPIKE_TOOL_STYLE="{style}", PYTHONPATH="{HERE}"}}',
        prompt,
    ]


def reset() -> None:
    """Back to the login form if the last run signed in."""
    recipe = json.loads(replay.RECIPE.read_text())
    try:
        replay.Replay(recipe, recipe["window_title"]).sign_out()
    except replay.Refused:
        pass


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--agents", default="claude,codex")
    p.add_argument("--styles", default="approved,bare")
    p.add_argument("--prompts", default="plain,url")
    args = p.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    builders = {"claude": claude_cmd, "codex": codex_cmd}
    summary = []
    for agent in args.agents.split(","):
        if shutil.which(agent) is None:
            summary.append({"agent": agent, "result": "not installed"})
            continue
        for style in args.styles.split(","):
            for key in args.prompts.split(","):
                before = len(calls())
                with tempfile.TemporaryDirectory() as tmp:
                    ENV.clear()
                    cmd = builders[agent](PROMPTS[key], style, Path(tmp))
                    t0 = time.time()
                    proc = subprocess.run(
                        cmd,
                        cwd=tmp,
                        capture_output=True,
                        text=True,
                        timeout=600,
                        stdin=subprocess.DEVNULL,
                        env={**os.environ, **ENV},
                    )
                name = f"{agent}_{style}_{key}"
                (OUT / f"{name}.txt").write_text(
                    f"$ {' '.join(cmd[:3])} ...\nexit {proc.returncode}\n"
                    f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr[-4000:]}"
                )
                new = calls()[before:]
                row = {
                    "agent": agent,
                    "style": style,
                    "prompt": key,
                    "tool_calls": len(new),
                    "signed_in": any(c.get("ok") for c in new),
                    "seconds": round(time.time() - t0, 1),
                }
                summary.append(row)
                print(json.dumps(row), flush=True)
                if row["signed_in"]:
                    reset()
    (OUT / "summary.json").write_text(json.dumps(summary, indent=1) + "\n")


if __name__ == "__main__":
    main()
