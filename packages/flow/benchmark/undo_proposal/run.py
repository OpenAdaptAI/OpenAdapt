"""Measure how many saving steps get a verified undo (``undo propose``).

For each synthetic app, end to end and live:

1. start the app and its record store (in process, on 127.0.0.1);
2. record the canonical demonstration in a real browser with network capture
   on (the demo driver's clicks, as ``record --capture-network`` keeps them);
3. compile it, and bind the save's record effect the way an operator does
   (the fields the save's request carries, counted as new records only);
4. write a deployment file that declares the app a test copy, with a reset
   URL and a ``rest`` reward contract over the whole record store;
5. run ``openadapt-flow undo propose`` against it, and keep its report.

Synthetic data only; nothing leaves 127.0.0.1 and no model is called.

    cd packages/flow && PYTHONPATH=../types:. python benchmark/undo_proposal/run.py \\
        --out benchmark/undo_proposal/results

Run one fixture at a time (``--fixture mockmed``) when the machine is busy.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

import yaml


@dataclass(frozen=True)
class Fixture:
    name: str
    serve: Callable[..., Any]
    record: Callable[..., Path]
    record_kwargs: dict[str, Any]
    query: str
    #: The request-body fields that identify the saved record.
    match_fields: tuple[str, ...]
    identity_key: str
    #: Literal selector fields the request doesn't carry.
    extra_match: tuple[tuple[str, str], ...] = ()
    #: Plain words on what this fixture's save does.
    note: str = ""


def _fixtures() -> dict[str, Fixture]:
    from openadapt_flow import demo_driver
    from openadapt_flow.mockloan.fault_server import serve as serve_loan
    from openadapt_flow.mockmed.fault_server import serve as serve_med

    return {
        "mockmed": Fixture(
            name="mockmed",
            serve=serve_med,
            record=demo_driver.record_triage_demo,
            record_kwargs={"note_text": "Synthetic follow-up in two weeks"},
            query="?fault=ok",
            match_fields=("patient_id", "type"),
            identity_key="patient_id",
            note="Save Encounter writes one encounter record",
        ),
        "mockloan": Fixture(
            name="mockloan",
            serve=serve_loan,
            record=demo_driver.record_disbursement_demo,
            record_kwargs={"memo_text": "Synthetic funding memo"},
            query="?fault=ok",
            match_fields=("loan_id", "product"),
            identity_key="loan_id",
            extra_match=(("surface", "disbursements"),),
            note="Authorize writes one disbursement record",
        ),
        "mockloan-fee-row": Fixture(
            name="mockloan-fee-row",
            serve=serve_loan,
            record=demo_driver.record_disbursement_demo,
            record_kwargs={"memo_text": "Synthetic funding memo"},
            query="?fault=collateral",
            match_fields=("loan_id", "product"),
            identity_key="loan_id",
            extra_match=(("surface", "disbursements"),),
            note=(
                "Authorize writes the disbursement and also a fee row on a "
                "second surface (MockLoan's collateral mode)"
            ),
        ),
    }


def _record_with_capture(fixture: Fixture, url: str, out: Path) -> Path:
    """The demo driver's recording, with the browser's traffic captured."""
    from openadapt_flow import demo_driver
    from openadapt_flow.network_capture import NetworkCapture

    original = demo_driver.PlaywrightBackend.launch
    captures: list[tuple[NetworkCapture, float]] = []

    def launch(*args: Any, **kwargs: Any):
        backend, close = original(*args, **kwargs)
        captures.append(
            (NetworkCapture(backend.page.context).start(), time.monotonic())
        )
        return backend, close

    demo_driver.PlaywrightBackend.launch = launch  # type: ignore[method-assign]
    try:
        recording = fixture.record(url, out, **fixture.record_kwargs)
    finally:
        demo_driver.PlaywrightBackend.launch = original  # type: ignore[method-assign]
    capture, t0 = captures[0]
    capture.write(recording, t0)
    return Path(recording)


def _save_body(recording: Path) -> dict[str, Any]:
    from openadapt_flow.network_capture import load

    for line in reversed(load(recording)):
        if line.get("method") == "POST" and "/api/" in line.get("url", ""):
            try:
                return json.loads(line.get("body") or "{}")
            except json.JSONDecodeError:
                return {}
    return {}


def _bind_save_effect(bundle: Path, fixture: Fixture, body: dict[str, Any]) -> str:
    """Bind the save's record effect as an operator would; return its step id."""
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.effects.effect import Effect, EffectKind
    from openadapt_flow.undo_proposal import saving_steps

    workflow = Workflow.load(bundle)
    saves = saving_steps(workflow)
    if len(saves) != 1:
        raise SystemExit(f"{fixture.name}: expected one saving step, got {len(saves)}")
    index, step = saves[-1]
    match = {k: str(body[k]) for k in fixture.match_fields if k in body}
    if len(match) != len(fixture.match_fields):
        raise SystemExit(
            f"{fixture.name}: the save's request lacks {fixture.match_fields}"
        )
    match.update(dict(fixture.extra_match))
    step.effects = [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=match,
            expected_count=1,
            count_new_only=True,
            risk="irreversible",
            probe="one new record for the save",
            timeout_s=5.0,
        )
    ]
    workflow.steps[index] = step
    workflow.save(bundle)
    return step.id


