"""The Medplum leaderboard runs, as a trajectory judge sees them.

Reads the scored rows listed in ``inputs/medplum_rows.manifest`` from the
leaderboard results at commit 04e76283a (``git show``, so no checkout of that
branch is needed) and yields, per run: the task text the agent got, one step
per agent turn and the start and end screenshots. Agent replies are omitted.
See JUDGE_INPUTS.md for the current input boundary and PROTOCOL.md for
the historical population.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Iterator

HERE = Path(__file__).resolve().parent
COMMIT = "04e76283a03c37a18e47887c1954728812d21533"
RESULTS = "packages/flow/benchmark/fhir_medplum/leaderboard/results"
MANIFEST = HERE / "inputs" / "medplum_rows.manifest"
TASKS = HERE / "inputs" / "medplum_tasks.json"


def _show(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{COMMIT}:{path}"],
        cwd=HERE,
        check=True,
        capture_output=True,
    ).stdout


def _action_name(action: dict[str, Any]) -> str:
    """The computer-use action kind, from the keys the harness logged."""

    if "coordinate" in action:
        return "click"
    if "text" in action:
        text = str(action["text"])
        is_key = (
            len(text) < 25
            and " " not in text
            and ("+" in text or text[:1].isupper() or text in ("Return", "Tab"))
        )
        return "keypress" if is_key else "input_text"
    if "scroll_direction" in action or "scroll" in action:
        return "scroll"
    if "duration" in action:
        return "wait"
    if "region" in action:
        return "zoom"
    if not action:
        return "screenshot"
    return "action"


def steps(transcript: list[dict[str, Any]], end_url: str) -> list[dict[str, Any]]:
    # At COMMIT, claude_cu records model response text blocks as turn.text;
    # openai_cu records output_text messages there. Neither is observed state.
    out = []
    for turn in transcript:
        actions = [a for a in turn.get("actions") or [] if a]
        name = (
            _action_name(actions[0])
            if len(actions) == 1
            else ("+".join(_action_name(a) for a in actions) if actions else "reply")
        )
        out.append(
            {
                "url": end_url or "",
                "action_name": name,
                "action_args": {"actions": actions} if actions else {},
            }
        )
    return out


def runs() -> Iterator[dict[str, Any]]:
    tasks = json.loads(TASKS.read_text())
    for line in MANIFEST.read_text().splitlines():
        _sha, path = line.split("  ", 1)
        agent = path.split("/")[-3]
        row = json.loads(_show(path))
        run_id = Path(path).stem
        folder = path[: -len(".json")]
        claim = row.get("claim") or {}
        view = None
        if claim.get("status") == "done":
            transcript = json.loads(_show(f"{folder}/transcript.json"))["transcript"]
            view = {
                "task": tasks[row["id"]],
                "init_url": "",
                "steps": steps(transcript, ""),
                "screenshots": [
                    _show(f"{folder}/start.png"),
                    _show(f"{folder}/end.png"),
                ],
            }
        yield {"agent": agent, "run": run_id, "row": row, "view": view}
