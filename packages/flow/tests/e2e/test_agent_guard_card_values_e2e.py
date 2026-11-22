"""The approval card shows what the held save sends, or says it can't.

The end-to-end finding this pins: with ``approval: each_item`` and no write
adapter, the card showed the task's intended note as the value saved and said
"This is exactly what OpenAdapt will save", while the agent had typed another
note. The person approved and the wrong note landed.

A scripted Playwright agent (``benchmark/agent_guard/agent.py``) drives its
own headless browser; ``openadapt-flow guard`` runs in another process,
attached over CDP, and holds the save for a person. The person here reads the
card through Flow's console (``save_approval_detail``, what the phone shows)
and answers with a registered test approver key.

* No write adapter, the agent types another note: the card says the values
  were not read and not checked, shows the task's note beside ``not read``,
  and never says "exactly". Approved anyway, the read-back catches it.
* MockMed's write adapter, the right note: the card shows the note read from
  the held request, marks it checked, and flags no difference.
* MockMed's write adapter, the agent types another note: the guard blocks the
  save before any card is shown.

Synthetic MockMed in front of the ``effect_e2e`` record store; localhost only.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
from typing import Any, Optional

import pytest

pytest.importorskip("playwright")

from benchmark.agent_guard import run as bench  # noqa: E402
from benchmark.agent_guard.app import serve  # noqa: E402
from tests import _approver_keys as ak  # noqa: E402

pytestmark = pytest.mark.timeout(600)

ADAPTER = "tests._teach_scope:EncounterWrites"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _scenario(name: str) -> bench.Scenario:
    return next(s for s in bench.SCENARIOS if s.name == name)


def _chromium_or_skip() -> None:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as pw:
            pw.chromium.launch(headless=True).close()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"chromium unavailable: {exc}")


@pytest.fixture
def signer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
    from openadapt_flow.jit.authority import KEY_PASSPHRASE_ENV
    from openadapt_flow.jit.lifecycle import KEY_DIR_ENV

    key_dir = tmp_path / "approver-keys"
    monkeypatch.setenv(KEY_DIR_ENV, str(key_dir))
    monkeypatch.setenv(KEY_PASSPHRASE_ENV, ak.PASSPHRASE)
    _, sign = ak.provision(key_dir)
    monkeypatch.setenv(ROOT_ENV, ak.root_of(key_dir))
    return sign


def _files(app: Any, work: Path, *, adapter: Optional[str]) -> dict[str, Path]:
    files = bench.write_setup(work, app.base_url, teach_dir=None)
    extra = "approval: each_item\n"
    if adapter:
        extra += f"write_adapter: {adapter}\n"
        # The adapter attributes a write to ``patient_id`` only.
        task = json.loads(files["task"].read_text())
        task["expected_record"] = {"patient_id": "p1"}
        files["task"].write_text(json.dumps(task))
    files["guard"].write_text(files["guard"].read_text() + extra)
    return files


class _Person(threading.Thread):
    """Waits for the held save, reads its card as the phone would, answers."""

    def __init__(self, work: Path, decision: str, signer: Any) -> None:
        super().__init__(daemon=True)
        self.work = work
        self.decision = decision
        self.signer = signer
        self.stop = threading.Event()
        self.shown: Optional[dict[str, Any]] = None
        self.frame: Optional[bytes] = None
        self.error: Optional[str] = None

    def run(self) -> None:
        from openadapt_flow.approval_mode.item_approval import (
            ItemApprovalStore,
            make_answer,
        )
        from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE
        from openadapt_flow.console.save_approvals import (
            save_approval_detail,
            save_approval_frame,
        )

        requests: list[Path] = []
        while not self.stop.is_set():
            requests = list(self.work.rglob("item-approval/request-*.json"))
            if requests:
                break
            time.sleep(0.1)
        if not requests:
            return
        run_dir = requests[0].parent.parent
        request_id = requests[0].stem.split("request-", 1)[1]
        try:
            self.shown = save_approval_detail(run_dir.parent, request_id)[
                "presentation"
            ]
            self.frame = save_approval_frame(run_dir.parent, request_id)
            store = ItemApprovalStore(run_dir)
            store.write_answer(
                make_answer(
                    store.read_request(request_id),
                    decision=self.decision,
                    decided_by="human",
                    route=ITEM_APPROVAL_ROUTE,
                    approver_id=None,
                    auth_method="none",
                    policy=store.context(),
                    signer=self.signer,
                )
            )
        except Exception as exc:  # noqa: BLE001 - reported by the test
            self.error = repr(exc)


def _run(app: Any, scenario: str, work: Path, files: dict, person: _Person) -> dict:
    person.start()
    try:
        return bench.run_one(
            app,
            _scenario(scenario),
            "guard",
            work,
            _port(),
            think_s=0.3,
            files=files,
            teach=False,
        )
    finally:
        person.stop.set()
        person.join(timeout=60)


def test_the_card_never_says_exactly_when_it_could_not_read_the_values(
    tmp_path: Path, signer: Any
) -> None:
    _chromium_or_skip()
    app = serve(tmp_path / "app.sqlite")
    try:
        # 1. No write adapter, the agent types another note.
        work = tmp_path / "no-adapter-wrong"
        files = _files(app, tmp_path / "setup-a", adapter=None)
        person = _Person(work, "approve", signer)
        result = _run(app, "agent_wrong_value", work, files, person)
        assert person.error is None, person.error
        shown = person.shown
        assert shown is not None, result
        assert shown["values_checked"] is False
        assert "exactly" not in shown["explanation"].lower()
        assert "not checked before saving" in shown["explanation"]
        by_label = {f["label"]: f for f in shown["fields"]}
        # Never the task's note standing in for the one the agent typed.
        assert by_label["note"]["after"] == "not read"
        assert bench.NOTE not in json.dumps(shown["fields"])
        sources = {r["label"]: r for r in shown["sources"]}
        assert sources["note"]["intended"] == bench.NOTE
        assert "values this save sends could not be read" in shown["step_intent"]
        # What a careful person checks instead: the screen at the hold. The
        # note the agent typed is read from the form and flagged; the
        # encounter type is a button group, not a form value, so it is not
        # read and the screenshot covers it.
        assert sources["note"]["screen"] == bench.OTHER_NOTE
        assert sources["note"]["screen_differs"] is True
        assert sources["type"]["screen"] is None
        assert "values on screen" in shown["explanation"]
        from openadapt_flow.agent_guard.screen_values import readable_png

        assert readable_png(person.frame)
        # Approved anyway: the read-back still catches the other note.
        assert result["verdict"] == "REFUTED", result

        # 2. MockMed's write adapter, the right note: read from the request.
        work = tmp_path / "adapter-ok"
        files = _files(app, tmp_path / "setup-b", adapter=ADAPTER)
        person = _Person(work, "approve", signer)
        result = _run(app, "ok", work, files, person)
        assert person.error is None, person.error
        shown = person.shown
        assert shown is not None, result
        assert shown["values_checked"] is True
        by_label = {f["label"]: f for f in shown["fields"]}
        assert by_label["note"]["after"] == bench.NOTE
        assert by_label["type"]["after"] == "Triage"
        sources = {r["label"]: r for r in shown["sources"]}
        assert sources["note"]["intended"] == bench.NOTE
        assert sources["note"]["held_differs"] is False
        assert result["verdict"] == "VERIFIED", result

        # 3. MockMed's write adapter, another note: blocked before any card.
        work = tmp_path / "adapter-wrong"
        files = _files(app, tmp_path / "setup-c", adapter=ADAPTER)
        person = _Person(work, "approve", signer)
        result = _run(app, "agent_wrong_value", work, files, person)
        assert person.shown is None
        assert not list(work.rglob("item-approval/request-*.json"))
        assert result["verdict"] is None, result
        assert result["blocked"] == ["write_value_not_declared"], result
    finally:
        app.stop()


_BLIND = """
from openadapt_flow.agent_guard import cdp

