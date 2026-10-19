"""Reopen read-back against the MockMed fault server.

A small scripted app sits on the fault server's record API: a save form, a
search box, and a record page. The read-back replays a learned search (by
this run's patient, or by the reference number the save showed), then reads
the record page. A search by the patient confirms only when the record it
opens shows the number this save showed: an older note with the same text
must not confirm a save that failed. Ground truth is the fault server's
store, never the screen. Zero model calls, no browser.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Optional

import pytest
import requests

from openadapt_flow.mockmed.fault_server import serve as fault_serve
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectKind,
    ReadbackAdmission,
    ReadbackField,
    ReadbackGuard,
    ReadbackNav,
    ReadbackReference,
    ReadbackReset,
    ReadbackSpec,
    ValueExpr,
    Verdict,
)
from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier

NOTE = "chest pain, follow up in two weeks"
SAVE_AT = (330, 412)
SEARCH_AT = (60, 30)
BANNER = (100, 100, 300, 24)
PATIENT = (100, 150, 200, 24)
NOTE_BOX = (100, 200, 320, 40)
ENCOUNTER = (100, 260, 200, 24)
REF_PATTERN = r"(?<![A-Za-z0-9])(E\d{1,6})(?![A-Za-z0-9])"


class FaultApp:
    """A scripted record app whose saves go to the fault server."""

    def __init__(self, base: str, fault: str) -> None:
        self.base = base
        self.fault = fault
        self.screen = "form"
        self.banner = ""
        self.query = ""
        self.record: Optional[dict] = None
        self.writes = 0
        self.extra_banner = ""
        self.last_reason = ""

    # -- the application ---------------------------------------------------

    def save(self) -> None:
        self.writes += 1
        try:
            reply = requests.post(
                f"{self.base}/api/encounter?fault={self.fault}",
                json={"patient_id": "p1", "type": "Triage", "note": NOTE},
                timeout=1.5,
            )
            body = reply.json() if reply.ok else {}
        except (requests.RequestException, ValueError):
            body = {}
        if self.fault == "session":
            self.banner = "Session expired. Please sign in again."
        elif self.fault == "optimistic" or "id" not in body:
            self.banner = "Saved."  # optimistic UI paints success
        else:
            self.banner = f"Saved. Encounter E{body['id']}"

    def _records(self) -> list[dict]:
        return requests.get(f"{self.base}/api/db", timeout=1.5).json()["records"]

    def _open(self) -> None:
        records = self._records()
        query = self.query.strip()
        hit = None
        if re.fullmatch(r"E\d+", query):
            hit = next((r for r in records if f"E{r['id']}" == query), None)
        else:
            hit = next(
                (
                    r
                    for r in reversed(records)
                    if r["patient_id"] == query and r["type"] == "Triage"
                ),
                None,
            )
        self.record = hit
        self.screen = "record"

    # -- the backend surface ----------------------------------------------

    def screenshot(self) -> bytes:
        return f"{self.screen}|{self.query}|{self.banner}|{self.record}".encode()

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        if abs(x - SAVE_AT[0]) < 30 and abs(y - SAVE_AT[1]) < 12:
            self.save()
        elif abs(x - SEARCH_AT[0]) < 40 and abs(y - SEARCH_AT[1]) < 12:
            self.screen = "search"
            self.query = ""

    def type_text(self, text: str) -> None:
        if self.screen == "search":
            self.query += text

    def press(self, key: str) -> None:
        if key.lower() == "enter" and self.screen == "search":
            self._open()

    def lines(self) -> list[SimpleNamespace]:
        out = [line("Save", (300, 400, 60, 24)), line("Search", (20, 18, 80, 24))]
        if self.screen == "form":
            if self.banner:
                out.append(line(self.banner + self.extra_banner, BANNER))
            out.append(line(f"Note: {NOTE}", NOTE_BOX))  # what was typed
        elif self.screen == "record" and self.record is not None:
            out.append(line(f"Patient: {self.record['patient_id']}", PATIENT))
            out.append(line(f"Note: {self.record.get('note') or ''}", NOTE_BOX))
            out.append(line(f"Encounter E{self.record['id']}", ENCOUNTER))
        return out


def line(text: str, region: tuple[int, int, int, int]) -> SimpleNamespace:
    return SimpleNamespace(text=text, region=region, confidence=0.95)


def _overlaps(a, b) -> bool:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def vision_for(app: FaultApp) -> SimpleNamespace:
    def ocr(png: bytes, *, region=None):
        return [
            item
            for item in app.lines()
            if region is None or _overlaps(item.region, region)
        ]

    return SimpleNamespace(ocr=ocr)


def _admit(spec: ReadbackSpec) -> ReadbackSpec:
    spec.admission = ReadbackAdmission(
        admitted_by="reviewer",
        admitted_at=datetime.now(timezone.utc).isoformat(),
        spec_digest=spec.admission_digest(),
    )
    return spec


SEARCH_PATIENT = [
    ReadbackNav(action="click", point=SEARCH_AT),
    ReadbackNav(action="type", param="patient_id"),
    ReadbackNav(action="key", key="Enter"),
]
SEARCH_REFERENCE = [
    ReadbackNav(action="click", point=SEARCH_AT),
    ReadbackNav(action="type", reference=True),
    ReadbackNav(action="key", key="Enter"),
]
FIELDS = [
    ReadbackField(
        name="patient_id", region=PATIENT, value=ValueExpr(param="patient_id")
    ),
    ReadbackField(name="note", region=NOTE_BOX, value=ValueExpr(param="note")),
]


def _effect(renavigation, *, reference=False, admitted=True) -> Effect:
    spec = ReadbackSpec(
        region=NOTE_BOX,
        different_path=True,
        renavigation=list(renavigation),
        fields=list(FIELDS),
        demonstrated=True,
        reset=ReadbackReset(kind="new_search", at=1),
        guard=ReadbackGuard(commit_labels=["Save"]),
        reference=(
            ReadbackReference(region=(40, 76, 420, 72), pattern=REF_PATTERN)
            if reference
            else None
        ),
    )
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        value=ValueExpr(param="note"),
        risk="irreversible",
        readback=_admit(spec) if admitted else spec,
    )


PARAMS = {"patient_id": "p1", "note": NOTE}

#: fault -> does the intended note truly live on p1's record?
FAULTS = {
    "ok": True,
    "partial": False,
    "optimistic": False,
    "session": False,
    "no_persist": False,
    "wrong_record": False,
    "timeout": True,
}


@pytest.fixture(scope="module")
def server():
    base_url, db, stop = fault_serve()
    yield base_url.rstrip("/"), db
    stop()


def _older_note(base: str) -> None:
    """An earlier run's note for the same patient, with the same text."""

    reply = requests.post(
        f"{base}/api/encounter?fault=ok",
        json={"patient_id": "p1", "type": "Triage", "note": NOTE},
        timeout=1.5,
    )
    assert reply.ok


