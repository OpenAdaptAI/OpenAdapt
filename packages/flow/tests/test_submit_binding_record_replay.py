"""Record -> compile -> replay: an icon-only Save bound to the form it submits.

The page has openIMIS's claim-form shape: an unlabeled back chevron in the form
header, three fields, a column header row level with the Save (so the step's
identity band is armed, as on openIMIS), and a floating floppy-disk Save with
no text, accessible name, or tooltip, outside the form. Before the binding,
Flow refused the Save: a consequential click it could not tie to a record.

The demonstration is recorded through the real ``Recorder`` on Chromium,
compiled by the real compiler (OCR included), and replayed by the real
``Replayer`` with different values.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.compiler import compile_recording
from openadapt_flow.ir import ActionKind, Workflow
from openadapt_flow.policy import lint_workflow
from openadapt_flow.recorder import Recorder
from openadapt_flow.runtime import Replayer

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(600)

SAVE_ICON = (
    "M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 "
    "16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"
)
TRASH_ICON = (
    "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"
)
BACK_ICON = "M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"


def _page(extra_icon: bool = False) -> str:
    extra = (
        '<button type="button" class="discard" style="position:fixed;'
        "right:24px;bottom:110px;width:56px;height:56px;border:0;"
        'border-radius:50%;background:#7a1f1f" onclick="window.actions.'
        'push(\'discard\')"><svg width="24" height="24" viewBox="0 0 24 24">'
        f'<path fill="#fff" d="{TRASH_ICON}"></path></svg></button>'
        if extra_icon
        else ""
    )
    return f"""<!doctype html><html><head><meta charset="utf-8"><title>Claim</title>
