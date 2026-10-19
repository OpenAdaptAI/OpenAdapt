"""Record at 100%, replay at another display scale, on the pixel path.

Uses the in-process kiosk renderings from ``benchmark/pixel_scale`` (no DOM,
no accessibility tree) and the production Recorder, compiler and Replayer.
A correct run books the active record; a run that cannot confirm identity, or
that meets a font change rather than a display-scale change, halts with no
write.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from openadapt_flow.ir import Workflow
from openadapt_flow.runtime.replayer import Replayer

pytestmark = pytest.mark.timeout(900)

# Loaded by path, as the sweep script loads it: an installed package named
# ``benchmark`` can shadow this repository's benchmark namespace.
sys.path.insert(
    0, str(Path(__file__).resolve().parents[1] / "benchmark" / "pixel_scale")
)
import run_pixel_scale_sweep as sweep  # noqa: E402
from kiosk_render import (  # noqa: E402
    SPECS,
    KioskBackend,
    RenderProfile,
    ScaledKiosk,
)


@pytest.fixture(scope="module")
def bundles(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    work = tmp_path_factory.mktemp("pixel-scale")
    return {
        name: sweep._record(name, work)[1]
        for name in ("canvas_notes", "rdp_appointments")
    }


def _replay(
    tmp_path: Path,
    bundle_dir: Path,
    kiosk: ScaledKiosk,
    *,
    after_type=None,
    render_scale=None,
):
    workflow = Workflow.load(bundle_dir)
    backend = KioskBackend(kiosk, after_type=after_type)
    report = Replayer(
        backend,
        poll_interval_s=0.0,
        pixel_verify_enabled=True,
        layout_drift=True,
        render_scale=render_scale,
    ).run(
        workflow,
        params=sweep.REPLAY[kiosk.spec.name],
        bundle_dir=bundle_dir,
        run_dir=tmp_path / "run",
    )
    return report


def _first_failure(report):
    return next(result for result in report.results if not result.ok)


def test_canvas_kiosk_at_150_percent_cleartype_off_books_the_right_record(
    tmp_path: Path, bundles: dict[str, Path]
) -> None:
    kiosk = ScaledKiosk(SPECS["canvas_notes"](), RenderProfile(1.5, cleartype=False))
    report = _replay(tmp_path, bundles["canvas_notes"], kiosk)
    assert report.success, [r.error for r in report.results if r.error]
    assert report.model_calls == 0
    assert report.render_scale["status"] == "normalized"
    assert report.render_scale["scale"] == pytest.approx(1.5, abs=0.005)
    assert kiosk.state.saved == [
        {
            "patient_name": "Ada Lovelace",
            "patient_mrn": "MRN 24682",
            "note": sweep.REPLAY["canvas_notes"]["note"],
        }
    ]


def test_wrong_record_at_a_new_scale_still_halts_before_the_write(
    tmp_path: Path, bundles: dict[str, Path]
) -> None:
    kiosk = ScaledKiosk(SPECS["canvas_notes"](), RenderProfile(1.25, cleartype=True))

    def switch_record(k: ScaledKiosk, text: str) -> None:
        k.select_patient(1)

    report = _replay(tmp_path, bundles["canvas_notes"], kiosk, after_type=switch_record)
    assert not report.success
    assert kiosk.state.saved == []
    failure = _first_failure(report)
    assert failure.identity is not None and failure.identity.status == "mismatch"


def test_unconfirmable_identity_at_a_new_scale_halts(
    tmp_path: Path, bundles: dict[str, Path]
) -> None:
    """The RDP fixture's IDs (A1001) rest on 1/l and 0/O. Only an exact pixel
    match at the recorded render confirms them, so at 125% the run halts at
    the identity check even though every target resolves."""
    kiosk = ScaledKiosk(
        SPECS["rdp_appointments"](), RenderProfile(1.25, cleartype=True)
    )
    report = _replay(tmp_path, bundles["rdp_appointments"], kiosk)
    assert not report.success
    assert kiosk.state.saved == []
    assert report.render_scale["status"] == "normalized"
    failure = _first_failure(report)
    assert failure.resolution is not None
    assert failure.identity is not None and failure.identity.status != "verified"


def test_font_change_with_the_layout_unchanged_still_halts(
    tmp_path: Path, bundles: dict[str, Path]
) -> None:
    kiosk = ScaledKiosk(
        SPECS["canvas_notes"](), RenderProfile(1.0, cleartype=True, font_scale=1.25)
    )
    report = _replay(tmp_path, bundles["canvas_notes"], kiosk)
    assert not report.success
    assert kiosk.state.saved == []
    assert report.render_scale["status"] == "no_estimate"
    assert "Layout drift" in (_first_failure(report).error or "")


def test_scale_change_during_the_run_halts(
    tmp_path: Path, bundles: dict[str, Path]
) -> None:
    kiosk = ScaledKiosk(SPECS["canvas_notes"](), RenderProfile(1.25, cleartype=True))

    def rescale(k: ScaledKiosk, text: str) -> None:
        k.profile = RenderProfile(1.5, cleartype=True)

    report = _replay(tmp_path, bundles["canvas_notes"], kiosk, after_type=rescale)
    assert not report.success
    assert kiosk.state.saved == []


def test_opting_out_keeps_the_first_step_halt(
    tmp_path: Path, bundles: dict[str, Path]
) -> None:
    kiosk = ScaledKiosk(SPECS["canvas_notes"](), RenderProfile(1.25, cleartype=True))
    report = _replay(tmp_path, bundles["canvas_notes"], kiosk, render_scale=False)
    assert not report.success
    assert report.render_scale is None
    failure = _first_failure(report)
    assert failure.step_id == "step_000"
    assert "Layout drift" in (failure.error or "")
