"""Native structural previews cannot become semantic values or effects."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.backends.win_agent.value_protocol import (
    locator_scope,
    valid_structural_value,
)
from openadapt_flow.backends.windows_backend import WindowsBackend
from openadapt_flow.ir import StructuralLocator
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr, Verdict
from openadapt_flow.runtime.effects.desktop_readback import (
    ControlRef,
    DesktopReadbackSpec,
    DesktopReadbackVerifier,
    DesktopSessionFacts,
    Located,
    Seen,
    resolve_desktop_spec,
)
from openadapt_flow.runtime.effects.desktop_readback_sessions import UiaReadSurface
from tests.test_approval_form_fields import _agent, _Control, _proof, _window

SCOPE = locator_scope({"automation_id": "field"})


def complete(value):
    return {
        "status": "ok",
        "match": "unique",
        "candidate_count": 1,
        "point": [1, 2],
        "text": value,
        "complete": True,
        "value_proof": _proof(value),
        "value_identity": "a" * 64,
        "read_scope": SCOPE,
        "preview_text": value[:4096],
        "reason": None,
    }


def unavailable(preview="prefix"):
    return {
        **complete(""),
        "text": None,
        "complete": False,
        "value_proof": None,
        "value_identity": None,
        "preview_text": preview,
        "reason": "value_unavailable",
    }


def client(payload):
    backend = object.__new__(WindowsBackend)
    backend._post_typed_read = lambda *_a, **_k: SimpleNamespace(
        available=True, payload=payload
    )
    return backend


def surface(payload):
    return UiaReadSurface(
        client(payload), facts=DesktopSessionFacts(True, True, False, "uia"), settle_s=0
    )


@given(st.text(max_size=10000))
def test_complete_wire_and_surface_retain_full_value_without_preview_clipping(value):
    payload = complete(value)
    backend = client(payload)
    assert (
        backend.read_structural(StructuralLocator(automation_id="field"))["text"]
        == value
    )
    seen = surface(payload).read(ControlRef(automation_id="field"))
    assert seen.status == "ok" and seen.item.text == value


@pytest.mark.parametrize(
    "fault",
    [
        "legacy",
        "missing_proof",
        "bad_hash",
        "bad_extent",
        "bad_identity",
        "wrong_scope",
        "bool_count",
        "missing_point",
        "extra",
        "incomplete_text",
    ],
)
def test_missing_mismatched_or_legacy_value_proof_cannot_create_seen(fault):
    payload = complete("full value")
    if fault == "legacy":
        payload = {
            key: payload[key]
            for key in ("status", "match", "candidate_count", "point", "text")
        }
    elif fault == "missing_proof":
        payload["value_proof"] = None
    elif fault == "bad_hash":
        payload["value_proof"]["sha256_utf16le"] = "0" * 64
    elif fault == "bad_extent":
        payload["value_proof"]["utf16_units"] += 1
    elif fault == "bad_identity":
        payload["value_identity"] = None
    elif fault == "wrong_scope":
        payload["read_scope"] = locator_scope({"automation_id": "other"})
    elif fault == "bool_count":
        payload["candidate_count"] = True
    elif fault == "missing_point":
        payload["point"] = None
    elif fault == "extra":
        payload["unchecked"] = True
    else:
        payload = {**unavailable(), "text": "prefix"}
    assert (
        client(payload).read_structural(StructuralLocator(automation_id="field"))
        is None
    )
    # Also enforce proof on a direct backend implementation bypassing the
    # Windows wire client, rather than trusting a complete=True caller flag.
    direct = SimpleNamespace(read_structural=lambda *_a, **_k: payload)
    observed = UiaReadSurface(
        direct, facts=DesktopSessionFacts(True, True, False, "uia"), settle_s=0
    ).read(ControlRef(automation_id="field"))
    assert observed.status != "ok" and observed.item is None


@pytest.mark.parametrize("kind", ["EditControl", "DocumentControl", "TextControl"])
def test_server_uses_full_shared_observation_and_separates_preview(monkeypatch, kind):
    import sys

    control = _Control(kind, value="z" * 8196)
    control.AutomationId = "field"
    perform = _agent(monkeypatch, _window(control))
    sys.modules["uiautomation"].GetRootControl = lambda: control.parent
    answer = perform("read", {"locator": {"automation_id": "field"}})
    assert valid_structural_value(answer, SCOPE) and answer["text"] == "z" * 8196
    assert len(answer["preview_text"]) == 4096
    control.native_override = "z" * 8196 + "different storage"
    refused = perform("read", {"locator": {"automation_id": "field"}})
    assert refused["text"] is None and not refused["complete"]
    assert (
        refused["value_proof"] is None
        and refused["reason"] == "provider_native_value_disagreement"
    )
    assert refused["preview_text"] == "z" * 4096


@pytest.mark.parametrize(
    "fault",
    ["child_error", "depth_limit", "ambiguous", "name_only", "hidden_matching_bounds"],
)
def test_unqualified_scope_and_name_only_sources_never_become_values(
    monkeypatch, fault
):
    import sys

    import openadapt_flow.backends.win_agent.server as server

    control = _Control("EditControl", value="exact")
    control.AutomationId = "field"
    window = _window(control)
    perform = _agent(monkeypatch, window)
    sys.modules["uiautomation"].GetRootControl = lambda: window
    if fault == "child_error":
        control.GetChildren = lambda: (_ for _ in ()).throw(RuntimeError("unreadable"))
    elif fault == "depth_limit":
        monkeypatch.setattr(server, "_MAX_UIA_DEPTH", 0)
    elif fault == "ambiguous":
        other = _Control("EditControl", value="exact")
        other.AutomationId = "field"
        window._children.append(other)
    elif fault == "name_only":
        control._value = None
        control.Name = "exact"
    else:
        other = _Control("EditControl", value="same locator but unreadable bounds")
        other.AutomationId = "field"
        other.BoundingRectangle = None
        window._children.append(other)
    answer = perform("read", {"locator": {"automation_id": "field"}})
    assert answer["text"] is None and answer["complete"] is False


def test_row_previews_cannot_authorize_inventory_even_when_empty():
    payload = {
        **complete("value"),
        "rows": None,
        "rows_truncated": True,
        "rows_complete": False,
        "preview_rows": [],
    }
    assert (
        client(payload).read_structural(
            StructuralLocator(automation_id="field"), rows=True
        )
        is None
    )
    direct = SimpleNamespace(
        read_structural=lambda *_a, **_k: {
            **payload,
            "rows": [],
            "rows_truncated": False,
        }
    )
    observed = UiaReadSurface(
        direct, facts=DesktopSessionFacts(True, True, False, "uia"), settle_s=0
    )
    assert observed.rows(ControlRef(automation_id="field")) is None


@pytest.mark.parametrize(
    "actual,expected,outcome,later_unavailable,ocr",
    [
        ("short", "short", Verdict.CONFIRMED, False, False),
        ("short", "other", Verdict.REFUTED, False, False),
        ("p" * 8196, "p" * 8196, Verdict.CONFIRMED, False, False),
        ("p" * 8196, "p" * 4096, Verdict.REFUTED, False, False),
        ("value" + " " * 4100, "value", Verdict.REFUTED, False, False),
        ("  value\r\n", "  value\r\n", Verdict.CONFIRMED, False, False),
        (None, "p" * 4096, Verdict.INDETERMINATE, False, False),
        ("short", "other", Verdict.REFUTED, True, False),
        ("short", "other", Verdict.INDETERMINATE, False, True),
    ],
)
def test_normal_effect_contract_requires_complete_value(
    actual, expected, outcome, later_unavailable, ocr
):
    # An ordinary declared-source unit fixture, with no server/SQL baseline
    # replay and no native execution or system-of-record qualification claim.
    field_surface = surface(
        complete(actual) if actual is not None else unavailable("p" * 4096)
    )
    spec = DesktopReadbackSpec.model_validate(
        {
            "session": {"kind": "windows"},
            "search": {"box": {"automation_id": "search"}, "query": "record"},
            "ready": {"automation_id": "ready", "shows": "Ready"},
            "identity": {
                "reader": {"automation_id": "identity"},
                "value": "record",
                "field": "id",
            },
            "fields": {
                "field": {
                    "control": {"text": "Field", "at": "right"}
                    if ocr
                    else {"automation_id": "field"}
                }
            },
            "navigation_timeout_s": 0.1,
            "settle_s": 0,
        }
    )

    class DeclaredSurface:
        facts = DesktopSessionFacts(True, True, False, "uia")
        entered = False

        def __init__(self, source):
            self.source = source

        def read(self, ref):
            if ref.automation_id == "field" or ref.text == "Field":
                if ocr:
                    return Located(
                        "ok", Seen(actual, (1, 2), ocr=True, confidence=0.95)
                    )
                return self.source.read(ref)
            if ref.automation_id == "ready":
                return Located(
                    "ok", Seen("Ready" if self.entered else "Loading", (1, 2))
                )
            return Located("ok", Seen("record", (1, 2)))

        locate = read

        def press_enter(self):
            self.entered = True

        def settle(self):
            pass

        def text_at(self, _):
            return "record"

    class Driver:
        opens = 0

        @contextmanager
        def open(self, *_):
            self.opens += 1
            source = (
                surface(unavailable())
                if later_unavailable and self.opens > 2
                else field_surface
            )
            yield DeclaredSurface(source)

    driver = Driver()
    verifier = DesktopReadbackVerifier(
        resolve_desktop_spec(spec), driver=driver, poll_interval_s=0.01
    )
    before = verifier.capture_pre_state()
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": ValueExpr(literal="record")},
        field="field",
        value=ValueExpr(literal=expected),
        timeout_s=0.2 if later_unavailable else 0,
    )
    verdict = verifier.verify(effect, before)
    assert verdict.verdict is outcome
    if ocr:
        assert "OCR can misread" in verdict.reason
    if later_unavailable:
        assert driver.opens > 2


def test_incomplete_native_marker_cannot_prove_ready_or_record_absence():
    from openadapt_flow.runtime.effects.desktop_readback import _present

    source = surface(unavailable("Visible marker preview"))
    marker = ControlRef(automation_id="field")
    assert source.locate(marker).status == "ok"
    assert source.read(marker).status == "unavailable"
    assert _present(source, marker) is None
    assert _present(surface(complete("Ready")), marker) is True
    assert _present(surface(complete("")), marker) is None
    explicit = ControlRef(automation_id="field", shows="Ready")
    assert _present(surface(complete("Loading")), explicit) is False
    assert _present(surface(unavailable("Loading")), explicit) is None


@pytest.mark.parametrize(
    "marker,before,after,expected_ok,expected_not_found",
    [
        ("ready", None, "Ready", False, False),
        ("ready", "Loading", "Ready", True, False),
        ("not_found", None, "No record", False, False),
        ("not_found", "Waiting", "No record", True, True),
    ],
)
def test_unknown_positive_and_negative_markers_do_not_supply_absence(
    marker,
    before,
    after,
    expected_ok,
    expected_not_found,
):
    from openadapt_flow.runtime.effects.desktop_readback import (
        ReadOnlyLookupGuard,
        look_up,
    )

    spec = DesktopReadbackSpec.model_validate(
        {
            "session": {"kind": "windows"},
            "search": {"box": {"automation_id": "search"}, "query": "record"},
            "ready": {"automation_id": "ready", "shows": "Ready"},
            "not_found": {"automation_id": "not_found", "shows": "No record"}
            if marker == "not_found"
            else None,
            "identity": {
                "reader": {"automation_id": "identity"},
                "value": "record",
                "field": "id",
            },
            "fields": {"field": {"control": {"automation_id": "field"}}},
            "navigation_timeout_s": 0.01,
            "settle_s": 0,
        }
    )

    class Markers:
        entered = False

        def read(self, ref):
            if ref.automation_id == marker:
                value = after if self.entered else before
                payload = complete(value) if value is not None else unavailable()
                payload["read_scope"] = locator_scope({"automation_id": marker})
                return surface(payload).read(ref)
            if ref.automation_id in {"ready", "not_found"}:
                return Located("unavailable")
            return Located(
                "ok",
                Seen("record" if ref.automation_id != "field" else "value", (1, 2)),
            )

        locate = read

        def press_enter(self):
            self.entered = True

        def settle(self):
            pass

        def text_at(self, _):
            return "record"

    target = resolve_desktop_spec(spec)
    raw = look_up(Markers(), target, ReadOnlyLookupGuard(spec))
    assert raw.ok is expected_ok and raw.not_found is expected_not_found


def test_ocr_mismatch_and_missing_native_marker_remain_unknown():
    from openadapt_flow.runtime.effects.desktop_readback import _present

    marker = ControlRef(automation_id="field", shows="Ready")

    class Source:
        def read(self, _):
            return Located("ok", Seen("Loading", (1, 2), ocr=True))

    assert _present(Source(), marker) is None
    missing = {
        **unavailable(),
        "match": "not_found",
        "candidate_count": 0,
        "point": None,
    }
    assert _present(surface(missing), marker) is None


def test_explicit_native_transforms_do_not_imply_whitespace_loss():
    from openadapt_flow.runtime.effects.desktop_readback import (
        DesktopFieldReader,
        _desktop_effect,
        _exact_value,
    )

    reference = ControlRef(automation_id="field")
    exact = DesktopFieldReader(control=reference)
    assert _exact_value("  Value\r\n", exact) == ("  Value\r\n", True)
    folded = DesktopFieldReader(control=reference, casefold=True)
    assert _exact_value(" A  B ", folded) == (" a  b ", True)
    dated = DesktopFieldReader(control=reference, date_format="%d/%m/%Y")
    observed, parsed = _exact_value("20/11/2026", dated)
    effect = Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"id": ValueExpr(literal="record")},
        field="field",
        value=ValueExpr(literal="2026-11-20"),
    )
    transformed = _desktop_effect(effect, {"field": dated}, set())
    assert parsed and observed == transformed.value.literal == "2026-11-20"
    assert effect.value.literal == "2026-11-20"
    assert _exact_value("invalid", dated)[1] is False
