"""Parallel smoke test: N concurrent sessions against one environment server.

    python -m openadapt_flow.rl_env.parallel path/to/env -n 8 --episodes 2 --out parallel.json

Starts the server with room for N sessions (unless ``--base-url`` names one),
opens N OpenEnv WebSocket clients at once, and has each play ``--episodes``
scripted episodes over the public tasks. It reports reset and episode
seconds (median and p95), throughput, and every episode whose reward was not
the scripted maximum: with isolated sessions there should be none.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.rl_env import lifecycle
from openadapt_flow.rl_env.agents import scripted
from openadapt_flow.rl_env.tasks import load_environment
from openadapt_flow.rl_env.validate import _free_port


def _pct(values: list[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(q * len(ordered)))], 2)


def run(
    env_dir: Path, n: int, episodes: int, base_url: Optional[str] = None
) -> dict[str, Any]:
    import subprocess
    import sys

    import httpx
    from openenv import GenericEnvClient

    env = load_environment(env_dir)
    if env.manifest.app.kind == "external" and n > 1:
        raise SystemExit(
            f"{env.manifest.name} shares one app database across sessions; "
            "run one server per app instance instead"
        )
    tasks = env.split("public")
    proc: Optional[subprocess.Popen[bytes]] = None
    if base_url is None:
        port = _free_port()
        proc = lifecycle.spawn(
            [
                sys.executable,
                "-m",
                "openadapt_flow.rl_env.server",
                str(env.directory),
                "--port",
                str(port),
                "--max-concurrent-envs",
                str(n),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        base_url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
    rows: list[dict[str, Any]] = []
    lock = threading.Lock()

    def worker(slot: int) -> None:
        try:
            with GenericEnvClient(
                base_url=base_url, message_timeout_s=300
            ).sync() as client:
                for k in range(episodes):
                    task = tasks[(slot + k * n) % len(tasks)]
                    t0 = time.monotonic()
                    result = client.reset(task_id=task.id, episode_id=f"p{slot}-{k}")
                    t_reset = time.monotonic() - t0
                    for action in scripted(task):
                        result = client.step(action)
                        if result.done:
                            break
                    with lock:
                        rows.append(
                            {
                                "slot": slot,
                                "task_id": task.id,
                                "reward": result.reward,
                                "reset_s": round(t_reset, 2),
                                "episode_s": round(time.monotonic() - t0, 2),
                            }
                        )
        except Exception as exc:  # noqa: BLE001 - reported per slot
            with lock:
                rows.append({"slot": slot, "error": f"{type(exc).__name__}: {exc}"})

    started = time.monotonic()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = time.monotonic() - started
    if proc is not None:
        lifecycle.stop(proc)
    done = [r for r in rows if "error" not in r]
    resets = [r["reset_s"] for r in done]
    episodes_s = [r["episode_s"] for r in done]
    return {
        "env": env.manifest.name,
        "sessions": n,
        "episodes": len(done),
        "errors": [r for r in rows if "error" in r],
        "not_max_reward": [r for r in done if r["reward"] != 1.0],
        "reset_s": {"median": _pct(resets, 0.5), "p95": _pct(resets, 0.95)},
        "episode_s": {"median": _pct(episodes_s, 0.5), "p95": _pct(episodes_s, 0.95)},
        "wall_s": round(wall, 1),
        "episodes_per_minute": round(60 * len(done) / wall, 2) if wall else None,
        "passed": len(done) == n * episodes and all(r["reward"] == 1.0 for r in done),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0], allow_abbrev=False
    )
    parser.add_argument("env_dir", type=Path)
    parser.add_argument("-n", type=int, default=8)
    parser.add_argument("--episodes", type=int, default=2)
    parser.add_argument("--base-url")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = run(args.env_dir, args.n, args.episodes, args.base_url)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
