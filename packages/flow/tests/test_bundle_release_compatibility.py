"""Frozen release-produced bundles load, retain their seal, and drive a real browser."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from openadapt_flow import bundle_validation as bundles
from openadapt_flow.ir import Workflow

FIXTURES = Path(__file__).parent / "fixtures/bundle_compatibility"
RELEASES = {
    "2.2.0": "98c437f12b04f11e44fbc864e051252e480a848e",
    "2.5.0": "5a3bf49a0bf2d662430386295a1c34036c064d58",
    "2.6.0": "1c832365d11e9dbe2a166485b13f7b8effd6d8d0",
}
CASES = [(release, shape) for release in RELEASES for shape in ("linear", "program")]


def hashes(directory):
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in directory.rglob("*")
        if path.is_file()
    }


@pytest.fixture(params=CASES, ids=["-".join(case) for case in CASES])
def frozen_bundle(request, tmp_path):
    release, shape = request.param
    provenance = json.loads((FIXTURES / release / "provenance.json").read_bytes())
    assert provenance["commit"] == RELEASES[release]
    assert provenance["release"] == release
    assert provenance["original_release_load_verified"] is True
    expected = next(item for item in provenance["bundles"] if item["shape"] == shape)
    source = FIXTURES / release / shape
    assert hashes(source) == expected["files"]
    bundle = tmp_path / "bundle"
    shutil.copytree(source, bundle)
    return bundle, expected


def test_frozen_bundle_original_seal_loads_without_rewriting(frozen_bundle):
    bundle, expected = frozen_bundle
    before = hashes(bundle)
    canonical = (bundle / "canonical.json").read_bytes()
    assert hashlib.sha256(canonical).hexdigest() == expected["content_digest"]
    workflow = Workflow.load(bundle)
    assert workflow.manifest.content_digest == expected["content_digest"]
    bundles.verify_integrity(workflow, bundle, workflow.manifest)
    assert hashes(bundle) == before


def test_verified_historical_bundle_can_be_resaved(frozen_bundle):
    bundle, _expected = frozen_bundle
    workflow = Workflow.load(bundle)

    workflow.save(bundle)

    reloaded = Workflow.load(bundle)
    bundles.verify_integrity(reloaded, bundle, reloaded.manifest)


@pytest.mark.parametrize("attack", ["workflow", "version", "asset"])
def test_frozen_bundle_tampering_refuses(frozen_bundle, attack):
    bundle, _ = frozen_bundle
    if attack == "asset":
        (bundle / "templates/button.png").write_bytes(b"different synthetic pixels")
    else:
        raw = json.loads((bundle / "workflow.json").read_bytes())
        if attack == "workflow":
            raw["name"] = "changed after sealing"
        else:
            raw["schema_version"] = 1
        (bundle / "workflow.json").write_text(json.dumps(raw))
    with pytest.raises(bundles.BundleIntegrityError):
        Workflow.load(bundle)


@pytest.fixture(scope="module")
def chromium():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


def test_frozen_bundle_replays_one_exact_input_in_real_chromium(
    frozen_bundle, chromium, tmp_path
):
    from openadapt_flow.backends.playwright_backend import (
        PlaywrightBackend,
        prepare_ws_guard,
    )
    from openadapt_flow.runtime import Replayer

    bundle, expected = frozen_bundle
    before = hashes(bundle)
    context = chromium.new_context(viewport={"width": 320, "height": 200})
    try:
        prepare_ws_guard(context)
        page = context.new_page()
        page.set_content("""<!doctype html><meta charset="utf-8">
<button id="apply" style="position:absolute;left:40px;top:50px;width:80px;height:40px"
 onclick="document.querySelector('#count').textContent=Number(document.querySelector('#count').textContent)+1">Apply</button>
<output id="count" style="position:absolute;left:40px;top:110px">0</output>""")
        workflow = Workflow.load(bundle)
        report = Replayer(PlaywrightBackend(page)).run(
            workflow, bundle_dir=bundle, run_dir=tmp_path / "run"
        )
        assert report.success, [
            (result.step_id, result.error) for result in report.results
        ]
        assert page.locator("#count").inner_text() == "1"
        assert len(report.results) == 1
        assert report.results[0].resolution.rung == "structural"
        assert workflow.manifest.content_digest == expected["content_digest"]
        assert hashes(bundle) == before
    finally:
        context.close()
