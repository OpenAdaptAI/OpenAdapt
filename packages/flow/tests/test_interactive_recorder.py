"""End-to-end: `record --url` interactively records the user's OWN app.

A scripted, headless driver plays the canonical MockMed triage flow as if a
user were clicking and typing in the headed browser; the InteractiveRecorder
watches real DOM events and writes the exact recording format the compiler
consumes. We then compile it and replay it — closing the self-serve loop
(record -> compile -> replay) for an arbitrary app, not just the bundled demo.

The password field (``input[type=password]``) is auto-detected as a secret:
its value is never persisted, its region is redacted from the frames, and at
replay it is injected from the environment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import numpy as np
import pytest
from PIL import Image

from openadapt_flow.compiler import compile_recording
from openadapt_flow.interactive_recorder import record_interactive
from openadapt_flow.ir import Workflow
from openadapt_flow.mockmed.server import serve

pytestmark = pytest.mark.timeout(600)

NOTE = "Follow-up in two weeks; recheck blood pressure at that visit."
SECRET = "s3cr3t-PASSWORD-never-persist"
REPLAY_NOTE = "A DIFFERENT note value supplied only at replay time."


def _drive_triage(page, pump) -> None:
    """Play the MockMed triage flow, pumping the recorder between actions."""

    def click(selector: str) -> None:
        page.wait_for_selector(selector, state="visible", timeout=20000)
        page.click(selector)
        pump()
        pump()

    click("#username")
    page.keyboard.type("nurse.demo")
    pump()
    click("#password")
    page.keyboard.type(SECRET)  # input[type=password] -> auto secret
    pump()
    click("#signin")
    click(".open-btn")
    click("#new-encounter")
    click("#type-triage")
    click("#note")
    page.keyboard.type(NOTE)
    pump()
    pump()
    click("#save-encounter")
    for _ in range(4):
        pump()


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    url, stop = serve(port=0)
    yield url
    stop()


@pytest.fixture(scope="module")
def recording(server_url: str, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("interactive_rec") / "rec"
    return record_interactive(
        server_url,
        out,
        param_fields=("note",),
        headless=True,
        script=_drive_triage,
    )


@pytest.fixture(scope="module")
def bundle(recording: Path, tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("interactive_bundle") / "bundle"
    compile_recording(recording, out, name="my-app-triage")
    return out


def _events(rec_dir: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (rec_dir / "events.jsonl").read_text().splitlines()
        if line.strip()
    ]


def test_recording_shape_matches_the_demonstration(recording: Path) -> None:
    meta = json.loads((recording / "meta.json").read_text())
    assert meta["viewport"] == [1280, 800]
    assert meta["app_url"]

    events = _events(recording)
    # The same 11-action triage flow the canonical demo produces.
    assert [e["kind"] for e in events] == [
        "click",  # username field
        "type",  # username (literal)
        "click",  # password field
        "type",  # password (SECRET)
        "click",  # Sign In
        "click",  # Open referral
        "click",  # New Encounter
        "click",  # Triage
        "click",  # Note field
        "type",  # note (parameter)
        "click",  # Save Encounter
    ]
    # every event has before/after frames on disk
    for i in range(len(events)):
        for suffix in ("before", "after"):
            assert (recording / "frames" / f"{i:04d}_{suffix}.png").is_file()

    # structured identity was captured in-page for at least one row click
    assert any(e.get("structured_identity") for e in events if e["kind"] == "click")


def test_secret_auto_detected_and_never_persisted(recording: Path) -> None:
    meta = json.loads((recording / "meta.json").read_text())
    assert meta["secret_params"] == ["password"]

    type_events = [e for e in _events(recording) if e["kind"] == "type"]
    secret_events = [e for e in type_events if e.get("secret")]
    assert len(secret_events) == 1
    assert secret_events[0]["param"] == "password"
    assert "text" not in secret_events[0]

    # The literal secret appears in NO persisted text artifact.
    blob = (recording / "meta.json").read_text() + (
        recording / "events.jsonl"
    ).read_text()
    assert SECRET not in blob


def test_secret_field_region_redacted_in_frames(recording: Path) -> None:
    events = _events(recording)
    secret_i = next(e["i"] for e in events if e.get("secret"))
    for suffix in ("before", "after"):
        arr = np.asarray(
            Image.open(recording / "frames" / f"{secret_i:04d}_{suffix}.png").convert(
                "RGB"
            )
        )
        # Redaction fills the field rect solid black — a sizeable pure-black
        # block that an unredacted login screen would never contain.
        assert int((arr.sum(axis=2) == 0).sum()) > 500


def test_secret_absent_from_compiled_bundle(bundle: Path) -> None:
    workflow = Workflow.load(bundle)
    assert workflow.secret_params == ["password"]
    secret_steps = [s for s in workflow.steps if s.secret]
    assert len(secret_steps) == 1 and secret_steps[0].text is None
    # No persisted text file in the bundle carries the literal.
    for path in bundle.rglob("*"):
        if path.suffix in (".json", ".py", ".txt", ".md"):
            assert SECRET not in path.read_text()


def _replay(bundle: Path, url: str, run_dir: Path):
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.runtime import Replayer

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            viewport={"width": 1280, "height": 800}, device_scale_factor=1
        )
        try:
            page.goto(url)
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle),
                params={"note": REPLAY_NOTE},
                bundle_dir=bundle,
                run_dir=run_dir,
            )
        finally:
            browser.close()
    return report


def test_replay_requires_secret_from_env(
    bundle: Path, server_url: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("OPENADAPT_FLOW_SECRET_PASSWORD", raising=False)
    report = _replay(bundle, server_url, tmp_path / "run_missing")
    assert not report.success
    failed = [r for r in report.results if not r.ok]
    assert failed and "OPENADAPT_FLOW_SECRET_PASSWORD" in (failed[0].error or "")


def test_replay_succeeds_with_secret_and_new_param(
    bundle: Path, server_url: str, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENADAPT_FLOW_SECRET_PASSWORD", SECRET)
    report = _replay(bundle, server_url, tmp_path / "run_ok")
    assert report.success, [r.error for r in report.results if not r.ok]
    assert len(report.results) == 11


def test_identifier_field_marking_records_region_and_compiles_crop(
    server_url: str, tmp_path: Path
) -> None:
    """``record --identifier FIELD``: the marked field's rect is captured
    in-page on every click (``identifier_region`` on the event), and the
    compiler crops those pixels (``anchor.identifier_crop``) — forcing the
    pixel identity tier armed even though this is a structured (DOM)
    recording, so the bundle stays identity-armed on a later remote-display
    replay."""

    def drive(page, pump) -> None:
        page.wait_for_selector("#username", state="visible", timeout=20000)
        page.click("#username")
        pump()
        pump()

    rec = record_interactive(
        server_url,
        tmp_path / "rec",
        identifier_fields=("brand",),  # the always-present MockMed topbar
        headless=True,
        script=drive,
    )
    clicks = [e for e in _events(rec) if e["kind"] == "click"]
    assert clicks
    region = clicks[0].get("identifier_region")
    assert region and len(region) == 4
    x, y, w, h = region
    assert w > 0 and h > 0

    bundle = tmp_path / "bundle"
    workflow = compile_recording(rec, bundle, name="marked-identifier")
    click_steps = [s for s in workflow.steps if s.anchor is not None]
    assert click_steps
    anchor = click_steps[0].anchor
    assert anchor.identifier_crop is not None
    assert anchor.identifier_region == tuple(region)
    assert (bundle / anchor.identifier_crop).is_file()
    assert click_steps[0].identifier_crop_missing_reason is None
