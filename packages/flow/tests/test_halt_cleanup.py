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
    step.anchor.click_point = POINTS[text]
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
    reason = run_cleanup(
        lambda: _replayer(app),
        _workflow(),
        _cleanup(tmp_path),
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "cleanup",
    )
    assert reason is None
    assert app.screen == "start"
    assert app.saves == 0  # the dialog's Confirm was never pressed


def test_a_screen_the_cleanup_cannot_recover_leaves_the_session_dirty(tmp_path):
    app = App("dialog", cancel_works=False)
    reason = run_cleanup(
        lambda: _replayer(app),
        _workflow(),
        _cleanup(tmp_path),
        bundle_dir=_bundle(tmp_path),
        run_dir=tmp_path / "cleanup",
    )
    assert reason == "the start screen isn't showing"


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
        )
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
        )
        == "the start screen isn't showing"
    )


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

    def __init__(self, name: str, verdicts: list[Optional[str]], *, halt: bool = True):
        self.name = name
        self.verdicts = verdicts
        self.halt = halt
        self.ran: list[int] = []

    def open(self) -> None:
        pass

    def session_fingerprint(self):
        return self.name

    def run_item(self, idx, row, item_dir):
        from openadapt_flow.runtime.worklist_pool import HALTED, ItemRun

        self.ran.append(idx)
        return ItemRun(
            state=HALTED if self.halt else "verified",
            detail={"transaction_outcome": "HALTED_BEFORE_EFFECT"},
        )

    def clean_after_halt(self, cleanup_dir):
        return self.verdicts.pop(0) if self.verdicts else None

    def close(self) -> None:
        pass


def _pool(tmp_path, workers, rows=4):
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
            on_halt=ON_HALT_SKIP_UNWRITTEN,
            items_independent=True,
        ),
        fingerprint_salt="s",
    )


def test_a_clean_session_takes_the_next_item(tmp_path):
    worker = CleanupWorker("w1", [None, None, None, None])
    _pool(tmp_path, [worker])
    assert worker.ran == [0, 1, 2, 3]


def test_a_dirty_session_takes_no_more_items(tmp_path):
    worker = CleanupWorker("w1", [None, "the start screen isn't showing"])
    report = _pool(tmp_path, [worker])
    assert worker.ran == [0, 1]  # dirty after item 1: items 2 and 3 never ran on it
    [summary] = report.per_worker
    assert summary.dirty == "the start screen isn't showing"