<style>
body {{ font-family: Arial, sans-serif; margin: 0; background: #e6f2f2; }}
.head {{ display: flex; align-items: center; padding: 12px 24px;
         background: #b9d9d9; }}
.head h1 {{ font-size: 22px; margin: 0 0 0 12px; font-weight: normal; }}
.icon {{ width: 40px; height: 40px; border: 0; background: transparent; }}
.panel {{ padding: 24px; display: grid; grid-template-columns: 1fr 1fr;
          gap: 24px 48px; max-width: 900px; }}
label {{ display: block; font-size: 14px; color: #555; }}
input {{ width: 100%; font-size: 18px; border: 0; border-bottom: 1px solid #888;
         background: transparent; padding: 6px 0; }}
.fab {{ position: fixed; right: 24px; bottom: 24px; }}
.fab button {{ width: 56px; height: 56px; border-radius: 50%; border: 0;
               background: #00636b; }}
.columns {{ position: fixed; left: 40px; bottom: 40px; width: 1000px;
            display: flex; gap: 120px; font-size: 16px; color: #333; }}
#status {{ padding: 24px; font-size: 20px; }}
</style></head><body>
<form id="claim" autocomplete="off" onsubmit="return false">
  <div class="head">
    <button type="button" class="icon back"
      onclick="window.actions.push('back')"><svg width="24" height="24"
      viewBox="0 0 24 24"><path d="{BACK_ICON}"></path></svg></button>
    <h1>Claim</h1>
  </div>
  <div class="panel">
    <div><label for="ins">Insurance No.</label><input id="ins"></div>
    <div><label for="cno">Claim No.</label><input id="cno"></div>
    <div><label for="expl">Explanation</label><input id="expl"></div>
  </div>
</form>
<div class="fab"><button type="button" class="savefab"
  onclick="window.actions.push('save');
           document.getElementById('status').textContent = 'Claim saved'"><svg
  width="24" height="24" viewBox="0 0 24 24"><path fill="#fff"
  d="{SAVE_ICON}"></path></svg></button></div>
{extra}
<div id="status">Not saved yet</div>
<div class="columns"><span>Service</span><span>Quantity</span><span>Price</span>
  <span>Explanation</span></div>
<script>window.actions = [];</script>
</body></html>"""


@pytest.fixture(scope="module")
def pages(tmp_path_factory: pytest.TempPathFactory) -> dict[str, str]:
    root = tmp_path_factory.mktemp("claims-pages")
    urls = {}
    for name, html in (("claims", _page()), ("two-icons", _page(extra_icon=True))):
        path = root / f"{name}.html"
        path.write_text(html)
        urls[name] = path.as_uri()
    return urls


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()


def _new_page(browser: Any, url: str) -> Any:
    page = browser.new_page(
        viewport={"width": 1280, "height": 800}, device_scale_factor=1
    )
    page.goto(url)
    return page


def _center(page: Any, selector: str) -> tuple[int, int]:
    box = page.locator(selector).bounding_box()
    assert box is not None
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _record_and_compile(browser: Any, url: str, out: Path) -> Workflow:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = _new_page(browser, url)
    try:
        recorder = Recorder(PlaywrightBackend(page), out / "recording", app_url=url)
        for selector, value, param in (
            ("#ins", "610420233", "insurance_no"),
            ("#cno", "VD263957", "claim_no"),
            ("#expl", "Red skin", "explanation"),
        ):
            recorder.click(*_center(page, selector))
            recorder.type_text(value, param=param)
        recorder.click(*_center(page, ".savefab"))
        recorder.finish()
        assert page.evaluate("window.actions") == ["save"]
    finally:
        page.close()
    return compile_recording(out / "recording", out / "bundle", name="claims")


def _replay(browser: Any, url: str, bundle: Path, run_dir: Path) -> tuple[Any, list]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = _new_page(browser, url)
    try:
        report = Replayer(PlaywrightBackend(page)).run(
            Workflow.load(bundle),
            params={
                "insurance_no": "610420118",
                "claim_no": "VD264752",
                "explanation": "Fever",
            },
            bundle_dir=bundle,
            run_dir=run_dir,
        )
        actions = page.evaluate("window.actions")
    finally:
        page.close()
    return report, actions


@pytest.fixture(scope="module")
def claims_bundle(
    browser: Any, pages: dict[str, str], tmp_path_factory: pytest.TempPathFactory
) -> tuple[Workflow, Path]:
    out = tmp_path_factory.mktemp("claims-demo")
    workflow = _record_and_compile(browser, pages["claims"], out)
    return workflow, out / "bundle"


def test_compiled_save_carries_its_form_binding(
    claims_bundle: tuple[Workflow, Path],
) -> None:
    workflow, bundle = claims_bundle
    save = workflow.steps[-1]

    assert save.action is ActionKind.CLICK
    assert save.risk == "irreversible"
    assert save.anchor is not None and save.anchor.structural is None
    binding = save.submit_binding
    assert binding is not None, save.submit_binding_unbound_reason
    assert binding.form_owner == "fields_only"
    assert binding.submit_role == "detached"
    assert binding.filled_fields == 3
    assert (binding.order, binding.vertical, binding.horizontal) == (
        "after",
        "below",
        "right",
    )
    typing = [step.id for step in workflow.steps if step.action is ActionKind.TYPE]
    assert list(binding.field_step_ids) == typing
    assert "submit binding:" in (bundle / "workflow.py").read_text()
    findings = {
        (finding.code, finding.step_id) for finding in lint_workflow(workflow).findings
    }
    assert ("submit_binding", save.id) in findings


def test_replay_verifies_the_binding_and_saves_once(
    browser: Any,
    pages: dict[str, str],
    claims_bundle: tuple[Workflow, Path],
    tmp_path: Path,
) -> None:
    _, bundle = claims_bundle

    report, actions = _replay(browser, pages["claims"], bundle, tmp_path / "run")
    save = report.results[-1]

    assert report.success is True, save.error
    assert save.submit_binding_verified is True
    assert save.identity is not None and save.identity.status == "verified"
    assert actions == ["save"]


def test_replay_refuses_when_a_second_unlabeled_icon_appears(
    browser: Any,
    pages: dict[str, str],
    claims_bundle: tuple[Workflow, Path],
    tmp_path: Path,
) -> None:
    _, bundle = claims_bundle

    report, actions = _replay(browser, pages["two-icons"], bundle, tmp_path / "run")
    save = report.results[-1]

    assert report.success is False
    assert save.safety_halt is True
    assert "2 unlabeled controls" in (save.error or "")
    assert actions == []


def test_a_demonstration_with_two_unlabeled_icons_compiles_unbound_and_refuses(
    browser: Any,
    pages: dict[str, str],
    tmp_path: Path,
) -> None:
    workflow = _record_and_compile(browser, pages["two-icons"], tmp_path / "demo")
    save = workflow.steps[-1]

    assert save.submit_binding is None
    assert "2 unlabeled controls" in (save.submit_binding_unbound_reason or "")
    codes = {finding.code for finding in lint_workflow(workflow).findings}
    assert "submit_binding_unbound" in codes

    report, actions = _replay(
        browser, pages["two-icons"], tmp_path / "demo" / "bundle", tmp_path / "run"
    )
    assert report.success is False
    assert report.results[-1].safety_halt is True
    assert actions == []
