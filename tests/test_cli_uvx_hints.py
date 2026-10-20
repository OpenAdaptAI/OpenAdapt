"""The quickstart's next steps reach the same version under ``uvx``."""

from __future__ import annotations

import sys

from openadapt import cli


def test_hint_repeats_uvx_form_in_a_uvx_environment(monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/Users/x/.cache/uv/archive-v0/kj_30_Pw78")
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert cli._hint("flow demo") == f"uvx --python {py} openadapt flow demo"


def test_hint_keeps_plain_form_when_installed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/openadapt")
    assert cli._hint("flow lint bundle") == "openadapt flow lint bundle"