def _contract(directory: Path, base_url: str, fixture: Fixture, body: dict) -> Path:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from openadapt_flow.reward.seed import write_bundle

    match: dict[str, Any] = {k: str(body[k]) for k in fixture.match_fields}
    match.update(dict(fixture.extra_match))
    match[fixture.identity_key] = {"param": fixture.identity_key}
    write_bundle(
        directory,
        contract_id=f"undo_propose_{fixture.name}",
        oracle={"kind": "rest", "base_url": base_url},
        channel="api",
        key=Ed25519PrivateKey.generate(),
        issuer_key_id="self_signed:benchmark",
        certify=False,
        required_effects=[
            {
                "kind": "record_written",
                "match": match,
                "expected_count": 1,
                "count_new_only": True,
            }
        ],
        forbidden_effects=[],
        identity_keys=[fixture.identity_key],
    )
    return directory


def _deployment(path: Path, fixture: Fixture, base_url: str, contract: Path) -> Path:
    path.write_text(
        yaml.safe_dump(
            {
                "name": f"{fixture.name}-test-copy",
                "backend": {"kind": "web", "url": base_url + "/" + fixture.query},
                "effects": {"kind": "rest", "base_url": base_url},
                "actuation": {"api": True, "base_url": base_url},
                "test_copy": {
                    "declared": True,
                    "reset_url": base_url + "/api/reset",
                    "reward_contract": str(contract),
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_fixture(fixture: Fixture, out_dir: Path, work: Path) -> dict[str, Any]:
    from openadapt_flow.__main__ import main
    from openadapt_flow.compiler.compile import compile_recording

    url, _db, stop = fixture.serve(port=0)
    base = url.rstrip("/")
    started = time.monotonic()
    try:
        recording = _record_with_capture(
            fixture, base + "/" + fixture.query, work / "rec"
        )
        bundle = work / "bundle"
        compile_recording(recording, bundle, name=f"{fixture.name}-demo")
        body = _save_body(recording)
        step_id = _bind_save_effect(bundle, fixture, body)
        contract = _contract(work / "contract", base, fixture, body)
        deployment = _deployment(work / "deployment.yaml", fixture, base, contract)
        report_path = out_dir / f"{fixture.name}.json"
        code = main(
            [
                "undo",
                "propose",
                str(bundle),
                "--config",
                str(deployment),
                "--recording",
                str(recording),
                "--work-dir",
                str(work / "propose"),
                "--report",
                str(report_path),
            ]
        )
    finally:
        stop()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "fixture": fixture.name,
        "what_the_save_does": fixture.note,
        "mode": "live (real browser, app declared a test copy)",
        "exit_code": code,
        "save_step": step_id,
        "seconds": round(time.monotonic() - started, 1),
        "summary": report["summary"],
        "steps": _rows(report),
    }


def _rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "step_id": row["step_id"],
            "verified_undo": row["verified_undo"],
            "reason": row["reason"],
            "to_close_the_gap": row.get("to_close_the_gap", ""),
            "candidates": [
                {k: c[k] for k in ("source", "status", "basis", "reason")}
                for c in row["candidates"]
            ],
        }
        for row in report["saving_steps"]
    ]


def run_offline(
    name: str, bundle: Path, recording: Optional[Path], out_dir: Path, note: str
) -> dict[str, Any]:
    """Candidates from an existing bundle and recording, with no test copy.

    Used where the fixture is shared or down: nothing is reset or run, so a
    buildable candidate stays ``not_run``. It answers whether any candidate
    can be built at all.
    """
    from openadapt_flow.__main__ import main

    report_path = out_dir / f"{name}.json"
    with tempfile.TemporaryDirectory(prefix=f"undo-{name}-") as tmp:
        copy = Path(tmp) / "bundle"
        shutil.copytree(bundle, copy)
        argv = ["undo", "propose", str(copy), "--report", str(report_path)]
        if recording is not None:
            argv += ["--recording", str(recording)]
        code = main(argv)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    return {
        "fixture": name,
        "what_the_save_does": note,
        "mode": "offline (no test copy; nothing was run)",
        "exit_code": code,
        "summary": report["summary"],
        "steps": _rows(report),
    }


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--fixture", action="append", default=None)
    parser.add_argument(
        "--offline",
        nargs=4,
        metavar=("NAME", "BUNDLE", "RECORDING", "NOTE"),
        action="append",
        default=[],
        help=(
            "Also list candidates for an existing bundle and recording without "
            "a test copy (RECORDING may be '-')"
        ),
    )
    args = parser.parse_args(argv)
    fixtures = _fixtures()
    names = args.fixture if args.fixture is not None else list(fixtures)
    if args.offline and args.fixture is None:
        names = []
    args.out.mkdir(parents=True, exist_ok=True)
    rows = []
    for name in names:
        with tempfile.TemporaryDirectory(prefix=f"undo-{name}-") as tmp:
            rows.append(run_fixture(fixtures[name], args.out, Path(tmp)))
    for name, bundle, recording, note in args.offline:
        rows.append(
            run_offline(
                name,
                Path(bundle),
                None if recording == "-" else Path(recording),
                args.out,
                note,
            )
        )
    summary_path = args.out / "summary.json"
    previous: dict[str, Any] = {}
    if summary_path.is_file():
        previous = json.loads(summary_path.read_text(encoding="utf-8"))
    by_name = {r["fixture"]: r for r in previous.get("fixtures", [])}
    by_name.update({r["fixture"]: r for r in rows})
    summary = {
        "schema": "openadapt.undo-propose-measurement/v1",
        "engine_commit": _git_head(),
        "measured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "host": platform.platform(),
        "load_1m": round(os.getloadavg()[0], 2),
        "fixtures": [by_name[k] for k in sorted(by_name)],
        "totals": {
            key: sum(r["summary"][key] for r in by_name.values())
            for key in ("saving_steps", "verified_undo", "no_verified_undo")
        },
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary["totals"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
