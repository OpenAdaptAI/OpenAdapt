"""A model-free, OpenAI-compatible endpoint that plays the scripted solution.

    python environments/prime/scripted_model.py environments/openenv/mockmed --port 8780

verifiers' OpenEnv driver sends each observation to "the model" and steps the
environment with the reply. This stub answers with the task's reference
action for the observation's step, so a ``verifiers`` eval runs the whole
Prime Intellect path (taskset, driver, OpenEnv server, verify-run reward)
with no model call and no cost. ``--claim success`` replaces the final claim,
playing an agent that trusts the screen.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional


def _payload(messages: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    for message in reversed(messages):
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )
        if message.get("role") == "user" and isinstance(content, str):
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                continue
    return None


def make_handler(
    solutions: dict[str, list[dict[str, Any]]], claim: Optional[str]
) -> type:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_POST(self) -> None:  # noqa: N802
            body = json.loads(
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
            )
            payload = _payload(body.get("messages", [])) or {}
            obs = payload.get("observation", {})
            # The observation carries no task id, so the stub matches the
            # instruction, which is unique per task.
            steps = solutions.get(obs.get("instruction", ""), [])
            step = int(obs.get("step", 0))
            action = dict(steps[step]) if step < len(steps) else {"kind": "done"}
            if claim and action.get("kind") == "done":
                action["claim"] = claim
            reply = {
                "id": f"scripted-{time.time_ns()}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": body.get("model", "scripted"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(action)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("env_dir", type=Path)
    parser.add_argument("--port", type=int, default=8780)
    parser.add_argument("--claim", choices=["success", "failure"])
    args = parser.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from openadapt_flow.rl_env.tasks import load_environment

    env = load_environment(args.env_dir)
    solutions = {task.instruction: task.solution for task in env.tasks}
    server = ThreadingHTTPServer(
        ("127.0.0.1", args.port), make_handler(solutions, args.claim)
    )
    print(f"scripted model on http://127.0.0.1:{args.port}/v1", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
