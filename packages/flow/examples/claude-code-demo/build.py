"""Record, compile, and check the "add an expense" workflow.

Run it while the expenses app is up (``./demo.sh`` starts the app and runs
this for you):

    python build.py              # scripted demonstration in a headless browser
    python build.py --by-hand    # you do the demonstration in a real browser

Output:

    work/recording/              what was demonstrated (frames and events)
    work/bundles/add-expense/    the compiled program
    work/deployment.yaml         where the app is and how to read its database
    mcp.json                     the MCP server entry for Claude Code

Nothing here calls a model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
CONTRACT = HERE / "effect_contract.yaml"
POLICY = HERE / "policy.yaml"

WORKFLOW_NAME = "add-expense"
PARAMS = ("description", "amount")
DEMO_VALUES = {"description": "Team lunch", "amount": "42.50"}

#: Keys that only move focus between fields. The build marks them reversible,
#: so a recording that tabs from one field to the next needs no extra review.
FOCUS_KEYS = {"Tab", "Shift+Tab"}

Rows = Callable[[], list[dict[str, Any]]]


class BuildError(SystemExit):
    """A build step failed. The message says what to do next."""


@dataclass(frozen=True)
class Layout:
    """Where the build writes. Everything lives under one work directory."""

    work: Path

    @property
    def recording(self) -> Path:
        return self.work / "recording"

    @property
    def bundles(self) -> Path:
        return self.work / "bundles"

    @property
    def bundle(self) -> Path:
        return self.bundles / WORKFLOW_NAME

    @property
    def runs(self) -> Path:
        return self.work / "runs"

    @property
    def deployment(self) -> Path:
        return self.work / "deployment.yaml"


def say(message: str = "") -> None:
    print(message, flush=True)


def read_rows(db: Path) -> Rows:
    """A read-only view of the expenses table, used only while recording."""

    def read() -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, description, amount, submitted_by, status"
                " FROM expenses ORDER BY id"
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    return read


def check_app(url: str) -> None:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            if response.status == 200:
                return
    except OSError:
        pass
    raise BuildError(f"The expenses app is not answering at {url}. Run ./demo.sh")


def record_scripted(url: str, rows: Rows, layout: Layout) -> None:
    """Fill in the form once through Flow's recorder, as a person would."""

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.recorder import Recorder

    backend, close = PlaywrightBackend.launch(url, headless=True)
    try:
        page = backend.page
        recorder = Recorder(
            backend, layout.recording, app_url=url, system_of_record_reader=rows
        )

        def center(selector: str) -> tuple[int, int]:
            locator = page.locator(selector).first
            locator.wait_for(state="visible")
            box = locator.bounding_box()
            assert box is not None
            return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)

        # Selectors only tell this script where to click. The recorder keeps
        # screenshots, events, and the rows it saw in the database; replay
        # finds each target again from that evidence.
        recorder.click(*center("#description"))
        recorder.type_text(DEMO_VALUES["description"], param="description")
        recorder.click(*center("#amount"))
        recorder.type_text(DEMO_VALUES["amount"], param="amount")
        recorder.click(*center("#save"))
        page.wait_for_selector("#saved-banner", state="visible")
        recorder.finish()
    finally:
        close()


def record_by_hand(url: str, rows: Rows, layout: Layout) -> None:
    from openadapt_flow.interactive_recorder import record_interactive

    baseline = len(rows())
    say("      A browser window opens on the expenses app. In it:")
    say("        1. Click 'Expense description' and type a description.")
    say("        2. Click 'Amount in US dollars' and type an amount.")
    say("        3. Click 'Add expense'.")
    say("      Recording stops when the new row reaches the database.")
    record_interactive(
        url,
        layout.recording,
        param_fields=PARAMS,
        headless=False,
        system_of_record_reader=rows,
        stop_when=lambda: len(rows()) > baseline,
    )


def compile_bundle(layout: Layout) -> Any:
    """Compile with effect mining on. Tab-only focus moves are marked reversible."""

    from openadapt_flow.compiler import compile_recording

    def build(overrides: dict[str, str] | None = None) -> Any:
        shutil.rmtree(layout.bundle, ignore_errors=True)
        return compile_recording(
            layout.recording,
            layout.bundle,
            name=WORKFLOW_NAME,
            mine_effects=True,
            risk_overrides=overrides,
        )

    workflow = build()
    focus_steps = {
        step.id: "reversible"
        for step in workflow.steps
        if step.action.value == "key" and step.key in FOCUS_KEYS
    }
    return build(focus_steps) if focus_steps else workflow


def describe(effect: Any) -> str:
    """One-line summary of an effect contract."""

    def value(expr: Any) -> str:
        return f"<{expr.param}>" if expr.param else repr(expr.literal)

    where = ", ".join(f"{key}={value(expr)}" for key, expr in effect.match.items())
    if effect.kind.value == "field_equals":
        return f"{effect.field} == {value(effect.value)} where {where}"
    new = " new" if effect.count_new_only else ""
    return f"{effect.expected_count}{new} row(s) where {where}"


