"""Deterministic, model-free simulation of the OpenEMR add-patient-note task.

This module supplies the three pieces the integrated end-to-end harness
(:mod:`benchmark.openemr_e2e.harness`) needs to drive the FULL runtime
pipeline -- compile -> replay -> effect-verify -> halt -> teach -> re-run --
against a CI-reproducible fixture instead of the live OpenEMR public demo:

- :class:`SimBackend` -- a fake pixel backend (screenshot in, clicks/keys out)
  whose consequential ``Save`` keypress POSTs an encounter to the in-process
  MockMed ``fault_server`` system of record, exactly as
  ``tests/test_replayer_effects.py`` does. The write lands in real
  ground-truth state, so the runtime's :class:`RestRecordVerifier` reads the
  actual record, never the screen.
- :class:`AddNoteVision` -- a scripted vision namespace that resolves the
  add-note anchors and, when a ``consent`` modal drift is injected, blocks the
  post-save confirmation until an operator-taught dismiss step clicks it away
  (the same modal-once mechanism proven in ``tests/test_halt_learn_loop.py``).
- :func:`build_add_note_program` -- the compiled add-patient-note workflow as a
  :class:`ProgramGraph`, whose ``Save`` step carries the system-of-record
  ``Effect`` contracts (record-written + note field-equals) the effect stage
  verifies.

Everything here is deterministic, makes ZERO model calls, and touches nothing
beyond localhost -- the whole point of the fixture path. The live OpenEMR path
(a real recording compiled with ``compile_recording`` + a ``PlaywrightBackend``
+ a ``FhirEffectVerifier``) is wired separately in the harness and is
env-gated; this module is the fixture substrate.

The clinical framing (a consent notice intercepting a patient-note save)
mirrors the real add-patient-note flagship recorded by
``scripts/openemr_demo.py``; only fake data is used.
"""

from __future__ import annotations

import io
from typing import Any, Optional

import requests
from PIL import Image

from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    Postcondition,
    PostconditionKind,
    ProgramGraph,
    State,
    StateKind,
    Step,
    Transition,
    Workflow,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

# -- workflow vocabulary ------------------------------------------------------

#: The compiled add-patient-note skill id (also the teach/library skill id).
SKILL_ID = "openemr-add-patient-note"

INTENT_OPEN = "Open patient messages"
INTENT_NOTE = "Enter patient note"
INTENT_SAVE = "Save patient note"
INTENT_CONFIRM = "Confirm note saved"
INTENT_DISMISS = "Dismiss consent notice"

#: The unexpected on-screen state the injected UI drift paints. Distinctive so
#: the induced branch guard keys on it and cannot swallow a different modal.
CONSENT_MODAL_FACT = "Consent Required"

#: The run parameter carrying the note text (substituted into the TYPE step and
#: into the ``field_equals`` effect -- so a pass proves substitution against
#: real state, not replay of a baked-in literal).
NOTE_PARAM = "note"

#: Fixed demo target the effect contracts match against (fake patient only).
PATIENT_ID = "p1"
ENCOUNTER_TYPE = "Triage"

# Anchor origins/points, kept mutually distant so the scripted vision's
# prefer-near branches never collide (see AddNoteVision).
_OPEN_ORIGIN = (250, 150)
_OPEN_POINT = (255, 155)
_VERIFY_ORIGIN = (400, 300)
_CONFIRM_POINT = (410, 305)
#: Where the taught dismiss step clicks. Matches the reference inducer's
#: default spliced-anchor click point so the re-run resolves the dismiss.
_DISMISS_POINT = (100, 100)
_VIEWPORT = (640, 480)


def _png(
    size: tuple[int, int] = _VIEWPORT, color: tuple[int, int, int] = (245, 245, 245)
) -> bytes:
    """A solid PNG frame (the fixture is pixel-stable by construction)."""
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def note_effects() -> list[Effect]:
    """The Save step's system-of-record contracts.

    Two effects, verified against the record (never the screen):

    - ``record_written`` -- an encounter row for the demo patient must exist
      (catches a phantom/rejected write the UI still paints as saved);
    - ``field_equals`` -- that row's ``note`` must equal the run's ``note``
      param (catches a partial save that drops the note silently).

    Both are irreversible (a clinical write) with a tight timeout suited to the
    in-process fixture.
    """
    match = {
        "patient_id": ValueExpr(literal=PATIENT_ID),
        "type": ValueExpr(literal=ENCOUNTER_TYPE),
    }
    return [
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match=dict(match),
            expected_count=1,
            risk="irreversible",
            timeout_s=2.0,
        ),
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match=dict(match),
            field="note",
            value=ValueExpr(param=NOTE_PARAM),
            risk="irreversible",
            timeout_s=2.0,
        ),
    ]


def _click_step(step_id: str, intent: str, origin: tuple[int, int]) -> Step:
    return Step(
        id=step_id,
        intent=intent,
        action=ActionKind.CLICK,
        anchor=Anchor(
            template=f"templates/{step_id}.png",
            region=(*origin, 40, 20),
            click_point=(origin[0] + 5, origin[1] + 5),
            ocr_text=intent,
        ),
    )


