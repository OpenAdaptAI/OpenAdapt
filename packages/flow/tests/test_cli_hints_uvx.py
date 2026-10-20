"""Next-step hints reach the same version under ``uvx``.

``uvx openadapt quickstart`` runs from a throwaway environment in uv's cache.
The person's shell may have no ``openadapt``, or an older one without the verb
(2026-10-20: ``openadapt flow demo`` hit a stale 1.5.1 install), so hints repeat
the ``uvx`` form there.
"""

from __future__ import annotations

import sys

from openadapt_flow.cli_hints import command, launcher_command, uvx_ephemeral

UVX = "/Users/x/.cache/uv/archive-v0/kj_30_Pw78unEQ3B"
VENV = "/Users/x/project/.venv"
TOOL = "/Users/x/.local/share/uv/tools/openadapt"
PY = f"{sys.version_info[0]}.{sys.version_info[1]}"


def test_uvx_cache_environment_is_detected() -> None:
    assert uvx_ephemeral(UVX)
    assert uvx_ephemeral("C:\\Users\\x\\AppData\\Local\\uv\\cache\\archive-v0\\abc")
    assert not uvx_ephemeral(VENV)
    assert not uvx_ephemeral(TOOL)


def test_launcher_hint_repeats_uvx_form() -> None:
    assert command("demo", argv=["/x/openadapt"], prefix=UVX) == (
        f"uvx --python {PY} openadapt flow demo"
    )
    assert launcher_command("quickstart", prefix=UVX) == (
        f"uvx --python {PY} openadapt quickstart"
    )


def test_installed_environments_keep_the_plain_form() -> None:
    assert command("demo", argv=["/x/openadapt"], prefix=VENV) == "openadapt flow demo"
    assert command("demo", argv=["/x/openadapt-flow"], prefix=TOOL) == (
        "openadapt-flow demo"
    )
    assert launcher_command("quickstart", prefix=VENV) == "openadapt quickstart"
