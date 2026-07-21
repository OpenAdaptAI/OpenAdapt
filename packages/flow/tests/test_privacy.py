"""Tests for the PHI/PII scrubbing integration (openadapt_flow.privacy).

Covered:

* the ``privacy`` extra is OPTIONAL — the module imports and the runtime runs
  with openadapt-privacy absent (default ``auto`` posture => plaintext, no crash);
* ``OPENADAPT_FLOW_SCRUB=on`` fails CLOSED when the capability is missing;
* ``OPENADAPT_FLOW_SCRUB=off`` never scrubs;
* text written to REPORT.md is scrubbed when a scrubber is present (injected fake);
* the drift-oracle console log is scrubbed;
* opt-in image redaction (``OPENADAPT_FLOW_SCRUB_IMAGES``) gates the persisted PNGs.

The fake scrubber avoids pulling in Presidio/spaCy so the suite stays fast and
offline (no model download, no Anthropic calls).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from openadapt_flow import privacy
from openadapt_flow.ir import RunReport, StepResult, UnarmedStep
from openadapt_flow.report import render_run_report


class _FakeTextScrubber:
    """Redacts a fixed set of PHI tokens; enough to prove wiring, not Presidio."""

    _TOKENS = {
        "John Smith": "<PERSON>",
        "Jane Doe": "<PERSON>",
        "01/15/1985": "<DATE_TIME>",
        "MRN-99321": "<ID>",
        "john@example.com": "<EMAIL_ADDRESS>",
    }

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        for needle, repl in self._TOKENS.items():
            text = text.replace(needle, repl)
        return text


class _FakeImageScrubber:
    """Returns a solid-black image (stand-in for Presidio box redaction)."""

    def scrub_image(self, image, fill_color=None):
        return Image.new("RGB", image.size, (0, 0, 0))


@pytest.fixture(autouse=True)
def _clean_privacy_env(monkeypatch):
    """Each test starts from a clean scrub posture and no cached scrubber."""
    monkeypatch.delenv("OPENADAPT_FLOW_SCRUB", raising=False)
    monkeypatch.delenv("OPENADAPT_FLOW_SCRUB_IMAGES", raising=False)
    privacy.reset_scrubbers()
    yield
    privacy.reset_scrubbers()


# -- posture / optionality ---------------------------------------------------


def test_scrub_text_is_noop_without_scrubber(monkeypatch):
    """auto posture + no capability => plaintext, no crash (extra is optional)."""
    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    assert privacy.scrub_mode() == "auto"
    assert privacy.text_scrubbing_enabled() is False
    assert privacy.scrub_text("Patient John Smith") == "Patient John Smith"


def test_scrub_on_fails_closed_when_capability_missing(monkeypatch):
    """OPENADAPT_FLOW_SCRUB=on must raise when openadapt-privacy is absent."""
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    with pytest.raises(privacy.PrivacyNotAvailable):
        privacy.get_text_scrubber()


def test_scrub_off_never_scrubs(monkeypatch):
    """OPENADAPT_FLOW_SCRUB=off is an explicit no-op even with a scrubber present."""
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")
    privacy.set_text_scrubber(_FakeTextScrubber())
    assert privacy.scrub_mode() == "off"
    assert privacy.text_scrubbing_enabled() is False
    assert privacy.scrub_text("John Smith") == "John Smith"


def test_scrub_text_and_params_with_injected_scrubber():
    privacy.set_text_scrubber(_FakeTextScrubber())
    assert privacy.scrub_text("Patient John Smith DOB 01/15/1985") == (
        "Patient <PERSON> DOB <DATE_TIME>"
    )
    scrubbed = privacy.scrub_params({"patient": "Jane Doe", "note": "ok"})
    assert scrubbed == {"patient": "<PERSON>", "note": "ok"}


# -- REPORT.md text scrubbing ------------------------------------------------


def _phi_report() -> RunReport:
    return RunReport(
        workflow_name="Chart for John Smith",
        started_at="2026-07-12T00:00:00+00:00",
        params={"patient": "John Smith", "dob": "01/15/1985", "mrn": "MRN-99321"},
        results=[
            StepResult(
                step_id="step_open",
                intent="click 'John Smith' row",
                ok=False,
                error="postcondition text_present 'John Smith' timed out",
                elapsed_ms=100.0,
            )
        ],
        success=False,
        identity_applicable_steps=1,
        identity_armed_steps=0,
        identity_unarmed=[
            UnarmedStep(
                step_id="step_open",
                intent="click 'John Smith' row",
                reason="band text 'John Smith 01/15/1985' too generic",
            )
        ],
    )


def test_report_md_scrubbed_with_scrubber(tmp_path: Path):
    privacy.set_text_scrubber(_FakeTextScrubber())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    md = render_run_report(run_dir).read_text()
    assert "John Smith" not in md
    assert "01/15/1985" not in md
    assert "MRN-99321" not in md
    assert "<PERSON>" in md  # redaction placeholder rendered
    # Non-PHI structure is preserved.
    assert "`patient`" in md
    assert "Totals" in md


def test_report_md_plaintext_when_scrubbing_off(tmp_path: Path):
    privacy.set_text_scrubber(_FakeTextScrubber())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    import os

    os.environ["OPENADAPT_FLOW_SCRUB"] = "off"
    try:
        md = render_run_report(run_dir).read_text()
    finally:
        del os.environ["OPENADAPT_FLOW_SCRUB"]
    # off posture: the report still renders, unscrubbed (operator's choice).
    assert "John Smith" in md


# -- opt-in image redaction --------------------------------------------------


def _png_bytes(color=(200, 40, 40)) -> bytes:
    import io

    buf = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buf, format="PNG")
    return buf.getvalue()


def test_image_redaction_off_by_default():
    privacy.set_image_scrubber(_FakeImageScrubber())
    original = _png_bytes()
    # No OPENADAPT_FLOW_SCRUB_IMAGES => passthrough.
    assert privacy.image_redaction_enabled() is False
    assert privacy.scrub_image_bytes(original) == original


def test_image_redaction_opt_in(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB_IMAGES", "1")
    privacy.set_image_scrubber(_FakeImageScrubber())
    assert privacy.image_redaction_enabled() is True
    out = privacy.scrub_image_bytes(_png_bytes((200, 40, 40)))
    # The fake redactor blacks out the frame.
    import io

    assert Image.open(io.BytesIO(out)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)


def test_save_step_png_redacts_when_opt_in(monkeypatch, tmp_path: Path):
    """Replayer._save_step_png routes through opt-in image redaction."""
    from openadapt_flow.runtime.replayer import Replayer

    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB_IMAGES", "1")
    privacy.set_image_scrubber(_FakeImageScrubber())
    run_dir = tmp_path / "run"
    (run_dir / "steps").mkdir(parents=True)
    rel = Replayer._save_step_png(
        run_dir, "step_x", "before", _png_bytes((200, 40, 40))
    )
    import io

    saved = (run_dir / rel).read_bytes()
    assert Image.open(io.BytesIO(saved)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)


# -- SCRUB=on implies image redaction (no silent frame leak) ------------------


def test_scrub_on_implies_image_redaction_without_flag(monkeypatch):
    """SCRUB=on redacts persisted frames even with OPENADAPT_FLOW_SCRUB_IMAGES unset.

    Closes the false-sense gap: a compliance-pinned run must not leave full-frame
    PHI screenshots unredacted in the shareable REPORT.md while text is scrubbed.
    """
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    # Note: OPENADAPT_FLOW_SCRUB_IMAGES is NOT set (default 0).
    privacy.set_image_scrubber(_FakeImageScrubber())
    assert privacy.image_redaction_enabled() is True
    out = privacy.scrub_image_bytes(_png_bytes((200, 40, 40)))
    import io

    assert Image.open(io.BytesIO(out)).convert("RGB").getpixel((0, 0)) == (0, 0, 0)


def test_scrub_on_image_redaction_fails_closed_when_capability_missing(monkeypatch):
    """SCRUB=on + no image scrubber => image_redaction_enabled raises (fail closed)."""
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    with pytest.raises(privacy.PrivacyNotAvailable):
        privacy.image_redaction_enabled()


def test_auto_still_requires_flag_for_image_redaction(monkeypatch):
    """Under auto (default), frame redaction stays opt-in via the flag."""
    privacy.set_image_scrubber(_FakeImageScrubber())
    assert privacy.scrub_mode() == "auto"
    assert privacy.image_redaction_enabled() is False  # no flag => off


# -- plaintext-PHI warning when REPORT.md is written unscrubbed ---------------


def test_report_md_warns_when_plaintext_phi(monkeypatch, tmp_path: Path):
    """auto + no scrubber + identity-like text => one-time PlaintextPHIWarning."""
    from openadapt_flow.report import PlaintextPHIWarning

    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    with pytest.warns(PlaintextPHIWarning):
        md = render_run_report(run_dir).read_text()
    # Behavior unchanged: the report is still written (in plaintext).
    assert "John Smith" in md


def test_report_md_no_warning_when_scrubbing_active(tmp_path: Path):
    """A live scrubber => no plaintext warning."""
    from openadapt_flow.report import PlaintextPHIWarning

    privacy.set_text_scrubber(_FakeTextScrubber())
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    import warnings as _w

    with _w.catch_warnings():
        _w.simplefilter("error", PlaintextPHIWarning)
        render_run_report(run_dir)  # must not raise


def test_report_md_no_warning_when_scrub_off(monkeypatch, tmp_path: Path):
    """SCRUB=off is a deliberate opt-out => stays silent."""
    from openadapt_flow.report import PlaintextPHIWarning

    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")
    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    _phi_report().save(run_dir)

    import warnings as _w

    with _w.catch_warnings():
        _w.simplefilter("error", PlaintextPHIWarning)
        render_run_report(run_dir)  # must not raise


def test_default_mockmed_replay_warns_for_arbitrary_phi_bundle(
    monkeypatch, tmp_path: Path
):
    """Default MockMed routing cannot declare an arbitrary bundle synthetic.

    ``replay`` accepts any bundle while defaulting the target URL to MockMed.
    The bundle's provenance is therefore unknown, even with no parameter or
    worklist overrides, and its identity-like report text must still warn.
    """
    from contextlib import contextmanager
    from types import SimpleNamespace

    import openadapt_flow.__main__ as cli
    from openadapt_flow.report import PlaintextPHIWarning

    monkeypatch.setattr(privacy, "_build_provider", lambda: None)
    privacy.reset_scrubbers()
    bundle = tmp_path / "arbitrary-phi-bundle"
    bundle.mkdir()
    run_dir = tmp_path / "run"
    observed: dict[str, object] = {"served": False, "stopped": False}

    def fake_load(path):
        observed["bundle"] = Path(path)
        return object()

    def fake_serve(*, port):
        assert port == 0
        observed["served"] = True

        def stop() -> None:
            observed["stopped"] = True

        return "http://127.0.0.1:45678/", stop

    def fake_run(_backend, **kwargs):
        report = _phi_report()
        report.save(kwargs["run_dir"])
        return report

    page = SimpleNamespace(
        url="http://127.0.0.1:45678/", video=None, goto=lambda _url: None
    )
    browser = SimpleNamespace(new_page=lambda **_kw: page, close=lambda: None)
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **_kw: browser))

    @contextmanager
    def fake_sync_playwright():
        yield playwright

    monkeypatch.setattr("openadapt_flow.ir.Workflow.load", fake_load)
    monkeypatch.setattr("openadapt_flow.mockmed.server.serve", fake_serve)
    monkeypatch.setattr(
        "openadapt_flow._browser_setup.ensure_chromium_installed", lambda: None
    )
    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync_playwright)
    monkeypatch.setattr(
        "openadapt_flow.backends.factory.build_backend", lambda *_a, **_k: object()
    )
    monkeypatch.setattr(cli, "_build_and_run_replayer", fake_run)

    args = cli.build_parser().parse_args(
        ["replay", str(bundle), "--run-dir", str(run_dir)]
    )

    with pytest.warns(PlaintextPHIWarning):
        assert cli._cmd_replay(args) == 1
    assert observed == {"served": True, "stopped": True, "bundle": bundle}
    assert "John Smith" in (run_dir / "REPORT.md").read_text(encoding="utf-8")