cdp.CdpGuardPage.screenshot = lambda self: None
cdp.CdpGuardPage.form_controls = lambda self: None
"""


def test_no_values_and_no_screenshot_offers_no_approval(
    tmp_path: Path, signer: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard's screenshot and form read both fail (patched in the guard's
    own process), and there is no write adapter: nobody could see what would
    be approved, so no approval is asked and nothing is written."""

    import os

    _chromium_or_skip()
    patch = tmp_path / "blind"
    patch.mkdir()
    (patch / "sitecustomize.py").write_text(_BLIND)
    monkeypatch.setenv(
        "PYTHONPATH", os.pathsep.join([str(patch), os.environ.get("PYTHONPATH", "")])
    )
    app = serve(tmp_path / "app.sqlite")
    try:
        work = tmp_path / "blind-run"
        files = _files(app, tmp_path / "setup", adapter=None)
        person = _Person(work, "approve", signer)
        result = _run(app, "agent_wrong_value", work, files, person)
        assert person.shown is None, person.shown
        assert not list(work.rglob("item-approval/request-*.json"))
        assert result["verdict"] is None, result
        assert result["blocked"] == ["values_unread"], result
        assert result["writes"] == 0, result
        row = json.loads((work / "guard_ledger_row.json").read_text())
        assert (row["verdict"], row["verdict_reason"]) == ("ESCALATED", "values_unread")
    finally:
        app.stop()
