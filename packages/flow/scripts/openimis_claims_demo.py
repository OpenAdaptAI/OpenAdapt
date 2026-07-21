"""openIMIS claims-intake reference demo: up -> bootstrap -> record -> compile -> replay.

Drives the INSURANCE reference environment in ``benchmark/openimis_claims``
(a real open-source health-insurance system run from digest-pinned images)
through the same loop the healthcare (OpenEMR) and lending (Frappe) reference
environments use:

1. ``up``         start the pinned stack (loopback-only, synthetic demo data)
2. ``bootstrap``  create the synthetic policyholder with in-force coverage
3. ``record``     scripted demonstration of the claims-intake form, captured
                  through ``Recorder`` (frames + events, replay never uses
                  selectors)
4. ``compile``    recording -> workflow bundle
5. ``replay``     replay the bundle with a fresh claim number; success is
                  established ONLY by the SQL claim oracle (exactly one
                  'Entered' claim row), never by pixels or self-report
6. ``down``       stop the stack

Record time is allowed to cheat with Playwright locators (to find pixel
coordinates via ``bounding_box()``), exactly like the MockMed demo driver;
every action is performed through ``Recorder`` so frames and events are
captured as a human demonstration would be.

Authentication and opening the blank claim form (health facility + claim
administrator context) are declared unmeasured setup, mirroring the Frappe
Lending benchmark's setup boundary.

``--record-video`` on record/replay opt-in captures a WebM of the session for
the website media pipeline; it never changes what is recorded or replayed.

All data is synthetic. Any published screenshot must be labelled as the
"openIMIS reference environment" and must not imply a customer deployment.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time
from pathlib import Path
from typing import Any, Callable

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "benchmark"))

from openimis_claims.fixture import (  # noqa: E402
    ACTOR_PASSWORD,
    ACTOR_USER,
    CLAIM_ADMIN_CODE,
    CLAIM_CODE_MAX_LEN,
    DEFAULT_CLAIM_CODE,
    DIAGNOSIS_OPTION,
    DIAGNOSIS_QUERY,
    HEALTH_FACILITY_CODE,
    POLICYHOLDER_CHF,
    POLICYHOLDER_NAME,
    SERVICE_OPTION,
    SERVICE_QUERY,
    FixtureError,
    OpenIMISFixture,
)

EXPLANATION_TEXT = "Synthetic demonstration claim (OpenAdapt reference run)"
SETTLE = {
    "settle_timeout_s": 10.0,
    "settle_stable_frames": 3,
    "settle_interval_s": 0.3,
}


def _center(locator: Any) -> tuple[int, int]:
    locator.wait_for(state="visible", timeout=30_000)
    box = locator.bounding_box()
    if box is None:
        raise FixtureError("target control has no bounding box")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _control_input(page: Any, label_text: str) -> Any:
    """The input inside the MUI FormControl whose label contains ``label_text``."""
    return page.locator(
        f'xpath=//label[contains(normalize-space(.), "{label_text}")]'
        '/ancestor::*[contains(@class,"MuiFormControl-root")][1]//input'
    ).first


def _setup_claim_form(page: Any, fixture: OpenIMISFixture) -> None:
    """Unmeasured setup: authenticate and open a blank claim form.

    Logs in with the synthetic demo actor, opens Claims -> Health Facility
    Claims, fixes the health-facility + claim-administrator context, and opens
    the blank claim form. None of this is part of the recorded demonstration,
    mirroring the Frappe Lending setup boundary.
    """
    page.goto(fixture.front_url, wait_until="networkidle", timeout=60_000)
    page.locator("input[type=text]").fill(ACTOR_USER)
    page.locator("input[type=password]").fill(ACTOR_PASSWORD)
    page.get_by_role("button", name="LOG IN").click()
    page.wait_for_url("**/home", timeout=60_000)
    page.goto(
        f"{fixture.base_url}/front/claim/healthFacilities",
        wait_until="networkidle",
    )
    hf = page.get_by_placeholder("Search a Health Facility")
    hf.click()
    hf.fill(HEALTH_FACILITY_CODE)
    option = page.locator("[role=option]", has_text=HEALTH_FACILITY_CODE).first
    option.wait_for(state="visible", timeout=30_000)
    option.click()
    admin = page.get_by_placeholder("Search a Claim")
    admin.click()
    option = page.locator("[role=option]", has_text=CLAIM_ADMIN_CODE).first
    option.wait_for(state="visible", timeout=30_000)
    option.click()
    fab = page.locator("button.MuiFab-root:enabled").last
    fab.wait_for(state="visible", timeout=30_000)
    fab.click()
    page.wait_for_url("**/claim/healthFacilities/claim", timeout=30_000)
    # The form is ready when the Insurance No. control is interactable. Focus
    # it as the last unmeasured setup action: the top form row mixes the
    # fixed facility code with date controls whose OCR line segmentation is
    # unstable frame-to-frame, so a recorded click there would bake a
    # fragile identity band into the bundle (the zero-budget identifier
    # check would then over-halt healthy replays). The demonstration itself
    # starts at the first keystroke of the insuree number.
    ins = _control_input(page, "Insurance No")
    ins.wait_for(state="visible", timeout=30_000)
    page.wait_for_load_state("networkidle")
    ins.click()


def _wait_option(page: Any, text: str) -> Any:
    option = page.locator("[role=option]", has_text=text).first
    option.wait_for(state="visible", timeout=30_000)
    return option


def _demonstrate_claim(recorder: Any, page: Any, *, claim_code: str) -> None:
    """The recorded demonstration: enter and save one claim."""
    # Policyholder: typing the insuree number (into the field focused by
    # setup) resolves the synthetic policyholder's name and (active) policy
    # panel before anything else is entered — the identity evidence the
    # demonstration is anchored on.
    recorder.type_text(POLICYHOLDER_CHF, param="insurance_no")
    # The resolved policyholder name renders inside the read-only Name input
    # ("Doe Avery" — last name first), and the coverage panel flips to
    # "Policy Information (Active)". Both must appear before continuing.
    resolved_name = " ".join(reversed(POLICYHOLDER_NAME.split()))
    page.wait_for_function(
        "(name) => [...document.querySelectorAll('input')]"
        ".some((i) => i.value.includes(name))",
        arg=resolved_name,
        timeout=30_000,
    )
    page.get_by_text("Policy Information (Active)").wait_for(
        state="visible", timeout=30_000
    )

    claim_no = _control_input(page, "Claim No")
    recorder.click(*_center(claim_no))
    recorder.type_text(claim_code, param="claim_no")

    diagnosis = page.get_by_placeholder("Search Diagnosis").first
    recorder.click(*_center(diagnosis))
    recorder.type_text(DIAGNOSIS_QUERY)
    option = _wait_option(page, DIAGNOSIS_OPTION)
    recorder.click(*_center(option))

    explanation = _control_input(page, "Explanation")
    recorder.click(*_center(explanation))
    recorder.type_text(EXPLANATION_TEXT, param="explanation")

    # The Services grid sits below the initial viewport once the active
    # policy panel renders. Record the wheel scroll explicitly (immediately
    # after the last top-of-form input) so replay never relies on
    # locator-induced hidden scrolling — same discipline as Frappe Lending.
    recorder.scroll(0, 400)
    page.wait_for_timeout(500)

    service = page.get_by_placeholder("Search Service").first
    recorder.click(*_center(service))
    recorder.type_text(SERVICE_QUERY)
    option = _wait_option(page, SERVICE_OPTION)
    recorder.click(*_center(option))
    # Selecting the service auto-fills quantity (1) and the tariff price.
    page.wait_for_timeout(1_000)

    save = page.locator("button.MuiFab-root:enabled").last
    recorder.click(*_center(save))
    # Saved: the form flips to its read-only (locked) presentation.
    page.wait_for_timeout(3_000)


def cmd_up(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port)
    print(f"starting pinned openIMIS stack on {fixture.base_url} ...")
    fixture.up()
    print("stack ready:", fixture.front_url)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port)
    holder = fixture.bootstrap()
    print("synthetic policyholder ready:", holder)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port)
    fixture.down(volumes=args.volumes)
    print("stack stopped")
    return 0


def _launch(fixture: OpenIMISFixture, *, headed: bool, video_dir: str | None):
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    return PlaywrightBackend.launch(
        fixture.front_url,
        headless=not headed,
        record_video_dir=video_dir,
    )


def _finish_video(backend: Any, close: Callable[[], None], out_hint: str) -> None:
    video = getattr(backend.page, "video", None)
    close()
    if video is not None:
        try:
            print(f"{out_hint} video:", video.path())
        except Exception:  # noqa: BLE001 - videos are best-effort media
            pass


def cmd_record(args: argparse.Namespace) -> int:
    from openadapt_flow.recorder import Recorder

    out_dir = Path(args.out)
    if out_dir.exists():
        raise FixtureError(f"recording path already exists: {out_dir}")
    claim_code = _validate_claim_code(args.claim_code)
    fixture = OpenIMISFixture(http_port=args.port)
    fixture.policyholder()  # fail fast if bootstrap has not run
    if fixture.claim_rows(claim_code):
        raise FixtureError(f"claim code {claim_code!r} already exists; choose another")
    backend, close = _launch(fixture, headed=args.headed, video_dir=args.record_video)
    try:
        _setup_claim_form(backend.page, fixture)
        recorder = Recorder(backend, out_dir, app_url=backend.page.url, **SETTLE)
        _demonstrate_claim(recorder, backend.page, claim_code=claim_code)
        recording = recorder.finish()
        row = fixture.verify_claim(claim_code)
        print("recording:", recording)
        print("oracle-verified claim row:", row)
    finally:
        _finish_video(backend, close, "record")
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler import compile_recording

    bundle_dir = Path(args.bundle)
    if bundle_dir.exists():
        raise FixtureError(f"bundle path already exists: {bundle_dir}")
    workflow = compile_recording(
        Path(args.recording), bundle_dir, name="openimis-claim-intake"
    )
    if not workflow.steps:
        raise FixtureError("compiler produced no steps")
    print("bundle:", bundle_dir)
    return 0


def _validate_claim_code(code: str) -> str:
    if not (0 < len(code) <= CLAIM_CODE_MAX_LEN) or not code.isalnum():
        raise FixtureError(
            f"claim code must be 1-{CLAIM_CODE_MAX_LEN} alphanumeric "
            f"characters (openIMIS claim-form limit), got {code!r}"
        )
    return code


def _fresh_claim_code() -> str:
    # 8-char limit: "OA" + HHMMSS keeps replays collision-free in practice
    # while satisfying the claim-form input limit.
    return "OA" + _dt.datetime.now(tz=_dt.timezone.utc).strftime("%H%M%S")


def cmd_replay(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    claim_code = _validate_claim_code(args.claim_code or _fresh_claim_code())
    fixture = OpenIMISFixture(http_port=args.port)
    fixture.policyholder()
    if fixture.claim_rows(claim_code):
        raise FixtureError(f"claim code {claim_code!r} already exists; choose another")
    bundle_dir = Path(args.bundle)
    run_dir = Path(
        args.run_dir
        or f"runs/openimis-replay-{_dt.datetime.now(tz=_dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    )
    backend, close = _launch(fixture, headed=args.headed, video_dir=args.record_video)
    started = time.monotonic()
    try:
        _setup_claim_form(backend.page, fixture)
        report = Replayer(backend).run(
            Workflow.load(bundle_dir),
            params={"claim_no": claim_code},
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        wall_s = time.monotonic() - started
        row = fixture.verify_claim(claim_code)
        print("replay success:", report.success)
        print("run dir:", run_dir)
        print(f"replay wall time: {wall_s:.1f}s")
        print("oracle-verified claim row:", row)
        if not report.success:
            return 1
    finally:
        _finish_video(backend, close, "replay")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=OpenIMISFixture().http_port,
        help="loopback port the pinned stack serves on",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("up", help="start the pinned stack and wait until ready")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser(
        "bootstrap", help="create the synthetic policyholder (idempotent)"
    )
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("record", help="record the scripted claims-intake demo")
    p.add_argument("--out", required=True, help="recording output directory")
    p.add_argument(
        "--claim-code",
        default=DEFAULT_CLAIM_CODE,
        help=f"claim number to enter (max {CLAIM_CODE_MAX_LEN} chars)",
    )
    p.add_argument("--headed", action="store_true", help="run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help="OPT-IN: capture a WebM of the session into DIR (media pipeline)",
    )
    p.set_defaults(func=cmd_record)

    p = sub.add_parser("compile", help="compile a recording into a bundle")
    p.add_argument("--recording", required=True)
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser("replay", help="replay the bundle with a fresh claim number")
    p.add_argument("--bundle", required=True)
    p.add_argument(
        "--claim-code",
        default=None,
        help="claim number to write (default: fresh OAHHMMSS)",
    )
    p.add_argument("--run-dir", default=None)
    p.add_argument("--headed", action="store_true", help="run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help="OPT-IN: capture a WebM of the session into DIR (media pipeline)",
    )
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("down", help="stop the stack")
    p.add_argument("--volumes", action="store_true", help="also remove data volumes")
    p.set_defaults(func=cmd_down)

    args = parser.parse_args()
    try:
        return args.func(args)
    except FixtureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
