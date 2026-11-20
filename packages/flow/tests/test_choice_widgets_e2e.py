"""Record, compile and run dropdown choices and a confirm() end to end.

Real headless Chromium against the synthetic registration app
(benchmark/choice_widgets): a person's demonstration through
``record_interactive``, the compiler, then the replayer in a fresh browser.
Correctness is read from the app's stored records, never from the run report.

Covers the three ways a list commits a choice (native select change, a list
that chooses on press, a searchable list that chooses on release), a choice
bound to a parameter and run with a value the demonstration never used, a
missing option that must stop the run before the save, and the confirm()
before the save, which recording must keep and replay must answer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from benchmark.choice_widgets.app import ChoiceApp, Server
from benchmark.choice_widgets.measure import _record, _replay

pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(900)


@pytest.fixture(scope="module")
def server() -> Iterator[Server]:
    srv = ChoiceApp().serve()
    yield srv
    srv.close()


_BUNDLES: dict[str, Path] = {}


def _bundle(server: Server, kind: str, root: Path) -> Path:
    if kind not in _BUNDLES:
        bundle, status = _record(server, kind, root / kind)
        assert bundle is not None, status
        _BUNDLES[kind] = bundle
    return _BUNDLES[kind]


def _run(
    server: Server,
    kind: str,
    bundle: Path,
    params: dict[str, str],
    query: str,
    run_dir: Path,
) -> tuple[bool, str, list[dict[str, Any]]]:
    before = len(server.app.records)
    ok, detail = _replay(server, kind, bundle, query, params, run_dir)
    return ok, detail, server.app.records[before:]


@pytest.mark.parametrize("kind", ["native", "div_dropdown", "select2"])
def test_a_choice_compiles_to_one_parameterized_step_and_answers_the_confirm(
    server: Server, tmp_path_factory: pytest.TempPathFactory, kind: str
) -> None:
    from openadapt_flow.ir import ActionKind, Workflow

    bundle = _bundle(server, kind, tmp_path_factory.mktemp("rec"))
    workflow = Workflow.load(bundle)
    choices = [s for s in workflow.steps if s.choice is not None]
    assert len(choices) == 1
    assert choices[0].action is ActionKind.SELECT_OPTION
    assert choices[0].param == "department"
    assert workflow.params["department"] == "Cardiology"
    assert workflow.steps[-1].dialog is not None
    assert workflow.steps[-1].dialog.response == "accept"

    ok, detail, records = _run(
        server,
        kind,
        bundle,
        {"patient_name": f"Held out {kind}", "department": "Orthopaedics"},
        "order=rev&delay=600",
        tmp_path_factory.mktemp("run"),
    )
    assert ok, detail
    assert [(r["patient_name"], r["department"]) for r in records] == [
        (f"Held out {kind}", "ORTH")
    ]


def test_a_missing_option_stops_before_the_save(
    server: Server, tmp_path_factory: pytest.TempPathFactory
) -> None:
    bundle = _bundle(server, "select2", tmp_path_factory.mktemp("rec"))
    ok, detail, records = _run(
        server,
        "select2",
        bundle,
        {"patient_name": "Missing option", "department": "Cardiology"},
        "missing=CARD",
        tmp_path_factory.mktemp("run"),
    )
    assert not ok
    assert "choice_missing" in detail
    assert "Cardiology" not in detail
    assert records == []


def test_an_unexpected_dialog_stops_the_run(
    server: Server, tmp_path_factory: pytest.TempPathFactory
) -> None:
    # The page asks a question the demonstration never saw; the backend
    # dismisses it and the run stops instead of saving anyway.
    bundle = _bundle(server, "native", tmp_path_factory.mktemp("rec"))
    ok, detail, records = _run(
        server,
        "native",
        bundle,
        {"patient_name": "Surprise", "department": "Cardiology"},
        "surprise=confirm",
        tmp_path_factory.mktemp("run"),
    )
    assert not ok
    assert "dialog_unexpected" in detail or "choice_unverified" in detail
    assert records == []


def test_the_persons_own_dialog_answer_is_recorded(
    server: Server, tmp_path: Path
) -> None:
    # The person answers "Cancel". The recorder never answers a dialog
    # itself; it reports the answer the page received, so the step keeps
    # "dismiss", not a default.
    import json

    from openadapt_flow.interactive_recorder import record_interactive

    def drive(page: Any, pump: Any) -> None:
        page.context.on("dialog", lambda dialog: dialog.dismiss())
        page.wait_for_selector("#patient_name", state="visible")
        page.locator("#patient_name").click()
        for _ in range(10):
            pump()
        page.keyboard.type("Person Answer")
        for _ in range(10):
            pump()
        page.locator("#save").click()
        for _ in range(40):
            pump()

    before = len(server.app.records)
    rec = record_interactive(
        f"{server.base}/w/native", tmp_path / "rec", headless=True, script=drive
    )
    events = [
        json.loads(line) for line in (rec / "events.jsonl").read_text().splitlines()
    ]
    dialogs = [e["dialog"] for e in events if e.get("dialog")]
    assert dialogs == [{"response": "dismiss", "message": "Register this patient?"}]
    assert json.loads((rec / "meta.json").read_text())["dialogs_captured"] is True
    assert server.app.records[before:] == []


def test_calendar_effects_need_explicit_fixture_risk_review(
    server: Server, tmp_path: Path
) -> None:
    from openadapt_flow.ir import Workflow

    refused, detail = _record(server, "datepicker", tmp_path / "unreviewed")
    assert refused is None
    assert detail.startswith("compile_refused: SectionsError:")
    bundle, detail = _record(
        server, "datepicker", tmp_path / "reviewed", review_fixture_choices=True
    )
    assert bundle is not None, detail
    choices = [step for step in Workflow.load(bundle).steps if step.choice is not None]
    assert choices
    assert all(not step.risk_review_required for step in choices)
    assert all(
        "operator-qualified override" in step.risk_explanation for step in choices
    )
