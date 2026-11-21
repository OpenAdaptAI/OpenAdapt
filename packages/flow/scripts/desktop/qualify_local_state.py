"""Record, compile, replay and check a local-state example on a Windows lab host.

Lab only (the zenbook or the UTM VM, under its lease). Synthetic text only.

For one example under ``examples/local_state/<name>/``:

1. reset the state the example changes, open the app, and record the
   demonstration with the desktop recorder (``record_desktop_demo`` over the
   lab's win_agent backend, so every click is armed with its UIA locator);
2. compile the recording;
3. reset again, take the checker's baseline, replay with a new parameter
   value, and run ``openadapt-flow verify-run`` with the example's
   ``verify.yaml`` **inside the guest**, where the state lives. The receipt
   is the check;
4. a negative control: reset, skip the replay, and run the same check. It
   must refute;
5. an unreadable case: the same check pointed at state it cannot read. It
   must halt (``INDETERMINATE``), never pass.

The guest needs the checker installed at ``C:\\oa\\lsc`` (``venv`` with
``pydantic pyyaml cryptography httpx idna``, and the worktree's
``packages/flow`` and ``packages/types`` under ``src``); this script prints
what is missing.

Usage, from packages/flow::

    OAFLOW_LAB_PROFILE=winlap python scripts/desktop/qualify_local_state.py \\
        notepad-save --out ~/oa/scratch/local-state-check/runs/notepad-1
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
sys.path.insert(0, str(HERE))

import winrm_lab  # noqa: E402

LSC = r"C:\oa\lsc"
GUEST_PY = rf"{LSC}\venv\Scripts\python.exe"
GUEST_PATH = rf"{LSC}\src\packages\flow;{LSC}\src\packages\types"
#: Where the examples' files live in the guest (``~`` in their verify.yaml).
EXAMPLE_DIR = r"$env:USERPROFILE\openadapt-examples"


# -- the examples' lab steps (fixtures, not engine code) -----------------------


class Example:
    """What the lab does around one example: reset, open, demonstrate."""

    name: str
    params_recorded: dict[str, str]
    params_replayed: dict[str, str]

    def reset(self, lab: winrm_lab.WinRMLab) -> None:
        raise NotImplementedError

    def bring_to_front(self, agent: Any, viewport: tuple[int, int]) -> None:
        return None

    def demonstrate(self, rec: Any, viewport: tuple[int, int]) -> None:
        raise NotImplementedError

    def unreadable_verify(self, verify: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError


class NotepadSave(Example):
    name = "notepad-save"
    params_recorded = {"note": "synthetic note 4711 recorded"}
    params_replayed = {"note": "synthetic note 9022 replayed"}

    def reset(self, lab: winrm_lab.WinRMLab) -> None:
        lab.stop_interactive("OAFlowLSC")
        lab.run_ps(
            "Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue; "
            # Close what would cover the app: a vendor screensaver blocks capture.
            "Get-Process | Where-Object { $_.ProcessName -like '*Screensaver*' } | "
            "Stop-Process -Force -ErrorAction SilentlyContinue; "
            # Notepad restores unsaved tabs after it is closed; start clean
            # (the lab backed the original tab state up to C:\\oa\\lsc first).
            "Start-Sleep 1; Remove-Item -Recurse -Force "
            "$env:LOCALAPPDATA\\Packages\\Microsoft.WindowsNotepad_8wekyb3d8bbwe\\LocalState\\TabState\\* "
            "-ErrorAction SilentlyContinue; "
            f"New-Item -ItemType Directory -Force {EXAMPLE_DIR} | Out-Null; "
            f"Set-Content -NoNewline -Encoding ascii -Path {EXAMPLE_DIR}\\note.txt -Value ''",
            check=False,
            timeout_s=90.0,
        )
        lab.launch_interactive(
            rf'cmd.exe /c start "" /max notepad.exe "%USERPROFILE%\openadapt-examples\note.txt"',
            name="OAFlowLSC",
        )
        time.sleep(6)

    def bring_to_front(self, agent: Any, viewport: tuple[int, int]) -> None:
        """Setup, not part of the demonstration: put Notepad in front.

        A window started from a scheduled task doesn't always get the
        foreground, so typed keys could go nowhere. One click in the empty
        editor puts the caret there, the state a person starts from.
        """
        w, h = viewport
        agent.click(w // 2, h // 2)
        time.sleep(1.0)

    def demonstrate(self, rec: Any, viewport: tuple[int, int]) -> None:
        # Notepad opens the file with the caret in the editor, so the person
        # types straight away. A click in blank editor space has no control
        # to anchor to (Notepad's editor exposes no UIA AutomationId there),
        # and the visual resolver then anchors it to the nearest text, the
        # Edit menu.
        rec.type_text(self.params_recorded["note"], param="note")
        rec.hotkey(["ctrl"], "s")
        time.sleep(1.5)

    def unreadable_verify(self, verify: dict[str, Any]) -> dict[str, Any]:
        doc = json.loads(json.dumps(verify))
        doc["verifier"]["local_state"][0]["path"] = "~/openadapt-examples-missing/note.txt"
        return doc


EXAMPLES: dict[str, Callable[[], Example]] = {"notepad-save": NotepadSave}


# -- guest checker -------------------------------------------------------------


def _guest_check(
    lab: winrm_lab.WinRMLab,
    out: Path,
    tag: str,
    verify: dict[str, Any],
    params: dict[str, str],
    *,
    baseline: bool,
    run_record: dict[str, Any],
) -> dict[str, Any]:
    """Run verify-run in the guest; return the receipt and the exit code."""
    work = out / f"check-{tag}"
    work.mkdir(parents=True, exist_ok=True)
    g = rf"{LSC}\checks\{tag}"
    lab.run_ps(
        f"Remove-Item -Recurse -Force '{g}' -ErrorAction SilentlyContinue; "
        f"New-Item -ItemType Directory -Force '{g}' | Out-Null"
    )
    for name, body in (
        ("verify.json", verify),
        ("params.json", params),
        ("run-record.json", run_record),
    ):
        (work / name).write_text(json.dumps(body, indent=2))
        lab.push_file(work / name, rf"{g}\{name}")
    env = f"$env:PYTHONPATH='{GUEST_PATH}'; $env:OPENADAPT_FLOW_PLAINTEXT_AT_REST='lab: synthetic text'; "
    base = f"& '{GUEST_PY}' -m openadapt_flow verify-run --verify '{g}\\verify.json' --params '{g}\\params.json'"
    return {"guest_dir": g, "env": env, "base": base, "work": work, "baseline": baseline}


def _capture_baseline(lab: winrm_lab.WinRMLab, ctx: dict[str, Any]) -> str:
    done = lab.run_ps(
        ctx["env"] + ctx["base"] + f" --capture-baseline '{ctx['guest_dir']}\\baseline.json'",
        check=False,
    )
    return (done.stdout or "") + (done.stderr or "")


def _verify(lab: winrm_lab.WinRMLab, ctx: dict[str, Any]) -> dict[str, Any]:
    g = ctx["guest_dir"]
    extra = f" --baseline '{g}\\baseline.json'" if ctx["baseline"] else ""
    done = lab.run_ps(
        ctx["env"]
        + ctx["base"]
        + extra
        + f" --run-record '{g}\\run-record.json' --authorization-ref lab-{Path(g).name}"
        + f" --out '{g}\\out'; \"EXIT=$LASTEXITCODE\"",
        check=False,
    )
    text = (done.stdout or "") + (done.stderr or "")
    (ctx["work"] / "verify-run.log").write_text(text)
    receipt_raw = lab.run_ps(
        f"Get-Content -Raw '{g}\\out\\effect-receipt.json' -ErrorAction SilentlyContinue",
        check=False,
    ).stdout
    receipt = None
    if receipt_raw and "{" in receipt_raw:
        body = receipt_raw[receipt_raw.index("{") : receipt_raw.rindex("}") + 1]
        doc = json.loads(body)
        (ctx["work"] / "effect-receipt.json").write_text(json.dumps(doc, indent=2))
        # write_result stores {"receipt": ..., "evidence": ...}.
        receipt = doc.get("receipt", doc)
        receipt["readback_effects"] = doc.get("evidence", {}).get("readback", {}).get("effects")
        receipt["frame_status"] = doc.get("evidence", {}).get("frame", {}).get("status")
    exit_line = [ln for ln in text.splitlines() if ln.startswith("EXIT=")]
    return {
        "exit": exit_line[-1][5:] if exit_line else None,
        "effect_verdict": receipt and receipt.get("effect_verdict"),
        "outcome": receipt and receipt.get("outcome"),
        "verification_tier": receipt and receipt.get("verification_tier"),
        "checked_how": receipt and receipt.get("checked_how"),
        "frame_status": receipt and receipt.get("frame_status"),
        "effects": receipt and [
            {k: e.get(k) for k in ("kind", "verdict", "substrate", "flow_verification_tier")}
            for e in receipt.get("readback_effects") or []
        ],
        "log_tail": text.strip().splitlines()[-6:],
    }


# -- the trial -----------------------------------------------------------------


def run_trial(example: Example, out: Path, row: dict[str, Any]) -> None:
    from openadapt_flow.adapters.desktop_recorder import (
        record_desktop_demo,
        structural_armed_coverage,
    )
    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.runtime import Replayer

    verify = _load_yaml(FLOW / "examples" / "local_state" / example.name / "verify.yaml")
    lab = winrm_lab.WinRMLab()
    prefix = lab.profile.prefix
    row.update(
        {
            "example": example.name,
            "host": lab.profile.name,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidate_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=HERE
            ).stdout.strip(),
        }
    )
    endpoint = lab.launch_agent(token=os.environ[prefix + "AGENT_TOKEN"], wait_s=240)
    agent = endpoint.backend()
    (Path.home() / "oa/scratch" / lab.profile.name / "agent_pin.txt").write_text(
        endpoint.pin_fingerprint or ""
    )
    viewport = agent.viewport
    row["viewport"] = list(viewport)

    # 1. Record.
    example.reset(lab)
    rec_check = _guest_check(
        lab, out, "record", verify, example.params_recorded, baseline=True,
        run_record={"executor_kind": "human", "executor_id": "lab-demonstration"},
    )
    row["record_baseline"] = _capture_baseline(lab, rec_check)[-300:]
    example.bring_to_front(agent, viewport)
    t0 = time.monotonic()
    recording = record_desktop_demo(agent, out / "recording", lambda r: example.demonstrate(r, viewport))
    row["record_s"] = round(time.monotonic() - t0, 1)
    row["record_check"] = _verify(lab, rec_check)

    # 2. Compile.
    t0 = time.monotonic()
    workflow = compile_recording(recording, out / "bundle", name=example.name)
    row["compile_s"] = round(time.monotonic() - t0, 1)
    row["steps"] = [
        {"id": s.id, "action": getattr(s.action, "value", s.action), "param": s.param}
        for s in workflow.steps
    ]
    row["params"] = sorted(workflow.params)
    row["structural"] = structural_armed_coverage(workflow)

    # 3. Replay with a new value, then check.
    example.reset(lab)
    run_check = _guest_check(
        lab, out, "replay", verify, example.params_replayed, baseline=True,
        run_record={"executor_kind": "flow_replay", "executor_id": f"lab-{example.name}"},
    )
    row["replay_baseline"] = _capture_baseline(lab, run_check)[-300:]
    example.bring_to_front(agent, viewport)
    t0 = time.monotonic()
    report = Replayer(agent, use_structural=True).run(
        workflow,
        params=dict(example.params_replayed),
        bundle_dir=out / "bundle",
        run_dir=out / "run",
    )
    row["replay_s"] = round(time.monotonic() - t0, 1)
    row["replay"] = {
        "success": report.success,
        "steps": [
            {"step": r.step_id, "status": getattr(r, "status", None), "reason": (getattr(r, "reason", "") or "")[:160]}
            for r in report.results
        ],
        # What the replay itself says about checking, with no effect declared.
        "verified": getattr(report, "verified", None),
        "transaction_outcome": getattr(report, "transaction_outcome", None),
    }
    row["replay_check"] = _verify(lab, run_check)

    # 4. Negative control: reset, no replay, same check.
    example.reset(lab)
    neg = _guest_check(
        lab, out, "no-replay", verify, example.params_replayed, baseline=True,
        run_record={"executor_kind": "flow_replay", "executor_id": "lab-skipped"},
    )
    _capture_baseline(lab, neg)
    row["negative_control"] = _verify(lab, neg)

    # 5. Unreadable: the same contract on state that cannot be read.
    bad = _guest_check(
        lab, out, "unreadable", example.unreadable_verify(verify), example.params_replayed,
        baseline=False,
        run_record={"executor_kind": "flow_replay", "executor_id": "lab-unreadable"},
    )
    row["unreadable"] = _verify(lab, bad)
    lab.stop_interactive("OAFlowLSC")
    lab.run_ps("Stop-Process -Name notepad -Force -ErrorAction SilentlyContinue", check=False)

    row["accepted"] = bool(
        row["record_check"]["outcome"] == "verified"
        and report.success
        and row["replay_check"]["effect_verdict"] == "CONFIRMED"
        and row["replay_check"]["outcome"] == "verified"
        and row["negative_control"]["effect_verdict"] == "REFUTED"
        and row["unreadable"]["effect_verdict"] == "INDETERMINATE"
    )


def _load_yaml(path: Path) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("example", choices=sorted(EXAMPLES))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    row: dict[str, Any] = {}
    try:
        run_trial(EXAMPLES[args.example](), args.out, row)
    except Exception as exc:  # noqa: BLE001 - recorded as a rejected trial
        row["accepted"] = False
        row["failure"] = f"{type(exc).__name__}: {str(exc)[:400]}"
        (args.out / "traceback.txt").write_text(traceback.format_exc())
    row["completed_at"] = datetime.now(timezone.utc).isoformat()
    (args.out / "evidence.json").write_text(json.dumps(row, indent=2, sort_keys=True, default=str) + "\n")
    print(json.dumps(row, indent=2, sort_keys=True, default=str))
    return 0 if row.get("accepted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
