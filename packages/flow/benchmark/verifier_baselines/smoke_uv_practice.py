"""One-off smoke run of the UV judge port on a PRACTICE trajectory.

Usage (secrets loaded into the environment beforehand, never printed):
    python smoke_uv_practice.py RUN_DIR RUN_JSON OUT_JSON
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uv_judge  # noqa: E402

KEY_NAMES = {
    "return",
    "enter",
    "tab",
    "escape",
    "esc",
    "backspace",
    "delete",
    "space",
    "up",
    "down",
    "left",
    "right",
    "home",
    "end",
    "page_up",
    "page_down",
    "pageup",
    "pagedown",
}


def action_kind(a: dict) -> str:
    """Computer-use action kind inferred from the action dict's keys."""
    keys = set(a)
    if not keys:
        return "screenshot"
    if "scroll_direction" in keys:
        return "scroll"
    if "start_coordinate" in keys:
        return "left_click_drag"
    if "region" in keys:
        return "zoom"
    if "duration" in keys:
        return "wait"
    if "text" in keys:
        t = str(a["text"])
        if t.lower() in KEY_NAMES or ("+" in t and " " not in t and len(t) < 30):
            return "key"
        return "type"
    if "coordinate" in keys:
        return "left_click"
    return "unknown"


def transcript_to_steps(transcript: list, url: str) -> list:
    steps = []
    for turn in transcript:
        actions = turn.get("actions") or []
        kinds = [action_kind(a) for a in actions]
        if len(actions) == 1:
            name, args = kinds[0], actions[0]
        else:
            name = "+".join(kinds) if kinds else "none"
            args = {"actions": [{"action": k, **a} for k, a in zip(kinds, actions)]}
        steps.append(
            {
                "url": url,
                "action_name": name,
                "action_args": args,
            }
        )
    return steps


async def main(run_dir: Path, run_json: Path, out_path: Path) -> None:
    row = json.loads(run_json.read_text())
    tr = json.loads((run_dir / "transcript.json").read_text())["transcript"]
    claim = row.get("claim") or {}
    task = (
        row.get("task")
        or row.get("instruction")
        or (
            f"{row.get('note', '').strip()} Create and save a new {row.get('type', 'record')} "
            "record in the application."
        )
    )
    end_url = claim.get("end_url") or ""
    parts = urlsplit(end_url)
    init_url = f"{parts.scheme}://{parts.netloc}/" if parts.netloc else ""
    steps = transcript_to_steps(tr, "")
    shots = [(run_dir / "start.png").read_bytes(), (run_dir / "end.png").read_bytes()]

    gpt5 = uv_judge.OpenAIJudgeClient("gpt-5")
    # UV's action-only scorer defaults to o4-mini, which OpenAI has retired
    # (404 "deprecated" on 2026-11-03), so gpt-5 plays both roles here.
    try:
        res = await uv_judge.judge(
            task, init_url, steps, shots, gpt5
        )
    finally:
        await gpt5.close()
    res["_inputs"] = {
        "task": task,
        "init_url": init_url,
        "n_steps": len(steps),
        "n_screenshots": len(shots),
        "run_dir": str(run_dir),
    }
    out_path.write_text(json.dumps(res, indent=2, default=str))
    print("output_success:", res["output_success"])
    print(
        "rubric_score:",
        round(res["rubric_score"], 3),
        "penalty_applied:",
        res["penalty_applied"],
    )
    print("primary_intent:", res["primary_intent"])
    print("reasoning:", res["reasoning"])
    for c in res["calls"]:
        print(
            f"  {c['step']:<24} {c.get('model')}: in={c.get('tokens_in')} cached={c.get('cached_tokens')} "
            f"out={c.get('tokens_out')} usd={c.get('usd'):.4f}"
        )
    print(
        "total tokens_in",
        res["tokens_in"],
        "tokens_out",
        res["tokens_out"],
        "usd",
        round(res["usd"], 4),
    )


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])))