def apply_reviewed_contract(workflow: Any, layout: Layout) -> Any:
    """Replace the proposed effect with the reviewed one in effect_contract.yaml."""

    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime.effects.effect import Effect

    saves = [step for step in workflow.steps if step.risk == "irreversible"]
    if len(saves) != 1:
        raise BuildError(
            f"Expected one saving click in the recording, found {len(saves)}. "
            "Record again: fill in both fields, then click 'Add expense' once."
        )
    save = saves[0]
    proposed = [e for e in save.effects if e.kind.value == "record_written"]
    if not proposed or any(e.needs_operator_confirmation for e in save.effects):
        raise BuildError(
            "The recording did not see a new row appear in the expenses table, "
            "so there is nothing to check against. Record again."
        )
    say("      Proposed from the row that appeared during the recording:")
    for effect in save.effects:
        say(f"        {describe(effect)}")

    spec = yaml.safe_load(CONTRACT.read_text())
    effects = [
        Effect.model_validate({"risk": save.risk, **item}) for item in spec["effects"]
    ]
    workflow = Workflow.load(layout.bundle)
    target = next(step for step in workflow.steps if step.id == save.id)
    target.effects = effects
    workflow.save(layout.bundle)  # re-seals the bundle's integrity manifest
    say(f"      Replaced with {CONTRACT.name}:")
    for effect in effects:
        say(f"        {describe(effect)}")
    return workflow


def write_deployment(url: str, db: Path, layout: Layout) -> None:
    deployment = {
        "name": "expenses-demo",
        "backend": {"kind": "web", "url": url},
        "effects": {
            "kind": "sql",
            "sql_driver": "sqlite3",
            "sql_connect_args": {"database": f"file:{db}?mode=ro", "uri": True},
            "sql_query": (
                "SELECT id, description, amount, submitted_by, status FROM expenses"
            ),
            "timeout_s": 5.0,
            "poll_interval_s": 0.2,
        },
        "policy": {"policy": str(POLICY)},
    }
    header = (
        "# Written by build.py and read by `openadapt-flow run`: where the app\n"
        "# is, how to read the expenses table after the save (read-only), and\n"
        "# which policy the bundle must pass.\n"
    )
    layout.deployment.write_text(header + yaml.safe_dump(deployment, sort_keys=False))


def certify(layout: Layout) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "openadapt_flow",
            "certify",
            str(layout.bundle),
            "--config",
            str(layout.deployment),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in (result.stdout or result.stderr).strip().splitlines():
        say(f"      {line}")
    if result.returncode != 0:
        raise BuildError("The bundle did not pass policy.yaml. See the lines above.")


def write_mcp_config(layout: Layout, path: Path) -> None:
    agent = Path(sys.executable).with_name("openadapt-agent")
    config = {
        "mcpServers": {
            "openadapt": {
                "command": str(agent),
                "args": [
                    "serve",
                    "--bundles",
                    str(layout.bundles),
                    "--config",
                    str(layout.deployment),
                    "--runs-dir",
                    str(layout.runs),
                    "--allow-run",
                    # Lets the client read the full local run report. The data
                    # here is synthetic; leave this off for real records.
                    "--allow-protected-export",
                    # The demo bundle is not encrypted at rest.
                    "--extra-run-arg=--allow-unencrypted",
                ],
                # Synthetic data, so run reports are written without scrubbing.
                "env": {"OPENADAPT_FLOW_SCRUB": "off"},
            }
        }
    }
    path.write_text(json.dumps(config, indent=2) + "\n")


def build(url: str, db: Path, layout: Layout, *, by_hand: bool = False) -> None:
    """Record, compile, attach the database check, and certify."""

    check_app(url)
    shutil.rmtree(layout.recording, ignore_errors=True)
    layout.work.mkdir(parents=True, exist_ok=True)
    rows = read_rows(db)

    say("[1/4] Record: fill in the form once and click 'Add expense'")
    (record_by_hand if by_hand else record_scripted)(url, rows, layout)

    say("[2/4] Compile the recording into a program (no model calls)")
    workflow = compile_bundle(layout)
    say(f"      {len(workflow.steps)} steps; parameters: {', '.join(workflow.params)}")

    say("[3/4] Attach the database check and certify")
    apply_reviewed_contract(workflow, layout)
    write_deployment(url, db, layout)
    certify(layout)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8642/")
    parser.add_argument("--db", default=str(HERE / "work" / "expenses.db"))
    parser.add_argument(
        "--by-hand",
        action="store_true",
        help="record your own demonstration in a visible browser",
    )
    args = parser.parse_args()
    layout = Layout(HERE / "work")

    build(args.url, Path(args.db).resolve(), layout, by_hand=args.by_hand)

    say("[4/4] Write mcp.json for Claude Code")
    write_mcp_config(layout, HERE / "mcp.json")
    say()
    say("Ready. Start Claude Code in this folder with the tool attached:")
    say("  claude --mcp-config mcp.json")
    say("Then ask: Add an 18.75 expense for parking.")


if __name__ == "__main__":
    main()
