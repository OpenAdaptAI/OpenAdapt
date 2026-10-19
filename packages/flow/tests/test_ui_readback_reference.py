"""The separate-session UI read-back when the record's identifier is issued
at the save (a claim, submission, or confirmation number).

``reference`` reads that identifier once from the run's own session right
after the save, then the separate session opens the record page by it. A fake
driver stands in for the browser; ``tests/test_ui_readback_browser.py`` runs
the same path against real pages.
"""

from __future__ import annotations

import contextlib
from typing import Any, Iterator, Optional

import pytest

from openadapt_flow.assurance import SCREEN_READBACK, UNCHECKED
from openadapt_flow.deployment import (
    EffectsConfig,
    _effect_verifier_config_identity,
    build_effect_verifier,
)
from openadapt_flow.mockmed.chart_pages import (
    READER_PASSWORD,
    READER_USERNAME,
    chart_readback_spec,
)
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr, Verdict
from openadapt_flow.runtime.effects.ui_readback import (
    RawPage,
    SeparateSessionReadbackVerifier,
    SessionFacts,
    UiReadbackSpec,
    build_ui_readback_verifier,
    resolve_spec,
)

BASE = "http://chart.test"
ENV = {
    "CHART_READER_USERNAME": READER_USERNAME,
    "CHART_READER_PASSWORD": READER_PASSWORD,
}
FRESH = SessionFacts(own_browser=True, fresh_context=True, signed_in=True)
URL_PATTERN = r"/chart/patient/([^/?#]+)"
SCREEN_PATTERN = r"Reference[:\s]+([A-Z]{3}-\d{4,})"


class _Session:
    def __init__(self, pages: list[RawPage]) -> None:
        self._pages = pages
        self.facts = FRESH
        self.reads = 0

    def read(self) -> RawPage:
        page = self._pages[min(self.reads, len(self._pages) - 1)]
        self.reads += 1
        return page


class RecordingDriver:
    """One scripted session per ``open``; remembers which URL it was sent to."""

    def __init__(self, *pages: RawPage) -> None:
        self._pages = list(pages)
        self.urls: list[str] = []

    def open(self, target: Any, username: str, password: str) -> Any:
        self.urls.append(target.record_url)
        session = _Session(self._pages)

        @contextlib.contextmanager
        def ctx() -> Iterator[_Session]:
            yield session

        return ctx()


class RunSession:
    """The run's own session: a page URL and a screenshot."""

    def __init__(self, url: Optional[str] = None, png: bytes = b"png") -> None:
        self._url = url
        self._png = png
        self.screenshots = 0

    def url(self) -> Optional[str]:
        return self._url

    def screenshot(self) -> bytes:
        self.screenshots += 1
        return self._png


class _Line:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeVision:
    def __init__(self, *lines: str) -> None:
        self._lines = lines
        self.regions: list[Any] = []

    def ocr(self, png: bytes, *, region: Any = None) -> list[_Line]:
        self.regions.append(region)
        return [_Line(text) for text in self._lines]


def _reference_spec(**reference: Any) -> dict[str, Any]:
    data = chart_readback_spec(BASE)
    data["url_params"] = {}
    data["identity"] = {"reader": {"selector": "#chart-patient"}, "field": "claim_ref"}
    data["reference"] = {"url_param": "patient", "pattern": URL_PATTERN, **reference}
    return data


def _verifier(
    driver: RecordingDriver,
    *,
    backend: Any = None,
    vision: Any = None,
    **reference: Any,
) -> SeparateSessionReadbackVerifier:
    return build_ui_readback_verifier(
        UiReadbackSpec.model_validate(_reference_spec(**reference)),
        {},
        timeout_s=0.0,
        poll_interval_s=0.0,
        driver=driver,
        environ=ENV,
        backend=backend,
        vision=vision,
    )


def _saved(identity: str = "CLM-20431") -> RawPage:
    return RawPage(
        ok=True,
        identity=identity,
        rows=[{"id": "e9", "type": "Triage", "note": "water damage, kitchen"}],
    )


def _effect(**kw: Any) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"type": "Triage"},
        field="note",
        value=ValueExpr(literal="water damage, kitchen"),
        risk="irreversible",
        timeout_s=0.0,
        **kw,
    )