def build_add_note_program() -> ProgramGraph:
    """The compiled add-patient-note workflow as a state-machine program.

    Flow: open the patient messages composer -> type the note (parameterized)
    -> Save (carries the system-of-record effects, presses the commit key)
    -> confirm the saved banner -> done. The confirm step is what a consent
    modal intercepts under the injected drift, so a workflow never demonstrated
    to handle it HALTS rather than guessing (the "before"); the taught dismiss
    branch resolves it (the "after").
    """
    save_step = Step(
        id="s_save",
        intent=INTENT_SAVE,
        action=ActionKind.KEY,
        key="S",
        expect=[
            Postcondition(
                kind=PostconditionKind.TEXT_PRESENT,
                text="Saved",
                timeout_s=0.5,
            )
        ],
        effects=note_effects(),
    )
    note_step = Step(
        id="s_note",
        intent=INTENT_NOTE,
        action=ActionKind.TYPE,
        param=NOTE_PARAM,
    )
    return ProgramGraph(
        entry="s_open",
        states={
            "s_open": State(
                id="s_open",
                kind=StateKind.ACTION,
                step=_click_step("s_open", INTENT_OPEN, _OPEN_ORIGIN),
                transitions=[Transition(target="s_note")],
            ),
            "s_note": State(
                id="s_note",
                kind=StateKind.ACTION,
                step=note_step,
                transitions=[Transition(target="s_save")],
            ),
            "s_save": State(
                id="s_save",
                kind=StateKind.ACTION,
                step=save_step,
                transitions=[Transition(target="s_confirm")],
            ),
            "s_confirm": State(
                id="s_confirm",
                kind=StateKind.ACTION,
                step=Step(
                    id="s_confirm",
                    intent=INTENT_CONFIRM,
                    action=ActionKind.CLICK,
                    anchor=Anchor(
                        template="templates/s_confirm.png",
                        region=(*_VERIFY_ORIGIN, 50, 20),
                        click_point=_CONFIRM_POINT,
                        ocr_text=INTENT_CONFIRM,
                    ),
                    timeout_s=0.2,  # keep the halt-path resolution retry fast
                ),
                transitions=[Transition(target="__end__")],
            ),
            "__end__": State(id="__end__", kind=StateKind.TERMINAL, outcome="success"),
        },
    )


def write_bundle(bundle_dir: Any) -> Workflow:
    """Materialize the compiled add-note program as an on-disk bundle.

    Writes ``workflow.json`` and the template PNGs the anchors reference (the
    reference inducer also splices a ``templates/x.png`` dismiss anchor on
    promotion, so that crop is provided too). Returns the loaded
    :class:`Workflow`.
    """
    from pathlib import Path

    bundle = Path(bundle_dir)
    templates = bundle / "templates"
    templates.mkdir(parents=True, exist_ok=True)
    for name in ("s_open", "s_confirm", "x"):
        (templates / f"{name}.png").write_bytes(_png((40, 20)))
    workflow = Workflow(name=SKILL_ID, program=build_add_note_program())
    workflow.save(bundle)
    return workflow


class _Match:
    """A resolved template/text match (duck-types the runtime's match)."""

    def __init__(
        self,
        point: tuple[int, int],
        region: tuple[int, int, int, int],
        confidence: float = 0.95,
    ):
        self.point = point
        self.region = region
        self.confidence = confidence


