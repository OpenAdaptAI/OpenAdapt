"""PHI governance (audit REM-1 + GAP-3 scrub-on-compile).

Covers:

* the OPTIONAL Presidio scrub on the compile path drops an identifier-bearing
  TEXT_PRESENT postcondition (injected fake scrubber — no Presidio needed);
* the compiled manifest classifies the bundle (contains_phi / phi_scrubbed /
  encrypted);
* the pre-commit / CI guard (scripts/check_bundle_phi.py) blocks a bundle with a
  plaintext identity band and passes a PHI-free one.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from openadapt_flow import privacy
from openadapt_flow.compiler.compile import _new_text_postcondition, _text_carries_phi
from openadapt_flow.ir import Workflow
from openadapt_flow.vision.ocr import OcrLine

_GUARD = Path(__file__).resolve().parent.parent / "scripts" / "check_bundle_phi.py"


def _load_guard():
    spec = importlib.util.spec_from_file_location("check_bundle_phi", _GUARD)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class _FakeScrubber:
    """Redacts one fixed identifier; enough to prove wiring without Presidio."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace("Belford, Phil", "<PERSON>")


@pytest.fixture(autouse=True)
def _reset_privacy(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    privacy.reset_scrubbers()
    yield
    privacy.reset_scrubbers()


def _line(text: str, x: int = 10, y: int = 300) -> OcrLine:
    return OcrLine(text=text, region=(x, y, 200, 20), confidence=0.99)


def test_scrub_drops_identifier_postcondition_when_active(monkeypatch):
    privacy.set_text_scrubber(_FakeScrubber())
    assert privacy.text_scrubbing_enabled() is True
    assert _text_carries_phi("Patient Messages for Belford, Phil") is True
    assert _text_carries_phi("Save complete") is False

    before = [_line("Search")]
    # Two new candidates after the action: one carries the identifier, one is
    # a clean UI banner. The PHI candidate must be dropped.
    after = [
        _line("Patient Messages for Belford, Phil", y=300),
        _line("Chart synchronization complete", y=340),
    ]
    pc = _new_text_postcondition(before, after)
    assert pc is not None
    assert "Belford" not in (pc.text or "")
    assert pc.text == "Chart synchronization complete"


def test_scrub_drops_identifier_landmark_when_active(tmp_path):
    # A patient-name row next to the click target would otherwise be mined as a
    # geometry LANDMARK (plaintext). With the scrub active it must be dropped.
    from tests.test_compiler import blank, draw_button, draw_text, write_frame

    privacy.set_text_scrubber(_FakeScrubber())
    rec = tmp_path / "rec"
    (rec / "frames").mkdir(parents=True)
    before = blank()
    draw_text(before, 40, 430, "Belford, Phil")  # the identifier, nearest text
    draw_button(before, 560, 400, 160, 48, "Open")
    after = before.copy()
    draw_text(after, 420, 244, "Patient Chart Overview")
    write_frame(rec, 0, "before", before)
    write_frame(rec, 0, "after", after)
    (rec / "events.jsonl").write_text(
        json.dumps({"i": 0, "kind": "click", "x": 640, "y": 424, "t": 1.0}) + "\n"
    )
    (rec / "meta.json").write_text(
        json.dumps(
            {
                "id": "lm",
                "created_at": "2026-07-06T00:00:00+00:00",
                "viewport": [1280, 800],
                "app_url": "http://x/",
                "params": {},
            }
        )
    )
    from openadapt_flow.compiler import compile_recording

    wf = compile_recording(rec, tmp_path / "bundle", name="lm")
    blob = (tmp_path / "bundle" / "workflow.json").read_text()
    assert "Belford" not in blob
    for step in wf.steps:
        if step.anchor:
            for lm in step.anchor.landmarks:
                assert "Belford" not in lm.ocr_text


def test_scrub_inactive_is_a_noop(monkeypatch):
    # Default auto + no scrubber installed => nothing is treated as PHI.
    privacy.set_text_scrubber(None)
    assert _text_carries_phi("Patient Messages for Belford, Phil") is False


def test_guard_blocks_plaintext_identity_band(tmp_path):
    guard = _load_guard()
    bundle = tmp_path / "workflow.json"
    bundle.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "bad",
                "steps": [
                    {
                        "id": "s1",
                        "intent": "click",
                        "action": "click",
                        "anchor": {
                            "template": "t.png",
                            "region": [0, 0, 1, 1],
                            "click_point": [0, 0],
                            "context_text": "Belford, Phil 1948-01-01 MRN99321",
                        },
                    }
                ],
            }
        )
    )
    assert guard.main([str(bundle)]) == 1


def test_guard_passes_phi_free_bundle(tmp_path):
    guard = _load_guard()
    bundle = tmp_path / "workflow.json"
    bundle.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "name": "ok",
                "contains_phi": False,
                "steps": [
                    {
                        "id": "s1",
                        "intent": "click",
                        "action": "click",
                        "anchor": {
                            "template": "t.png",
                            "region": [0, 0, 1, 1],
                            "click_point": [0, 0],
                            "context_text": None,
                            "structured_identity": None,
                            "identity_template": {"salt": "ab", "tokens": []},
                        },
                    }
                ],
            }
        )
    )
    assert guard.main([str(bundle)]) == 0


def test_committed_showcase_bundle_is_phi_free():
    repo = Path(__file__).resolve().parent.parent
    wf = Workflow.load(repo / "docs" / "showcase-openemr" / "bundle")
    assert wf.contains_phi is False
    assert wf.encrypted is False
    for step in wf.steps:
        a = step.anchor
        if a is not None:
            assert a.context_text is None
            assert a.structured_identity is None
    guard = _load_guard()
    assert guard.main([str(repo / "docs/showcase-openemr/bundle/workflow.json")]) == 0
