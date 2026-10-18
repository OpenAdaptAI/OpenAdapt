"""Fail-closed surface routing and PHI-free surface evidence.

Each test pins one rule (docs/MULTI_SURFACE.md):

- a step never runs on a page that closed under it, whatever the surface kind;
- a page surface rebinds only to a page claimed on this run (a resume, before
  the surface is first bound, is the one exception), and only on visual
  evidence; a held page that fails its check halts instead of being used;
- a switch to a page surface needs identity evidence, as primary and backend
  switches do;
- dialog errors carry a code, not the dialog's text; the bundle seals a
  pattern, not a record number or a parameter value; URL patterns generalize
  identifier path segments;
- the layout-drift check follows the active surface.

The router tests drive :class:`SurfaceRouter` with in-memory page doubles, so
an attach-mode browser (the person's own tabs already open) is cheap to set up.
"""

from __future__ import annotations

import re
from typing import Any, Optional

import pytest

from openadapt_flow.compiler.compile import _dialog_identity, _surface_url_pattern
from openadapt_flow.ir import (
    ActionKind,
    DialogResponse,
    Postcondition,
    PostconditionKind,
    Step,
    SurfaceSpec,
    Workflow,
)
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.runtime.surfaces import SurfaceRouter

ORIGIN = "http://127.0.0.1:8000"


class _Context:
    def __init__(self) -> None:
        self.pages: list[_Page] = []


class _Page:
    def __init__(
        self, context: _Context, url: str, *, opener: Optional["_Page"] = None
    ) -> None:
        self.context = context
        self.url = url
        self._opener = opener
        self.closed = False
        context.pages.append(self)

    def is_closed(self) -> bool:
        return self.closed

    def opener(self) -> Optional["_Page"]:
        return self._opener

    def title(self) -> str:
        return ""

    def evaluate(self, _script: str) -> bool:
        return self._opener is not None  # window.opener is set

    def bring_to_front(self) -> None:
        pass

    def wait_for_timeout(self, _ms: float) -> None:
        pass

    def close(self) -> None:
        self.closed = True
        self.context.pages.remove(self)


class _PageBackend:
    def __init__(self, page: _Page) -> None:
        self.page = page

    def screenshot(self) -> bytes:
        return b""


_MAIN = SurfaceSpec(id="main", kind="web")
_POPUP = SurfaceSpec(
    id="popup_1",
    kind="web",
    opened_by_step="step_000",
    opener="main",
    open_order=0,
    opener_link="window_opener",
    origin=ORIGIN,
    url_pattern=re.escape(f"{ORIGIN}/popup.html") + r"(?:[?#].*)?",
)
_SHOWS_POPUP = [Postcondition(kind=PostconditionKind.TEXT_PRESENT, text="Approval")]


def _workflow() -> Workflow:
    return Workflow(
        name="wf",
        surfaces=[_MAIN, _POPUP],
        steps=[
            Step(
                id="step_000",
                intent="open the popup",
                action=ActionKind.WAIT,
                opens=["popup_1"],
            ),
            _switch("step_001", "popup_1"),
            _act("step_002"),
        ],
    )


def _switch(step_id: str, surface: str, **kw: Any) -> Step:
    kw.setdefault("expect", list(_SHOWS_POPUP))
    kw.setdefault("surface_origin", ORIGIN)
    return Step(
        id=step_id,
        intent=f"switch to surface '{surface}'",
        action=ActionKind.SWITCH_SURFACE,
        surface=surface,
        risk="reversible",
        **kw,
    )


def _act(step_id: str, surface: str = "popup_1") -> Step:
    return Step(id=step_id, intent="act", action=ActionKind.WAIT, surface=surface)


def _router(
    *, visual: Optional[bool] = True, resume_active: Optional[str] = None
) -> tuple[SurfaceRouter, _Context, _Page]:
    context = _Context()
    main = _Page(context, f"{ORIGIN}/")
    router = SurfaceRouter(
        _workflow(),
        _PageBackend(main),
        page_backend_factory=_PageBackend,
        poll_interval_s=0.01,
        identity_check=lambda _backend, _step: visual,
        resume_active=resume_active,
    )
    return router, context, main


def _open_and_switch(router: SurfaceRouter, context: _Context, main: _Page) -> _Page:
    workflow = _workflow()
    popup = _Page(context, f"{ORIGIN}/popup.html", opener=main)
    assert router.after_action_refusal(workflow.steps[0], timeout_s=0.0) is None
    backend, refusal = router.switch(workflow.steps[1])
    assert refusal is None and backend.page is popup
    return popup