def test_reads_the_reference_from_the_run_url_and_opens_that_record() -> None:
    driver = RecordingDriver(_saved())
    verifier = _verifier(
        driver, backend=RunSession(url=BASE + "/chart/patient/CLM-20431?saved=1")
    )
    verdict = verifier.verify(_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.assurance == SCREEN_READBACK
    assert driver.urls == [BASE + "/chart/patient/CLM-20431"]
    # The captured identifier is carried on the matched record for the audit.
    assert verdict.matched_records[0]["claim_ref"] == "CLM-20431"


def test_reads_the_reference_from_the_run_screen() -> None:
    driver = RecordingDriver(_saved())
    vision = FakeVision("Claim submitted", "Reference: CLM-20431", "Thank you")
    run = RunSession()
    verifier = _verifier(
        driver,
        backend=run,
        vision=vision,
        source="screen",
        pattern=SCREEN_PATTERN,
        region=(0, 0, 400, 200),
    )
    verdict = verifier.verify(_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.CONFIRMED
    assert driver.urls == [BASE + "/chart/patient/CLM-20431"]
    assert run.screenshots == 1
    assert vision.regions == [(0, 0, 400, 200)]


def test_a_bound_backend_is_used_after_build() -> None:
    driver = RecordingDriver(_saved())
    verifier = _verifier(driver)
    verifier.bind_backend(RunSession(url=BASE + "/chart/patient/CLM-20431"))
    assert verifier.verify(_effect(), verifier.capture_pre_state()).confirmed


@pytest.mark.parametrize(
    ("backend", "reason"),
    [
        (None, "no run session is bound"),
        (RunSession(url=BASE + "/chart/search"), "shows no record reference"),
        (RunSession(url=None), "does not expose its page URL"),
    ],
)
def test_no_reference_means_no_check(backend: Any, reason: str) -> None:
    driver = RecordingDriver(_saved())
    verifier = _verifier(driver, backend=backend)
    verdict = verifier.verify(_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.unavailable is True
    assert verdict.assurance == UNCHECKED
    assert reason in verdict.reason
    assert driver.urls == []


def test_two_different_references_on_screen_are_refused() -> None:
    driver = RecordingDriver(_saved())
    verifier = _verifier(
        driver,
        backend=RunSession(),
        vision=FakeVision("Reference: CLM-20431", "Previous Reference: CLM-20430"),
        source="screen",
        pattern=SCREEN_PATTERN,
    )
    verdict = verifier.verify(_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "2 different record references" in verdict.reason
    assert "CLM-2043" not in verdict.reason
    assert driver.urls == []


def test_the_same_reference_shown_twice_is_one_reference() -> None:
    driver = RecordingDriver(_saved())
    verifier = _verifier(
        driver,
        backend=RunSession(),
        vision=FakeVision("Reference: CLM-20431", "Your Reference: CLM-20431"),
        source="screen",
        pattern=SCREEN_PATTERN,
    )
    assert verifier.verify(_effect(), verifier.capture_pre_state()).confirmed


def test_a_page_for_another_record_is_indeterminate() -> None:
    driver = RecordingDriver(_saved(identity="CLM-99999"))
    verifier = _verifier(
        driver, backend=RunSession(url=BASE + "/chart/patient/CLM-20431")
    )
    verdict = verifier.verify(_effect(), verifier.capture_pre_state())
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "different record identifier" in verdict.reason


def test_a_wrong_value_on_the_record_is_refuted() -> None:
    page = RawPage(
        ok=True,
        identity="CLM-20431",
        rows=[{"id": "e9", "type": "Triage", "note": "water damage, bathroom"}],
    )
    verifier = _verifier(
        RecordingDriver(page), backend=RunSession(url=BASE + "/chart/patient/CLM-20431")
    )
    assert verifier.verify(_effect(), verifier.capture_pre_state()).verdict is (
        Verdict.REFUTED
    )


def test_a_url_unsafe_reference_stays_on_the_record_origin() -> None:
    driver = RecordingDriver(_saved(identity="a/b?c"))
    verifier = _verifier(
        driver,
        backend=RunSession(),
        vision=FakeVision("Reference: a/b?c"),
        source="screen",
        pattern=r"Reference: (\S+)",
    )
    verifier.verify(_effect(), verifier.capture_pre_state())
    assert driver.urls == [BASE + "/chart/patient/a%2Fb%3Fc"]


def test_there_is_no_pre_state_and_baseline_effects_refuse_before_input() -> None:
    driver = RecordingDriver(_saved())
    verifier = _verifier(driver, backend=RunSession(url=BASE + "/chart/patient/X"))
    before = verifier.capture_pre_state()
    assert before.reachable is False
    assert "captured after the save" in before.detail["reason"]
    assert driver.urls == []  # nothing was opened before the save
    assert (
        verifier.requires_readable_pre_state_for(_effect(forbid_collateral_loss=False))
        is False
    )
    assert verifier.requires_readable_pre_state_for(_effect())
    counted = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"type": "Triage"},
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=False,
        timeout_s=0.0,
    )
    assert verifier.requires_readable_pre_state_for(counted)


def test_without_a_reference_the_pre_state_rule_is_unchanged() -> None:
    plain = build_ui_readback_verifier(
        UiReadbackSpec.model_validate(chart_readback_spec(BASE)),
        {"patient_id": "p1"},
        driver=RecordingDriver(_saved()),
        environ=ENV,
    )
    counted = Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={"type": "Triage"},
        expected_count=1,
        count_new_only=True,
        forbid_collateral_loss=False,
    )
    for effect in (counted, _effect(), _effect(forbid_collateral_loss=False)):
        assert plain.requires_readable_pre_state_for(effect) is bool(
            effect.requires_baseline or effect.forbid_collateral_loss
        )
    assert (
        plain.requires_readable_pre_state_for(_effect(forbid_collateral_loss=False))
        is False
    )


def test_the_probe_signs_in_without_a_record() -> None:
    driver = RecordingDriver(_saved())
    probe = _verifier(driver).test_connection()
    assert probe.ok is True
    assert "captured after the save" in probe.reason
    assert driver.urls == [BASE + "/chart/patient/reference-not-captured-yet"]


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (
            {"identity": {"reader": {"selector": "#x"}, "value": "p1"}},
            "must be omitted",
        ),
        ({"record_url": BASE + "/chart/patient"}, "no {patient} placeholder"),
        ({"url_params": {"patient": "p1"}}, "also in url_params"),
        ({"reference": {"url_param": "patient", "pattern": "CLM"}}, "exactly one"),
        ({"reference": {"url_param": "patient", "pattern": "(a)(b)"}}, "exactly one"),
        ({"reference": {"url_param": "patient", "pattern": "("}}, "is invalid"),
        (
            {
                "reference": {
                    "url_param": "patient",
                    "pattern": "(a)",
                    "region": [0, 0, 9, 9],
                }
            },
            "needs source: screen",
        ),
        (
            {
                "reference": {
                    "url_param": "patient",
                    "pattern": "(a)",
                    "source": "screen",
                    "region": [0, 0, 0, 9],
                }
            },
            "must have a size",
        ),
    ],
)
def test_spec_validation(change: dict[str, Any], message: str) -> None:
    data = _reference_spec()
    data.update(change)
    with pytest.raises(ValueError, match=message):
        UiReadbackSpec.model_validate(data)


