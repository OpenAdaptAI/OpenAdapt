"""Tests for openadapt.error_reporting: filtering + gating (no network)."""

from openadapt import error_reporting as er
from openadapt.config import settings


def _hint(exc: Exception) -> dict:
    return {"exc_info": (type(exc), exc, None)}


def test_drops_unsupported_environment_errors() -> None:
    for msg in [
        "this platform is not supported: failed to acquire X connection",
        "ScreenShotError: $DISPLAY not set.",
        "No module named 'pywinauto'",
        "module 'pywinauto' has no attribute 'application'",
        "_ARRAY_API not found",
    ]:
        assert er.before_send_event({}, _hint(RuntimeError(msg))) is None


def test_keeps_real_errors() -> None:
    event = {"id": "real"}
    result = er.before_send_event(event, _hint(ValueError("real extraction bug")))
    assert result is event


def test_filter_reads_event_exception_values_too() -> None:
    event = {
        "exception": {
            "values": [{"type": "ImportError", "value": "No module named 'pywinauto'"}]
        }
    }
    assert er.before_send_event(event, {}) is None


def test_disabled_by_default_from_source(monkeypatch) -> None:
    monkeypatch.setattr(er.sys, "frozen", False, raising=False)
    monkeypatch.setattr(settings, "error_reporting_enabled", True)
    monkeypatch.setattr(settings, "error_reporting_force", False)
    assert er._reporting_enabled() is False


def test_enabled_when_frozen(monkeypatch) -> None:
    monkeypatch.setattr(er.sys, "frozen", True, raising=False)
    monkeypatch.setattr(settings, "error_reporting_enabled", True)
    assert er._reporting_enabled() is True


def test_master_switch_off(monkeypatch) -> None:
    monkeypatch.setattr(er.sys, "frozen", True, raising=False)
    monkeypatch.setattr(settings, "error_reporting_enabled", False)
    assert er._reporting_enabled() is False


def test_force_enables_from_source(monkeypatch) -> None:
    monkeypatch.setattr(er.sys, "frozen", False, raising=False)
    monkeypatch.setattr(settings, "error_reporting_enabled", True)
    monkeypatch.setattr(settings, "error_reporting_force", True)
    assert er._reporting_enabled() is True


def test_configure_never_raises_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(settings, "error_reporting_enabled", False)
    er.configure_error_reporting()  # no-op, must not raise
