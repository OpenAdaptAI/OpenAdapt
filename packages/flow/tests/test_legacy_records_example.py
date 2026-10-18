"""The legacy-records example: intake, exceptions, decisions, grades, exchanges.

The desktop part needs an X display and runs only with
OPENADAPT_LEGACY_RECORDS_DEMO=1 (see examples/legacy_records/README.md).
"""

from __future__ import annotations

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any, Callable, Optional

import pytest

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "legacy_records"
sys.path.insert(0, str(EXAMPLE))

import ap_invoice  # noqa: E402
import evidence  # noqa: E402
import make_inbox  # noqa: E402
import pipeline  # noqa: E402
import referral_triage  # noqa: E402
from x11 import DriverHalt, Frame, OcrLine  # noqa: E402

from openadapt_flow.interchange import check_file, emit_exchange  # noqa: E402


@pytest.fixture()
def inbox(tmp_path: Path) -> Path:
    make_inbox.main(tmp_path)
    return tmp_path


def _people() -> pipeline.People:
    return pipeline.People(
        "intake_staff:t-staff",
        "physician:t-doc",
        {
            "corrections": {"referral-0007.pdf": {"subject_id": "TEST-0000-0007"}},
            "decisions": {"referral-0007.pdf": "urgent"},
        },
    )


def test_intake_flags_the_look_alike_id_and_staff_correct_it(inbox: Path) -> None:
    logs: list[str] = []
    run = inbox / "run"
    ex = pipeline.intake(
        inbox / "fax_inbox" / "referral-0007.pdf",
        referral_triage.SPECS,
        run,
        workflow="t",
        artifact={
            "source_type": "fax_folder",
            "file_number": "x",
            "date": "2026-10-29",
            "doctype": "referral",
        },
        people=_people(),
        staff_role="intake_staff",
        log=logs.append,
    )
    sid = next(f for f in ex["fields"] if f["name"] == "subject_id")
    assert sid["value"] == "TEST-0000-0007"
    assert sid["status"] == "corrected"
    assert sid["confirmed_by"]["id"] == "intake_staff:t-staff"
    assert sid["reads"][0]["value"] == "TEST-OOOO-0007"
    assert any("exception: subject_id" in m for m in logs)

    pipeline.decide(
        ex,
        _people(),
        question="Priority?",
        options=["urgent", "routine"],
        role="physician",
        output_field="priority",
        log=logs.append,
    )
    path = evidence.write_exchange(evidence.seal(ex, run), run)
    assert check_file(path) == []
    refs = emit_exchange(path, inbox / "l1")
    assert refs[0].path.name == "x_2026-10-29_referral.pdf"


def test_one_person_cannot_confirm_and_approve(tmp_path: Path) -> None:
    answers = tmp_path / "a.json"
    answers.write_text('{"staff": "p:same", "approver": "p:same"}')
    with pytest.raises(SystemExit, match="cannot also approve"):
        pipeline.People.load(answers, "intake_staff", "physician")


def _frame() -> Frame:
    return Frame(b"\x89PNG not decoded here", [])


def test_grades_follow_the_reading_rule() -> None:
    line = OcrLine("REC-1", (1, 1, 5, 5), 0.97)
    kw = dict(
        expected="REC-1",
        clipboard="REC-1",
        ocr_line=None,
        frame=_frame(),
        artifact_id="a",
        screen="s",
    )
    single = evidence.read_field("r", "code", **kw, session="reopened")
    assert single["grade"] == "unchecked"

    frame = _frame()
    frame.crop_sha256 = lambda bbox: "0" * 64  # type: ignore[method-assign]
    kw.update(ocr_line=line, frame=frame)
    assert (
        evidence.read_field("r", "code", **kw, session="acting")["grade"]
        == "same_screen"
    )
    # Two channels of the reopened record, with no read in the acting session,
    # are not a reopened read-back.
    assert (
        evidence.read_field("r", "code", **kw, session="reopened")["grade"]
        == "same_screen"
    )
    assert (
        evidence.read_field(
            "r", "code", **kw, session="reopened", acting=[("clipboard", "REC-1")]
        )["grade"]
        == "reopened"
    )
    # An acting read that saw something else gives no grade at all.
    assert (
        evidence.read_field(
            "r", "code", **kw, session="reopened", acting=[("clipboard", "REC-2")]
        )["grade"]
        == "unchecked"
    )
    assert (
        evidence.read_field(
            "r",
            "code",
            **kw,
            session="reopened",
            exports=[("REC-1", "committed_store")],
        )["grade"]
        == "system_of_record"
    )


