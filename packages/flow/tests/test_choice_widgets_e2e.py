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
    assert workflow.steps[-1].dialog.kind == "confirm"

    ok, detail, records = _run(
        server,
        kind,
        bundle,
        {"patient_name": f"Held out {kind}", "department": "Orthopaedics"},
        "order=rev&delay=600",
        tmp_path_factory.mktemp("run"),
    )
    if kind == "div_dropdown":
        # The plain div exposes no label-to-code map for an unseen label.
        # The recorded code is valid only for the demonstrated label.
        assert not ok
        assert "choice_unverified" in detail
        assert records == []
        return
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
    assert "choice_unverified" in detail
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
    assert dialogs == [
        {"response": "dismiss", "message": "Register this patient?", "type": "confirm"}
    ]
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


@pytest.mark.parametrize(
    "mode",
    [
        "declared_select",
        "reflected_native_value",
        "reflected_custom_value",
        "reflected_dialog",
    ],
)
def test_choice_and_dialog_metadata_never_persists_declared_secrets(
    server: Server, tmp_path: Path, mode: str
) -> None:
    from openadapt_flow.interactive_recorder import (
        BrowserAttachError,
        record_interactive,
    )

    secret = "-".join(("synthetic", "private", "sentinel", "539d8c"))

    def drive(page: Any, pump: Any) -> None:
        page.wait_for_selector("#patient_name")
        if mode == "declared_select":
            page.evaluate(
                "s => document.querySelector('#department option[value=CARD]').value = s",
                secret,
            )
        else:
            page.evaluate(
                """s => {
              const field = document.createElement('input'); field.type = 'password';
              field.name = 'auth'; field.value = s; document.body.appendChild(field);
            }""",
                secret,
            )
            if mode == "reflected_native_value":
                page.evaluate(
                    "s => document.querySelector('#department option[value=CARD]').value = s",
                    secret,
                )
        for _ in range(15):
            pump()
        if mode == "reflected_custom_value":
            page.locator(".dd").click()
            for _ in range(15):
                pump()
            page.evaluate(
                """s => Array.from(document.querySelectorAll('.dd-item'))
              .find(el => el.textContent === 'Cardiology').setAttribute('data-value', s)""",
                secret,
            )
            page.locator(".dd-item", has_text="Cardiology").click()
        elif mode == "reflected_dialog":
            page.context.on("dialog", lambda dialog: dialog.accept())
            page.evaluate(
                """s => {
              const button = document.createElement('button'); button.id = 'warn';
              button.textContent = 'Show warning'; button.onclick = () => confirm(s);
              document.body.appendChild(button);
            }""",
                secret,
            )
            page.locator("#warn").click()
        else:
            # Native change capture follows a person's trusted keyboard
            # selection; programmatic select_option events are ignored.
            page.locator("#department").click()
            for _ in range(10):
                pump()
            page.keyboard.type("Cardiology")
            page.keyboard.press("Enter")
        for _ in range(30):
            pump()

    kind = "div_dropdown" if mode == "reflected_custom_value" else "native"
    with pytest.raises(BrowserAttachError, match="secret") as error:
        record_interactive(
            server.base + "/w/" + kind,
            tmp_path / "rec",
            headless=True,
            secret_fields=("department",) if mode == "declared_select" else (),
            script=drive,
        )
    assert secret not in str(error.value)
    for artifact in (tmp_path / "rec").rglob("*"):
        if artifact.suffix in (".json", ".jsonl"):
            assert secret not in artifact.read_text()


def test_a_foreign_document_dialog_does_not_borrow_the_pending_click(
    server: Server, tmp_path: Path
) -> None:
    from openadapt_flow.interactive_recorder import (
        BrowserAttachError,
        record_interactive,
    )

    def drive(page: Any, pump: Any) -> None:
        page.context.on("dialog", lambda dialog: dialog.accept())
        page.evaluate("""() => {
          const frame = document.createElement('iframe'); frame.id = 'foreign';
          frame.srcdoc = '<p>Another document</p>'; document.body.appendChild(frame);
          const button = document.createElement('button'); button.id = 'cross-dialog';
          button.textContent = 'Ask another document';
          button.onclick = () => frame.contentWindow.confirm('Synthetic confirmation');
          document.body.appendChild(button);
        }""")
        page.frame_locator("#foreign").locator("p").wait_for()
        for _ in range(15):
            pump()
        page.locator("#cross-dialog").click()
        for _ in range(30):
            pump()

    with pytest.raises(BrowserAttachError, match="document"):
        record_interactive(
            server.base + "/w/native", tmp_path / "rec", headless=True, script=drive
        )


@pytest.mark.parametrize("search_target", ["owned_popup", "unrelated_field"])
def test_search_folding_links_come_from_the_actual_input_owner(
    server: Server, tmp_path: Path, search_target: str
) -> None:
    import json

    from openadapt_flow.interactive_recorder import record_interactive

    def drive(page: Any, pump: Any) -> None:
        page.locator(".select2-selection").click()
        for _ in range(10):
            pump()
        if search_target == "owned_popup":
            # An extra click in the popup search is part of the same gesture.
            page.locator(".select2-search__field").click()
        else:
            # Keep the popup open while typing a matching substring elsewhere.
            # Temporal proximity and text overlap cannot establish ownership.
            page.locator("#patient_name").focus()
        for _ in range(10):
            pump()
        page.keyboard.type("Card")
        for _ in range(10):
            pump()
        page.locator(".select2-results__option", has_text="Cardiology").click()
        for _ in range(20):
            pump()

    rec = record_interactive(
        server.base + "/w/select2", tmp_path / "rec", headless=True, script=drive
    )
    events = [
        json.loads(line) for line in (rec / "events.jsonl").read_text().splitlines()
    ]
    opener = next(event for event in events if event.get("choice_gesture"))
    typed = next(event for event in events if event.get("kind") == "type")
    choice = next(event["choice"] for event in events if event.get("choice"))
    if search_target == "owned_popup":
        search_click = next(
            event for event in events if event.get("choice_search_gesture")
        )
        assert search_click["choice_search_gesture"] == opener["choice_gesture"]
        assert (
            typed["choice_gesture"]
            == opener["choice_gesture"]
            == choice["opening_gesture"]
        )
        assert (
            typed["input_session"]
            == search_click["input_session"]
            == choice["search_input_session"]
        )
    else:
        assert "choice_gesture" not in typed
        assert choice["search_input_session"] is None
        assert not any(event.get("choice_search_gesture") for event in events)
