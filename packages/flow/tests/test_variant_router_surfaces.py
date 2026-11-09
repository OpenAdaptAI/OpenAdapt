"""``route`` reads the first screen from the surface a deployment file names.

The variant set's bundles are recorded on a desktop or remote surface. With
``--config deploy.yaml`` (or ``--backend``), ``route`` builds that backend
through the factory and reads one frame from it; it never opens a browser. See
docs/BACKEND_PARITY.md.
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow.ir import Workflow
from openadapt_flow.variants import VariantInput, VariantWhen, author_variant_set
from tests.test_admitted_composition_authoring import _envelope_for
from tests.test_variant_router import ADMISSIONS, VERSIONS, _bundle

SECTIONS = {
    "windows": "backend:\n  kind: windows\n  agent_url: http://127.0.0.1:5001\n",
    "macos": "backend:\n  kind: macos\n  macos_app: TextEdit\n",
    "linux": (
        "backend:\n  kind: linux\n  linux_app: notes\n  linux_window_title: Notes\n"
    ),
    "rdp": "backend:\n  kind: rdp\n  rdp_host: 10.0.0.5\n",
    "citrix": (
        "backend:\n  kind: citrix\n  rdp_window: wfica32\n"
        "  rdp_readiness_text: Patient Notes\n"
    ),
}


def _png(level: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (320, 200), (level, level, level)).save(buffer, format="PNG")
    return buffer.getvalue()


def _surface_set(tmp_path: Path, surface: str) -> Path:
    items = []
    for name, when in (
        ("light", VariantWhen(theme="light")),
        ("dark", VariantWhen(theme="dark")),
    ):
        bundle, _ = _bundle(tmp_path, name)
        workflow = Workflow.load(bundle)
        workflow.surface = surface  # type: ignore[assignment]
        workflow.save(bundle)
        envelope = _envelope_for(
            bundle, admission_id=ADMISSIONS[name], workflow_version_id=VERSIONS[name]
        )
        items.append(
            VariantInput(name=name, bundle=bundle, admission=envelope, when=when)
        )
    out = tmp_path / "set"
    author_variant_set(items, name="encounter-intake", out=out)
    return out


@pytest.mark.parametrize("surface", sorted(SECTIONS))
def test_route_reads_the_first_screen_from_the_configured_surface(
    tmp_path: Path, monkeypatch, capsys, surface: str
) -> None:
    import openadapt_flow.backends.factory as factory
    from openadapt_flow import cli_variants
    from openadapt_flow.__main__ import main

    built: list = []

    class Backend:
        closed = False

        def screenshot(self) -> bytes:
            return _png(18)

        def close(self) -> None:
            Backend.closed = True

    def build_backend(cfg, **kwargs):
        assert kwargs.get("page") is None
        built.append(cfg)
        return Backend()

    monkeypatch.setattr(factory, "build_backend", build_backend)
    monkeypatch.setattr(
        cli_variants,
        "_launched_web_screen",
        lambda url: pytest.fail("route must not open a browser"),
    )
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text(SECTIONS[surface])
    variant_set = _surface_set(tmp_path, surface)

    capsys.readouterr()
    rc = main(["route", str(variant_set), "--config", str(deploy)])
    decision = json.loads(capsys.readouterr().out)
    assert rc == 0, decision
    assert decision["variant"] == "dark"
    assert [cfg.kind for cfg in built] == [surface]
    assert Backend.closed


def test_route_on_a_surface_with_no_frame_halts_with_a_named_reason(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    import openadapt_flow.backends.factory as factory
    from openadapt_flow.__main__ import main

    def build_backend(cfg, **kwargs):
        raise ValueError("backend.kind 'macos' requires backend.macos_app")

    monkeypatch.setattr(factory, "build_backend", build_backend)
    deploy = tmp_path / "deploy.yaml"
    deploy.write_text("backend:\n  kind: macos\n")
    variant_set = _surface_set(tmp_path, "macos")
    capsys.readouterr()
    rc = main(["route", str(variant_set), "--config", str(deploy)])
    decision = json.loads(capsys.readouterr().out)
    assert rc != 0
    assert decision["reason"] == "first_screen_unavailable"