def test_identity_value_is_still_required_without_a_reference() -> None:
    data = chart_readback_spec(BASE)
    data["identity"] = {"reader": {"selector": "#chart-patient"}}
    with pytest.raises(ValueError, match="identity.value is required"):
        UiReadbackSpec.model_validate(data)


def test_resolve_spec_needs_a_reference_exactly_when_declared() -> None:
    with_ref = UiReadbackSpec.model_validate(_reference_spec())
    with pytest.raises(ValueError, match="no reference was captured"):
        resolve_spec(with_ref, {})
    bound = resolve_spec(with_ref, {}, reference="CLM-1")
    assert bound.record_url == BASE + "/chart/patient/CLM-1"
    assert bound.identity_value == "CLM-1"
    plain = UiReadbackSpec.model_validate(chart_readback_spec(BASE))
    with pytest.raises(ValueError, match="ui_readback.reference is not set"):
        resolve_spec(plain, {"patient_id": "p1"}, reference="CLM-1")


def test_deployment_wires_the_run_session(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    cfg = EffectsConfig.model_validate(
        {
            "kind": "ui_readback",
            "ui_readback": _reference_spec(),
            "evidence_redact_fields": ["note"],
        }
    )
    verifier = build_effect_verifier(cfg, {})
    run = RunSession(url=BASE + "/chart/patient/CLM-7")
    verifier.bind_backend(run)
    inner = verifier._inner
    assert isinstance(inner, SeparateSessionReadbackVerifier)
    assert inner._backend is run
    assert verifier.requires_readable_pre_state_for(
        _effect(forbid_collateral_loss=True)
    )
    # A reference section enters the verifier identity; its absence does not.
    plain = EffectsConfig.model_validate(
        {"kind": "ui_readback", "ui_readback": chart_readback_spec(BASE)}
    )
    plain_verifier = build_effect_verifier(plain, {"patient_id": "p1"})
    assert _effect_verifier_config_identity(plain, plain_verifier) == (
        "sha256:b9c837bb91e9f4f75dc6ff4dff79e53fcb56e92c87ae552354d08c7296c6c1de"
    )
    assert _effect_verifier_config_identity(cfg, verifier) != (
        _effect_verifier_config_identity(plain, plain_verifier)
    )
