"""Unavailable native APIs remain unknown, not a claimed grant."""

import sys
from types import ModuleType

import pytest

from openadapt_desktop import dispatch


@pytest.mark.parametrize(
    "module, symbol, check",
    [
        ("Quartz", "CGPreflightScreenCaptureAccess", dispatch._mac_preflight_screen),
        ("ApplicationServices", "AXIsProcessTrusted", dispatch._mac_preflight_accessibility),
    ],
)
@pytest.mark.parametrize("answer", [True, False, None])
def test_passive_permission_result_preserves_unknown(monkeypatch, module, symbol, check, answer):
    fake = ModuleType(module)

    def probe():
        if answer is None:
            raise RuntimeError("unavailable synthetic permission API")
        return answer

    setattr(fake, symbol, probe)
    monkeypatch.setitem(sys.modules, module, fake)
    assert check() is answer
