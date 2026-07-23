"""openIMIS coverage / eligibility-check reference demo (effect-verified).

Drives the INSURANCE reference environment in ``benchmark/openimis_claims``
through a front-office **coverage verification**: look up a policyholder by
insuree number in openIMIS's Insuree Enquiry, confirm the policy panel and a
service-eligibility answer, and certify the result against the SYSTEM OF
RECORD -- a read-only SQL effect contract over the exact policy/product/service
rows
(``docs/EFFECT_KIT.md``), never the pixels.

1. ``up``         start the pinned stack (loopback-only, synthetic demo data)
2. ``bootstrap``  synthetic policyholders (two eligible on the declared date,
                  one expired, one future-dated) + the read-only SQL-oracle role
3. ``record``     scripted demonstration of the enquiry lookup, captured
                  through ``Recorder`` (frames + structural/visual evidence)
4. ``compile``    recording -> bundle, with the eligibility outcome bound to
                  the run's insuree, service, and as-of-date parameters
5. ``replay``     replay the bundle for any policyholder; every run is
                  verified by the SQL effect verifier built from
                  ``deployment.eligibility.yaml``. A policyholder who is NOT
                  eligible for the declared service/date REFUTES the contract
                  and the run HALTS -- the demo's halt-on-anomaly moment
                  (``--insuree 999000002 --expect-halt``)
6. ``down``       stop the stack

Why the effect contract is the point: the enquiry dialog for the LAPSED
policyholder still renders a service-eligibility thumbs-up -- a screen a
human (or a screen-scraping bot) can misread as "covered". The system-of-
record read refuses: eligibility = Ineligible -> REFUTED -> HALT with evidence.

The declared question is fixed and reproducible: is service A1 (General
Consultation) eligible on 2026-07-21 for the run's ``insurance_no``?  The
oracle joins the policy to its product/service benefit and evaluates policy
status plus effective/expiry dates; it never consults the host clock.

Record time uses Playwright locators to find pixel coordinates, while the
recorder also captures stable DOM locators and run-bound structured identity
for the three clicked controls. Every action is performed through ``Recorder``.
Authentication is declared unmeasured setup, mirroring the claims demo's setup
boundary. ``--record-video`` opt-in captures a WebM for the media pipeline; it
never changes what is recorded or replayed.

All data is synthetic. Any published screenshot must be labelled as the
"openIMIS reference environment" and must not imply a customer deployment.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
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
    ELIGIBILITY_AS_OF_DATE,
    ELIGIBILITY_SERVICE_CODE,
    LAPSED_CHF,
    ORACLE_PASSWORD_ENV,
    POLICYHOLDER_CHF,
    SECOND_ACTIVE_CHF,
    FixtureError,
    OpenIMISFixture,
)

from openadapt_flow.backend import StructuralResolutionRefused  # noqa: E402
from openadapt_flow.backends.playwright_backend import PlaywrightBackend  # noqa: E402
from openadapt_flow.ir import StructuralHandle, StructuralLocator  # noqa: E402

HERE = Path(__file__).resolve().parent
DEPLOYMENT_YAML = (
    REPO_ROOT / "benchmark" / "openimis_claims" / ("deployment.eligibility.yaml")
)

SERVICE_QUERY = "General"
SERVICE_OPTION = "General Consultation"
SETTLE: dict[str, Any] = {
    "settle_timeout_s": 10.0,
    "settle_stable_frames": 3,
    "settle_interval_s": 0.3,
}


class OpenIMISEligibilityBackend(PlaywrightBackend):
    """Browser backend with openIMIS-specific stable target evidence.

    Material UI renders the global enquiry and service inputs with generated
    class names/ids, and the service options in a portal outside the dialog.
    Generic pixel recording therefore cannot reliably bind these controls to
    the checked policyholder.  This narrow adapter records stable semantic
    locators and a structured identity string that includes the run's insuree
    number for dialog-scoped actions.  The compiler parameterizes that value,
    so replay verifies the current run's policyholder before acting.
    """

    _STRUCTURAL_JS = r"""([px, py]) => {
        const hit = document.elementFromPoint(px, py);
        if (!hit) return null;
        const input = hit.closest('input');
        const placeholder = input ? (input.getAttribute('placeholder') || '') : '';
        if (placeholder.startsWith('Insuree enquiry')) {
            return {
                selector: 'input[placeholder^="Insuree enquiry"]',
                role: 'textbox',
                name: 'Insuree enquiry'
            };
        }
        if (placeholder.startsWith('Search Service')) {
            return {
                selector: 'input[placeholder^="Search Service"]',
                role: 'textbox',
                name: 'Search Service'
            };
        }
        const option = hit.closest('[role="option"]');
        if (option) {
            const name = (option.textContent || '').replace(/\s+/g, ' ').trim();
            return name ? {role: 'option', name: name} : null;
        }
        return null;
    }"""

    _IDENTITY_JS = r"""([px, py]) => {
        const hit = document.elementFromPoint(px, py);
        if (!hit) return null;
        const input = hit.closest('input');
        const placeholder = input ? (input.getAttribute('placeholder') || '') : '';
        let targetKind = null;
        let targetId = null;
        if (placeholder.startsWith('Insuree enquiry')) {
            targetKind = 'eligibility_lookup';
            targetId = 'insurance_no';
        } else if (placeholder.startsWith('Search Service')) {
            targetKind = 'eligibility_service';
            targetId = 'service_code';
        } else {
            const option = hit.closest('[role="option"]');
            if (option) {
                targetKind = 'eligibility_service_option';
                targetId = (option.textContent || '').replace(/\s+/g, ' ').trim();
            }
        }
        if (!targetKind) return null;
        const context = {target_kind: targetKind, target_id: targetId};
        let dialogIdentifiers = [];
        if (targetKind !== 'eligibility_lookup') {
            const visibleDialogs = Array.from(
                document.querySelectorAll('[role="dialog"]')
            ).filter((dialog) => {
                const style = window.getComputedStyle(dialog);
                const rect = dialog.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.width > 0 && rect.height > 0;
            });
            for (const dialog of visibleDialogs) {
                const identifiers = new Set();
                for (const match of (dialog.innerText || '').matchAll(/\b999\d{6}\b/g)) {
                    identifiers.add(match[0]);
                }
                if (identifiers.size) dialogIdentifiers.push(Array.from(identifiers));
            }
        }
        return {context: context, dialog_identifiers: dialogIdentifiers};
    }"""

    def structural_locator_at(self, x: int, y: int) -> StructuralLocator | None:
        try:
            result = self.page.evaluate(self._STRUCTURAL_JS, [int(x), int(y)])
        except Exception:
            return None
        if not result:
            return None
        return StructuralLocator(
            selector=result.get("selector"),
            role=result.get("role"),
            name=result.get("name"),
        )

    def structured_text_at(self, x: int, y: int) -> str | None:
        try:
            result = self.page.evaluate(self._IDENTITY_JS, [int(x), int(y)])
        except Exception:
            return None
        if not isinstance(result, dict) or not isinstance(result.get("context"), dict):
            return None
        context = dict(result["context"])
        if context.get("target_kind") != "eligibility_lookup":
            dialogs = result.get("dialog_identifiers")
            # A single distinct identifier is not enough when two dialogs
            # display the same record. Require exactly one record-bearing
            # dialog and one identifier so Set de-duplication cannot collapse
            # either same-record or different-record ambiguity.
            if (
                not isinstance(dialogs, list)
                or len(dialogs) != 1
                or not isinstance(dialogs[0], list)
                or len(dialogs[0]) != 1
                or not isinstance(dialogs[0][0], str)
            ):
                return None
            context["insurance_no"] = dialogs[0][0]
        return json.dumps(context, separators=(",", ":"))

    def locate_structural(self, locator: StructuralLocator) -> StructuralHandle | None:
        """Refuse duplicate semantic candidates instead of falling to pixels.

        The generic browser backend treats an ambiguous structural locator as
        a miss so less specialized workflows may continue down the evidence
        ladder.  This reference adapter authors exact semantic locators for
        every clicked control, so more than one live candidate is a record/
        dialog ambiguity and must halt rather than let a visual rung pick one.
        """
        if locator.selector:
            candidates = self.page.locator(locator.selector)
        elif locator.role and locator.name:
            candidates = self.page.get_by_role(
                locator.role,  # type: ignore[arg-type]
                name=locator.name,
                exact=True,
            )
        else:
            return None
        try:
            visible_indices = candidates.evaluate_all(
                """elements => elements.flatMap((el, index) => {
                    const style = window.getComputedStyle(el);
                    const rect = el.getBoundingClientRect();
                    const visible = style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && style.visibility !== 'collapse'
                        && rect.width > 0 && rect.height > 0
                        && rect.right > 0 && rect.bottom > 0
                        && rect.left < window.innerWidth
                        && rect.top < window.innerHeight;
                    return visible ? [index] : [];
                })"""
            )
            if not isinstance(visible_indices, list) or not all(
                isinstance(index, int) for index in visible_indices
            ):
                return None
            if len(visible_indices) > 1:
                raise StructuralResolutionRefused(
                    "openIMIS eligibility target is structurally ambiguous"
                )
            if not visible_indices:
                return None
            candidate = candidates.nth(visible_indices[0])
            box = candidate.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                return None
            cx = int(round(box["x"] + box["width"] / 2))
            cy = int(round(box["y"] + box["height"] / 2))
            vw, vh = self.viewport
            if not (0 <= cx < vw and 0 <= cy < vh):
                return None
            topmost = candidate.evaluate(
                """(el, pt) => {
                    const n = document.elementFromPoint(pt[0], pt[1]);
                    return !!n && (n === el || el.contains(n));
                }""",
                [cx, cy],
            )
            if not topmost:
                return None
            return StructuralHandle(point=(cx, cy), candidate_count=1)
        except StructuralResolutionRefused:
            raise
        except Exception:
            # Observation failures are a miss; the resolver may use other
            # evidence, but a proven multi-candidate ambiguity above is a
            # terminal refusal and never reaches a weaker rung.
            return None


def eligibility_effects() -> list[Any]:
    """The eligibility outcome bound to the run's ``insurance_no``.

    Substrate-neutral (``docs/EFFECT_KIT.md``); the committed
    ``deployment.eligibility.yaml`` wires them to the read-only SQL verifier
    over openIMIS's own PostgreSQL policy/product/service tables.
    """
    from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr

    return [
        Effect(
            kind=EffectKind.FIELD_EQUALS,
            match={
                "chf_id": ValueExpr(param="insurance_no"),
                "service_code": ValueExpr(param="service_code"),
                "as_of_date": ValueExpr(param="as_of_date"),
            },
            field="eligibility",
            value=ValueExpr(literal="Eligible"),
            risk="reversible",
            probe=(
                f"service {ELIGIBILITY_SERVICE_CODE} is covered by exactly one "
                f"active, effective policy on {ELIGIBILITY_AS_OF_DATE}"
            ),
        )
    ]


def _center(locator: Any) -> tuple[int, int]:
    locator.wait_for(state="visible", timeout=30_000)
    box = locator.bounding_box()
    if box is None:
        raise FixtureError("target control has no bounding box")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _login(page: Any, fixture: OpenIMISFixture) -> None:
    """Unmeasured setup: authenticate and settle the home layout.

    The home app bar assembles asynchronously (the language selector, the
    welcome banner, and the tasks drawer each land on their own GraphQL
    round-trip) and the menu row RE-WRAPS as they land, moving the enquiry
    field. Record and replay must both demonstrate from the FINAL layout, so
    setup waits for the late-loading landmarks before handing control to the
    measured demonstration.
    """
    page.goto(fixture.front_url, wait_until="networkidle", timeout=60_000)
    page.locator("input[type=text]").fill(ACTOR_USER)
    page.locator("input[type=password]").fill(ACTOR_PASSWORD)
    page.get_by_role("button", name="LOG IN").click()
    page.wait_for_url("**/home", timeout=60_000)
    page.wait_for_load_state("networkidle")
    page.get_by_text("ENGLISH").first.wait_for(state="visible", timeout=30_000)
    page.get_by_text("Welcome Admin").first.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(2_500)


def _demonstrate_eligibility(recorder: Any, page: Any, *, insuree: str) -> None:
    """The recorded demonstration: one coverage / eligibility check."""
    # Look up the policyholder: the app-bar Insuree Enquiry resolves the
    # insuree and their policy panel from the insuree number alone.
    enquiry = page.get_by_placeholder("Insuree enquiry")
    recorder.click(*_center(enquiry))
    recorder.type_text(insuree, param="insurance_no")
    recorder.press("Enter")
    # Record-time cheat (waits only): the enquiry dialog must render the
    # policyholder's policy table before the demonstration continues.
    dialog = page.locator("[role=dialog]").first
    dialog.wait_for(state="visible", timeout=30_000)
    dialog.get_by_text("Policies").first.wait_for(state="visible", timeout=30_000)
    page.wait_for_timeout(1_500)

    # Ask the service-eligibility question a front office asks ("is this
    # visitor covered for this service?").
    service = page.get_by_placeholder("Search Service").first
    recorder.click(*_center(service))
    recorder.type_text(SERVICE_QUERY)
    option = page.locator("[role=option]", has_text=SERVICE_OPTION).first
    option.wait_for(state="visible", timeout=30_000)
    recorder.click(*_center(option))
    # The eligibility indicator renders next to the selected service.
    page.wait_for_timeout(2_000)


def _launch(fixture: OpenIMISFixture, *, headed: bool, video_dir: str | None):
    return OpenIMISEligibilityBackend.launch(
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


def _stage_oracle_password(fixture: OpenIMISFixture) -> None:
    """Surface the oracle role's generated secret as the kit's env reference.

    The committed deployment YAML names ``OPENIMIS_ORACLE_PASSWORD`` (secrets
    are references, never literals); the fixture's generated secret lives in
    the ignored ``out/state/secrets.json``. An operator-set value wins.
    """
    os.environ.setdefault(ORACLE_PASSWORD_ENV, fixture.oracle_password())


def _build_verifier(params: dict[str, str], *, oracle_port: int) -> Any:
    from openadapt_flow.deployment import build_effect_verifier, load_deployment

    config = load_deployment(DEPLOYMENT_YAML)
    # The committed deployment uses the canonical fixture port (9402). An
    # isolated rehearsal may select another loopback port; only the transport
    # endpoint changes, never the query, role, or outcome contract.
    config.effects.sql_connect_args["port"] = oracle_port
    verifier = build_effect_verifier(config.effects, params)
    if verifier is None:
        raise FixtureError(f"{DEPLOYMENT_YAML} wired no effect verifier")
    return verifier


def _report_contract_error(
    report: Any,
    *,
    expect_halt: bool,
    expected_contract_hash: str,
) -> str | None:
    """Return why a replay did not prove the one declared SQL outcome.

    ``--expect-halt`` is evidence tooling, so an unrelated resolver, login, or
    postcondition failure must never be counted as the intended
    system-of-record refusal.  The positive lane is equally strict: a generic
    successful report without the exact confirmed contract is not this demo's
    success condition.
    """
    results = list(report.results)
    contracted = [result for result in results if result.effect_contract_hashes]
    if len(contracted) != 1:
        return f"expected exactly one effect-armed step, observed {len(contracted)}"
    effect_step = contracted[0]
    if list(effect_step.effect_contract_hashes) != [expected_contract_hash]:
        return "the effect-armed step does not carry the exact eligibility contract"
    if not results or effect_step is not results[-1]:
        return "the effect-armed eligibility step was not the final executed step"

    if expect_halt:
        if report.success:
            return "expected SQL refusal, but the run reported success"
        if any(not result.ok for result in results[:-1]):
            return "the run failed before the eligibility outcome was checked"
        if effect_step.ok or effect_step.effect_verified is not False:
            return "the final step did not halt on a refuted effect contract"
        if not any(
            "[sql] field_equals: REFUTED" in line for line in effect_step.effect_results
        ):
            return "the final step lacks the expected SQL field_equals refusal"
        return None

    if not report.success or any(not result.ok for result in results):
        return "the run did not complete every compiled step successfully"
    if effect_step.effect_verified is not True:
        return "the final eligibility outcome was not independently confirmed"
    if not any(
        "[sql] field_equals: CONFIRMED" in line for line in effect_step.effect_results
    ):
        return "the final step lacks the expected SQL field_equals confirmation"
    return None


def cmd_up(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port, db_port=args.oracle_port)
    print(f"starting pinned openIMIS stack on {fixture.base_url} ...")
    fixture.up()
    print("stack ready:", fixture.front_url)
    return 0


def cmd_bootstrap(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port, db_port=args.oracle_port)
    holders = fixture.bootstrap_eligibility()
    for chf, holder in holders.items():
        print(f"synthetic policyholder {chf}:", holder)
    return 0


def cmd_down(args: argparse.Namespace) -> int:
    fixture = OpenIMISFixture(http_port=args.port, db_port=args.oracle_port)
    fixture.down(volumes=args.volumes)
    print("stack stopped")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    from openadapt_flow.recorder import Recorder

    out_dir = Path(args.out)
    if out_dir.exists():
        raise FixtureError(f"recording path already exists: {out_dir}")
    fixture = OpenIMISFixture(http_port=args.port, db_port=args.oracle_port)
    coverage = fixture.coverage(args.insuree)
    if coverage["eligibility"] != "Eligible":
        raise FixtureError(
            f"demonstration insuree {args.insuree} is not eligible "
            f"for service {ELIGIBILITY_SERVICE_CODE} on "
            f"{ELIGIBILITY_AS_OF_DATE} ({coverage}); demonstrate on a "
            "policyholder eligible for that declared question"
        )
    backend, close = _launch(fixture, headed=args.headed, video_dir=args.record_video)
    try:
        _login(backend.page, fixture)
        recorder = Recorder(backend, out_dir, app_url=backend.page.url, **SETTLE)
        _demonstrate_eligibility(recorder, backend.page, insuree=args.insuree)
        recording = recorder.finish()
        print("recording:", recording)
        print("oracle coverage row:", coverage)
    finally:
        _finish_video(backend, close, "record")
    return 0


def cmd_compile(args: argparse.Namespace) -> int:
    from openadapt_flow.compiler import compile_recording

    bundle_dir = Path(args.bundle)
    if bundle_dir.exists():
        raise FixtureError(f"bundle path already exists: {bundle_dir}")
    workflow = compile_recording(
        Path(args.recording), bundle_dir, name="openimis-eligibility-check"
    )
    if not workflow.steps:
        raise FixtureError("compiler produced no steps")
    # Make the complete eligibility question explicit in every run report and
    # bind all three dimensions into both the effect selector and SQL query.
    workflow.params["service_code"] = ELIGIBILITY_SERVICE_CODE
    workflow.params["as_of_date"] = ELIGIBILITY_AS_OF_DATE
    # Bind the coverage contracts to the LAST demonstrated step: verification
    # runs after the eligibility answer is on screen, certifying the check
    # against the system of record before the run may report success.
    workflow.steps[-1].effects = eligibility_effects()
    workflow.save(bundle_dir)
    print("bundle:", bundle_dir)
    print(
        "effect contracts:",
        [e.probe for e in workflow.steps[-1].effects],
    )
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    fixture = OpenIMISFixture(http_port=args.port, db_port=args.oracle_port)
    insuree = args.insuree
    expected = fixture.coverage(insuree)  # fail fast if bootstrap has not run
    _stage_oracle_password(fixture)
    params = {
        "insurance_no": insuree,
        "service_code": ELIGIBILITY_SERVICE_CODE,
        "as_of_date": ELIGIBILITY_AS_OF_DATE,
    }
    verifier = _build_verifier(params, oracle_port=args.oracle_port)
    bundle_dir = Path(args.bundle)
    run_dir = Path(
        args.run_dir
        or "runs/openimis-eligibility-"
        f"{_dt.datetime.now(tz=_dt.timezone.utc):%Y%m%dT%H%M%SZ}"
    )
    backend, close = _launch(fixture, headed=args.headed, video_dir=args.record_video)
    started = time.monotonic()
    try:
        _login(backend.page, fixture)
        report = Replayer(backend, effect_verifier=verifier).run(
            Workflow.load(bundle_dir),
            params=params,
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
    finally:
        _finish_video(backend, close, "replay")
    wall_s = time.monotonic() - started
    from openadapt_flow.report import render_run_report

    report_md = render_run_report(run_dir)
    print("replay success:", report.success)
    print("run dir:", run_dir)
    print("run report:", report_md)
    print(f"replay wall time: {wall_s:.1f}s")
    print("system-of-record coverage row:", expected)
    for result in report.results:
        if result.effect_contract_hashes:
            print(
                f"step {result.step_id}: effect_verified={result.effect_verified} "
                f"contracts={result.effect_contract_hashes}"
            )
    expected_contract_hash = eligibility_effects()[0].resolve(params).contract_hash()
    contract_error = _report_contract_error(
        report,
        expect_halt=args.expect_halt,
        expected_contract_hash=expected_contract_hash,
    )
    if contract_error is not None:
        print(f"error: {contract_error}", file=sys.stderr)
        return 1
    if args.expect_halt:
        print(
            "HALT demonstrated: the system-of-record contract refused to "
            "certify coverage (see the run report's effect-verification "
            "section)"
        )
        return 0
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--port",
        type=int,
        default=OpenIMISFixture().http_port,
        help="loopback port the pinned stack serves on",
    )
    parser.add_argument(
        "--oracle-port",
        type=int,
        default=OpenIMISFixture().db_port,
        help="loopback PostgreSQL port for the read-only eligibility verifier",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("up", help="start the pinned stack and wait until ready")
    p.set_defaults(func=cmd_up)

    p = sub.add_parser(
        "bootstrap",
        help=(
            "create the synthetic policyholders (eligible + expired + future) "
            "and the read-only SQL-oracle role (idempotent)"
        ),
    )
    p.set_defaults(func=cmd_bootstrap)

    p = sub.add_parser("record", help="record the scripted eligibility check")
    p.add_argument("--out", required=True, help="recording output directory")
    p.add_argument(
        "--insuree",
        default=POLICYHOLDER_CHF,
        help="insuree number to demonstrate on (must be eligible for A1/date)",
    )
    p.add_argument("--headed", action="store_true", help="run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help="OPT-IN: capture a WebM of the session into DIR (media pipeline)",
    )
    p.set_defaults(func=cmd_record)

    p = sub.add_parser(
        "compile",
        help="compile a recording into a bundle with the coverage contracts",
    )
    p.add_argument("--recording", required=True)
    p.add_argument("--bundle", required=True)
    p.set_defaults(func=cmd_compile)

    p = sub.add_parser(
        "replay",
        help=(
            "replay the eligibility check for any policyholder; the SQL "
            "effect verifier certifies (or refuses) the coverage answer. "
            f"--insuree {LAPSED_CHF} --expect-halt demonstrates the halt"
        ),
    )
    p.add_argument("--bundle", required=True)
    p.add_argument(
        "--insuree",
        default=SECOND_ACTIVE_CHF,
        help=(
            "insuree number to check (default: the A1/date-eligible "
            "policyholder the demonstration never saw)"
        ),
    )
    p.add_argument("--run-dir", default=None)
    p.add_argument("--headed", action="store_true", help="run the browser headed")
    p.add_argument(
        "--record-video",
        default=None,
        metavar="DIR",
        help="OPT-IN: capture a WebM of the session into DIR (media pipeline)",
    )
    p.add_argument(
        "--expect-halt",
        action="store_true",
        help=(
            "assert the replay HALTS (exit 0 on halt, 1 on unexpected "
            "success) -- the halt-on-anomaly demonstration"
        ),
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