class AddNoteVision:
    """Scripted vision for the add-note flow with an optional consent-modal drift.

    ``modal_text=None`` is a clean (no-drift) run: every anchor resolves and the
    workflow completes. A non-None ``modal_text`` injects a modal-ONCE drift: the
    consent modal covers the post-save confirmation banner until its dismiss
    button is clicked once, so the naive program HALTS on the blocked confirm
    and a program that dismisses first succeeds -- one behaviour driving both,
    exactly the mechanism proven in ``tests/test_halt_learn_loop.py``.
    """

    def __init__(self, backend: "SimBackend", *, modal_text: Optional[str] = None):
        self.backend = backend
        self.modal_text = modal_text
        # Interface parity with the runtime's vision namespace.
        self.settle_count = 0

    # -- drift state ---------------------------------------------------------
    def _dismissed(self) -> bool:
        return any(
            a[0] == "click"
            and abs(a[1] - _DISMISS_POINT[0]) <= 15
            and abs(a[2] - _DISMISS_POINT[1]) <= 15
            for a in self.backend.actions
        )

    def _modal_up(self) -> bool:
        return self.modal_text is not None and not self._dismissed()

    @staticmethod
    def _near(a: tuple[int, int], b: tuple[int, int], tol: int = 8) -> bool:
        return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol

    # -- vision surface the Replayer touches --------------------------------
    def find_template(
        self,
        screen_png: bytes,
        template_png: bytes,
        *,
        search_region: Any = None,
        prefer_near: Any = None,
        scales: Any = (0.85, 1.0, 1.18),
        threshold: float = 0.82,
    ) -> Optional[_Match]:
        if prefer_near is not None and self._near(prefer_near, _OPEN_ORIGIN):
            # The composer entry point: always available.
            return _Match(_OPEN_POINT, (*_OPEN_ORIGIN, 40, 20))
        if prefer_near is not None and self._near(prefer_near, _VERIFY_ORIGIN):
            # The confirm banner: reachable only when no modal covers it.
            if self._modal_up():
                return None
            return _Match(_CONFIRM_POINT, (*_VERIFY_ORIGIN, 50, 20))
        # The consent modal's dismiss button: present only while the modal is up
        # (the taught dismiss step's spliced anchor resolves here).
        if self._modal_up():
            return _Match(_DISMISS_POINT, (90, 90, 20, 20))
        return None

    def find_text(
        self,
        screen_png: bytes,
        text: str,
        *,
        region: Any = None,
        min_ratio: float = 0.8,
    ) -> Optional[_Match]:
        if text == "Saved":
            # The app always paints "Saved" after the commit key -- the whole
            # point of effect verification is that this can be true while the
            # RECORD is wrong.
            return _Match((60, 12), (30, 5, 50, 14))
        if self._modal_up() and text == self.modal_text:
            return _Match((60, 12), (30, 5, 160, 16))
        return None

    def text_present(
        self,
        screen_png: bytes,
        text: str,
        *,
        region: Any = None,
        min_ratio: float = 0.8,
    ) -> bool:
        return (
            self.find_text(screen_png, text, region=region, min_ratio=min_ratio)
            is not None
        )

    def ocr(self, screen_png: bytes, *, region: Any = None) -> list[Any]:
        from openadapt_flow.vision.ocr import OcrLine

        if self._modal_up():
            return [
                OcrLine(
                    text=self.modal_text or "", region=(30, 5, 160, 16), confidence=0.9
                )
            ]
        return []

    def pixels_changed(
        self,
        before_png: bytes,
        after_png: bytes,
        *,
        region: Any = None,
        threshold: int = 20,
        min_pixels: int = 4,
    ) -> bool:
        # The typed note visibly lands (the fixture never fails to render input).
        return True

    def phash_png(self, png: bytes, region: Any = None) -> str:
        return "aa"

    def phash_distance(self, a: str, b: str) -> int:
        return 0

    def wait_settled(
        self,
        backend: Any,
        *,
        interval_s: float = 0.1,
        stable_frames: int = 2,
        timeout_s: float = 3.0,
    ) -> bytes:
        self.settle_count += 1
        return backend.screenshot()


class SimBackend:
    """A fake pixel backend whose Save keypress writes to the real system of record.

    Screenshot in, clicks/keys out -- the only interface the runtime uses. The
    consequential ``Save`` keypress (KEY ``S``) POSTs the typed note as an
    encounter to the in-process MockMed ``fault_server`` at ``sor_url``; the
    ``fault`` query flag selects the transactional fault the server injects
    (``""`` = a clean commit, ``"partial"`` = row written but note dropped,
    ``"optimistic"`` = UI success then backend 409, etc.). Every other action
    is a pure record, exactly like the unit-test ``FakeBackend``. No screenshot
    ever leaves the box; no model is called.
    """

    def __init__(
        self, sor_url: str, *, fault: str = "", viewport: tuple[int, int] = _VIEWPORT
    ):
        self.sor_url = sor_url.rstrip("/")
        self.fault = fault
        self._viewport = viewport
        self._frame = _png(viewport)
        self.actions: list[Any] = []
        self.last_typed: str = ""
        self.posts: list[dict[str, Any]] = []

    @property
    def viewport(self) -> tuple[int, int]:
        return self._viewport

    def screenshot(self) -> bytes:
        return self._frame

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self.actions.append(("click", x, y, double))

    def type_text(self, text: str) -> None:
        self.actions.append(("type", text))
        self.last_typed = text

    def text_value_at(self, x: int, y: int) -> str:
        """Return the fixture textarea value for exact delivery verification.

        The deterministic benchmark has one editable control.  Exposing its
        value models the structural readback available from the real browser
        backend and keeps this fixture from relying on an unsafe pixel-only
        success heuristic.
        """
        del x, y
        return self.last_typed

    def focused_text_value(self) -> str:
        """Return the value of the fixture's sole focused editable control."""
        return self.last_typed

    def press(self, key: str) -> None:
        self.actions.append(("press", key))
        # Model the app: the commit keypress POSTs the encounter to the SoR.
        payload = {
            "patient_id": PATIENT_ID,
            "type": ENCOUNTER_TYPE,
            "note": self.last_typed,
        }
        self.posts.append(payload)
        url = f"{self.sor_url}/api/encounter"
        if self.fault:
            url += f"?fault={self.fault}"
        try:
            requests.post(url, json=payload, timeout=5)
        except requests.exceptions.RequestException:
            # ``timeout`` mode commits server-side then hangs past the client
            # abort -- the row still landed; the effect check reads the record.
            pass

    def scroll(self, dx: int, dy: int) -> None:
        self.actions.append(("scroll", dx, dy))