def _run(
    server, fault: str, effect: Effect, *, older: bool = False
) -> tuple[Verdict, FaultApp, int]:
    base, db = server
    db.reset()
    if older:
        _older_note(base)
    app = FaultApp(base, fault)
    app.save()
    rows_after_save = len(db.snapshot()["records"])
    verifier = OnScreenReadbackVerifier(app, vision=vision_for(app))
    verdict = verifier.verify(
        effect.resolve(PARAMS), verifier.capture_pre_state(), PARAMS
    )
    rows_after_check = len(db.snapshot()["records"])
    assert rows_after_check == rows_after_save, "the read-back wrote a record"
    app.last_reason = verdict.reason
    return verdict.verdict, app, rows_after_check


@pytest.mark.parametrize("older", [False, True])
@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_search_by_patient_never_confirms_a_save_that_did_not_land(
    server, fault, older
):
    verdict, _app, _rows = _run(
        server, fault, _effect(SEARCH_PATIENT, reference=True), older=older
    )
    if fault == "timeout":
        # No reference number on screen: nothing ties a record to this save.
        assert verdict is Verdict.INDETERMINATE
    elif FAULTS[fault]:
        assert verdict is Verdict.CONFIRMED
    else:
        assert verdict is not Verdict.CONFIRMED


@pytest.mark.parametrize("fault", ["no_persist", "partial", "optimistic", "session"])
def test_an_older_matching_note_never_confirms_a_failed_save(server, fault):
    """The patient already has a note with this text. The save fails, the
    search by patient opens the older note, and it reads exactly right."""

    for effect in (_effect(SEARCH_PATIENT), _effect(SEARCH_PATIENT, reference=True)):
        verdict, app, rows = _run(server, fault, effect, older=True)
        assert app.screen in {"form", "record"}
        assert verdict is not Verdict.CONFIRMED