# -- closed pages ---------------------------------------------------------------


def test_a_step_on_a_closed_page_surface_is_refused() -> None:
    router, context, main = _router()
    popup = _open_and_switch(router, context, main)
    popup.close()
    refusal = router.entry_refusal(_act("step_002"))
    assert refusal is not None and "was closed" in refusal


# -- rebinding --------------------------------------------------------------------


def test_a_switch_never_binds_a_page_this_run_did_not_open() -> None:
    context = _Context()
    main = _Page(context, f"{ORIGIN}/")
    # Attach mode: the person already has a tab that looks like the popup.
    theirs = _Page(context, f"{ORIGIN}/popup.html", opener=main)
    router = SurfaceRouter(
        _workflow(),
        _PageBackend(main),
        page_backend_factory=_PageBackend,
        identity_check=lambda _backend, _step: True,
    )
    backend, refusal = router.switch(_switch("step_001", "popup_1"))
    assert backend is None
    assert refusal is not None and "not opened on this run" in refusal
    assert "popup_1" not in router.backends
    assert not theirs.closed


def test_a_closed_popup_does_not_rebind_to_the_persons_tab() -> None:
    router, context, main = _router()
    popup = _open_and_switch(router, context, main)
    popup.close()
    theirs = _Page(context, f"{ORIGIN}/popup.html", opener=main)
    router._remember(theirs)  # in the inventory, as an attached tab would be
    error = router.route(_act("step_002"))
    assert error is not None and "no open browser page passes" in error
    assert router.backends["popup_1"].page is not theirs


def test_a_resume_rebinds_only_on_visual_evidence() -> None:
    # A resumed run may bind a page the earlier run opened, but only when the
    # step's recorded screen evidence shows on it.
    for visual, binds in ((None, False), (False, False), (True, True)):
        context = _Context()
        main = _Page(context, f"{ORIGIN}/")
        earlier = _Page(context, f"{ORIGIN}/popup.html", opener=main)
        router = SurfaceRouter(
            _workflow(),
            _PageBackend(main),
            page_backend_factory=_PageBackend,
            identity_check=lambda _backend, _step, v=visual: v,
            resume_active="main",
        )
        backend, refusal = router.switch(_switch("step_001", "popup_1"))
        if binds:
            assert refusal is None and backend.page is earlier
        else:
            assert backend is None and refusal is not None, visual


def test_a_held_page_that_fails_identity_halts() -> None:
    router, context, main = _router(visual=True)
    popup = _open_and_switch(router, context, main)
    # A page claimed earlier on this run that no surface holds now.
    spare = _Page(context, f"{ORIGIN}/popup.html", opener=main)
    router._remember(spare)
    router._claimed_on_run.append(spare)
    router._identity_check = lambda _backend, _step: False
    error = router.route(_act("step_002"))
    assert error is not None and "no longer passes its identity check" in error
    assert router.backends["popup_1"].page is popup


# -- switching ---------------------------------------------------------------------


def test_a_page_switch_without_identity_evidence_halts() -> None:
    router, context, main = _router()
    _Page(context, f"{ORIGIN}/popup.html", opener=main)
    assert router.after_action_refusal(_workflow().steps[0], timeout_s=0.0) is None
    backend, refusal = router.switch(
        _switch("step_001", "popup_1", expect=[], surface_origin=None)
    )
    assert backend is None
    assert refusal is not None and "no identity evidence" in refusal
    assert router.active_id == "main"


# -- dialogs -------------------------------------------------------------------------


class _DialogBackend:
    def __init__(self, seen: list[dict[str, Any]]) -> None:
        self.seen = seen
        self.armed: list[tuple[Any, ...]] = []

    def arm_dialog(self, *args: Any, **kwargs: Any) -> None:
        self.armed.append((args, kwargs))

    def take_dialogs(self, _timeout_s: float = 0.0) -> list[dict[str, Any]]:
        seen, self.seen = self.seen, []
        return seen


def _dialog_error(seen: list[dict[str, Any]], dialog: DialogResponse) -> str:
    replayer = Replayer.__new__(Replayer)
    replayer.backend = _DialogBackend(seen)  # type: ignore[assignment]
    replayer._act_without_dialog = lambda *_a, **_k: None  # type: ignore[method-assign]
    step = Step(
        id="s", intent="delete", action=ActionKind.KEY, key="Enter", dialog=dialog
    )
    error = Replayer._act(replayer, step, None, {})
    assert error is not None
    return error