def test_an_export_from_the_live_session_is_not_system_of_record() -> None:
    line = OcrLine("REC-1", (1, 1, 5, 5), 0.97)
    frame = _frame()
    frame.crop_sha256 = lambda bbox: "0" * 64  # type: ignore[method-assign]
    f = evidence.read_field(
        "r",
        "code",
        expected="REC-1",
        clipboard="REC-1",
        ocr_line=line,
        frame=frame,
        artifact_id="a",
        screen="s",
        session="acting",
        exports=[("REC-1", "live_session")],
    )
    assert f["grade"] == "same_screen"
    assert f["reads"][-1]["source_kind"] == "live_session"
    assert f["reads"][-1]["session"] == "acting"


def test_a_lost_write_is_reported_as_lost_not_as_disagreement() -> None:
    f = evidence.read_field(
        "p",
        "enum",
        expected="urgent",
        clipboard=None,
        ocr_line=None,
        frame=_frame(),
        session="reopened",
        artifact_id="a",
        screen="s",
    )
    assert f["needs_review"] and "wrote 'urgent'" in f["review_reason"]


@pytest.mark.skipif(
    os.environ.get("OPENADAPT_LEGACY_RECORDS_DEMO") != "1",
    reason="needs Xvfb, xdotool, xclip and ImageMagick",
)
def test_demo_end_to_end(tmp_path: Path) -> None:
    for tool in ("Xvfb", "xdotool", "xclip", "import"):
        assert shutil.which(tool), tool
    env = dict(
        os.environ,
        FLOW_PYTHON=sys.executable,
        APP_PYTHON=os.environ.get("APP_PYTHON", "python3"),
    )
    out = subprocess.run(
        [str(EXAMPLE / "demo.sh"), str(tmp_path / "demo")],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout
    assert "referral-0009.pdf: halted" in out.stdout
    assert "referral-0008.pdf: halted" in out.stdout  # the dropped save
    assert "invoice-INV-20419.pdf: halted" in out.stdout


# -- headless runs of the referral template ---------------------------------
#
# The mock EMR's own logic runs against an in-memory stand-in for its Tk
# shell, and a stand-in driver presses its keys. No display is needed.


class HeadlessApp:
    """The screens of ``apps/_tkapp.App`` without a window."""

    def __init__(self, name: str, **_: Any) -> None:
        self.name = name
        self.title = ""
        self.lines: list[str] = []
        self.binds: dict[str, Callable[[], None]] = {}
        self.on_enter: Optional[Callable[[str], None]] = None
        self.on_save: Optional[Callable[[dict[str, str]], None]] = None
        self.labels: list[str] = []
        self.values: list[str] = []

    def _show(self, title: str) -> None:
        self.title, self.lines, self.on_enter, self.on_save = title, [], None, None

    def text_screen(self, title: str, lines: list[str], hint: str = "") -> None:
        self._show(title)
        self.lines = list(lines)

    def form(self, title: str, labels: list[str], on_save, hint: str = "") -> None:
        self._show(title)
        self.on_save, self.labels, self.values = on_save, labels, [""]

    def prompt(self, title: str, label: str, on_enter) -> None:
        self._show(title)
        self.on_enter, self.values = on_enter, [""]

    def set_status(self, text: str) -> None:
        pass

    def bind(self, seq: str, fn: Callable[[], None]) -> None:
        self.binds[seq] = fn


class _Frame(Frame):
    def crop_sha256(self, bbox: tuple[int, int, int, int]) -> str:
        return "0" * 64


class HeadlessDriver:
    """Stands in for ``x11.X11App``: keys, typing, title, clipboard, OCR."""

    KEYS = {
        "Escape": "<Escape>",
        "ctrl+n": "<Control-n>",
        "ctrl+o": "<Control-o>",
        "ctrl+t": "<Control-t>",
        "ctrl+b": "<Control-b>",
        "ctrl+e": "<Control-e>",
    }

    def __init__(self, app: HeadlessApp, fail_on: Optional[str] = None) -> None:
        self.app = app
        self.fail_on = fail_on

    def key(self, *keys: str) -> None:
        a = self.app
        for k in keys:
            if k == "Return" and a.on_enter:
                a.on_enter(a.values[0])
            elif k == "Tab" and a.on_save:
                a.values.append("")
            elif k == "ctrl+s" and a.on_save:
                a.on_save(dict(zip(a.labels, a.values)))
            elif k in self.KEYS and self.KEYS[k] in a.binds:
                a.binds[self.KEYS[k]]()

    def type(self, text: str) -> None:
        self.app.values[-1] += text

    def wait_screen(self, pattern: str) -> re.Match[str]:
        if self.fail_on and pattern.startswith(self.fail_on):
            raise DriverHalt(f"expected screen {pattern!r}, saw 'Not responding'")
        m = re.fullmatch(pattern, self.app.title)
        if not m:
            raise DriverHalt(f"expected screen {pattern!r}, saw {self.app.title!r}")
        return m

    def fill(self, values: list[str]) -> None:
        for i, v in enumerate(values):
            if i:
                self.key("Tab")
            self.type(v)

    def copy_pane(self) -> str:
        if not self.app.lines:
            raise DriverHalt("clipboard stayed empty after copy")
        return "\n".join(self.app.lines)

    def frame(self) -> Frame:
        lines = [
            OcrLine(t, (0, 20 * i, 400, 18), 0.97) for i, t in enumerate(self.app.lines)
        ]
        return _Frame(b"not a png", lines)


@pytest.fixture()
def mock_emr(monkeypatch: pytest.MonkeyPatch):
    fake = types.ModuleType("_tkapp")
    fake.App = HeadlessApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_tkapp", fake)
    monkeypatch.syspath_prepend(str(EXAMPLE / "apps"))
    monkeypatch.delitem(sys.modules, "mock_emr", raising=False)
    return importlib.import_module("mock_emr")


def _emr(mock_emr, db: Path, *, drop_save: bool = False, fail_on=None):
    if not db.exists():
        con = mock_emr.connect(db)
        con.executemany("INSERT OR REPLACE INTO chart VALUES (?,?,?,?)", mock_emr.SEED)
        con.commit()
    return HeadlessDriver(mock_emr.MockEmr(db, drop_save).app, fail_on)


def _scripted(decisions: dict[str, str], corrections=None) -> pipeline.People:
    return pipeline.People(
        "intake_staff:t-staff",
        "physician:t-doc",
        {
            "corrections": corrections
            if corrections is not None
            else {"referral-0007.pdf": {"subject_id": "TEST-0000-0007"}},
            "decisions": decisions,
        },
    )


def _triage(driver, fax: Path, people, run: Path) -> tuple[str, dict[str, Any]]:
    run.mkdir(parents=True, exist_ok=True)
    logs: list[str] = []
    outcome = referral_triage.triage_one(
        driver, fax, people, run, run / "l1", logs.append
    )
    result = json.loads((run / f"{fax.stem}-result.json").read_text())
    assert check_file(run / f"{fax.stem}-result.json") == []
    assert check_file(run / f"{fax.stem}.json") == []
    return outcome, result


def _field(ex: dict[str, Any], name: str) -> dict[str, Any]:
    return next(f for f in ex["fields"] if f["name"] == name)


def test_new_chart_is_committed_with_grades_it_can_prove(inbox: Path, mock_emr) -> None:
    fax = inbox / "fax_inbox" / "referral-0007.pdf"
    outcome, res = _triage(
        _emr(mock_emr, inbox / "emr.sqlite"),
        fax,
        _scripted({"referral-0007.pdf": "urgent"}),
        inbox / "run",
    )
    assert outcome == "committed" and res["outcome"] == "committed"
    assert _field(res, "record_id")["grade"] == "reopened"
    assert _field(res, "subject_id")["grade"] == "reopened"
    # Typed into a form, then read only after reopening: two channels, no
    # acting read of the saved value.
    assert _field(res, "priority")["grade"] == "same_screen"
    assert _field(res, "triage_entries")["value"] == 1
    match = res["identities"][0]["match"]
    assert match["status"] == "new"
    assert match["record_ref"] == _field(res, "chart_id")["value"]


def test_rerun_against_a_dropped_save_halts(inbox: Path, mock_emr) -> None:
    """The same fax again, into an EMR that says Saved and keeps nothing: the
    earlier entry with the same values is on screen, but the count is not."""
    fax = inbox / "fax_inbox" / "referral-0008.pdf"
    people = _scripted({"referral-0008.pdf": "routine"})
    db = inbox / "emr.sqlite"
    first, _ = _triage(_emr(mock_emr, db), fax, people, inbox / "run1")
    assert first == "committed"

    again, res = _triage(
        _emr(mock_emr, db, drop_save=True), fax, people, inbox / "run2"
    )
    assert again == "halted"
    assert res["outcome"] == "halted"
    assert "1 triage entries before the save and 1 after" in res["halt_reason"]
    assert not [f for f in res["fields"] if f.get("needs_review")]
    assert (inbox / "run2" / "review_queue" / "referral-0008-result.json").exists()


@pytest.mark.parametrize("review_decisions", [False, True])
def test_a_decline_is_no_action_with_a_read_back(
    inbox: Path, mock_emr, review_decisions: bool
) -> None:
    fax = inbox / "fax_inbox" / "referral-0008.pdf"
    people = _scripted({"referral-0008.pdf": "decline"})
    people.review_decisions = review_decisions
    outcome, res = _triage(
        _emr(mock_emr, inbox / "emr.sqlite"), fax, people, inbox / "run"
    )
    assert outcome == "declined"
    assert res["outcome"] == "no_action" and "halt_reason" not in res
    na = res["no_action"]
    assert na["reason_code"] == "declined"
    assert na["decided_by"]["id"] == "physician:t-doc"
    assert na["required_grade"] == referral_triage.COMMIT_GRADE
    assert na["evidence"] == ["search_matches", "triage_entries"]
    assert _field(res, "triage_entries")["grade"] == "reopened"
    assert res["identities"][0]["match"]["status"] == "existing"
    queued = (inbox / "run" / "review_queue" / "referral-0008-result.json").exists()
    assert queued == review_decisions


def test_a_decline_with_no_chart_reads_back_the_search(inbox: Path, mock_emr) -> None:
    fax = inbox / "fax_inbox" / "referral-0007.pdf"
    outcome, res = _triage(
        _emr(mock_emr, inbox / "emr.sqlite"),
        fax,
        _scripted({"referral-0007.pdf": "decline"}),
        inbox / "run",
    )
    assert outcome == "declined" and res["outcome"] == "no_action"
    assert res["no_action"]["evidence"] == ["search_matches"]
    assert _field(res, "search_matches")["grade"] == "same_screen"
    assert res["identities"][0]["match"]["status"] == "new"


def test_a_screen_halt_after_the_save_writes_a_halted_result(
    inbox: Path, mock_emr
) -> None:
    fax = inbox / "fax_inbox" / "referral-0008.pdf"
    outcome, res = _triage(
        _emr(mock_emr, inbox / "emr.sqlite", fail_on="Saved"),
        fax,
        _scripted({"referral-0008.pdf": "routine"}),
        inbox / "run",
    )
    assert outcome == "halted"
    assert "after the save keystroke" in res["halt_reason"]
    assert (inbox / "run" / "review_queue" / "referral-0008-result.json").exists()


def test_an_unsettled_scripted_exception_is_not_confirmed(
    inbox: Path, mock_emr
) -> None:
    fax = inbox / "fax_inbox" / "referral-0007.pdf"
    outcome, res = _triage(
        _emr(mock_emr, inbox / "emr.sqlite"),
        fax,
        _scripted({"referral-0007.pdf": "urgent"}, corrections={}),
        inbox / "run",
    )
    assert outcome == "halted" and "no person settled" in res["halt_reason"]
    req = json.loads((inbox / "run" / "referral-0007.json").read_text())
    sid = _field(req, "subject_id")
    assert sid.get("status", "proposed") == "proposed"
    assert "confirmed_by" not in sid
    assert sid["needs_review"]
    assert not req["decisions"]


def test_a_scripted_decision_must_be_an_option() -> None:
    people = _scripted({"x.pdf": "pay_twice"})
    with pytest.raises(SystemExit, match="not one of"):
        people.decide("x.pdf", "Priority?", ["urgent", "routine"])
    with pytest.raises(SystemExit, match="not one of"):
        people.decide("missing.pdf", "Priority?", ["urgent", "routine"])


@pytest.mark.parametrize("target", [".", "/", "~", "home", "nonempty"])
def test_demo_refuses_an_output_folder_it_did_not_make(
    tmp_path: Path, target: str
) -> None:
    keep = tmp_path / "nonempty"
    keep.mkdir()
    (keep / "keep.txt").write_text("x")
    arg = {"home": str(Path.home()), "nonempty": str(keep)}.get(target, target)
    out = subprocess.run(
        ["bash", str(EXAMPLE / "demo.sh"), arg],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 2, out.stderr
    assert "refusing" in out.stderr
    assert (keep / "keep.txt").exists()


@pytest.fixture()
def mock_ledger(monkeypatch: pytest.MonkeyPatch):
    fake = types.ModuleType("_tkapp")
    fake.App = HeadlessApp  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_tkapp", fake)
    monkeypatch.syspath_prepend(str(EXAMPLE / "apps"))
    monkeypatch.delitem(sys.modules, "mock_ledger", raising=False)
    ledger = importlib.import_module("mock_ledger")
    monkeypatch.setattr(
        ap_invoice,
        "scheduled_export",
        lambda _py, db, out: ledger.export_bills(
            ledger.connect(db), out / "scheduled" / "bills.csv"
        ),
    )
    return ledger


@pytest.mark.parametrize("choice", ["approve", "hold", "reject"])
def test_invoice_posting_conforms(inbox: Path, mock_ledger, choice: str) -> None:
    db, exports, run = inbox / "ledger.sqlite", inbox / "exports", inbox / "run"
    con = mock_ledger.connect(db)
    con.executemany("INSERT OR REPLACE INTO supplier VALUES (?,?,?)", mock_ledger.SEED)
    con.commit()
    driver = HeadlessDriver(mock_ledger.MockLedger(db, exports, False).app)
    people = pipeline.People(
        "ap_clerk:t-clerk",
        "controller:t-controller",
        {"corrections": {}, "decisions": {"invoice-INV-20418.pdf": choice}},
    )
    run.mkdir()
    inv = inbox / "invoice_inbox" / "invoice-INV-20418.pdf"
    logs: list[str] = []
    outcome = ap_invoice.post_one(
        driver, inv, people, run, run / "l1", exports, db, "python3", logs.append
    )
    path = run / "invoice-INV-20418-result.json"
    assert check_file(path) == [], logs
    res = json.loads(path.read_text())
    assert res["identities"][0]["match"]["record_ref"] == "V-0077"
    if choice == "approve":
        assert outcome == "committed"
        assert _field(res, "amount")["grade"] == "system_of_record"
    else:
        assert outcome == choice and res["outcome"] == "no_action"
        na = res["no_action"]
        assert na["reason_code"] == {"hold": "held", "reject": "rejected"}[choice]
        assert na["required_grade"] == ap_invoice.COMMIT_GRADE == "system_of_record"
        assert _field(res, "supplier_bills")["grade"] == "system_of_record"
        assert not (run / "review_queue").exists()
