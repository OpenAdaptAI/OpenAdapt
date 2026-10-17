"""Demo driver: records the canonical MockMed and MockLoan demonstrations.

Record time is allowed to cheat with Playwright locators (to find pixel
coordinates via ``bounding_box()``), but every action is performed through
`Recorder` so frames and events are captured exactly as a human demo would
be. Replay never uses selectors.

Two self-contained demonstration targets are supported: the healthcare
MockMed triage-save (``record_triage_demo``) and the non-healthcare MockLoan
loan-disbursement (``record_disbursement_demo``). Both drive the identical
governed record -> compile -> replay path.
"""

from __future__ import annotations

from pathlib import Path

from openadapt_flow.backends.playwright_backend import PlaywrightBackend
from openadapt_flow.recorder import Recorder


def record_triage_demo(
    url: str,
    out_dir: Path | str,
    *,
    note_text: str,
    param_name: str = "note",
    headed: bool = False,
    record_video_dir: Path | str | None = None,
) -> Path:
    """Record the canonical triage demo against a running MockMed app.

    Flow: login -> tasks -> Open first referral -> New Encounter -> Triage
    -> click Note field -> type note (as a parameter) -> Save Encounter.

    Args:
        url: MockMed base URL (from `openadapt_flow.mockmed.server.serve`).
        out_dir: Directory to write the recording into.
        note_text: The note typed during the demo (recorded as a parameter).
        param_name: Parameter name for the typed note (default ``"note"``).
        headed: Run the browser headed (visible) instead of headless.
        record_video_dir: OPT-IN (default ``None`` = off, no effect). When set,
            a WebM video of the recording session is captured into this
            directory (used to film the canonical demo for the website); it
            does not change what is recorded to ``out_dir``.

    Returns:
        The recording directory (contains meta.json, events.jsonl, frames/).
    """
    backend, close = PlaywrightBackend.launch(
        url,
        headless=not headed,
        record_video_dir=(
            str(record_video_dir) if record_video_dir is not None else None
        ),
    )
    try:
        page = backend.page
        recorder = Recorder(backend, out_dir, app_url=url)

        def center(selector: str) -> tuple[int, int]:
            """Center pixel coordinates of the first element matching CSS."""
            locator = page.locator(selector).first
            locator.wait_for(state="visible")
            box = locator.bounding_box()
            if box is None:  # pragma: no cover - visible => box exists
                raise RuntimeError(f"no bounding box for {selector!r}")
            return (
                int(box["x"] + box["width"] / 2),
                int(box["y"] + box["height"] / 2),
            )

        # Login
        recorder.click(*center("#username"))
        recorder.type_text("nurse.demo")
        recorder.click(*center("#password"))
        recorder.type_text("mockmed-demo-pass")
        recorder.click(*center("#signin"))

        # Tasks -> open first referral
        recorder.click(*center(".open-btn"))

        # Patient -> new encounter
        recorder.click(*center("#new-encounter"))

        # Encounter: type chooser, note (parameterized), save
        recorder.click(*center("#type-triage"))
        recorder.click(*center("#note"))
        recorder.type_text(note_text, param=param_name)
        recorder.click(*center("#save-encounter"))

        return recorder.finish()
    finally:
        close()


def record_disbursement_demo(
    url: str,
    out_dir: Path | str,
    *,
    memo_text: str,
    param_name: str = "memo",
    headed: bool = False,
    record_video_dir: Path | str | None = None,
) -> Path:
    """Record the canonical MockLoan disbursement demo against a running app.

    The non-healthcare companion to :func:`record_triage_demo`. Flow:
    login -> pipeline -> Open first loan -> New Disbursement -> Personal product
    -> click Funding memo field -> type memo (as a parameter) -> Authorize
    Disbursement. The consequential final step authorizes moving money to a
    borrower, the lending analog of clinically saving an encounter.

    Args:
        url: MockLoan base URL (from `openadapt_flow.mockloan.server.serve`).
        out_dir: Directory to write the recording into.
        memo_text: The funding memo typed during the demo (recorded as a param).
        param_name: Parameter name for the typed memo (default ``"memo"``).
        headed: Run the browser headed (visible) instead of headless.
        record_video_dir: OPT-IN (default ``None`` = off). When set, a WebM
            video of the recording session is captured into this directory; it
            does not change what is recorded to ``out_dir``.

    Returns:
        The recording directory (contains meta.json, events.jsonl, frames/).
    """
    backend, close = PlaywrightBackend.launch(
        url,
        headless=not headed,
        record_video_dir=(
            str(record_video_dir) if record_video_dir is not None else None
        ),
    )
    try:
        page = backend.page
        recorder = Recorder(backend, out_dir, app_url=url)

        def center(selector: str) -> tuple[int, int]:
            """Center pixel coordinates of the first element matching CSS."""
            locator = page.locator(selector).first
            locator.wait_for(state="visible")
            box = locator.bounding_box()
            if box is None:  # pragma: no cover - visible => box exists
                raise RuntimeError(f"no bounding box for {selector!r}")
            return (
                int(box["x"] + box["width"] / 2),
                int(box["y"] + box["height"] / 2),
            )

        # Login
        recorder.click(*center("#username"))
        recorder.type_text("officer.demo")
        recorder.click(*center("#password"))
        recorder.type_text("mockloan-demo-pass")
        recorder.click(*center("#signin"))

        # Pipeline -> open first loan
        recorder.click(*center(".open-btn"))

        # Loan -> new disbursement
        recorder.click(*center("#new-disbursement"))

        # Disbursement: product chooser, memo (parameterized), authorize
        recorder.click(*center("#product-personal"))
        recorder.click(*center("#memo"))
        recorder.type_text(memo_text, param=param_name)
        recorder.click(*center("#authorize"))

        return recorder.finish()
    finally:
        close()