def test_a_dialog_mismatch_error_quotes_no_dialog_text(monkeypatch) -> None:
    import openadapt_flow.runtime.replayer as replayer_mod

    monkeypatch.setattr(replayer_mod, "_DIALOG_WAIT_S", 0.0)
    live = "Discharge patient Jane Roe (MRN 991)?"
    dialog = DialogResponse(response="accept", message="Discharge this patient?")
    error = _dialog_error([{"message": live, "matched": False}], dialog)
    assert "dialog_mismatch" in error
    assert "Jane" not in error and "991" not in error
    assert "Discharge" not in error
    missing = _dialog_error([], dialog)
    assert "dialog_missing" in missing and "Discharge" not in missing


def test_a_dialog_pattern_is_armed_as_a_pattern(monkeypatch) -> None:
    import openadapt_flow.runtime.replayer as replayer_mod

    monkeypatch.setattr(replayer_mod, "_DIALOG_WAIT_S", 0.0)
    dialog = DialogResponse(response="accept", message_pattern=r"Delete\ record\ \d+\?")
    replayer = Replayer.__new__(Replayer)
    backend = _DialogBackend([{"message": "Delete record 7?", "matched": True}])
    replayer.backend = backend  # type: ignore[assignment]
    replayer._act_without_dialog = lambda *_a, **_k: None  # type: ignore[method-assign]
    step = Step(
        id="s", intent="delete", action=ActionKind.KEY, key="Enter", dialog=dialog
    )
    assert Replayer._act(replayer, step, None, {}) is None
    assert backend.armed[0] == (
        ("accept", None),
        {"message_pattern": r"Delete\ record\ \d+\?"},
    )


def test_a_dialog_message_is_sealed_without_its_values() -> None:
    sealed = _dialog_identity(
        {"response": "accept", "message": "Remove Jane Roe from list 2024-01-03?"},
        ("Jane Roe",),
    )
    assert sealed.message is None and sealed.message_pattern is not None
    dumped = sealed.model_dump_json()
    assert "Jane" not in dumped and "2024" not in dumped
    pattern = re.compile(sealed.message_pattern, re.DOTALL)
    assert pattern.fullmatch("Remove John Poe from list 2025-11-30?")
    assert not pattern.fullmatch("Remove everyone from every list?")
    # A message with no value and no digit is kept exactly.
    kept = _dialog_identity({"response": "dismiss", "message": "Sure?"}, ())
    assert kept == DialogResponse(response="dismiss", message="Sure?")


def test_a_dialog_response_needs_exactly_one_identity() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        DialogResponse(response="accept")
    with pytest.raises(ValueError, match="exactly one"):
        DialogResponse(response="accept", message="a", message_pattern="a")


# -- URL patterns ----------------------------------------------------------------------


def test_url_patterns_generalize_identifier_segments() -> None:
    pattern = _surface_url_pattern(
        "https://ehr.example/patient/12345/chart?state=abc", ()
    )
    assert "12345" not in pattern and "state" not in pattern
    assert re.fullmatch(pattern, "https://ehr.example/patient/777/chart")
    assert not re.fullmatch(pattern, "https://ehr.example/billing/777/chart")
    token = "AbCdEfGhIjKlMnOpQrStUvWxYz"
    pattern = _surface_url_pattern(
        f"https://a.example/api/v2/s/{token}/order-history", ()
    )
    assert token not in pattern
    assert r"/api/v2/" in pattern and r"order\-history" in pattern


# -- layout drift -----------------------------------------------------------------------


def test_layout_drift_follows_the_active_surface() -> None:
    class _Router:
        def active_kind(self) -> str:
            return "rdp"

    replayer = Replayer.__new__(Replayer)
    replayer.layout_drift = None
    replayer._layout_target_kind = None
    replayer.backend = object()  # type: ignore[assignment]
    replayer._surface_router = _Router()  # type: ignore[assignment]
    # A web-first bundle on its RDP segment keeps the check.
    workflow = Workflow(
        name="wf",
        surface="web",
        steps=[Step(id="s", intent="wait", action=ActionKind.WAIT)],
    )
    assert Replayer._layout_drift_on(replayer, workflow) is True
    replayer._surface_router = None
    assert Replayer._layout_drift_on(replayer, workflow) is False
