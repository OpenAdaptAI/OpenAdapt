"""A clean screen after a halted item (docs/HALT_CLEANUP.md).

A scripted three-screen app drives the REAL Replayer: ``start`` (the item's
start screen, with "New Patient"), ``form`` (with "Create") and ``dialog``
(a confirm dialog with "Confirm" and "Cancel"). The form and the dialog both
have Cancel, which returns to the start screen; Escape does nothing in this
app. Vision answers from the app's
current screen, so every check reads what the screen shows.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from openadapt_flow.halt_cleanup import (
    CANCEL_WORDS,
    cleanup_sha256,
    derive_cleanup,
    qualify_cleanup,
    run_cleanup,
    runnable_cleanup,
    start_state_of,
    write_cleanup,
)
from openadapt_flow.ir import (
    ActionKind,
    HaltCleanup,
    HaltCleanupQualification,
    PredicateKind,
    Step,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from tests.test_replayer import FakeBackend, FakeVision, Match, click_step

SCREENS = {
    "start": ["New Patient"],
    "form": ["Create", "Cancel"],
    "dialog": ["Confirm", "Cancel"],
    "stuck": ["Something else"],
}
POINTS = {
    "New Patient": (20, 20),
    "Create": (40, 40),
    "Confirm": (60, 60),
    "Cancel": (80, 80),
    "Something else": (5, 5),
}
GOES_TO = {"New Patient": "form", "Create": "dialog", "Cancel": "start"}


class App:
    def __init__(self, screen: str = "start", *, cancel_works: bool = True):
        self.screen = screen
        self.cancel_works = cancel_works
        self.saves = 0


class AppBackend(FakeBackend):
    def __init__(self, app: App):
        super().__init__(viewport=(300, 200))
        self.app = app

    def click(self, x, y, *, double=False):
        super().click(x, y, double=double)
        for text, point in POINTS.items():
            if (x, y) == point and text in SCREENS[self.app.screen]:
                if text == "Cancel" and not self.app.cancel_works:
                    return
                if text == "Confirm":
                    self.app.saves += 1
                    return
                self.app.screen = GOES_TO.get(text, self.app.screen)
                return


class AppVision(FakeVision):
    def __init__(self, app: App):
        super().__init__()
        self.app = app

    def find_text(self, screen_png, text, **kwargs):
        if text in SCREENS[self.app.screen]:
            x, y = POINTS[text]
            return Match(point=(x, y), region=(x - 5, y - 5, 10, 10), confidence=0.9)
        return None


def _click(step_id: str, text: str, **kw) -> Step:
    step = click_step(step_id, ocr_text=text, **kw)
    # The click lands on the label, inside its locator region; a click point
    # outside the region is a marked-row offset to the resolver.
    x, y = POINTS[text]
    step.anchor.region = (x - 5, y - 5, 10, 10)
    step.anchor.click_point = (x, y)
    return step


def _workflow() -> Workflow:
    return Workflow(
        name="new-patient",
        steps=[
            _click("open_form", "New Patient"),
            _click("create", "Create"),
            _click("confirm", "Confirm", risk="irreversible"),
        ],
    )


def _cancel_recording(tmp_path: Path) -> Path:
    other = Workflow(
        name="cancel a new patient",
        steps=[_click("press_cancel", "Cancel")],
    )
    path = tmp_path / "cancel-recording"
    other.save(path)
    (path / "templates").mkdir(exist_ok=True)
    (path / "templates" / "btn.png").write_bytes(b"png")
    return path


def _replayer(app: App) -> Replayer:
    return Replayer(AppBackend(app), vision=AppVision(app), poll_interval_s=0.01)


# ---------------------------------------------------------------------------
# Derive
# ---------------------------------------------------------------------------


def test_derive_takes_the_start_screen_and_the_recordings_cancel(tmp_path):
    derived = derive_cleanup(_workflow(), recordings=[_cancel_recording(tmp_path)])
    cleanup = derived.cleanup
    assert cleanup.start_state.kind is PredicateKind.ANCHOR_RESOLVES
    assert cleanup.start_state.anchor.ocr_text == "New Patient"
    assert [s.action for s in cleanup.steps] == [ActionKind.KEY, ActionKind.CLICK]
    escape, cancel = cleanup.steps
    assert escape.key == "Escape"
    assert cancel.anchor.ocr_text == "Cancel"
    assert cancel.guard.on_unmet == "skip"  # only when Cancel is on screen
    assert cancel.anchor.template.startswith("templates/cleanup/cancel-recording/")
    assert list(derived.assets) == [cancel.anchor.template]
    # The save is never a cleanup step.
    assert all("Confirm" not in (s.intent or "") for s in cleanup.steps)


def test_a_cleanup_can_only_navigate():
    start = start_state_of(_workflow())
    with pytest.raises(ValueError, match="could save"):
        HaltCleanup(
            steps=[_click("x", "Confirm", risk="irreversible")], start_state=start
        )
    with pytest.raises(ValueError, match="needs a guard"):
        HaltCleanup(steps=[_click("x", "Cancel")], start_state=start)
    with pytest.raises(ValueError, match="types"):
        HaltCleanup(
            steps=[Step(id="t", intent="type", action=ActionKind.TYPE, text="x")],
            start_state=start,
        )


# ---------------------------------------------------------------------------
# Run after a halt
# ---------------------------------------------------------------------------


def _cleanup(tmp_path: Path) -> HaltCleanup:
    return derive_cleanup(_workflow(), recordings=[_cancel_recording(tmp_path)]).cleanup


def _bundle(tmp_path: Path) -> Path:
    path = tmp_path / "bundle"
    _workflow().save(path)
    (path / "templates").mkdir(exist_ok=True)
    (path / "templates" / "btn.png").write_bytes(b"png")
    return path


def test_a_dialog_left_open_is_closed_and_the_start_screen_confirmed(tmp_path):
    app = App("dialog")
    result = run_cleanup(
        lambda: _replayer(app),
        _workflow(),
        _cleanup(tmp_path),
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "cleanup",
    )
    assert result.clean
    assert app.screen == "start"
    assert app.saves == 0  # the dialog's Confirm was never pressed


def test_a_screen_the_cleanup_cannot_recover_leaves_the_session_dirty(tmp_path):
    app = App("dialog", cancel_works=False)
    result = run_cleanup(
        lambda: _replayer(app),
        _workflow(),
        _cleanup(tmp_path),
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "cleanup",
    )
    assert result.dirty == "the start screen isn't showing"
    assert result.record_changed is None


def test_re_entering_counts_only_if_the_start_screen_then_shows(tmp_path):
    app = App("stuck")
    check_only = runnable_cleanup(_workflow())
    assert check_only.steps == []
    assert (
        run_cleanup(
            lambda: _replayer(app),
            _workflow(),
            check_only,
            bundle_dir=_bundle(tmp_path),
            run_dir=tmp_path / "a",
            reenter=lambda: setattr(app, "screen", "start"),
        ).dirty
        is None
    )
    app.screen = "stuck"
    assert (
        run_cleanup(
            lambda: _replayer(app),
            _workflow(),
            check_only,
            bundle_dir=_bundle(tmp_path),
            run_dir=tmp_path / "b",
            reenter=lambda: None,
        ).dirty
        == "the start screen isn't showing"
    )


def _records(app: App) -> list[dict]:
    return [{"id": i} for i in range(app.saves)]


def test_a_cleanup_that_saves_makes_the_write_uncertain(tmp_path):
    # Re-entry saves the form (autosave on leave): the read-back sees it.
    app = App("form")

    def reenter() -> None:
        app.saves += 1
        app.screen = "start"

    result = run_cleanup(
        lambda: _replayer(app),
        _workflow(),
        _cleanup(tmp_path),
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "cleanup",
        reenter=reenter,
        read_records=lambda: _records(app),
    )
    assert result.record_changed == "1 record(s) added"
    assert result.dirty is not None  # and the session takes no more items


def test_records_that_cannot_be_read_stop_the_cleanup_before_it_runs(tmp_path):
    app = App("dialog")
    result = run_cleanup(
        lambda: _replayer(app),
        _workflow(),
        _cleanup(tmp_path),
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "cleanup",
        read_records=lambda: None,
    )
    assert result.dirty.startswith("the records couldn't be read before")
    assert result.record_changed is None
    assert app.screen == "dialog"  # nothing ran


def test_back_and_exit_are_not_cancel_words():
    # Leaving a page can autosave the form; these aren't safe to derive.
    for label in ("Back", "Exit", "Go back"):
        assert not CANCEL_WORDS.search(label), label
    for label in ("Cancel", "Close", "Discard changes", "Don't save", "×"):
        assert CANCEL_WORDS.search(label), label


# ---------------------------------------------------------------------------
# Qualify on a test copy; only a qualified cleanup's steps run
# ---------------------------------------------------------------------------


class AppCopy:
    """The scripted app as a resettable test copy (fresh session per run)."""

    name = "app-test-copy"

    def __init__(self, *, cancel_works: bool = True):
        self.cancel_works = cancel_works
        self.apps: list[App] = []

    def reset(self) -> None:
        pass

    def snapshot(self):
        # The records of the latest session's app (each run is a fresh copy).
        return _records(self.apps[-1]) if self.apps else []

    def run(self, workflow, *, bundle_dir, run_dir, params):
        app = App("start", cancel_works=self.cancel_works)
        self.apps.append(app)
        return _replayer(app).run(
            workflow, params=params, bundle_dir=bundle_dir, run_dir=run_dir
        )


def test_qualify_tries_a_halt_after_every_step_but_the_last(tmp_path):
    copy = AppCopy()
    qualification, results = qualify_cleanup(
        _workflow(), _cleanup(tmp_path), target=copy, bundle_dir=_bundle(tmp_path)
    )
    assert [r.after_step for r in results] == ["open_form", "create"]
    assert all(r.passed for r in results), [r.reason for r in results]
    assert qualification.halt_points == 2
    # The dialog the "create" step opens is closed with Cancel, never Confirm.
    assert all(app.saves == 0 for app in copy.apps)


def test_a_cleanup_that_fails_a_halt_point_is_not_qualified(tmp_path):
    qualification, results = qualify_cleanup(
        _workflow(),
        _cleanup(tmp_path),
        target=AppCopy(cancel_works=False),
        bundle_dir=_bundle(tmp_path),
    )
    assert qualification is None
    assert not results[-1].passed  # stopped in the dialog: Cancel doesn't work
    assert results[-1].reason == "the start screen isn't showing"


def test_a_cleanup_click_that_saves_does_not_qualify(tmp_path, monkeypatch):
    # The review's probe: the form's "Close" saves the record on the way out,
    # as Odoo's autosave on leave does. The screen comes back; the record
    # check catches it.
    monkeypatch.setitem(SCREENS, "form", ["Create", "Close"])
    monkeypatch.setitem(POINTS, "Close", (90, 90))
    monkeypatch.setitem(GOES_TO, "Close", "start")
    orig = AppBackend.click

    def click(self, x, y, *, double=False):
        if (x, y) == (90, 90) and self.app.screen == "form":
            self.app.saves += 1
        return orig(self, x, y, double=double)

    monkeypatch.setattr(AppBackend, "click", click)
    rec = tmp_path / "close-rec"
    Workflow(name="close", steps=[_click("close", "Close")]).save(rec)
    derived = derive_cleanup(_workflow(), recordings=[_cancel_recording(tmp_path), rec])
    copy = AppCopy()
    qual, results = qualify_cleanup(
        _workflow(), derived.cleanup, target=copy, bundle_dir=_bundle(tmp_path)
    )
    assert sum(a.saves for a in copy.apps) >= 1
    assert qual is None
    by_point = {r.after_step: r for r in results}
    assert not by_point["open_form"].passed
    assert "the cleanup changed the records (1 record(s) added)" in (
        by_point["open_form"].reason
    )


def test_qualify_refuses_a_test_copy_that_cannot_read_its_records(tmp_path):
    class Blind:
        name = "blind"

        def reset(self):
            pass

        def run(self, *a, **k):
            raise AssertionError("nothing should run")

    with pytest.raises(ValueError, match="can't read its records"):
        qualify_cleanup(
            _workflow(),
            _cleanup(tmp_path),
            target=Blind(),
            bundle_dir=_bundle(tmp_path),
        )


def test_a_pool_runs_cleanup_steps_only_from_a_qualified_cleanup(tmp_path):
    cleanup = _cleanup(tmp_path)
    workflow = _workflow().model_copy(update={"halt_cleanup": cleanup})
    assert runnable_cleanup(workflow).steps == []  # unqualified: check only

    qualified = cleanup.model_copy(
        update={
            "qualification": HaltCleanupQualification(
                test_copy="t",
                halt_points=2,
                passed=2,
                qualified_at="t",
                cleanup_sha256=cleanup_sha256(cleanup),
            )
        }
    )
    workflow = _workflow().model_copy(update={"halt_cleanup": qualified})
    assert len(runnable_cleanup(workflow).steps) == 2

    # Edited after qualification: back to check only.
    edited = qualified.model_copy(update={"steps": qualified.steps[:1]})
    workflow = _workflow().model_copy(update={"halt_cleanup": edited})
    assert runnable_cleanup(workflow).steps == []


def test_write_stores_the_cleanup_and_its_crops(tmp_path):
    bundle = _bundle(tmp_path)
    derived = derive_cleanup(_workflow(), recordings=[_cancel_recording(tmp_path)])
    stored = write_cleanup(bundle, derived.cleanup, assets=derived.assets)
    assert stored.halt_cleanup == derived.cleanup
    for rel in derived.assets:
        assert (bundle / rel).is_file()
    assert "halt_cleanup" not in _workflow().model_dump()


# ---------------------------------------------------------------------------
# The pool: a dirty session takes no more items
# ---------------------------------------------------------------------------


class CleanupWorker:
    """A pool worker whose items halt, with a scripted cleanup verdict."""

    def __init__(
        self,
        name: str,
        verdicts: list,
        *,
        halt: bool = True,
        outcome: str = "HALTED_BEFORE_EFFECT",
        start: Optional[str] = None,
    ):
        self.name = name
        self.verdicts = verdicts
        self.halt = halt
        self.outcome = outcome
        self.start = start
        self.ran: list[int] = []
        self.cleans = 0
        self.start_checks = 0

    def open(self) -> None:
        pass

    def session_fingerprint(self):
        return self.name

    def check_start(self, check_dir):
        self.start_checks += 1
        return self.start

    def run_item(self, idx, row, item_dir):
        from openadapt_flow.runtime.worklist_pool import HALTED, ItemRun

        self.ran.append(idx)
        return ItemRun(
            state=HALTED if self.halt else "verified",
            detail={"transaction_outcome": self.outcome},
        )

    def clean_after_halt(self, cleanup_dir):
        self.cleans += 1
        return self.verdicts.pop(0) if self.verdicts else None

    def close(self) -> None:
        pass


def _pool(tmp_path, workers, rows=4, on_halt=None):
    from openadapt_flow.runtime.worklist_pool import (
        ON_HALT_SKIP_UNWRITTEN,
        PoolPolicy,
        run_worklist_pool,
    )

    return run_worklist_pool(
        [{"n": str(i)} for i in range(rows)],
        workers,
        run_dir=tmp_path / "pool",
        policy=PoolPolicy(
            workers=len(workers),
            on_halt=on_halt or ON_HALT_SKIP_UNWRITTEN,
            items_independent=True,
        ),
        fingerprint_salt="s",
    )


def test_a_clean_session_takes_the_next_item(tmp_path):
    worker = CleanupWorker("w1", [None, None, None, None])
    _pool(tmp_path, [worker])
    assert worker.ran == [0, 1, 2, 3]
    assert worker.start_checks == 1  # once, on the fresh session


def test_a_dirty_session_takes_no_more_items(tmp_path):
    from openadapt_flow.halt_cleanup import CleanupResult

    dirty = CleanupResult(dirty="the start screen isn't showing")
    w1 = CleanupWorker("w1", [dirty])
    w2 = CleanupWorker("w2", [None] * 6)
    report = _pool(tmp_path, [w1, w2], rows=6)
    assert len(w1.ran) <= 1  # dirty after its first item: no more on it
    assert sorted(w1.ran + w2.ran) == list(range(6))  # w2 carried on
    assert report.stop is None  # one clean worker left: no stop
    summary = {s.name: s for s in report.per_worker}
    if w1.ran:
        assert summary["w1"].dirty == "the start screen isn't showing"


def test_when_the_last_clean_worker_goes_dirty_the_run_says_why(tmp_path, capsys):
    from openadapt_flow.cli_worklist_pool import print_pool_report

    worker = CleanupWorker("w1", [None, "the start screen isn't showing"])
    report = _pool(tmp_path, [worker])
    assert worker.ran == [0, 1]  # dirty after item 1: items 2 and 3 never ran
    [summary] = report.per_worker
    assert summary.dirty == "the start screen isn't showing"
    assert report.stop is not None
    assert "every worker's session is dirty" in report.stop["reason"]
    assert [i["state"] for i in report.items][2:] == ["pending", "pending"]
    print_pool_report(report, tmp_path / "pool")
    out = capsys.readouterr().out
    assert "dirty: the start screen isn't showing" in out
    assert "every worker's session is dirty" in out
    assert (
        "dirty: the start screen isn't showing"
        in (tmp_path / "pool" / "WORKLIST.md").read_text()
    )


def test_no_cleanup_once_the_run_has_stopped(tmp_path):
    # The review's probe: under on_halt=stop the first halt ends the run, so
    # the screen is left as it is for the person who reads it.
    from openadapt_flow.runtime.worklist_pool import ON_HALT_STOP

    w = CleanupWorker("w1", [], outcome="EFFECT_UNCERTAIN")
    report = _pool(tmp_path, [w], rows=2, on_halt=ON_HALT_STOP)
    assert report.stop is not None and w.ran == [0]
    assert w.cleans == 0
    assert w.start_checks == 0  # the start check only matters when it cleans


def test_no_cleanup_after_an_uncertain_write(tmp_path):
    w = CleanupWorker("w1", [], outcome="RECONCILIATION_REQUIRED")
    report = _pool(tmp_path, [w], rows=2)
    assert report.stop is not None and w.ran == [0]
    assert w.cleans == 0


def test_a_cleanup_that_changed_the_record_makes_the_item_uncertain(tmp_path):
    from openadapt_flow.halt_cleanup import CleanupResult
    from openadapt_flow.runtime.worklist_pool import RECONCILIATION_REQUIRED

    w = CleanupWorker(
        "w1",
        [CleanupResult(dirty="records changed", record_changed="1 record(s) added")],
    )
    report = _pool(tmp_path, [w], rows=3)
    assert w.ran == [0]  # not retried, and nothing after it ran
    item = report.items[0]
    assert item["state"] == RECONCILIATION_REQUIRED
    result = item["result"]
    assert result["transaction_outcome"] == "RECONCILIATION_REQUIRED"
    assert result["transaction_outcome"] != "HALTED_BEFORE_EFFECT"
    assert result["uncertainty"] == "effect_uncertain"
    assert report.stop is not None
    assert "the records changed during the halt cleanup" in report.stop["reason"]
    assert [i["state"] for i in report.items][1:] == ["pending", "pending"]


def test_a_cleanup_that_raises_makes_the_item_uncertain(tmp_path):
    from openadapt_flow.runtime.worklist_pool import RECONCILIATION_REQUIRED

    class Raises(CleanupWorker):
        def clean_after_halt(self, cleanup_dir):
            raise RuntimeError("boom")

    w = Raises("w1", [])
    report = _pool(tmp_path, [w], rows=2)
    assert w.ran == [0]
    assert report.items[0]["state"] == RECONCILIATION_REQUIRED


def test_a_start_screen_missing_on_a_fresh_session_refuses_the_pool(tmp_path):
    from openadapt_flow.runtime.worklist_pool import PoolRefusal

    w1 = CleanupWorker("w1", [])
    w2 = CleanupWorker("w2", [], start="the start screen isn't showing")
    with pytest.raises(PoolRefusal, match="w2: the start screen isn't showing"):
        _pool(tmp_path, [w1, w2])
    assert w1.ran == [] and w2.ran == []
