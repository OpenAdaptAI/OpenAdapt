"""Matched, pinned, synthetic local OpenEMR benchmark driver.

The measured task starts after authentication on a blank patient-registration
form.  Each trial restores one SHA-256-bound SQL baseline.  The exact same
three arms and two conditions as the Frappe Lending reference are used:

* compiled: model-free replay of one recorded browser demonstration;
* agent: computer-use model, gated by explicit opt-in and two cost caps;
* api: model-free OpenEMR Standard REST control.

Success comes only from a separately authenticated read-only REST client plus
direct SQL/delta audit.  Pixels and actor self-report are never the oracle.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from benchmark.openemr_local.fixture import (  # noqa: E402
    FixtureError,
    OpenEMRFixture,
    audit_table_deltas,
)
from openadapt_flow.backends.playwright_backend import (  # noqa: E402
    PlaywrightBackend as _BasePlaywrightBackend,
)
from openadapt_flow.benchmark import agent_baseline  # noqa: E402
from openadapt_flow.benchmark.openemr_local import (  # noqa: E402
    CONDITIONS,
    INITIAL_TRIALS_PER_CELL,
    PUBLICATION_TRIALS_PER_CELL,
    OpenEMRPatientOracle,
    SyntheticPatientSpec,
    TrialRow,
    aggregate_rows,
    classify_patient_trial,
    patient_api_binding,
    patient_effects,
    patient_records_sha256,
    publication_gate,
)
from openadapt_flow.ir import (  # noqa: E402
    Postcondition,
    PostconditionKind,
    StructuralHandle,
    StructuralLocator,
)

FORM_PATH = "/interface/new/new_comprehensive.php"
FORM_FRAME_NAME = "openadapt-new-patient"
AGENT_MAX_ACTIONS = 35
# The free token-counting endpoint accepts the same tools/images/messages as a
# sampling call. Anthropic documents its estimate as potentially differing by
# a small amount, so every count gets a deliberately large 20k-token cushion.
# Requests whose guarded count could enter >200k long-context pricing are
# refused before the paid call. Within that standard tier, cache-write input is
# the most expensive possible input bucket and therefore the safe bound.
AGENT_STANDARD_INPUT_LIMIT = 200_000
AGENT_TOKEN_COUNT_MARGIN = 20_000
FULL_ARMS = ("compiled", "agent", "api")
MODEL_FREE_ARMS = ("compiled", "api")
ORACLE_SETTLE_S = 1.0
PRE_TRIAL_STABLE_SAMPLES = 3
PRE_TRIAL_STABLE_INTERVAL_S = 0.5
PRE_TRIAL_STABLE_TIMEOUT_S = 10.0


class AgentBudgetRefusal(RuntimeError):
    """A paid model call was refused before dispatch by the hard cost guard."""


class _AtomicUsageLedger(agent_baseline.UsageLedger):
    """Record a response only after every priced usage field is readable."""

    def record(self, usage: Any) -> None:
        input_tokens = int(usage.input_tokens)
        output_tokens = int(usage.output_tokens)
        cache_write = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        cache_read = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        if min(input_tokens, output_tokens, cache_write, cache_read) < 0:
            raise ValueError("provider usage token counts must be non-negative")
        self.api_calls += 1
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cache_creation_input_tokens += cache_write
        self.cache_read_input_tokens += cache_read


class _BudgetedMessages:
    def __init__(
        self,
        messages: Any,
        ledger: agent_baseline.UsageLedger,
        hard_cap_usd: float,
    ) -> None:
        self._messages = messages
        self._ledger = ledger
        self._hard_cap_usd = hard_cap_usd
        self.count_calls = 0
        self.paid_attempts = 0
        self.max_guarded_input_tokens = 0
        self.spend_indeterminate = False

    def create(self, **kwargs: Any) -> Any:
        """Count for free, reserve worst output/cache price, then dispatch."""
        count = self._messages.count_tokens(
            model=kwargs["model"],
            messages=kwargs["messages"],
            tools=kwargs["tools"],
            betas=kwargs["betas"],
        )
        self.count_calls += 1
        guarded_input = int(count.input_tokens) + AGENT_TOKEN_COUNT_MARGIN
        self.max_guarded_input_tokens = max(
            self.max_guarded_input_tokens, guarded_input
        )
        if guarded_input > AGENT_STANDARD_INPUT_LIMIT:
            raise AgentBudgetRefusal("guarded input could enter long-context pricing")
        worst_next_cost = (
            guarded_input / 1_000_000 * agent_baseline.CACHE_WRITE_USD_PER_MTOK
            + int(kwargs["max_tokens"]) / 1_000_000 * agent_baseline.OUTPUT_USD_PER_MTOK
        )
        if self._ledger.cost_usd + worst_next_cost > self._hard_cap_usd:
            raise AgentBudgetRefusal(
                "next paid response could exceed the approved per-run cap"
            )
        self.paid_attempts += 1
        try:
            return self._messages.create(**kwargs)
        except Exception:
            # A timeout/transport failure can occur after provider acceptance.
            # No local ledger can prove whether that attempt was billed.
            self.spend_indeterminate = True
            raise

    def spend_is_indeterminate(self, recorded_calls: int) -> bool:
        """Include responses returned without complete usage accounting."""
        return self.spend_indeterminate or self.paid_attempts != recorded_calls


class _BudgetedBeta:
    def __init__(self, messages: _BudgetedMessages) -> None:
        self.messages = messages


class _BudgetedAgentClient:
    """Minimal Anthropic-client facade consumed by ``run_agent``."""

    def __init__(
        self,
        client: Any,
        ledger: agent_baseline.UsageLedger,
        hard_cap_usd: float,
    ) -> None:
        self.messages = _BudgetedMessages(client.beta.messages, ledger, hard_cap_usd)
        self.beta = _BudgetedBeta(self.messages)


class _SettleOptions(TypedDict):
    settle_timeout_s: float
    settle_stable_frames: int
    settle_interval_s: float


SETTLE: _SettleOptions = {
    "settle_timeout_s": 10.0,
    "settle_stable_frames": 3,
    "settle_interval_s": 0.3,
}
DRIFT_CSS = """
body { background: #f5f3ff !important; }
.navbar, .oe-header { background: #ede9fe !important; }
"""


class OpenEMRPlaywrightBackend(_BasePlaywrightBackend):
    """Structural adapter for the pinned patient form's exact iframe.

    The general browser backend intentionally does not pierce iframes. This
    benchmark adapter resolves unique ``#form_*`` controls only when the point
    is topmost inside the authenticated new-patient frame. Identity binds the
    control to that exact form path, never to an OCR-adjacent patient value.
    """

    _INNER_TARGET_JS = """([px, py]) => {
        const el = document.elementFromPoint(px, py);
        if (!el) return null;
        const actionable = el.closest('input, select, textarea, button, a') || el;
        const id = actionable.id;
        if (!id) return null;
        if (id === 'create') {
            if (document.querySelectorAll('#create').length !== 1) return null;
            return {
                selector: '#create',
                role: 'button',
                target_kind: 'action',
                target_id: 'open_duplicate_check',
            };
        }
        if (!id.startsWith('form_')) return null;
        const selector = '#' + CSS.escape(id);
        if (document.querySelectorAll(selector).length !== 1) return null;
        let role = actionable.getAttribute('role');
        if (!role) {
            const tag = actionable.tagName.toLowerCase();
            role = {input: 'textbox', select: 'combobox', textarea: 'textbox',
                button: 'button', a: 'link'}[tag] || null;
        }
        const fieldname = id.slice(5);
        return {
            selector,
            role,
            fieldname,
            target_kind: 'field',
            target_id: fieldname,
        };
    }"""
    _CONFIRM_SELECTOR = "openemr://confirm-create"

    def _confirm_target(self, x: int, y: int) -> dict[str, str] | None:
        """Return the unique visible duplicate-confirm button under a point."""
        matches: list[Any] = []
        try:
            for frame in self.page.frames:
                locator = frame.locator("#confirmCreate")
                if locator.count() == 1 and locator.is_visible():
                    matches.append(locator)
            if len(matches) != 1:
                return None
            box = matches[0].bounding_box()
            if box is None or not (
                box["x"] <= x < box["x"] + box["width"]
                and box["y"] <= y < box["y"] + box["height"]
            ):
                return None
        except Exception:
            return None
        return {
            "selector": self._CONFIRM_SELECTOR,
            "role": "button",
            "name": "Create New Patient",
            "target_kind": "action",
            "target_id": "confirm_create",
        }

    def _form_target(self, x: int, y: int) -> dict[str, str] | None:
        try:
            iframe = self.page.locator(f'iframe[name="{FORM_FRAME_NAME}"]')
            if iframe.count() != 1:
                return None
            box = iframe.bounding_box()
            if box is None or not (
                box["x"] <= x < box["x"] + box["width"]
                and box["y"] <= y < box["y"] + box["height"]
            ):
                return None
            topmost = iframe.evaluate(
                "(el, pt) => document.elementFromPoint(pt[0], pt[1]) === el",
                [int(x), int(y)],
            )
            if not topmost:
                return None
            frame = _form_frame(self.page, timeout_s=1.0)
            result = frame.evaluate(
                self._INNER_TARGET_JS,
                [int(x - box["x"]), int(y - box["y"])],
            )
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    def structural_locator_at(self, x: int, y: int) -> StructuralLocator | None:
        target = self._confirm_target(x, y) or self._form_target(x, y)
        if target is None:
            return super().structural_locator_at(x, y)
        return StructuralLocator(
            selector=target.get("selector"),
            role=target.get("role"),
            name=target.get("name"),
        )

    def locate_structural(
        self, locator: StructuralLocator
    ) -> StructuralHandle | None:
        selector = locator.selector or ""
        if selector == self._CONFIRM_SELECTOR:
            try:
                target = self._unique_visible_confirm_locator()
                box = target.bounding_box()
                if box is None or box["width"] <= 0 or box["height"] <= 0:
                    return None
                cx = int(round(box["x"] + box["width"] / 2))
                cy = int(round(box["y"] + box["height"] / 2))
                vw, vh = self.viewport
                if not (0 <= cx < vw and 0 <= cy < vh):
                    return None
                topmost = target.evaluate(
                    """el => {
                        const box = el.getBoundingClientRect();
                        const node = el.ownerDocument.elementFromPoint(
                            box.x + box.width / 2, box.y + box.height / 2
                        );
                        return !!node && (node === el || el.contains(node));
                    }"""
                )
                if not topmost:
                    return None
                modal = self.page.locator("#modalframe")
                if modal.count() != 1:
                    return None
                modal_topmost = modal.evaluate(
                    """(el, pt) => {
                        const node = document.elementFromPoint(pt[0], pt[1]);
                        return !!node && (node === el || el.contains(node));
                    }""",
                    [cx, cy],
                )
                if not modal_topmost:
                    return None
                return StructuralHandle(point=(cx, cy))
            except Exception:
                return None
        if not (selector.startswith("#form_") or selector == "#create"):
            return super().locate_structural(locator)
        try:
            frame = _form_frame(self.page, timeout_s=1.0)
            loc = frame.locator(selector)
            if loc.count() != 1:
                return None
            box = loc.bounding_box()
            if box is None or box["width"] <= 0 or box["height"] <= 0:
                return None
            cx = int(round(box["x"] + box["width"] / 2))
            cy = int(round(box["y"] + box["height"] / 2))
            vw, vh = self.viewport
            if not (0 <= cx < vw and 0 <= cy < vh):
                return None
            target = self._form_target(cx, cy)
            if target is None or target.get("selector") != selector:
                return None
            return StructuralHandle(point=(cx, cy))
        except Exception:
            return None

    def structured_text_at(self, x: int, y: int) -> str | None:
        target = self._confirm_target(x, y) or self._form_target(x, y)
        if target is None:
            return super().structured_text_at(x, y)
        return json.dumps(
            {
                "form_path": FORM_PATH,
                "target_kind": target["target_kind"],
                "target_id": target["target_id"],
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def _unique_visible_confirm_locator(self) -> Any:
        matches: list[Any] = []
        for frame in self.page.frames:
            locator = frame.locator("#confirmCreate")
            if locator.count() == 1 and locator.is_visible():
                matches.append(locator)
        if len(matches) != 1:
            raise FixtureError(
                "expected exactly one visible OpenEMR confirmation control"
            )
        return matches[0]


def _center(locator: Any) -> tuple[int, int]:
    locator.wait_for(state="visible", timeout=30_000)
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError(f"no bounding box for {locator}")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _preauthenticate_browser(backend: Any, fixture: OpenEMRFixture) -> None:
    """Log in and open the form through OpenEMR's authentic tab shell."""
    page = backend.page
    values = fixture._runtime_values()
    page.goto(fixture.ui_base_url)
    page.locator("#authUser").fill(values["OPENEMR_ACTOR_USER"])
    page.locator("#clearPass").fill(values["OPENEMR_ACTOR_PASSWORD"])
    page.locator("#login-button").click()
    page.wait_for_url("**/interface/main/tabs/main.php**", timeout=60_000)
    page.wait_for_function(
        "() => typeof window.restoreSession === 'function' "
        "&& typeof window.navigateTab === 'function' "
        "&& typeof window.activateTabByName === 'function'",
        timeout=60_000,
    )
    # Loading the form as a top-level document breaks the real save contract:
    # new_comprehensive.php calls top.restoreSession().  The pinned application
    # creates new tabs hidden, so activation must be explicit after navigation.
    page.evaluate(
        "({path, name}) => { "
        "window.navigateTab(path, name); "
        "window.activateTabByName(name, true); "
        "}",
        {"path": FORM_PATH, "name": FORM_FRAME_NAME},
    )
    page.locator(f'iframe[name="{FORM_FRAME_NAME}"]').wait_for(
        state="visible", timeout=60_000
    )
    _form_frame(page).locator("#form_fname").wait_for(
        state="visible", timeout=60_000
    )
    # The pinned image displays an installation-registration dialog in the top
    # shell for every fresh browser profile. It intercepts all child-frame
    # pointer input. Dismiss it in unmeasured setup through the explicit
    # no-registration path. The image preselects anonymous telemetry, so turn
    # it off explicitly and verify the state before dismissing the dialog.
    registration = page.locator(".product-registration-modal")
    if registration.count() and registration.is_visible():
        telemetry = registration.locator("#allowTelemetry")
        if telemetry.count() and telemetry.is_checked():
            telemetry.uncheck()
        if telemetry.count() and telemetry.is_checked():
            raise FixtureError("OpenEMR anonymous telemetry could not be disabled")
        registration.get_by_role(
            "button", name="Ask again later", exact=True
        ).click()
        registration.wait_for(state="hidden", timeout=30_000)


def _form_frame(page: Any, *, timeout_s: float = 60.0) -> Any:
    """Return the authenticated patient form frame or fail closed."""
    deadline = time.monotonic() + timeout_s
    observed = None
    while time.monotonic() < deadline:
        frame = page.frame(name=FORM_FRAME_NAME)
        if frame is None:
            # Some OpenEMR/Chromium combinations do not expose a Knockout-
            # created iframe through Page.frame(name=...) even though the DOM
            # name attribute is present. Resolve the attached browsing context
            # from the exact iframe element as a deterministic fallback.
            try:
                handle = page.locator(
                    f'iframe[name="{FORM_FRAME_NAME}"]'
                ).element_handle()
                frame = None if handle is None else handle.content_frame()
            except Exception:  # noqa: BLE001 - attachment may still be racing
                frame = None
        observed = None if frame is None else frame.url
        if frame is not None and FORM_PATH in frame.url:
            if not frame.evaluate("() => typeof top.restoreSession === 'function'"):
                raise FixtureError(
                    "OpenEMR patient form is detached from its session shell"
                )
            return frame
        page.wait_for_timeout(100)
    raise FixtureError(
        "authenticated OpenEMR patient-form frame is unavailable; "
        f"observed_url={observed!r}"
    )


def _apply_condition(backend: Any, condition: str) -> None:
    if condition == "ui_cosmetic_v1":
        _form_frame(backend.page).add_style_tag(content=DRIFT_CSS)


def _confirm_create_locator(page: Any, *, timeout_s: float = 30.0) -> Any:
    """Find OpenEMR's duplicate-check confirmation inside its dialog iframe."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in page.frames:
            locator = frame.locator("#confirmCreate")
            try:
                if locator.is_visible():
                    return locator
            except Exception:  # noqa: BLE001 - iframe may detach while polling
                continue
        page.wait_for_timeout(200)
    # Preserve only bounded validation metadata, never entered values. This
    # turns a missing popup into a useful fail-closed form-contract error.
    try:
        invalid_ids = page.locator(
            "input:invalid, select:invalid, textarea:invalid"
        ).evaluate_all(
            "elements => elements.filter(e => e.offsetParent !== null)"
            ".slice(0, 20).map(e => e.id || e.name || e.tagName)"
        )
        errors = [
            text.strip()[:200]
            for text in page.locator(
                ".error:visible, .invalid-feedback:visible, .text-danger:visible"
            ).all_inner_texts()[:20]
            if text.strip()
        ]
    except Exception:  # noqa: BLE001 - diagnostics cannot weaken refusal
        invalid_ids = []
        errors = []
    raise FixtureError(
        "OpenEMR duplicate-check confirmation did not appear; "
        f"invalid_controls={invalid_ids!r}; validation_errors={errors!r}"
    )


def _wait_for_patient_write(
    fixture: OpenEMRFixture, *, timeout_s: float = 30.0
) -> None:
    """Wait for the durable SQL write after the duplicate-check confirmation."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if fixture.db_records():
            return
        time.sleep(0.2)
    raise FixtureError("confirmed patient save produced no durable SQL row")


def _stable_pre_trial_counts(fixture: OpenEMRFixture) -> dict[str, int]:
    """Wait for unmeasured setup/audit subscribers to reach quiescence.

    Login, form setup, and OAuth issuance are outside the measured task but the
    pinned application can commit their audit rows asynchronously. Starting a
    delta boundary while those rows are still arriving makes a correct write
    look like nondeterministic collateral. Require three identical complete
    table inventories before the actuation clock starts; never subtract a
    guessed amount after the fact.
    """
    deadline = time.monotonic() + PRE_TRIAL_STABLE_TIMEOUT_S
    previous: dict[str, int] | None = None
    stable = 0
    while time.monotonic() < deadline:
        current = fixture.table_counts()
        if current == previous:
            stable += 1
        else:
            previous = current
            stable = 1
        if stable >= PRE_TRIAL_STABLE_SAMPLES:
            return current
        time.sleep(PRE_TRIAL_STABLE_INTERVAL_S)
    raise FixtureError("OpenEMR pre-trial table inventory did not become stable")


def _reset_recording_state(fixture: OpenEMRFixture) -> OpenEMRPatientOracle:
    """Restore the bound baseline before any recording browser is launched."""
    fixture.reset()
    reader = fixture.token_session("oracle")
    return OpenEMRPatientOracle(fixture.api_base_url, reader)


def record(fixture: OpenEMRFixture, out_dir: Path, *, headed: bool) -> Path:
    """Record the patient-registration UI task after unmeasured login/setup."""
    from openadapt_flow.recorder import Recorder

    if out_dir.exists():
        raise FixtureError(f"recording path exists; refusing overwrite: {out_dir}")
    oracle = _reset_recording_state(fixture)
    backend, close = OpenEMRPlaywrightBackend.launch(
        fixture.ui_base_url, headless=not headed
    )
    try:
        _preauthenticate_browser(backend, fixture)
        page = backend.page
        form = _form_frame(page)
        before_counts = _stable_pre_trial_counts(fixture)
        before_patient_digest = fixture.non_target_patient_data_sha256()
        before_history_digest = fixture.history_data_sha256()
        recorder = Recorder(
            backend,
            out_dir,
            app_url=page.url,
            **SETTLE,
        )
        spec = SyntheticPatientSpec()

        def text_field(name: str, value: str, *, param: str) -> None:
            locator = form.locator(f"#form_{name}")
            recorder.click(*_center(locator))
            recorder.press("Control+a")
            recorder.type_text(value, param=param)

        def select_by_label(name: str, label: str, *, param: str | None = None) -> None:
            locator = form.locator(f"#form_{name}")
            recorder.click(*_center(locator))
            recorder.type_text(label, param=param)
            recorder.press("Enter")

        select_by_label("title", "Ms.", param="title")
        text_field("fname", spec.fname, param="fname")
        text_field("lname", spec.lname, param="lname")
        text_field("DOB", spec.dob, param="DOB")
        select_by_label("sex", "Female", param="sex")
        # The pinned comprehensive form keeps every contact control inside a
        # collapsed, real UI accordion. Record opening it; never type into the
        # duplicate hidden inputs that exist while the section is collapsed.
        recorder.click(*_center(form.get_by_text("Contact", exact=True)))
        # Scrolling is part of the demonstrated program. A hidden helper scroll
        # would leave replay at the top of the page with off-viewport click
        # coordinates and could silently type into the previously focused box.
        recorder.scroll(0, 600)
        text_field("street", spec.street, param="street")
        text_field("city", spec.city, param="city")
        # State and country are select or text controls depending on OpenEMR
        # globals. The pinned default fixture uses select controls.
        select_by_label("state", spec.state_label, param="state_label")
        text_field("postal_code", spec.postal_code, param="postal_code")
        text_field("phone_home", spec.phone_home, param="phone_home")
        text_field("email", spec.email, param="email")
        select_by_label("country_code", "USA", param="country_code")
        # Expanding Contact increases the document height after the first
        # recorded scroll. Move the governed save control into the middle of
        # the clipped application iframe before capturing its physical click.
        recorder.scroll(0, 600)
        recorder.click(*_center(form.locator("#create")))
        # The first Save click only opens the pinned duplicate-check iframe.
        # #confirmCreate invokes srcConfirmSave() in the parent and performs
        # the real POST.  This second click is the governed write step.
        recorder.click(*_center(_confirm_create_locator(page)))
        recording = recorder.finish()
        events = [
            json.loads(line)
            for line in (recording / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
        if not events or events[-1].get("kind") != "click":
            raise FixtureError("confirmation click was not retained in recording")
        _wait_for_patient_write(fixture)
        # OpenEMR commits the patient row before its REST representation has
        # consistently exposed the synchronized address fields. Use one fixed,
        # non-polling settle interval so the read-only REST call count and exact
        # api_log delta remain deterministic.
        time.sleep(ORACLE_SETTLE_S)
        evidence = _capture_post_evidence(
            oracle,
            fixture,
            before_counts=before_counts,
            before_non_target_patient_sha256=before_patient_digest,
            before_history_data_sha256=before_history_digest,
            arm="compiled",
        )
        verdict = classify_patient_trial(
            actor_reported_success=True,
            halted=False,
            rest_records=evidence.rest_records,
            db_records=evidence.db_records,
            unexpected_db_deltas=evidence.delta_violations,
            environment_healthy=evidence.readable,
        )
        if not verdict.success or not evidence.readable:
            raise FixtureError(
                "recorded UI save failed its independent oracle: " + verdict.detail
            )
        # A click inside an iframe is observed by the top-level backend as the
        # iframe element, not as #confirmCreate. Only after the durable and
        # independent oracle gate succeeds do we make the recording compile-
        # eligible by binding that exact event in an explicit sidecar.
        _write_private_exclusive(
            recording / "openemr-save-event.json",
            (
                json.dumps(
                    {
                        "schema_version": 1,
                        "event_index": events[-1]["i"],
                        "dom_target": "#confirmCreate inside duplicate-check iframe",
                        "effect_contract": "create exactly one synthetic patient",
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n"
            ).encode(),
        )
        return recording
    finally:
        close()


def _marked_save_step_id(recording_dir: Path) -> str:
    """Resolve the explicitly marked iframe confirmation event to a step ID."""
    marker_path = recording_dir / "openemr-save-event.json"
    if not marker_path.is_file():
        raise FixtureError("recording lacks the explicit OpenEMR save-event marker")
    try:
        marker = json.loads(marker_path.read_text())
        event_index = int(marker["event_index"])
        events = [
            json.loads(line)
            for line in (recording_dir / "events.jsonl").read_text().splitlines()
            if line.strip()
        ]
    except (
        KeyError,
        OSError,
        TypeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        raise FixtureError("OpenEMR save-event marker is malformed") from exc
    save_events = [event for event in events if event.get("i") == event_index]
    if (
        marker.get("schema_version") != 1
        or marker.get("dom_target") != "#confirmCreate inside duplicate-check iframe"
        or len(save_events) != 1
        or save_events[0].get("kind") != "click"
    ):
        raise FixtureError("save-event marker does not identify one confirmation click")
    return f"step_{event_index:03d}"


def compile_recording(recording_dir: Path, bundle_dir: Path) -> None:
    """Compile once, then bind the Save step's independent effect contract."""
    from openadapt_flow.compiler import compile_recording as compile_flow

    if bundle_dir.exists():
        raise FixtureError(f"bundle path exists; refusing overwrite: {bundle_dir}")
    save_step_id = _marked_save_step_id(recording_dir)
    workflow = compile_flow(
        recording_dir, bundle_dir, name="openemr-create-synthetic-patient"
    )
    if not workflow.steps:
        raise FixtureError("compiler produced no steps")
    save_steps = [step for step in workflow.steps if step.id == save_step_id]
    if len(save_steps) != 1 or save_steps[0].action.value != "click":
        raise FixtureError("compiler omitted the marked confirmation click")
    save_index = workflow.steps.index(save_steps[0])
    if save_index < 1:
        raise FixtureError("confirmation click has no preceding dialog-open step")
    open_step = workflow.steps[save_index - 1]
    if (
        open_step.action.value != "click"
        or open_step.anchor is None
        or open_step.anchor.structural is None
        or open_step.anchor.structural.selector != "#create"
    ):
        raise FixtureError("confirmation click is not preceded by exact #create")
    # Opening the duplicate-check dialog intentionally changes the full frame.
    # Generic recorder mining can emit a flaky region-stable expectation for
    # this transition. Bind the semantic state we actually require instead.
    open_step.expect = [_duplicate_dialog_postcondition()]
    save_steps[0].effects = patient_effects()
    save_steps[0].risk = "reversible"
    expected_params = set(SyntheticPatientSpec().params())
    if (
        set(workflow.params) != expected_params
        or set(workflow.param_specs) != expected_params
    ):
        raise FixtureError("recording did not parameterize every synthetic field")
    if any(not item.required for item in workflow.param_specs.values()):
        raise FixtureError("all synthetic benchmark parameters must be required")
    effects_payload = [effect.model_dump(mode="json") for effect in patient_effects()]
    marker_path = recording_dir / "openemr-save-event.json"
    source_recording_sha256 = _tree_manifest_sha256(recording_dir)
    contract = {
        "schema_version": 1,
        "workflow_name": "openemr-create-synthetic-patient",
        "save_step_id": save_step_id,
        "marker_sha256": _artifact_sha256(marker_path),
        "source_recording_sha256": source_recording_sha256,
        "required_params": sorted(expected_params),
        "effects_sha256": hashlib.sha256(
            json.dumps(effects_payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    contract_path = bundle_dir / "benchmark-contract.json"
    _write_private_exclusive(
        contract_path,
        (json.dumps(contract, indent=2, sort_keys=True) + "\n").encode(),
    )
    if workflow.manifest is None:
        raise FixtureError("compiler omitted bundle provenance manifest")
    workflow.recording_id = source_recording_sha256
    workflow.manifest.provenance.source_recording_sha256 = source_recording_sha256
    workflow.manifest.provenance.compiler_config_sha256 = _artifact_sha256(
        contract_path
    )
    workflow.save(bundle_dir)


def _is_sha256(value: Any) -> bool:
    """Return whether *value* is a canonical lowercase SHA-256 digest."""
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _duplicate_dialog_postcondition() -> Postcondition:
    return Postcondition(
        kind=PostconditionKind.TEXT_PRESENT,
        text="Confirm Create New Patient",
        timeout_s=10.0,
    )


def _validate_benchmark_bundle(bundle_dir: Path) -> Any:
    """Load and verify the exact OpenEMR benchmark contract before paid work."""
    from openadapt_flow.ir import Workflow

    workflow = Workflow.load(bundle_dir)
    contract_path = bundle_dir / "benchmark-contract.json"
    try:
        contract = json.loads(contract_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError("bundle benchmark contract is absent or malformed") from exc
    expected_params = set(SyntheticPatientSpec().params())
    effects_payload = [effect.model_dump(mode="json") for effect in patient_effects()]
    expected_effects_sha256 = hashlib.sha256(
        json.dumps(effects_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    effect_steps = [step for step in workflow.steps if step.effects]
    dialog_steps = [
        step
        for step in workflow.steps
        if step.anchor is not None
        and step.anchor.structural is not None
        and step.anchor.structural.selector == "#create"
    ]
    if (
        workflow.name != "openemr-create-synthetic-patient"
        or set(workflow.params) != expected_params
        or set(workflow.param_specs) != expected_params
        or any(not item.required for item in workflow.param_specs.values())
        or len(effect_steps) != 1
        or effect_steps[0].action.value != "click"
        or effect_steps[0].risk != "reversible"
        or [effect.model_dump(mode="json") for effect in effect_steps[0].effects]
        != effects_payload
        or len(dialog_steps) != 1
        or dialog_steps[0].expect != [_duplicate_dialog_postcondition()]
        or workflow.steps.index(dialog_steps[0]) + 1
        != workflow.steps.index(effect_steps[0])
    ):
        raise FixtureError("bundle differs from the exact OpenEMR task contract")
    provenance = workflow.manifest.provenance if workflow.manifest else None
    if (
        contract.get("schema_version") != 1
        or contract.get("workflow_name") != workflow.name
        or contract.get("save_step_id") != effect_steps[0].id
        or contract.get("required_params") != sorted(expected_params)
        or contract.get("effects_sha256") != expected_effects_sha256
        or not _is_sha256(contract.get("marker_sha256"))
        or not _is_sha256(contract.get("source_recording_sha256"))
        or provenance is None
        or workflow.recording_id != contract.get("source_recording_sha256")
        or provenance.compiler_config_sha256 != _artifact_sha256(contract_path)
        or provenance.source_recording_sha256 != contract.get("source_recording_sha256")
    ):
        raise FixtureError("bundle provenance is not bound to the save marker")
    return workflow


def _agent_prompt(spec: SyntheticPatientSpec) -> str:
    return (
        "You are already signed in to a local, synthetic-only OpenEMR test "
        "site and are looking at a blank patient-registration form. Create "
        "and save exactly one patient with these values:\n"
        f"- Title: {spec.title}\n"
        f"- First name: {spec.fname}\n"
        f"- Last name: {spec.lname}\n"
        f"- Date of birth: {spec.dob}\n"
        f"- Sex: {spec.sex}\n"
        f"- Street: {spec.street}\n"
        f"- City: {spec.city}\n"
        f"- State: {spec.state_label} ({spec.state})\n"
        f"- Postal code: {spec.postal_code}\n"
        f"- Home phone: {spec.phone_home}\n"
        f"- Email: {spec.email}\n"
        f"- Country: {spec.country_code}\n"
        "Use only the visible interface. Stop after the patient is saved. "
        "All values are fictional and reserved for testing. Start by taking "
        "a screenshot."
    )


@dataclass
class CapturedEvidence:
    """Complete independently observed state retained for one trial."""

    rest_records: list[dict[str, Any]] | None
    db_records: list[dict[str, Any]] | None
    before_table_counts: dict[str, int] | None
    after_table_counts: dict[str, int] | None
    all_table_deltas: dict[str, int] | None
    before_non_target_patient_sha256: str | None
    after_non_target_patient_sha256: str | None
    before_history_data_sha256: str | None
    after_history_data_sha256: str | None
    after_non_target_history_sha256: str | None
    target_history_count: int | None
    history_binding_readable: bool
    delta_violations: list[str]
    errors: list[str]

    @property
    def readable(self) -> bool:
        return (
            self.rest_records is not None
            and self.db_records is not None
            and self.before_table_counts is not None
            and self.after_table_counts is not None
            and self.all_table_deltas is not None
            and self.before_non_target_patient_sha256 is not None
            and self.after_non_target_patient_sha256 is not None
            and self.before_history_data_sha256 is not None
            and self.after_history_data_sha256 is not None
            and self.history_binding_readable
        )


def _capture_post_evidence(
    oracle: OpenEMRPatientOracle,
    fixture: OpenEMRFixture,
    *,
    before_counts: dict[str, int] | None,
    before_non_target_patient_sha256: str | None,
    before_history_data_sha256: str | None,
    arm: str,
) -> CapturedEvidence:
    """Capture each oracle independently; malformed/unreadable data is a row."""
    errors: list[str] = []
    rest: list[dict[str, Any]] | None = None
    db: list[dict[str, Any]] | None = None
    after: dict[str, int] | None = None
    deltas: dict[str, int] | None = None
    after_patient_digest: str | None = None
    after_history_digest: str | None = None
    after_non_target_history_digest: str | None = None
    target_history_count: int | None = None
    history_binding_readable = False
    violations: list[str] = []
    try:
        state = oracle.capture()
        if state.reachable:
            rest = state.records
        else:
            errors.append("REST oracle unreachable")
    except Exception as exc:  # noqa: BLE001 - preserve trial, fail indeterminate
        errors.append(f"REST oracle: {type(exc).__name__}")
    try:
        db = fixture.db_records()
    except Exception as exc:  # noqa: BLE001 - preserve trial, fail indeterminate
        errors.append(f"SQL oracle: {type(exc).__name__}")
    if before_counts is None:
        errors.append("pre-write table counts unavailable")
    else:
        try:
            after = fixture.table_counts()
            violations, deltas = audit_table_deltas(before_counts, after, arm=arm)
        except Exception as exc:  # noqa: BLE001 - preserve trial, fail indeterminate
            errors.append(f"table delta oracle: {type(exc).__name__}")
    if before_non_target_patient_sha256 is None:
        errors.append("pre-write non-target patient digest unavailable")
    else:
        try:
            after_patient_digest = fixture.non_target_patient_data_sha256()
            if after_patient_digest != before_non_target_patient_sha256:
                violations.append("patient_data:non-target-content-changed")
        except Exception as exc:  # noqa: BLE001 - preserve trial, fail indeterminate
            errors.append(f"non-target patient audit: {type(exc).__name__}")
    if before_history_data_sha256 is None:
        errors.append("pre-write history_data digest unavailable")
    else:
        try:
            after_history_digest = fixture.history_data_sha256()
            if db is not None and len(db) == 0:
                # A halted/no-write run has no target PID by definition. The
                # complete post-run history table is therefore non-target
                # history and remains independently comparable to the baseline.
                target_history_count = 0
                after_non_target_history_digest = after_history_digest
                if after_non_target_history_digest != before_history_data_sha256:
                    violations.append("history_data:non-target-content-changed")
                history_binding_readable = True
            elif db is None or len(db) != 1:
                raise ValueError("target PID unavailable")
            else:
                target_pid = int(db[0]["pid"])
                target_history_count = fixture.history_count_for_pid(target_pid)
                after_non_target_history_digest = fixture.history_data_sha256(
                    exclude_pid=target_pid
                )
                expected_target_history = 0 if arm == "api" else 1
                if target_history_count != expected_target_history:
                    violations.append(
                        "history_data:target-pid-count="
                        f"{target_history_count} (expected {expected_target_history})"
                    )
                if after_non_target_history_digest != before_history_data_sha256:
                    violations.append("history_data:non-target-content-changed")
                history_binding_readable = True
        except Exception as exc:  # noqa: BLE001 - preserve trial, fail indeterminate
            errors.append(f"target history_data audit: {type(exc).__name__}")
    return CapturedEvidence(
        rest_records=rest,
        db_records=db,
        before_table_counts=before_counts,
        after_table_counts=after,
        all_table_deltas=deltas,
        before_non_target_patient_sha256=before_non_target_patient_sha256,
        after_non_target_patient_sha256=after_patient_digest,
        before_history_data_sha256=before_history_data_sha256,
        after_history_data_sha256=after_history_digest,
        after_non_target_history_sha256=after_non_target_history_digest,
        target_history_count=target_history_count,
        history_binding_readable=history_binding_readable,
        delta_violations=violations,
        errors=errors,
    )


def _write_private_exclusive(path: Path, payload: bytes) -> None:
    """Create a 0600 evidence artifact without an exposure window."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short evidence write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _artifact_sha256(path: Path) -> str:
    mode = path.lstat().st_mode
    if path.is_symlink() or not stat.S_ISREG(mode):
        raise FixtureError(f"artifact must be a regular non-symlink file: {path}")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_manifest_sha256(path: Path) -> str:
    """Bind every regular artifact in a run directory by path and digest."""
    try:
        root_mode = path.lstat().st_mode
    except FileNotFoundError:
        return ""
    if stat.S_ISLNK(root_mode) or not stat.S_ISDIR(root_mode):
        raise FixtureError(f"artifact tree must be a real directory: {path}")
    rows = []
    for item in sorted(path.rglob("*")):
        mode = item.lstat().st_mode
        if item.is_symlink():
            raise FixtureError(f"artifact tree contains a symlink: {item}")
        if stat.S_ISDIR(mode):
            continue
        if not stat.S_ISREG(mode):
            raise FixtureError(f"artifact tree contains a non-regular file: {item}")
        rows.append(
            {
                "path": item.relative_to(path).as_posix(),
                "sha256": _artifact_sha256(item),
                "bytes": item.stat().st_size,
            }
        )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _json_safe(value: Any) -> Any:
    """Make malformed oracle evidence persistable without hiding its shape."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, bytes):
        return {
            "non_json_type": "bytes",
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return {"non_json_type": type(value).__name__, "repr": repr(value)[:500]}


def _persist_evidence(
    evidence_dir: Path,
    *,
    evidence: CapturedEvidence,
    environment_identity: dict[str, Any],
    relative_root: Path,
    final_screenshot: bytes | None = None,
    action_log: list[str] | None = None,
    run_artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist protected full evidence and return row-bound artifact hashes."""
    if evidence_dir.exists():
        raise FixtureError(f"evidence path exists; refusing overwrite: {evidence_dir}")
    evidence_dir.mkdir(parents=True, mode=0o700)
    payload = _json_safe(
        {
            "rest_records": evidence.rest_records,
            "db_records": evidence.db_records,
            "before_table_counts": evidence.before_table_counts,
            "after_table_counts": evidence.after_table_counts,
            "all_table_deltas": evidence.all_table_deltas,
            "before_non_target_patient_sha256": (
                evidence.before_non_target_patient_sha256
            ),
            "after_non_target_patient_sha256": evidence.after_non_target_patient_sha256,
            "before_history_data_sha256": evidence.before_history_data_sha256,
            "after_history_data_sha256": evidence.after_history_data_sha256,
            "after_non_target_history_sha256": (
                evidence.after_non_target_history_sha256
            ),
            "target_history_count": evidence.target_history_count,
            "history_binding_readable": evidence.history_binding_readable,
            "delta_violations": evidence.delta_violations,
            "oracle_errors": evidence.errors,
            "environment_identity": environment_identity,
        }
    )
    evidence_path = evidence_dir / "oracle-evidence.json"
    _write_private_exclusive(
        evidence_path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    )
    try:
        relative_path = evidence_path.relative_to(relative_root).as_posix()
    except ValueError as exc:
        raise FixtureError("evidence path escapes the declared output root") from exc
    metadata: dict[str, Any] = {
        "evidence_sha256": _artifact_sha256(evidence_path),
        "evidence_relative_path": relative_path,
        "oracle_errors": evidence.errors,
        "nonzero_table_deltas": {
            table: delta
            for table, delta in (evidence.all_table_deltas or {}).items()
            if delta
        },
    }
    if final_screenshot is not None:
        screenshot_path = evidence_dir / "final.png"
        _write_private_exclusive(screenshot_path, final_screenshot)
        metadata["final_screenshot_sha256"] = _artifact_sha256(screenshot_path)
    if action_log is not None:
        action_path = evidence_dir / "actions.json"
        _write_private_exclusive(
            action_path,
            (json.dumps(action_log, indent=2, sort_keys=True) + "\n").encode(),
        )
        metadata["action_log_sha256"] = _artifact_sha256(action_path)
    if run_artifacts_dir is not None:
        metadata["run_artifacts_manifest_sha256"] = _tree_manifest_sha256(
            run_artifacts_dir
        )
    return metadata


def _safe_records_sha256(records: list[dict[str, Any]] | None) -> str:
    if records is None:
        return ""
    try:
        return patient_records_sha256(records)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return ""


def _row(
    *,
    arm: str,
    condition: str,
    trial: int,
    baseline_hash: str,
    actor_reported_success: bool,
    halted: bool,
    wall_s: float,
    rest_records: list[dict[str, Any]] | None,
    db_records: list[dict[str, Any]] | None,
    unexpected_deltas: list[str],
    delta_audit_readable: bool = True,
    actions: int = 0,
    model_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_creation_input_tokens: int = 0,
    cache_read_input_tokens: int = 0,
    cost_usd: float = 0.0,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TrialRow:
    if not delta_audit_readable:
        db_records = None
    verdict = classify_patient_trial(
        actor_reported_success=actor_reported_success,
        halted=halted,
        rest_records=rest_records,
        db_records=db_records,
        unexpected_db_deltas=unexpected_deltas,
        task_feasible=True,
        execution_error=error,
    )
    return TrialRow(
        arm=arm,
        condition=condition,
        trial=trial,
        primary_outcome=verdict.primary_outcome.value,
        success=verdict.success,
        silent_incorrect_success=verdict.silent_incorrect_success,
        over_halt=verdict.over_halt,
        wall_s=wall_s,
        actions=actions,
        model_calls=model_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=cache_creation_input_tokens,
        cache_read_input_tokens=cache_read_input_tokens,
        cost_usd=cost_usd,
        baseline_snapshot_sha256=baseline_hash,
        rest_records_sha256=_safe_records_sha256(rest_records),
        db_records_sha256=_safe_records_sha256(db_records),
        error=error,
        detail=verdict.detail,
        metadata=metadata or {},
    )


def run_api_trial(
    fixture: OpenEMRFixture,
    *,
    condition: str,
    trial: int,
    evidence_dir: Path,
    environment_identity: dict[str, Any],
) -> TrialRow:
    from openadapt_flow.runtime.actuators import ApiActuator

    baseline_hash = fixture.reset()
    actor = fixture.token_session("actor")
    reader = fixture.token_session("oracle")
    oracle = OpenEMRPatientOracle(fixture.api_base_url, reader)
    before_counts = _stable_pre_trial_counts(fixture)
    before_patient_digest = fixture.non_target_patient_data_sha256()
    before_history_digest = fixture.history_data_sha256()
    result = None
    error = None
    start = time.monotonic()
    try:
        result = ApiActuator(fixture.api_base_url, session=actor, timeout_s=30).actuate(
            patient_api_binding(), SyntheticPatientSpec().params()
        )
    except Exception as exc:  # noqa: BLE001 - preserve post-write evidence
        error = f"{type(exc).__name__}: {exc}"
    actuation_wall_s = time.monotonic() - start
    time.sleep(ORACLE_SETTLE_S)
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_patient_sha256=before_patient_digest,
        before_history_data_sha256=before_history_digest,
        arm="api",
    )
    wall_s = time.monotonic() - start
    artifact_metadata = _persist_evidence(
        evidence_dir,
        evidence=evidence,
        environment_identity=environment_identity,
        relative_root=evidence_dir.parents[1],
    )
    actor_success = bool(result and result.actuated)
    return _row(
        arm="api",
        condition=condition,
        trial=trial,
        baseline_hash=baseline_hash,
        actor_reported_success=actor_success,
        halted=not actor_success,
        wall_s=wall_s,
        rest_records=evidence.rest_records,
        db_records=evidence.db_records,
        unexpected_deltas=evidence.delta_violations,
        delta_audit_readable=evidence.readable,
        actions=1,
        error=error,
        metadata={
            **artifact_metadata,
            "actuation_wall_s": actuation_wall_s,
            "timing_boundary": "actuation start through REST/SQL/delta verification",
            "actuation_status": result.status.value if result else "exception",
            "http_status": result.http_status if result else None,
            "actuation_reason": result.reason if result else error,
        },
    )


def run_compiled_trial(
    fixture: OpenEMRFixture,
    bundle_dir: Path,
    run_dir: Path,
    *,
    condition: str,
    trial: int,
    headed: bool,
    evidence_dir: Path,
    environment_identity: dict[str, Any],
) -> TrialRow:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    if run_dir.exists():
        raise FixtureError(f"run artifact path exists: {run_dir}")
    run_dir.mkdir(parents=True, mode=0o700)
    run_dir.chmod(0o700)
    baseline_hash = fixture.reset()
    reader = fixture.token_session("oracle")
    oracle = OpenEMRPatientOracle(fixture.api_base_url, reader)
    backend, close = OpenEMRPlaywrightBackend.launch(
        fixture.ui_base_url, headless=not headed
    )
    report = None
    error = None
    actuation_wall_s = 0.0
    start = 0.0
    before_counts = None
    before_patient_digest = None
    before_history_digest = None
    try:
        _preauthenticate_browser(backend, fixture)
        _apply_condition(backend, condition)
        before_counts = _stable_pre_trial_counts(fixture)
        before_patient_digest = fixture.non_target_patient_data_sha256()
        before_history_digest = fixture.history_data_sha256()
        start = time.monotonic()
        report = Replayer(backend, effect_verifier=oracle.verifier).run(
            Workflow.load(bundle_dir),
            params=SyntheticPatientSpec().params(),
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        actuation_wall_s = time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 - failed trial is preserved
        if start:
            actuation_wall_s = time.monotonic() - start
        error = f"{type(exc).__name__}: {exc}"
    time.sleep(ORACLE_SETTLE_S)
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_patient_sha256=before_patient_digest,
        before_history_data_sha256=before_history_digest,
        arm="compiled",
    )
    wall_s = time.monotonic() - start if start else 0.0
    try:
        close()
    except Exception as exc:  # noqa: BLE001
        if error is None:
            error = f"browser teardown: {type(exc).__name__}: {exc}"
    artifact_metadata = _persist_evidence(
        evidence_dir,
        evidence=evidence,
        environment_identity=environment_identity,
        relative_root=evidence_dir.parents[1],
        run_artifacts_dir=run_dir,
    )
    actor_success = bool(report and report.success)
    return _row(
        arm="compiled",
        condition=condition,
        trial=trial,
        baseline_hash=baseline_hash,
        actor_reported_success=actor_success,
        halted=not actor_success,
        wall_s=wall_s,
        rest_records=evidence.rest_records,
        db_records=evidence.db_records,
        unexpected_deltas=evidence.delta_violations,
        delta_audit_readable=evidence.readable,
        actions=len(report.results) if report else 0,
        error=error,
        metadata={
            **artifact_metadata,
            "actuation_wall_s": actuation_wall_s,
            "timing_boundary": "actuation start through REST/SQL/delta verification",
            "replayer_success": actor_success,
            "heal_count": report.heal_count if report else 0,
        },
    )


def run_agent_trial(
    fixture: OpenEMRFixture,
    *,
    condition: str,
    trial: int,
    headed: bool,
    max_cost_usd: float,
    evidence_dir: Path,
    environment_identity: dict[str, Any],
    client: Any = None,
) -> TrialRow:

    baseline_hash = fixture.reset()
    reader = fixture.token_session("oracle")
    oracle = OpenEMRPatientOracle(fixture.api_base_url, reader)
    backend, close = OpenEMRPlaywrightBackend.launch(
        fixture.ui_base_url, headless=not headed
    )
    result = None
    fallback_screenshot: bytes | None = None
    ledger = _AtomicUsageLedger()
    budgeted_client: _BudgetedAgentClient | None = None
    error = None
    actuation_wall_s = 0.0
    start = 0.0
    before_counts = None
    before_patient_digest = None
    before_history_digest = None
    try:
        _preauthenticate_browser(backend, fixture)
        _apply_condition(backend, condition)
        before_counts = _stable_pre_trial_counts(fixture)
        before_patient_digest = fixture.non_target_patient_data_sha256()
        before_history_digest = fixture.history_data_sha256()
        start = time.monotonic()
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=agent_baseline.load_api_key())
        budgeted_client = _BudgetedAgentClient(client, ledger, max_cost_usd)
        result = agent_baseline.run_agent(
            backend,
            _agent_prompt(SyntheticPatientSpec()),
            client=budgeted_client,
            max_actions=AGENT_MAX_ACTIONS,
            max_cost_usd=max_cost_usd,
            ledger=ledger,
        )
        actuation_wall_s = time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 - preserve incurred usage
        if start:
            actuation_wall_s = time.monotonic() - start
        error = f"{type(exc).__name__}: {exc}"
    time.sleep(ORACLE_SETTLE_S)
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_patient_sha256=before_patient_digest,
        before_history_data_sha256=before_history_digest,
        arm="agent",
    )
    wall_s = time.monotonic() - start if start else 0.0
    if result is None:
        try:
            fallback_screenshot = backend.screenshot()
        except Exception:  # noqa: BLE001 - oracle row still survives
            fallback_screenshot = None
    try:
        close()
    except Exception as exc:  # noqa: BLE001
        if error is None:
            error = f"browser teardown: {type(exc).__name__}: {exc}"
    artifact_metadata = _persist_evidence(
        evidence_dir,
        evidence=evidence,
        environment_identity=environment_identity,
        relative_root=evidence_dir.parents[1],
        final_screenshot=(
            result.final_screenshot if result is not None else fallback_screenshot
        ),
        action_log=result.action_log if result else [],
    )
    model_done = bool(result and result.stopped == "model_done")
    return _row(
        arm="agent",
        condition=condition,
        trial=trial,
        baseline_hash=baseline_hash,
        actor_reported_success=model_done,
        halted=not model_done,
        wall_s=wall_s,
        rest_records=evidence.rest_records,
        db_records=evidence.db_records,
        unexpected_deltas=evidence.delta_violations,
        delta_audit_readable=evidence.readable,
        actions=result.actions if result else 0,
        model_calls=ledger.api_calls,
        input_tokens=ledger.input_tokens,
        output_tokens=ledger.output_tokens,
        cache_creation_input_tokens=ledger.cache_creation_input_tokens,
        cache_read_input_tokens=ledger.cache_read_input_tokens,
        cost_usd=ledger.cost_usd,
        error=error,
        metadata={
            **artifact_metadata,
            "actuation_wall_s": (
                result.wall_s if result is not None else actuation_wall_s
            ),
            "timing_boundary": "actuation start through REST/SQL/delta verification",
            "stopped": result.stopped if result else "exception",
            "free_token_count_calls": (
                budgeted_client.messages.count_calls if budgeted_client else 0
            ),
            "max_guarded_input_tokens": (
                budgeted_client.messages.max_guarded_input_tokens
                if budgeted_client
                else 0
            ),
            "paid_request_attempts": (
                budgeted_client.messages.paid_attempts if budgeted_client else 0
            ),
            "spend_indeterminate": bool(
                budgeted_client
                and budgeted_client.messages.spend_is_indeterminate(ledger.api_calls)
            ),
        },
    )


def run_matrix(
    fixture: OpenEMRFixture,
    bundle_dir: Path,
    out_dir: Path,
    *,
    n: int,
    allow_paid_agent: bool,
    max_cost_per_run_usd: float,
    max_total_agent_cost_usd: float | None,
    headed: bool,
    model_free: bool = False,
) -> list[TrialRow]:
    """Run a full matrix or explicit non-publication model-free subset."""
    selected_arms = MODEL_FREE_ARMS if model_free else FULL_ARMS
    if model_free and allow_paid_agent:
        raise FixtureError("--model-free cannot be combined with --allow-paid-agent")
    if model_free and max_total_agent_cost_usd is not None:
        raise FixtureError(
            "--model-free does not accept --max-total-agent-cost-usd because "
            "it makes no paid requests"
        )
    if not model_free and not allow_paid_agent:
        raise FixtureError(
            "equal-arm matrix includes a paid agent arm; pass --allow-paid-agent "
            "and both cost caps only after explicit spend approval"
        )
    if not model_free and (
        not math.isfinite(max_cost_per_run_usd) or max_cost_per_run_usd <= 0
    ):
        raise FixtureError("--max-cost-per-run-usd must be positive")
    if not model_free and (
        max_total_agent_cost_usd is None
        or not math.isfinite(max_total_agent_cost_usd)
        or max_total_agent_cost_usd <= 0
    ):
        raise FixtureError("--max-total-agent-cost-usd must be positive")
    agent_total_cap = 0.0
    if max_total_agent_cost_usd is not None:
        agent_total_cap = max_total_agent_cost_usd
    planned_agent_runs = 0 if model_free else len(CONDITIONS) * n
    authorized_ceiling = planned_agent_runs * max_cost_per_run_usd
    if not model_free and agent_total_cap < authorized_ceiling:
        raise FixtureError(
            "--max-total-agent-cost-usd is below the complete matrix's hard "
            f"ceiling (${authorized_ceiling:.2f})"
        )
    if out_dir.exists():
        raise FixtureError(f"output path exists; refusing mixed runs: {out_dir}")
    fixture.up()
    # All free preconditions are checked before any paid request. There is no
    # paid preflight probe. Each measured model request gets a free token-count
    # precheck and the response itself is accounted in that row's UsageLedger.
    baseline_hash = fixture.baseline_hash()
    _validate_benchmark_bundle(bundle_dir)
    bundle_manifest_sha256 = _tree_manifest_sha256(bundle_dir)
    environment_identity = {
        "lock_sha256": hashlib.sha256(
            (REPO / "benchmark/openemr_local/environment.lock.json").read_bytes()
        ).hexdigest(),
        "openemr_image": fixture.image_identity(),
        "source_proofs": fixture.source_identity(),
        "baseline_snapshot_sha256": baseline_hash,
        "bundle_manifest_sha256": bundle_manifest_sha256,
    }

    out_dir.mkdir(parents=True, mode=0o700)
    out_dir.chmod(0o700)
    rows_path = out_dir / "rows.jsonl"
    rows: list[TrialRow] = []

    def persist(row: TrialRow) -> None:
        rows.append(row)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(rows_path, flags, 0o600)
        try:
            payload = memoryview(
                (json.dumps(asdict(row), sort_keys=True) + "\n").encode()
            )
            while payload:
                written = os.write(fd, payload)
                if written <= 0:
                    raise OSError("short JSONL evidence write")
                payload = payload[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    for condition in CONDITIONS:
        for trial in range(1, n + 1):
            trial_label = f"{condition}-{trial}"
            persist(
                run_compiled_trial(
                    fixture,
                    bundle_dir,
                    out_dir / "runs" / f"compiled-{trial_label}",
                    condition=condition,
                    trial=trial,
                    headed=headed,
                    evidence_dir=out_dir / "evidence" / f"compiled-{trial_label}",
                    environment_identity=environment_identity,
                )
            )
            persist(
                run_api_trial(
                    fixture,
                    condition=condition,
                    trial=trial,
                    evidence_dir=out_dir / "evidence" / f"api-{trial_label}",
                    environment_identity=environment_identity,
                )
            )
            if not model_free:
                spent = sum(row.cost_usd for row in rows if row.arm == "agent")
                if spent + max_cost_per_run_usd > agent_total_cap:
                    raise FixtureError(
                        "total paid-agent cap would be exceeded before the equal "
                        f"matrix completed (${spent:.4f} spent, "
                        f"${agent_total_cap:.2f} cap)"
                    )
                agent_row = run_agent_trial(
                    fixture,
                    condition=condition,
                    trial=trial,
                    headed=headed,
                    max_cost_usd=max_cost_per_run_usd,
                    evidence_dir=out_dir / "evidence" / f"agent-{trial_label}",
                    environment_identity=environment_identity,
                )
                persist(agent_row)
                if agent_row.metadata.get("spend_indeterminate") is True:
                    raise FixtureError(
                        "provider failure left the last paid attempt's spend "
                        "indeterminate; row/evidence were preserved and all "
                        "further paid calls are refused"
                    )
                spent = sum(row.cost_usd for row in rows if row.arm == "agent")
                if agent_row.cost_usd > max_cost_per_run_usd or spent > agent_total_cap:
                    raise FixtureError(
                        "provider usage exceeded the fixed reserved cost envelope; "
                        "row and evidence were preserved and further calls are refused"
                    )

    full_complete, full_reasons = publication_gate(rows, required_per_cell=n)
    selected_complete = all(
        sum(row.arm == arm and row.condition == condition for row in rows) == n
        for arm in selected_arms
        for condition in CONDITIONS
    )
    snapshots = {row.baseline_snapshot_sha256 for row in rows}
    selected_complete = (
        selected_complete and len(snapshots) == 1 and "" not in snapshots
    )
    reasons = list(full_reasons)
    if model_free:
        reasons.insert(
            0,
            "agent arm intentionally omitted by --model-free; the required "
            "three-arm comparative matrix is incomplete",
        )
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": (
            "model_free_subset_complete"
            if model_free and selected_complete
            else "complete"
            if full_complete
            else "incomplete"
        ),
        "run_mode": "model_free" if model_free else "full",
        "required_per_cell": n,
        "selected_subset_complete": selected_complete,
        "full_matrix_complete": full_complete,
        "publication_ready": (
            not model_free and n >= PUBLICATION_TRIALS_PER_CELL and full_complete
        ),
        "incomplete_reasons": reasons,
        "arms": list(selected_arms),
        "required_comparative_arms": list(FULL_ARMS),
        "omitted_arms": [arm for arm in FULL_ARMS if arm not in selected_arms],
        "conditions": list(CONDITIONS),
        "environment": {
            "platform": platform.platform(),
            **environment_identity,
            "paid_agent_authorized": not model_free,
            "agent": (
                {
                    "model": agent_baseline.MODEL,
                    "computer_tool": agent_baseline.COMPUTER_TOOL_TYPE,
                    "computer_use_beta": agent_baseline.COMPUTER_USE_BETA,
                    "max_actions": AGENT_MAX_ACTIONS,
                    "hard_max_cost_per_run_usd": max_cost_per_run_usd,
                    "token_count_margin": AGENT_TOKEN_COUNT_MARGIN,
                    "standard_input_limit": AGENT_STANDARD_INPUT_LIMIT,
                    "pre_call_cost_guard": (
                        "free token count + worst cache-write/output"
                    ),
                    "approved_max_total_cost_usd": agent_total_cap,
                }
                if not model_free
                else {"included": False, "model_calls_permitted": False}
            ),
        },
        "aggregates": aggregate_rows(rows),
        "runs": [asdict(row) for row in rows],
    }
    _write_private_exclusive(
        out_dir / "results.json",
        (json.dumps(result, indent=2, sort_keys=True) + "\n").encode(),
    )
    return rows


def _plan(n: int, *, model_free: bool = False) -> dict[str, Any]:
    selected_arms = MODEL_FREE_ARMS if model_free else FULL_ARMS
    return {
        "task": "create one complete synthetic patient record",
        "run_mode": "model_free" if model_free else "full",
        "arms": list(selected_arms),
        "required_comparative_arms": list(FULL_ARMS),
        "omitted_arms": [arm for arm in FULL_ARMS if arm not in selected_arms],
        "conditions": list(CONDITIONS),
        "trials_per_cell": n,
        "total_trials": len(selected_arms) * len(CONDITIONS) * n,
        "reset": "restore and verify one hashed SQL baseline before every trial",
        "oracle": "distinct read-only OpenEMR REST client plus direct SQL delta audit",
        "paid_agent": (
            "not included; credentials, budget, and model calls are not permitted"
            if model_free
            else "disabled unless explicitly opted in with per-run and total caps"
        ),
        "publication_eligible": False
        if model_free
        else n >= PUBLICATION_TRIALS_PER_CELL,
        "scope": (
            "compiled-versus-API engineering evidence only; never a complete "
            "three-arm comparative result"
            if model_free
            else "complete three-arm comparative protocol"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state", type=Path, default=REPO / "benchmark/openemr_local/state"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("prepare")
    sub.add_parser("up")
    sub.add_parser("bootstrap")
    sub.add_parser("snapshot")
    sub.add_parser("reset")
    plan_parser = sub.add_parser("plan")
    plan_parser.add_argument(
        "--profile", choices=("initial", "publication"), default="initial"
    )
    plan_parser.add_argument("--model-free", action="store_true")
    record_parser = sub.add_parser("record")
    record_parser.add_argument("--out", type=Path, required=True)
    record_parser.add_argument("--headed", action="store_true")
    compile_parser = sub.add_parser("compile")
    compile_parser.add_argument("--recording", type=Path, required=True)
    compile_parser.add_argument("--bundle", type=Path, required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--bundle", type=Path, required=True)
    run_parser.add_argument("--out", type=Path, required=True)
    run_parser.add_argument(
        "--profile", choices=("initial", "publication"), default="initial"
    )
    run_parser.add_argument("--allow-paid-agent", action="store_true")
    run_parser.add_argument("--model-free", action="store_true")
    run_parser.add_argument("--max-cost-per-run-usd", type=float, default=1.50)
    run_parser.add_argument("--max-total-agent-cost-usd", type=float)
    run_parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    fixture = OpenEMRFixture(args.state)

    try:
        if args.command == "preflight":
            fixture.runtime_preflight()
        elif args.command == "prepare":
            fixture.prepare()
        elif args.command == "up":
            fixture.up()
        elif args.command == "bootstrap":
            fixture.up()
            fixture.bootstrap()
        elif args.command == "snapshot":
            fixture.up()
            print(fixture.snapshot())
        elif args.command == "reset":
            fixture.up()
            print(fixture.reset())
        elif args.command == "plan":
            n = (
                INITIAL_TRIALS_PER_CELL
                if args.profile == "initial"
                else PUBLICATION_TRIALS_PER_CELL
            )
            print(json.dumps(_plan(n, model_free=args.model_free), indent=2))
        elif args.command == "record":
            fixture.up()
            record(fixture, args.out, headed=args.headed)
        elif args.command == "compile":
            compile_recording(args.recording, args.bundle)
        elif args.command == "run":
            n = (
                INITIAL_TRIALS_PER_CELL
                if args.profile == "initial"
                else PUBLICATION_TRIALS_PER_CELL
            )
            run_matrix(
                fixture,
                args.bundle,
                args.out,
                n=n,
                allow_paid_agent=args.allow_paid_agent,
                max_cost_per_run_usd=args.max_cost_per_run_usd,
                max_total_agent_cost_usd=args.max_total_agent_cost_usd,
                headed=args.headed,
                model_free=args.model_free,
            )
    except FixtureError as exc:
        print(f"openemr-local benchmark refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
