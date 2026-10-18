"""Desktop read-back: look the saved record up in a separate session, the way
a person does, on the WinForms fixture (UI Automation) and the RDP kiosk
fixture (OCR on pixels).

Both fixtures run their real persistence. The WinForms stand-in runs
``pn_db.py`` behind the real in-guest agent server and the real
``WindowsBackend`` client; the kiosk stand-in serves the kiosk's look-up panel
as an RDP framebuffer that the real OCR engine reads. Synthetic data only.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from pathlib import Path
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.backends.win_agent import AgentConfig, create_server
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.deployment import (
    BackendConfig,
    DeploymentConfig,
    EffectsConfig,
    build_effect_verifier,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr, Verdict
from openadapt_flow.runtime.effects.desktop_readback import (
    ControlRef,
    DesktopReadbackSpec,
    DesktopReadbackVerifier,
    DesktopSessionFacts,
    Located,
    ReadOnlyLookupGuard,
    ReadOnlyViolation,
    Seen,
    look_up,
    resolve_desktop_spec,
    save_like_words,
)
from openadapt_flow.runtime.effects.desktop_readback_sessions import (
    BackendSessionDriver,
    build_desktop_readback_verifier,
)
from openadapt_flow.verification import VerificationTier, verifier_effect_tier

from ._desktop_readback_support import (
    LOOKUP_REGION,
    KioskLookupTransport,
    WinFormsPatientNotes,
    tiny_png,
)

ENV = {"RB_USER": "reader", "RB_PASS": "synthetic-pass"}


# -- the rules, without a session ------------------------------------------------


def test_save_like_labels_are_found_as_whole_words() -> None:
    assert save_like_words("Save Note") == ["save"]
    assert save_like_words("saveButton") == ["save"]
    assert save_like_words("Sign and close") == ["close", "sign"]
    assert save_like_words("Search") == []
    assert save_like_words("Find") == []
    # A status column is data, not a command.
    assert save_like_words("REQ-1 Ada Lovelace Submitted") == []
    assert save_like_words("Sign in", sign_in=True) == []
    assert save_like_words("Sign in") == ["sign"]


def test_control_ref_names_one_path() -> None:
    with pytest.raises(ValueError, match="either"):
        ControlRef(automation_id="noteBox", text="Note")
    with pytest.raises(ValueError, match="either"):
        ControlRef()
    assert ControlRef(automation_id="noteBox").structured
    assert not ControlRef(text="Slot", at="right").structured


def _winforms_spec(**overrides: Any) -> DesktopReadbackSpec:
    data: dict[str, Any] = {
        "session": {"kind": "windows", "agent_url": "http://readback-seat:5001"},
        "search": {
            "box": {"automation_id": "searchBox"},
            "query": {"param": "patient_last"},
            "submit": {"automation_id": "searchButton"},
        },
        "open": {"results": {"automation_id": "patientGrid"}},
        "ready": {"automation_id": "patientHeader", "shows": "Patient:"},
        "identity": {
            "reader": {"automation_id": "patientHeader"},
            "value": {"param": "patient"},
            "field": "patient",
        },
        "fields": {"note": {"control": {"automation_id": "noteBox"}}},
        "navigation_timeout_s": 2.0,
        "settle_s": 0.0,
    }
    data.update(overrides)
    return DesktopReadbackSpec.model_validate(data)


def test_spec_refuses_a_web_session_and_sign_in_without_credentials() -> None:
    with pytest.raises(ValueError, match="ui_readback"):
        _winforms_spec(session={"kind": "web", "url": "http://x.test"})
    with pytest.raises(ValueError, match="credentials"):
        _winforms_spec(
            sign_in={
                "username": {"automation_id": "user"},
                "password": {"automation_id": "pass"},
                "submit": {"automation_id": "signIn"},
                "signed_in": {"automation_id": "home"},
            }
        )


class _ScriptedSurface:
    """A surface whose controls are fixed, to pin the guard."""

    facts = DesktopSessionFacts(True, True, True, "uia")

    def __init__(self, labels: dict[str, str], under: str = "") -> None:
        self.labels = labels
        self.under = under
        self.actions: list[str] = []

    def text_at(self, point: tuple[int, int]) -> Optional[str]:
        return self.under or None

    def _seen(self, ref: ControlRef) -> Located:
        key = ref.automation_id or ref.text or ""
        if key not in self.labels:
            return Located("missing")
        return Located("ok", Seen(text=self.labels[key], point=(1, 1)))

    locate = read = _seen

    def rows(self, ref: ControlRef) -> Optional[list[Seen]]:
        return None

    def type_into(self, target: Seen, text: str) -> None:
        self.actions.append(f"type {text}")

    def select_all_in(self, target: Seen) -> None:
        self.actions.append("select_all")

    def click(self, target: Seen, *, double: bool = False) -> None:
        self.actions.append(f"click {target.text}")

    def press_enter(self) -> None:
        self.actions.append("enter")

    def settle(self) -> None:
        return None


def test_guard_refuses_typing_outside_the_search_box() -> None:
    spec = _winforms_spec()
    guard = ReadOnlyLookupGuard(spec)
    surface = _ScriptedSurface({"noteBox": ""})
    with pytest.raises(ReadOnlyViolation, match="search box"):
        guard.type_into(surface, spec.fields["note"].control, Seen("", (1, 1)), "x")
    with pytest.raises(ReadOnlyViolation, match="control character"):
        guard.type_into(surface, spec.search.box, Seen("", (1, 1)), "a\nb")
    assert surface.actions == []


def test_guard_refuses_save_like_and_undeclared_clicks() -> None:
    spec = _winforms_spec(
        search={
            "box": {"automation_id": "searchBox"},
            "query": {"param": "patient_last"},
            "submit": {"automation_id": "saveButton"},
        }
    )
    guard = ReadOnlyLookupGuard(spec)
    surface = _ScriptedSurface({})
    with pytest.raises(ReadOnlyViolation, match="save-like"):
        guard.click(surface, spec.search.submit, Seen("saveButton", (1, 1)))
    with pytest.raises(ReadOnlyViolation, match="clicks only"):
        guard.click(surface, spec.ready, Seen("Patient: x", (1, 1)))
    with pytest.raises(ReadOnlyViolation, match="Enter"):
        guard.press_enter(surface, spec.search.box)
    assert surface.actions == []


def test_look_up_stops_when_typed_text_does_not_land_in_the_box() -> None:
    spec = _winforms_spec()
    target = resolve_desktop_spec(spec, {"patient_last": "Sorenson", "patient": "x"})
    surface = _ScriptedSurface({"searchBox": ""})  # the box never shows the text
    raw = look_up(surface, target, ReadOnlyLookupGuard(spec))
    assert not raw.ok
    assert "may have gone elsewhere" in raw.reason
    assert surface.actions == ["type Sorenson"]  # nothing submitted


# -- the WinForms fixture through the real agent and client -------------------------


@pytest.fixture()
def winforms(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    app = WinFormsPatientNotes(tmp_path / "patients.db", drift="decoy")
    monkeypatch.setitem(sys.modules, "uiautomation", app.uia_module())
    server = create_server(
        AgentConfig(host="127.0.0.1", port=0),
        grab_fn=tiny_png,
        input_fn=app.input_fn,
    )
    host, port = server.server_address[:2]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    app.agent_url = f"http://{host}:{port}"
    try:
        yield app
    finally:
        server.shutdown()
        server.server_close()


def _winforms_verifier(app: Any, params: dict[str, str], **spec: Any) -> Any:
    def open_backend(_user: str, _password: str) -> tuple[Any, DesktopSessionFacts]:
        backend = WindowsBackend(server_url=app.agent_url)
        return backend, DesktopSessionFacts(
            own_session=True, fresh_session=False, signed_in=False, path="uia"
        )

    return build_desktop_readback_verifier(
        _winforms_spec(**spec),
        params,
        timeout_s=0.0,
        poll_interval_s=0.0,
        driver=BackendSessionDriver(open_backend, uia=True, settle_s=0.0),
    )


NEIL = {"patient_last": "Sorenson", "patient": "Neil Sorenson"}


def _note_equals(value: str) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"patient": ValueExpr(literal="Neil Sorenson")},
        field="note",
        value=ValueExpr(literal=value),
        timeout_s=0.0,
    )


def test_winforms_save_is_confirmed_by_looking_the_patient_up(winforms: Any) -> None:
    verifier = _winforms_verifier(winforms, NEIL)
    # Without its own sign-in, a separate window re-reads saved state.
    assert (
        verifier_effect_tier(verifier) is VerificationTier.PERSISTED_STATE_REACQUISITION
    )
    assert verifier.independent_system_of_record is False

    before = verifier.capture_pre_state()
    assert before.reachable
    assert before.records == [{"note": "", "patient": "Neil Sorenson"}]

    winforms.save_note(1, "Follow up in two weeks")  # the run's save
    verdict = verifier.verify(_note_equals("Follow up in two weeks"), before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.assurance == "screen_readback"
    # The read-back typed the surname, pressed Search, opened Neil Sorenson.
    # It never pressed Save Note and never touched the note.
    assert winforms.saves == 0
    assert set(winforms.clicked) <= {"searchBox", "searchButton", "Row 0", "Row 1"}
    assert "saveButton" not in winforms.clicked
    assert winforms.all_patients()[0]["note"] == "Follow up in two weeks"


def test_winforms_wrong_note_is_refuted_through_ui_automation(winforms: Any) -> None:
    verifier = _winforms_verifier(winforms, NEIL)
    before = verifier.capture_pre_state()
    winforms.save_note(1, "Follow up in three weeks")
    verdict = verifier.verify(_note_equals("Follow up in two weeks"), before)
    assert verdict.verdict is Verdict.REFUTED
    assert "record view" in verdict.reason
    assert "system of record" not in verdict.reason


def test_winforms_decoy_with_the_same_first_name_is_not_opened(winforms: Any) -> None:
    # Searching "Neil" lists Neil Anderson above Neil Sorenson. Only the row
    # showing the full identity is opened.
    verifier = _winforms_verifier(
        winforms, {"patient_last": "Neil", "patient": "Neil Sorenson"}
    )
    before = verifier.capture_pre_state()
    winforms.save_note(1, "Seen today")
    verdict = verifier.verify(_note_equals("Seen today"), before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason


def test_winforms_two_matching_rows_is_indeterminate(winforms: Any) -> None:
    verifier = _winforms_verifier(winforms, {"patient_last": "Neil", "patient": "Neil"})
    verdict = verifier.verify(
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"patient": ValueExpr(literal="Neil")},
            field="note",
            value=ValueExpr(literal="x"),
            timeout_s=0.0,
        ),
        verifier.capture_pre_state(),
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "2 search results" in verdict.reason
    assert winforms.saves == 0


def test_winforms_search_button_declared_as_save_is_refused(winforms: Any) -> None:
    verifier = _winforms_verifier(
        winforms,
        NEIL,
        search={
            "box": {"automation_id": "searchBox"},
            "query": {"param": "patient_last"},
            "submit": {"automation_id": "saveButton"},
        },
    )
    verdict = verifier.verify(_note_equals("x"), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "save-like" in verdict.reason
    assert winforms.saves == 0


# -- the RDP kiosk through the RDP backend and real OCR --------------------------------

LOOKUP = list(LOOKUP_REGION)


def _kiosk_spec(**overrides: Any) -> DesktopReadbackSpec:
    data: dict[str, Any] = {
        "session": {"kind": "rdp", "rdp_host": "kiosk.test"},
        "credentials": {"username_env": "RB_USER", "password_env": "RB_PASS"},
        "search": {
            "box": {"text": "Find appointment", "at": "below", "region": LOOKUP},
            "query": {"param": "request_id"},
            "submit": {"text": "Find", "region": LOOKUP},
        },
        "ready": {"text": "Request ID", "at": "right", "region": LOOKUP},
        "not_found": {"text": "No appointment found", "region": LOOKUP},
        "identity": {
            "reader": {"text": "Request ID", "at": "right", "region": LOOKUP},
            "value": {"param": "request_id"},
            "field": "request_id",
        },
        "fields": {
            "patient_mrn": {
                "control": {"text": "MRN", "at": "right", "region": LOOKUP}
            },
            "appointment_slot": {
                "control": {"text": "Slot", "at": "right", "region": LOOKUP}
            },
            "visit_type": {
                "control": {"text": "Visit type", "at": "right", "region": LOOKUP},
                "casefold": True,
            },
        },
        "navigation_timeout_s": 2.0,
        "settle_s": 0.0,
        "min_ocr_confidence": 0.6,
    }
    data.update(overrides)
    return DesktopReadbackSpec.model_validate(data)


@pytest.fixture()
def kiosk(tmp_path: Path) -> KioskLookupTransport:
    pytest.importorskip("rapidocr_onnxruntime")
    return KioskLookupTransport(tmp_path / "appointments.sqlite3")


def _kiosk_verifier(kiosk: KioskLookupTransport, request_id: str) -> Any:
    def open_backend(user: str, password: str) -> tuple[Any, DesktopSessionFacts]:
        assert (user, password) == (ENV["RB_USER"], ENV["RB_PASS"])
        return FreeRDPBackend(kiosk), DesktopSessionFacts(
            own_session=True, fresh_session=True, signed_in=True, path="ocr"
        )

    return build_desktop_readback_verifier(
        _kiosk_spec(),
        {"request_id": request_id},
        timeout_s=0.0,
        poll_interval_s=0.0,
        driver=BackendSessionDriver(open_backend, settle_s=0.0),
        environ=ENV,
    )


def _booked(field: str = "visit_type", value: str = "follow-up") -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"request_id": ValueExpr(literal="REQ-4815")},
        field=field,
        value=ValueExpr(literal=value),
        timeout_s=0.0,
    )


def test_kiosk_booking_is_confirmed_from_its_own_rdp_session(
    kiosk: KioskLookupTransport,
) -> None:
    verifier = _kiosk_verifier(kiosk, "REQ-4815")
    # Its own RDP logon: a separate signed-in session (tier 2).
    assert verifier_effect_tier(verifier) is VerificationTier.INDEPENDENT_SESSION

    before = verifier.capture_pre_state()
    assert before.reachable
    assert before.records == []  # the kiosk says "No appointment found"

    kiosk.book("REQ-4815", "MRN A1001", "Ada Lovelace", "2026-11-02 09:30", "Follow-up")
    verdict = verifier.verify(_booked(), before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verifier.verify(_booked("patient_mrn", "MRN A1001"), before).confirmed
    assert verdict.assurance == "screen_readback"
    # Only the look-up box and Find were touched.
    assert kiosk.save_clicks == 0
    assert kiosk.booking_typed == ""


def test_kiosk_ocr_mismatch_is_never_refuted(kiosk: KioskLookupTransport) -> None:
    verifier = _kiosk_verifier(kiosk, "REQ-4815")
    before = verifier.capture_pre_state()
    kiosk.book("REQ-4815", "MRN A1001", "Ada Lovelace", "2026-11-02 09:30", "Intake")
    verdict = verifier.verify(_booked(), before)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "OCR can misread" in verdict.reason


def test_kiosk_count_check_without_a_results_list_halts(
    kiosk: KioskLookupTransport,
) -> None:
    # One record view can't show a duplicate, so a count check never
    # confirms from it.
    verifier = _kiosk_verifier(kiosk, "REQ-4815")
    before = verifier.capture_pre_state()
    kiosk.book("REQ-4815", "MRN A1001", "Ada Lovelace", "2026-11-02 09:30", "Follow-up")
    verdict = verifier.verify(
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"request_id": ValueExpr(literal="REQ-4815")},
            expected_count=1,
            timeout_s=0.0,
        ),
        before,
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "duplicate" in verdict.reason


def test_kiosk_record_not_found_is_not_proof_of_no_save(
    kiosk: KioskLookupTransport,
) -> None:
    verifier = _kiosk_verifier(kiosk, "REQ-4815")
    before = verifier.capture_pre_state()
    verdict = verifier.verify(_booked(), before)
    assert not verdict.confirmed
    # Never "affirmatively absent": the run ends in reconciliation.
    assert verdict.observed_count is None


# -- deployment wiring ------------------------------------------------------------------


def test_effects_config_builds_the_desktop_verifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    cfg = EffectsConfig(
        kind="desktop_readback",
        desktop_readback=_kiosk_spec().model_dump(mode="json"),
    )
    verifier = build_effect_verifier(cfg, {"request_id": "REQ-1"})
    inner = getattr(verifier, "_inner", verifier)
    assert isinstance(inner, DesktopReadbackVerifier) or (
        getattr(verifier, "substrate", None) == "desktop-readback"
    )
    assert "desktop_readback" not in EffectsConfig().model_dump()


def test_deployment_refuses_a_read_back_session_that_is_the_typing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RB_USER", "clerk")
    monkeypatch.setenv("RB_PASS", "x")
    spec = _kiosk_spec().model_dump(mode="json")
    with pytest.raises(ValueError, match="its own user"):
        DeploymentConfig(
            backend=BackendConfig(
                kind="rdp", rdp_host="kiosk.test", rdp_username="clerk"
            ),
            effects=EffectsConfig(kind="desktop_readback", desktop_readback=spec),
        )
    monkeypatch.setenv("RB_USER", "reader")
    DeploymentConfig(
        backend=BackendConfig(kind="rdp", rdp_host="kiosk.test", rdp_username="clerk"),
        effects=EffectsConfig(kind="desktop_readback", desktop_readback=spec),
    )
    windows = _winforms_spec(
        session={"kind": "windows", "agent_url": "http://vm:5001"}
    ).model_dump(mode="json")
    with pytest.raises(ValueError, match="same agent"):
        DeploymentConfig(
            backend=BackendConfig(kind="windows", agent_url="http://vm:5001"),
            effects=EffectsConfig(kind="desktop_readback", desktop_readback=windows),
        )


@contextlib.contextmanager
def _unused() -> Iterator[None]:  # pragma: no cover - keeps imports honest
    yield


# -- review findings, pinned ----------------------------------------------------------


def test_identity_must_stand_on_its_own() -> None:
    from openadapt_flow.runtime.effects.desktop_readback import _shows_identity

    assert _shows_identity("Patient: Neil Sorenson (1984-03-12)", "Neil Sorenson")
    assert _shows_identity("REQ-4815", "REQ-4815")
    assert not _shows_identity("REQ-4815-01", "REQ-4815")
    assert not _shows_identity("A.123", "123")
    assert not _shows_identity("123/2", "123")


def test_markers_and_identity_need_text_not_only_a_region() -> None:
    with pytest.raises(ValueError, match="ready needs text"):
        _kiosk_spec(ready={"region": LOOKUP})
    with pytest.raises(ValueError, match="identity.reader"):
        _kiosk_spec(
            identity={"reader": {"region": LOOKUP}, "value": {"param": "request_id"}}
        )


def test_guard_reads_what_is_under_the_point() -> None:
    spec = _winforms_spec()
    guard = ReadOnlyLookupGuard(spec)
    surface = _ScriptedSurface({}, under="Save Note")
    with pytest.raises(ReadOnlyViolation, match="save-like"):
        guard.type_into(surface, spec.search.box, Seen("", (1, 1)), "Sorenson")
    with pytest.raises(ReadOnlyViolation, match="save-like"):
        guard.click(surface, spec.search.submit, Seen("Search", (1, 1)))
    assert surface.actions == []


def test_guard_checks_the_automation_id_too() -> None:
    spec = _winforms_spec(
        search={
            "box": {"automation_id": "searchBox"},
            "query": {"param": "patient_last"},
            "submit": {"automation_id": "btnSaveAndClose"},
        }
    )
    with pytest.raises(ReadOnlyViolation, match="save-like"):
        ReadOnlyLookupGuard(spec).click(
            _ScriptedSurface({}), spec.search.submit, Seen("", (1, 1))
        )


def test_an_already_signed_in_session_is_not_a_separate_sign_in() -> None:
    spec = _winforms_spec(
        credentials={"username_env": "RB_USER", "password_env": "RB_PASS"},
        sign_in={
            "username": {"automation_id": "user"},
            "password": {"automation_id": "pass"},
            "submit": {"automation_id": "signIn"},
            "signed_in": {"automation_id": "home"},
        },
    )
    target = resolve_desktop_spec(spec, {"patient_last": "Sorenson", "patient": "x"})
    surface = _ScriptedSurface({"home": "Signed in as clerk"})
    raw = look_up(surface, target, ReadOnlyLookupGuard(spec))
    assert not raw.ok and "already signed in" in raw.reason
    assert surface.actions == []


def test_a_search_that_finds_nothing_never_confirms() -> None:
    from openadapt_flow.runtime.effects.desktop_readback import _ReadRecord

    spec = _winforms_spec(not_found={"automation_id": "statusLabel", "shows": "0"})
    verifier = DesktopReadbackVerifier(
        resolve_desktop_spec(spec, NEIL),
        driver=BackendSessionDriver(lambda u, p: (None, None)),
    )
    from openadapt_flow.runtime.effects import EffectState

    nothing = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"patient": ValueExpr(literal="Neil Sorenson")},
        expected_count=0,
        timeout_s=0.0,
    )
    verdict = verifier._judge(
        nothing,
        EffectState(substrate="desktop-readback", reachable=True, records=[]),
        _ReadRecord(True, "", [], set(), True),
    )
    assert verdict.verdict is Verdict.INDETERMINATE


def test_an_effect_on_a_field_the_look_up_does_not_read_halts(winforms: Any) -> None:
    verifier = _winforms_verifier(winforms, NEIL)
    verdict = verifier.verify(
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={"mrn": ValueExpr(literal="A1")},
            field="note",
            value=ValueExpr(literal="x"),
            timeout_s=0.0,
        ),
        verifier.capture_pre_state(),
    )
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "does not read" in verdict.reason


def test_a_session_past_its_time_is_abandoned_and_not_reopened() -> None:
    release = threading.Event()

    class SlowDriver:
        def open(self, target: Any, username: str, password: str) -> Any:
            @contextlib.contextmanager
            def ctx() -> Iterator[Any]:
                release.wait(5)
                raise RuntimeError("released")
                yield  # pragma: no cover

            return ctx()

    verifier = DesktopReadbackVerifier(
        resolve_desktop_spec(_winforms_spec(), NEIL),
        driver=SlowDriver(),
        session_timeout_s=0.2,
    )
    first = verifier.verify(_note_equals("x"), verifier_state())
    assert "did not finish in time" in first.reason
    second = verifier.verify(_note_equals("x"), verifier_state())
    assert "still running" in second.reason
    release.set()


def verifier_state() -> Any:
    from openadapt_flow.runtime.effects import EffectState

    return EffectState(substrate="desktop-readback", reachable=True, records=[])


def test_session_overlap_folds_loopback_names_and_window_titles() -> None:
    from openadapt_flow.deployment import desktop_readback_session_overlap

    windows = _winforms_spec(
        session={"kind": "windows", "agent_url": "http://localhost:5001/"}
    )
    assert desktop_readback_session_overlap(
        windows, [BackendConfig(kind="windows", agent_url="http://127.0.0.1:5001")]
    )
    assert not desktop_readback_session_overlap(
        windows, [BackendConfig(kind="windows", agent_url="http://127.0.0.1:5002")]
    )
    citrix = _kiosk_spec(session={"kind": "citrix", "rdp_window": "Citrix Viewer"})
    # No title on the read-back side matches whatever window is there.
    assert desktop_readback_session_overlap(
        citrix,
        [
            BackendConfig(
                kind="citrix", rdp_window="citrix viewer", rdp_window_title="EMR"
            )
        ],
    )


def test_run_refuses_a_cli_override_onto_the_read_back_session() -> None:
    from openadapt_flow.__main__ import _refuse_shared_readback_session

    spec = _winforms_spec(session={"kind": "windows", "agent_url": "http://seat:5001"})
    cfg = DeploymentConfig(
        backend=BackendConfig(kind="windows", agent_url="http://vm:5001"),
        effects=EffectsConfig(
            kind="desktop_readback", desktop_readback=spec.model_dump(mode="json")
        ),
    )
    # --agent-url pointed the typing backend at the read-back seat.
    overridden = cfg.backend.model_copy(update={"agent_url": "http://seat:5001"})
    assert _refuse_shared_readback_session(cfg, overridden, operation="run")
    assert not _refuse_shared_readback_session(cfg, cfg.backend, operation="run")