def test_a_search_by_patient_with_no_reference_number_confirms_nothing(server):
    base, db = server
    db.reset()
    app = FaultApp(base, "ok")
    app.save()
    verifier = OnScreenReadbackVerifier(app, vision=vision_for(app))
    verdict = verifier.verify(
        _effect(SEARCH_PATIENT).resolve(PARAMS), verifier.capture_pre_state(), PARAMS
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "older record with the same value" in verdict.reason


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_search_by_reference_never_confirms_a_save_that_did_not_land(server, fault):
    verdict, _app, _rows = _run(
        server, fault, _effect(SEARCH_REFERENCE, reference=True)
    )
    if fault == "timeout":
        # The row landed, but the client timed out before the app showed a
        # reference number: nothing to search by, so the check halts (a false
        # halt, the safe error).
        assert verdict is Verdict.INDETERMINATE
    elif FAULTS[fault]:
        assert verdict is Verdict.CONFIRMED
    else:
        # wrong_record: the reference opens the record, which belongs to p2,
        # so the patient field refutes it.
        assert verdict is not Verdict.CONFIRMED


def test_an_unadmitted_learned_check_confirms_nothing(server):
    verdict, app, _ = _run(server, "ok", _effect(SEARCH_PATIENT, admitted=False))
    assert verdict is Verdict.INDETERMINATE
    assert app.screen == "form"  # nothing was replayed


def test_the_guard_refuses_a_click_on_save(server):
    nav = [ReadbackNav(action="click", point=SAVE_AT), *SEARCH_PATIENT]
    base, db = server
    db.reset()
    app = FaultApp(base, "ok")
    app.save()
    verifier = OnScreenReadbackVerifier(app, vision=vision_for(app))
    verdict = verifier.verify(
        _effect(nav).resolve(PARAMS), verifier.capture_pre_state(), PARAMS
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "read-only guard" in verdict.reason
    assert app.writes == 1
    assert len(db.snapshot()["records"]) == 1


@pytest.mark.parametrize(
    "chord",
    [
        "ctrl+s",
        "cmd+s",
        "Delete",
        "ctrl+enter",
        "control+s",
        "Control+Enter",
        "ctrl+shift+s",
        "alt+s",
        "shift+delete",
        "command+return",
        "Meta+S",
        "s+ctrl",
    ],
)
def test_the_guard_refuses_save_and_delete_keys(server, chord):
    nav = [*SEARCH_REFERENCE[:2], ReadbackNav(action="key", key=chord)]
    verdict, app, _ = _run(server, "ok", _effect(nav, reference=True))
    assert verdict is Verdict.INDETERMINATE
    assert "read-only guard" in app.last_reason


@pytest.mark.parametrize("chord", ["Enter", "ctrl+f", "F5", "Escape", "Tab"])
def test_the_guard_allows_navigation_keys(chord):
    from openadapt_flow.runtime.effects.onscreen import is_commit_chord

    assert is_commit_chord(chord) is False


def test_the_guard_refuses_a_click_with_no_readable_label(server):
    """An icon-only control may be Save or Delete: no label, no click."""

    nav = [ReadbackNav(action="click", point=(600, 600)), *SEARCH_REFERENCE]
    verdict, app, _ = _run(server, "ok", _effect(nav, reference=True))
    assert verdict is Verdict.INDETERMINATE
    assert "no readable label" in app.last_reason
    assert app.screen == "form"  # nothing was clicked


def test_two_reference_numbers_on_screen_open_nothing(server):
    base, db = server
    db.reset()
    app = FaultApp(base, "ok")
    app.save()
    app.extra_banner = " (replaces E9999)"
    verifier = OnScreenReadbackVerifier(app, vision=vision_for(app))
    verdict = verifier.verify(
        _effect(SEARCH_REFERENCE, reference=True).resolve(PARAMS),
        verifier.capture_pre_state(),
        PARAMS,
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "2 different reference numbers" in verdict.reason
    assert app.screen == "form"
