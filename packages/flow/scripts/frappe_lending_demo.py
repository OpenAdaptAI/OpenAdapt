"""Pinned, synthetic Frappe Lending benchmark driver.

The measured task starts already authenticated on a blank Loan Application
form opened from one pinned synthetic Customer. Its read-only applicant and
the form's exact prepopulated Company are fixed before timing and recording
begin. Every trial restores the same hashed SQL snapshot. Three equal arms act
on the same effect contract:

* compiled: model-free replay of one recorded browser demonstration;
* agent: computer-use model, only after explicit paid opt-in and cost caps;
* api: model-free ``ApiActuator`` control using Frappe's own REST API.

Neither pixels nor actor self-report decide success. A separate read-only REST
session plus direct SQL delta audit classify each run.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import stat
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from benchmark.frappe_lending.fixture import (  # noqa: E402
    FixtureError,
    FrappeFixture,
    audit_table_deltas,
)
from openadapt_flow.backends.playwright_backend import (  # noqa: E402
    PlaywrightBackend as _BasePlaywrightBackend,
)
from openadapt_flow.benchmark import agent_baseline  # noqa: E402
from openadapt_flow.benchmark.frappe_lending import (  # noqa: E402
    CONDITIONS,
    INITIAL_TRIALS_PER_CELL,
    PUBLICATION_TRIALS_PER_CELL,
    FrappeLoanApplicationOracle,
    LoanApplicationSpec,
    TrialRow,
    aggregate_rows,
    classify_trial,
    loan_application_api_binding,
    loan_application_effects,
    publication_gate,
    records_sha256,
)
from openadapt_flow.ir import StructuralLocator  # noqa: E402

ACTOR_USER = "openadapt.actor@example.invalid"
ACTOR_PASSWORD = "openadapt-local-actor"
ORACLE_USER = "openadapt.oracle@example.invalid"
ORACLE_PASSWORD = "openadapt-local-oracle"
AGENT_MAX_ACTIONS = 30
# Reserve the worst marginal list-price cost of one fixed-model response before
# allowing a call. The three input/cache usage buckets are each conservatively
# bounded by the 200k-token context limit, plus the 4096-token output ceiling.
# ``run_agent`` checks its threshold after each response; subtracting this
# reserve makes the user-facing per-run cap hard rather than post-call soft.
AGENT_MAX_SINGLE_CALL_RESERVE_USD = (
    200_000
    / 1_000_000
    * (
        agent_baseline.INPUT_USD_PER_MTOK
        + agent_baseline.CACHE_WRITE_USD_PER_MTOK
        + agent_baseline.CACHE_READ_USD_PER_MTOK
    )
    + agent_baseline.MAX_TOKENS / 1_000_000 * agent_baseline.OUTPUT_USD_PER_MTOK
)
MAX_JSON_EVIDENCE_BYTES = 1024 * 1024
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_ACTION_LOG_BYTES = 256 * 1024
MAX_ROW_BYTES = 256 * 1024
MAX_TEXT_CHARS = 2_000
RECORDING_READY_MARKER = ".openadapt-recording-ready.json"
RECORDING_FAILED_MARKER = ".openadapt-recording-failed.json"
FULL_ARMS = ("compiled", "agent", "api")
MODEL_FREE_ARMS = ("compiled", "api")


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
:root { --primary: #7c3aed !important; --bg-color: #f5f3ff !important; }
.layout-main-section { border: 3px solid #7c3aed !important; padding: 14px !important; }
.form-control, .control-input { border-radius: 12px !important; }
.page-head { background: #ede9fe !important; }
"""


class FrappePlaywrightBackend(_BasePlaywrightBackend):
    """Structural adapter scoped to the pinned synthetic Frappe fixture.

    ``data-fieldname`` is only selector evidence here. Identity binds that
    field to the exact fixed Loan Application context independently asserted by
    :func:`_preauthenticate_browser`; a same-named field on another applicant
    or company cannot satisfy the gate.
    """

    _FIELD_TARGET_JS = """([px, py]) => {
        const el = document.elementFromPoint(px, py);
        if (!el) return null;
        const actionable = el.closest('input, select, textarea, button') || el;
        const field = actionable.closest('[data-fieldname]');
        const fieldname = field && field.getAttribute('data-fieldname');
        if (!fieldname) {
            const save = actionable.closest('button.primary-action');
            const saveButtons = Array.from(
                document.querySelectorAll('button.primary-action')
            ).filter(button =>
                (button.getAttribute('data-label') || button.textContent || '')
                    .replace(/\\s+/g, ' ').trim() === 'Save'
            );
            if (!save || saveButtons.length !== 1 || saveButtons[0] !== save)
                return null;
            const selector = save.getAttribute('data-label') === 'Save'
                ? 'button.primary-action[data-label="Save"]'
                : null;
            return {
                selector,
                role: 'button',
                name: 'Save',
                target_kind: 'action',
                target_id: 'save',
            };
        }
        const tag = actionable.tagName.toLowerCase();
        const direct = actionable.getAttribute('data-fieldname') === fieldname;
        const selector = direct
            ? tag + '[data-fieldname="' + CSS.escape(fieldname) + '"]'
            : '[data-fieldname="' + CSS.escape(fieldname) + '"] ' + tag;
        const matches = document.querySelectorAll(selector);
        if (matches.length !== 1 || matches[0] !== actionable) return null;
        let role = actionable.getAttribute('role');
        if (!role) {
            role = {input: 'textbox', select: 'combobox', textarea: 'textbox'}[tag]
                || null;
        }
        return {
            selector,
            role,
            fieldname,
            target_kind: 'field',
            target_id: fieldname,
        };
    }"""

    def _frappe_field_target(self, x: int, y: int) -> dict[str, str] | None:
        try:
            result = self.page.evaluate(self._FIELD_TARGET_JS, [int(x), int(y)])
        except Exception:
            return None
        return result if isinstance(result, dict) else None

    def structural_locator_at(self, x: int, y: int) -> StructuralLocator | None:
        target = self._frappe_field_target(x, y)
        if target is None:
            return super().structural_locator_at(x, y)
        return StructuralLocator(
            selector=target.get("selector"),
            role=target.get("role"),
            name=target.get("name"),
        )

    def structured_text_at(self, x: int, y: int) -> str | None:
        target = self._frappe_field_target(x, y)
        if target is None:
            return super().structured_text_at(x, y)
        try:
            result = self.page.evaluate(
                """(target) => {
                    const form = window.cur_frm;
                    const doc = form && form.doc;
                    if (!doc || doc.doctype !== 'Loan Application') return null;
                    return JSON.stringify({
                        doctype: doc.doctype,
                        applicant_type: String(doc.applicant_type || ''),
                        applicant: String(doc.applicant || ''),
                        company: String(doc.company || ''),
                        repayment_method: String(doc.repayment_method || ''),
                        target_kind: target.target_kind,
                        target_id: target.target_id,
                    });
                }""",
                {
                    "target_kind": target["target_kind"],
                    "target_id": target["target_id"],
                },
            )
        except Exception:
            return None
        return result if isinstance(result, str) and result else None


def _center(locator: Any) -> tuple[int, int]:
    locator.wait_for(state="visible", timeout=30_000)
    box = locator.bounding_box()
    if box is None:
        raise RuntimeError(f"no bounding box for {locator}")
    return int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2)


def _control(page: Any, fieldname: str) -> Any:
    return page.locator(f'[data-fieldname="{fieldname}"] input').last


def _preauthenticate_browser(backend: Any, fixture: FrappeFixture) -> None:
    """Assert exact unmeasured Customer/Company setup for both browser arms."""
    page = backend.page
    spec = LoanApplicationSpec()
    page.goto(f"{fixture.base_url}/login")
    page.locator("#login_email").fill(ACTOR_USER)
    page.locator("#login_password").fill(ACTOR_PASSWORD)
    page.get_by_role("button", name="Continue", exact=True).click()
    page.wait_for_url("**/desk**", timeout=60_000)
    # This mirrors the exact route semantics installed by pinned Lending's
    # ``public/js/custom_customer.js``. The public ``frappe.new_doc`` wrapper
    # constructs a QuickEntryForm even when quick entry is disabled; in this
    # pinned stack that leaves an orphan modal backdrop over the full form.
    # Execute its no-quick-entry branch directly: set the same route options,
    # create the local doc, and route to it. This does not require opening or
    # reading Customer, and remains shared unmeasured setup.
    page.evaluate(
        """async ({ applicantType, applicant }) => {
            frappe.route_options = {
                applicant_type: applicantType,
                applicant: applicant,
            };
            await new Promise((resolve, reject) => {
                frappe.model.with_doctype("Loan Application", () => {
                    try {
                        const doc = frappe.model.get_new_doc(
                            "Loan Application", null, null, true
                        );
                        frappe.set_route("Form", doc.doctype, doc.name)
                            .then(resolve, reject);
                    } catch (error) {
                        reject(error);
                    }
                });
            });
        }""",
        {"applicantType": spec.applicant_type, "applicant": spec.applicant},
    )
    page.wait_for_url("**/loan-application/new-loan-application-*", timeout=60_000)
    page.locator('input[data-fieldname="company"]').wait_for(
        state="visible", timeout=60_000
    )
    context = page.evaluate(
        """() => {
            const form = window.cur_frm;
            const control = form && form.fields_dict.applicant;
            const input = control && control.$input && control.$input.get(0);
            return {
                doctype: form && form.doc.doctype,
                applicant_type: form && form.doc.applicant_type,
                applicant: form && form.doc.applicant,
                company: form && form.doc.company,
                repayment_method: form && form.doc.repayment_method,
                applicant_control_status: control && control.disp_status,
                applicant_disabled: Boolean(input && input.disabled),
                orphan_modal_backdrops:
                    document.querySelectorAll(".modal-backdrop").length,
                body_modal_open: document.body.classList.contains("modal-open"),
            };
        }"""
    )
    expected = {
        "doctype": "Loan Application",
        "applicant_type": spec.applicant_type,
        "applicant": spec.applicant,
        "company": spec.company,
        "repayment_method": spec.repayment_method,
        "applicant_control_status": "Read",
        "applicant_disabled": True,
        "orphan_modal_backdrops": 0,
        "body_modal_open": False,
    }
    if context != expected:
        raise FixtureError(
            "supported Customer route did not produce the exact read-only "
            f"Loan Application context: {context!r}"
        )


def _apply_condition(backend: Any, condition: str) -> None:
    if condition == "ui_cosmetic_v1":
        backend.page.add_style_tag(content=DRIFT_CSS)


def _login_session(base_url: str, user: str, password: str) -> Any:
    import httpx

    # Keep the benchmark runnable from openadapt-flow's base dependency set;
    # ``requests`` is installed only by optional extras.
    session = httpx.Client(timeout=30)
    response = session.post(
        f"{base_url}/api/method/login",
        data={"usr": user, "pwd": password},
    )
    if response.status_code != 200:
        raise FixtureError(
            f"Frappe login for {user} returned HTTP {response.status_code}"
        )
    return session


def record(fixture: FrappeFixture, out_dir: Path, *, headed: bool) -> Path:
    """Record the synthetic form task; authentication is excluded from it."""
    from openadapt_flow.recorder import Recorder

    if out_dir.exists():
        raise FixtureError(
            f"recording path already exists; refusing overwrite: {out_dir}"
        )
    # Record from the same protected baseline used by every trial. A stale
    # target row would silently bake duplicate-state behavior into the bundle.
    baseline_hash = fixture.reset()
    reader = _login_session(fixture.base_url, ORACLE_USER, ORACLE_PASSWORD)
    oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
    backend, close = FrappePlaywrightBackend.launch(
        fixture.base_url, headless=not headed
    )
    try:
        _preauthenticate_browser(backend, fixture)
        page = backend.page
        before_counts = fixture.table_counts()
        before_non_target_loan_sha256 = (
            fixture.non_target_loan_applications_sha256()
        )
        recorder = Recorder(
            backend,
            out_dir,
            app_url=page.url,
            **SETTLE,
        )
        spec = LoanApplicationSpec()

        def type_field(field: str, value: str, *, param: str) -> None:
            locator = _control(page, field)
            recorder.click(*_center(locator))
            recorder.press("Control+a")
            recorder.type_text(value, param=param)

        def commit_field(field: str, expected: str) -> None:
            recorder.press("Tab")
            page.wait_for_function(
                "([field, expected]) => "
                "String(window.cur_frm?.doc?.[field] ?? '') === expected",
                arg=[field, expected],
                timeout=30_000,
            )

        def link_field(field: str, value: str, *, param: str) -> None:
            type_field(field, value, param=param)
            suggestion = page.locator(
                f'div.frappe-control[data-fieldname="{field}"] .awesomplete '
                'ul[role="listbox"]:not([hidden]) > [role="option"]'
            ).filter(has_text=re.compile(rf"^\s*{re.escape(value)}\s*$"))
            # Playwright's strict single-locator wait also rejects duplicate
            # exact options instead of silently selecting the first one.
            suggestion.wait_for(state="visible", timeout=30_000)
            if suggestion.inner_text().strip() != value:
                raise FixtureError(
                    f"Link field {field!r} did not expose the exact visible "
                    f"document value {value!r}"
                )
            recorder.press("Enter")
            page.wait_for_function(
                "([field, expected]) => window.cur_frm?.doc?.[field] === expected",
                arg=[field, value],
                timeout=30_000,
            )

        type_field(
            "applicant_email_address",
            spec.applicant_email_address,
            param="applicant_email_address",
        )
        type_field(
            "applicant_phone_number",
            spec.applicant_phone_input,
            param="applicant_phone_number",
        )
        commit_field("applicant_phone_number", spec.applicant_phone_number)
        # The remaining measured controls are below the initial viewport. Keep
        # this wheel action in the recording immediately after the last top
        # field so replay never relies on locator-induced hidden scrolling.
        recorder.scroll(0, 600)
        link_field("loan_product", spec.loan_product, param="loan_product")
        type_field("loan_amount", spec.loan_amount, param="loan_amount")
        # The repayment control remains below the viewport after the first
        # scroll. Capture the second movement explicitly so a coordinate replay
        # cannot type into the previously focused amount field and then report
        # a save refusal as an unexplained timeout.
        recorder.scroll(0, 500)
        type_field(
            "repayment_periods", spec.repayment_periods, param="repayment_periods"
        )
        commit_field("repayment_periods", spec.repayment_periods)
        recorder.click(*_center(page.locator("button.primary-action", has_text="Save")))
        page.locator(".indicator-pill", has_text="Not Saved").wait_for(
            state="hidden", timeout=60_000
        )
        recording_path = recorder.finish()
        evidence = _capture_post_evidence(
            oracle,
            fixture,
            before_counts=before_counts,
            before_non_target_loan_sha256=before_non_target_loan_sha256,
            arm="compiled",
        )
        verdict = classify_trial(
            actor_reported_success=True,
            halted=False,
            rest_records=evidence.rest_records,
            db_records=evidence.db_records if evidence.readable else None,
            unexpected_db_deltas=evidence.delta_violations,
        )
        marker = {
            "schema_version": 1,
            "baseline_snapshot_sha256": baseline_hash,
            "recording_manifest_sha256": _tree_manifest_sha256(
                recording_path,
                exclude_names=frozenset(
                    {RECORDING_READY_MARKER, RECORDING_FAILED_MARKER}
                ),
            ),
            "primary_outcome": verdict.primary_outcome.value,
            "rest_records_sha256": _safe_records_sha256(evidence.rest_records),
            "db_records_sha256": _safe_records_sha256(evidence.db_records),
            "before_non_target_loan_sha256": (
                evidence.before_non_target_loan_sha256
            ),
            "after_non_target_loan_sha256": evidence.after_non_target_loan_sha256,
            "all_table_deltas": evidence.all_table_deltas,
            "delta_violations": evidence.delta_violations,
            "oracle_errors": evidence.errors,
        }
        if not verdict.success or not evidence.readable:
            _write_private_exclusive(
                recording_path / RECORDING_FAILED_MARKER,
                (json.dumps(_json_safe(marker), sort_keys=True) + "\n").encode(),
            )
            raise FixtureError(
                "recorded UI save failed independent REST/SQL/delta verification: "
                + verdict.detail
            )
        _write_private_exclusive(
            recording_path / RECORDING_READY_MARKER,
            (json.dumps(marker, sort_keys=True) + "\n").encode(),
        )
        return recording_path
    finally:
        close()


def compile_recording(recording_dir: Path, bundle_dir: Path) -> None:
    """Compile once, then bind the declared SoR effects to the Save step."""
    from openadapt_flow.compiler import compile_recording as compile_flow

    if bundle_dir.exists():
        raise FixtureError(
            f"bundle path already exists; refusing overwrite: {bundle_dir}"
        )
    _validate_recording_ready(recording_dir)
    workflow = compile_flow(
        recording_dir, bundle_dir, name="frappe-lending-create-loan-application"
    )
    if not workflow.steps:
        raise FixtureError("compiler produced no steps")
    workflow.steps[-1].effects = loan_application_effects()
    workflow.steps[-1].risk = "reversible"
    workflow.save(bundle_dir)
    _validate_bundle_contract(bundle_dir)


def _agent_prompt(spec: LoanApplicationSpec) -> str:
    return (
        "You are already signed in to a local synthetic Frappe Lending test "
        "site. The blank Loan Application was opened from the pinned synthetic "
        f"Customer {spec.applicant!r}. Applicant Type {spec.applicant_type!r}, "
        f"that Applicant, Company {spec.company!r}, and Repayment Method "
        f"{spec.repayment_method!r} are already selected fixed shared setup; "
        "do not try to change them. Complete and save "
        "exactly one application "
        "with these remaining values:\n"
        f"- Applicant Email Address: {spec.applicant_email_address}\n"
        f"- Applicant Phone Digits: {spec.applicant_phone_input} "
        f"(the pinned widget stores {spec.applicant_phone_number})\n"
        f"- Loan Product: {spec.loan_product}\n"
        f"- Loan Amount: {spec.loan_amount}\n"
        f"- Repayment Period in Months: {spec.repayment_periods}\n"
        "Use only the visible interface. Stop after the form is saved. All "
        "names and values are synthetic. Start by taking a screenshot."
    )


@dataclass
class CapturedEvidence:
    """Independent post-write evidence, including unreadable/malformed state."""

    rest_records: Any
    db_records: Any
    before_table_counts: dict[str, int] | None
    after_table_counts: dict[str, int] | None
    all_table_deltas: dict[str, int] | None
    before_non_target_loan_sha256: str | None
    after_non_target_loan_sha256: str | None
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
            and self.before_non_target_loan_sha256 is not None
            and self.after_non_target_loan_sha256 is not None
        )


def _capture_post_evidence(
    oracle: FrappeLoanApplicationOracle,
    fixture: FrappeFixture,
    *,
    before_counts: dict[str, int] | None,
    before_non_target_loan_sha256: str | None,
    arm: str,
) -> CapturedEvidence:
    """Capture each oracle independently; every failure remains a result row."""
    errors: list[str] = []
    rest: Any = None
    db: Any = None
    after: dict[str, int] | None = None
    deltas: dict[str, int] | None = None
    after_non_target_loan_sha256: str | None = None
    violations: list[str] = []
    try:
        state = oracle.capture()
        if state.reachable:
            rest = state.records
        else:
            errors.append("REST oracle unreachable")
    except Exception as exc:  # noqa: BLE001 - preserve trial after write/spend
        errors.append(f"REST oracle: {type(exc).__name__}")
    try:
        db = fixture.db_records()
    except Exception as exc:  # noqa: BLE001 - preserve trial after write/spend
        errors.append(f"SQL oracle: {type(exc).__name__}")
    if before_counts is None:
        errors.append("pre-write table counts unavailable")
    else:
        try:
            after = fixture.table_counts()
            violations, deltas = audit_table_deltas(before_counts, after, arm=arm)
        except Exception as exc:  # noqa: BLE001 - unreadable is not clean
            errors.append(f"table delta oracle: {type(exc).__name__}")
    if before_non_target_loan_sha256 is None:
        errors.append("pre-write non-target Loan Application digest unavailable")
    else:
        try:
            after_non_target_loan_sha256 = (
                fixture.non_target_loan_applications_sha256()
            )
            if after_non_target_loan_sha256 != before_non_target_loan_sha256:
                violations.append("tabLoan Application:non-target-content-changed")
        except Exception as exc:  # noqa: BLE001 - unreadable is not clean
            errors.append(f"non-target Loan Application audit: {type(exc).__name__}")
    return CapturedEvidence(
        rest_records=rest,
        db_records=db,
        before_table_counts=before_counts,
        after_table_counts=after,
        all_table_deltas=deltas,
        before_non_target_loan_sha256=before_non_target_loan_sha256,
        after_non_target_loan_sha256=after_non_target_loan_sha256,
        delta_violations=violations,
        errors=errors,
    )


def _write_private_exclusive(path: Path, payload: bytes) -> None:
    """Create and fsync a 0600 artifact without an exposure window."""
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


def _bounded_payload(payload: bytes, *, limit: int, kind: str) -> bytes:
    """Return payload or a bounded, hash-preserving omission manifest."""
    if len(payload) <= limit:
        return payload
    return (
        json.dumps(
            {
                "bounded_omission": True,
                "kind": kind,
                "original_bytes": len(payload),
                "original_sha256": hashlib.sha256(payload).hexdigest(),
            },
            sort_keys=True,
        )
        + "\n"
    ).encode()


def _artifact_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_manifest_sha256(
    path: Path, *, exclude_names: frozenset[str] = frozenset()
) -> str:
    """Bind every in-tree regular artifact; refuse links and special files."""
    if not path.exists():
        return ""
    rows = []
    for item in sorted(path.rglob("*")):
        if item.parent == path and item.name in exclude_names:
            continue
        item_lstat = item.lstat()
        if stat.S_ISDIR(item_lstat.st_mode):
            continue
        if not stat.S_ISREG(item_lstat.st_mode):
            raise FixtureError(f"artifact tree contains non-regular path: {item}")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(item, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise FixtureError(f"artifact changed type while hashing: {item}")
            digest = hashlib.sha256()
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(fd)
        rows.append(
            {
                "path": item.relative_to(path).as_posix(),
                "bytes": opened.st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _validate_recording_ready(recording_dir: Path) -> dict[str, Any]:
    """Require a hash-bound marker produced only after independent verification."""
    ready = recording_dir / RECORDING_READY_MARKER
    failed = recording_dir / RECORDING_FAILED_MARKER
    if (
        failed.exists()
        or failed.is_symlink()
        or not ready.exists()
        or ready.is_symlink()
    ):
        raise FixtureError(
            "recording lacks an unambiguous independently verified readiness marker"
        )
    if (
        not stat.S_ISREG(ready.lstat().st_mode)
        or stat.S_IMODE(ready.stat().st_mode) != 0o600
    ):
        raise FixtureError("recording readiness marker is not a protected regular file")
    try:
        marker = json.loads(ready.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError("recording readiness marker is malformed") from exc
    if (
        marker.get("schema_version") != 1
        or marker.get("primary_outcome") != "correct"
        or marker.get("delta_violations") != []
        or marker.get("oracle_errors") != []
        or not re.fullmatch(r"[0-9a-f]{64}", marker.get("baseline_snapshot_sha256", ""))
        or not re.fullmatch(r"[0-9a-f]{64}", marker.get("rest_records_sha256", ""))
        or marker.get("rest_records_sha256") != marker.get("db_records_sha256")
        or not re.fullmatch(
            r"[0-9a-f]{64}", marker.get("before_non_target_loan_sha256", "")
        )
        or marker.get("before_non_target_loan_sha256")
        != marker.get("after_non_target_loan_sha256")
    ):
        raise FixtureError("recording readiness marker does not certify the task")
    nonzero_deltas = {
        table: delta
        for table, delta in marker.get("all_table_deltas", {}).items()
        if delta
    }
    if nonzero_deltas != {"tabLoan Application": 1}:
        raise FixtureError("recording readiness marker has the wrong database delta")
    observed = _tree_manifest_sha256(
        recording_dir,
        exclude_names=frozenset({RECORDING_READY_MARKER, RECORDING_FAILED_MARKER}),
    )
    if marker.get("recording_manifest_sha256") != observed:
        raise FixtureError("recording changed after independent verification")
    return marker


def _validate_bundle_contract(bundle_dir: Path) -> Any:
    """Load and require the exact compiled Frappe task/effect contract."""
    from openadapt_flow.ir import ActionKind, Workflow

    workflow = Workflow.load(bundle_dir)
    if workflow.name != "frappe-lending-create-loan-application":
        raise FixtureError("bundle workflow name differs from the benchmark task")
    if not workflow.steps:
        raise FixtureError("bundle contains no executable steps")
    expected_params = LoanApplicationSpec().params()
    if workflow.params != expected_params:
        raise FixtureError(
            "bundle parameters differ from measured task fields; fixed Customer "
            "and prepopulated Company context must not become recorded parameters"
        )
    effect_steps = [index for index, step in enumerate(workflow.steps) if step.effects]
    if effect_steps != [len(workflow.steps) - 1]:
        raise FixtureError("bundle must bind effects only to its final Save step")
    final = workflow.steps[-1]
    if final.action is not ActionKind.CLICK or final.risk != "reversible":
        raise FixtureError("bundle final transition is not the reversible Save click")
    expected = [effect.model_dump(mode="json") for effect in loan_application_effects()]
    observed = [effect.model_dump(mode="json") for effect in final.effects]
    if observed != expected:
        raise FixtureError("bundle final effect contract differs from the task")
    if final.api_binding is not None:
        raise FixtureError("compiled browser bundle unexpectedly contains an API write")
    return workflow


def _json_safe(value: Any, *, depth: int = 0) -> Any:
    """Make malformed evidence serializable while bounding recursive shape."""
    if depth >= 8:
        return {"bounded_omission": True, "reason": "maximum depth"}
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_TEXT_CHARS]
    if isinstance(value, bytes):
        return {
            "non_json_type": "bytes",
            "bytes": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if isinstance(value, dict):
        items = list(value.items())
        result = {
            str(key)[:200]: _json_safe(item, depth=depth + 1)
            for key, item in items[:200]
        }
        if len(items) > 200:
            result["bounded_omission"] = {"remaining_items": len(items) - 200}
        return result
    if isinstance(value, (list, tuple)):
        items_result = [_json_safe(item, depth=depth + 1) for item in value[:200]]
        if len(value) > 200:
            items_result.append({"bounded_omission": len(value) - 200})
        return items_result
    return {
        "non_json_type": type(value).__name__,
        "repr": repr(value)[:MAX_TEXT_CHARS],
    }


def _persist_evidence(
    evidence_dir: Path,
    *,
    output_root: Path,
    evidence: CapturedEvidence,
    environment_identity: dict[str, Any],
    final_screenshot: bytes | None = None,
    action_log: list[str] | None = None,
    run_artifacts_dir: Path | None = None,
) -> dict[str, Any]:
    """Persist durable bounded evidence and return row-bound artifact hashes."""
    if evidence_dir.exists():
        raise FixtureError(f"evidence path exists; refusing overwrite: {evidence_dir}")
    evidence_dir.mkdir(parents=True, mode=0o700)
    evidence_dir.chmod(0o700)
    payload = _json_safe(
        {
            "rest_records": evidence.rest_records,
            "db_records": evidence.db_records,
            "before_table_counts": evidence.before_table_counts,
            "after_table_counts": evidence.after_table_counts,
            "all_table_deltas": evidence.all_table_deltas,
            "before_non_target_loan_sha256": (
                evidence.before_non_target_loan_sha256
            ),
            "after_non_target_loan_sha256": evidence.after_non_target_loan_sha256,
            "delta_violations": evidence.delta_violations,
            "oracle_errors": evidence.errors,
            "environment_identity": environment_identity,
        }
    )
    raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode()
    bounded = _bounded_payload(
        raw, limit=MAX_JSON_EVIDENCE_BYTES, kind="oracle-evidence"
    )
    evidence_path = evidence_dir / "oracle-evidence.json"
    _write_private_exclusive(evidence_path, bounded)
    try:
        relative_evidence_path = evidence_path.relative_to(output_root).as_posix()
    except ValueError as exc:
        raise FixtureError(
            "evidence directory escapes the benchmark output root"
        ) from exc
    metadata: dict[str, Any] = {
        "evidence_sha256": _artifact_sha256(evidence_path),
        "evidence_relative_path": relative_evidence_path,
        "evidence_bounded_omission": len(bounded) != len(raw),
        "oracle_errors": evidence.errors,
        "nonzero_table_deltas": {
            table: delta
            for table, delta in (evidence.all_table_deltas or {}).items()
            if delta
        },
    }
    if final_screenshot is not None:
        screenshot = _bounded_payload(
            final_screenshot, limit=MAX_SCREENSHOT_BYTES, kind="final-screenshot"
        )
        omitted = len(final_screenshot) > MAX_SCREENSHOT_BYTES
        screenshot_path = evidence_dir / ("final.json" if omitted else "final.png")
        _write_private_exclusive(screenshot_path, screenshot)
        metadata["final_screenshot_sha256"] = hashlib.sha256(
            final_screenshot
        ).hexdigest()
        metadata["final_screenshot_bounded_omission"] = omitted
    if action_log is not None:
        raw_actions = (
            json.dumps(_json_safe(action_log), indent=2, sort_keys=True) + "\n"
        ).encode()
        actions_path = evidence_dir / "actions.json"
        _write_private_exclusive(
            actions_path,
            _bounded_payload(
                raw_actions, limit=MAX_ACTION_LOG_BYTES, kind="agent-action-log"
            ),
        )
        metadata["action_log_sha256"] = _artifact_sha256(actions_path)
    if run_artifacts_dir is not None:
        metadata["run_artifacts_manifest_sha256"] = _tree_manifest_sha256(
            run_artifacts_dir
        )
    return metadata


def _safe_records_sha256(records: Any) -> str:
    if records is None:
        return ""
    try:
        return records_sha256(records)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return ""


def _bounded_text(value: str | None) -> str | None:
    return value[:MAX_TEXT_CHARS] if value is not None else None


def _row(
    *,
    arm: str,
    condition: str,
    trial: int,
    baseline_hash: str,
    actor_reported_success: bool,
    halted: bool,
    wall_s: float,
    rest_records: Any,
    db_records: Any,
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
        # A missing collateral audit makes the combined oracle incomplete even
        # when target-field SQL read-back happened to succeed.
        db_records = None
    safe_error = _bounded_text(error)
    verdict = classify_trial(
        actor_reported_success=actor_reported_success,
        halted=halted,
        rest_records=rest_records,
        db_records=db_records,
        unexpected_db_deltas=unexpected_deltas,
        task_feasible=True,
        execution_error=safe_error,
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
        error=safe_error,
        detail=_bounded_text(verdict.detail) or "",
        metadata=_json_safe(metadata or {}),
    )


def run_api_trial(
    fixture: FrappeFixture,
    *,
    condition: str,
    trial: int,
    evidence_dir: Path,
    output_root: Path,
    environment_identity: dict[str, Any],
) -> TrialRow:
    from openadapt_flow.runtime.actuators import ApiActuator

    baseline_hash = fixture.reset()
    writer = _login_session(fixture.base_url, ACTOR_USER, ACTOR_PASSWORD)
    reader = _login_session(fixture.base_url, ORACLE_USER, ORACLE_PASSWORD)
    oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
    before_counts = fixture.table_counts()
    before_non_target_loan_sha256 = fixture.non_target_loan_applications_sha256()
    result = None
    error = None
    start = time.monotonic()
    try:
        spec = LoanApplicationSpec()
        result = ApiActuator(fixture.base_url, session=writer, timeout_s=30).actuate(
            loan_application_api_binding(spec), spec.params()
        )
    except Exception as exc:  # noqa: BLE001 - preserve post-write evidence
        error = f"{type(exc).__name__}: {exc}"
    actuation_wall_s = time.monotonic() - start
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_loan_sha256=before_non_target_loan_sha256,
        arm="api",
    )
    wall_s = time.monotonic() - start
    artifact_metadata = _persist_evidence(
        evidence_dir,
        output_root=output_root,
        evidence=evidence,
        environment_identity=environment_identity,
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
    fixture: FrappeFixture,
    bundle_dir: Path,
    run_dir: Path,
    *,
    condition: str,
    trial: int,
    headed: bool,
    evidence_dir: Path,
    output_root: Path,
    environment_identity: dict[str, Any],
) -> TrialRow:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    baseline_hash = fixture.reset()
    reader = _login_session(fixture.base_url, ORACLE_USER, ORACLE_PASSWORD)
    oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
    backend, close = FrappePlaywrightBackend.launch(
        fixture.base_url, headless=not headed
    )
    report = None
    error = None
    actuation_wall_s = 0.0
    start = 0.0
    before_counts = None
    before_non_target_loan_sha256 = None
    try:
        _preauthenticate_browser(backend, fixture)
        _apply_condition(backend, condition)
        # Authentication/form navigation are declared unmeasured setup; take
        # the collateral-delta baseline only after they finish.
        before_counts = fixture.table_counts()
        before_non_target_loan_sha256 = (
            fixture.non_target_loan_applications_sha256()
        )
        start = time.monotonic()
        report = Replayer(backend, effect_verifier=oracle.verifier).run(
            Workflow.load(bundle_dir),
            params=LoanApplicationSpec().params(),
            bundle_dir=bundle_dir,
            run_dir=run_dir,
        )
        actuation_wall_s = time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 - trial failure is a result row
        if start:
            actuation_wall_s = time.monotonic() - start
        error = f"{type(exc).__name__}: {exc}"
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_loan_sha256=before_non_target_loan_sha256,
        arm="compiled",
    )
    wall_s = time.monotonic() - start if start else 0.0
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - outside shared timing boundary
        if error is None:
            error = f"browser teardown: {type(exc).__name__}: {exc}"
    artifact_metadata = _persist_evidence(
        evidence_dir,
        output_root=output_root,
        evidence=evidence,
        environment_identity=environment_identity,
        run_artifacts_dir=run_dir,
    )
    success = bool(report and report.success)
    return _row(
        arm="compiled",
        condition=condition,
        trial=trial,
        baseline_hash=baseline_hash,
        actor_reported_success=success,
        halted=not success,
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
            "replayer_success": success,
            "heal_count": report.heal_count if report else 0,
        },
    )


def run_agent_trial(
    fixture: FrappeFixture,
    *,
    condition: str,
    trial: int,
    headed: bool,
    max_cost_usd: float,
    evidence_dir: Path,
    output_root: Path,
    environment_identity: dict[str, Any],
    client: Any = None,
) -> TrialRow:
    baseline_hash = fixture.reset()
    reader = _login_session(fixture.base_url, ORACLE_USER, ORACLE_PASSWORD)
    oracle = FrappeLoanApplicationOracle(fixture.base_url, reader)
    backend, close = FrappePlaywrightBackend.launch(
        fixture.base_url, headless=not headed
    )
    result = None
    fallback_screenshot: bytes | None = None
    ledger = agent_baseline.UsageLedger()
    error = None
    spend_indeterminate = False
    actuation_wall_s = 0.0
    start = 0.0
    before_counts = None
    before_non_target_loan_sha256 = None
    try:
        _preauthenticate_browser(backend, fixture)
        _apply_condition(backend, condition)
        before_counts = fixture.table_counts()
        before_non_target_loan_sha256 = (
            fixture.non_target_loan_applications_sha256()
        )
        start = time.monotonic()
        result = agent_baseline.run_agent(
            backend,
            _agent_prompt(LoanApplicationSpec()),
            client=client,
            max_actions=AGENT_MAX_ACTIONS,
            max_cost_usd=max_cost_usd,
            ledger=ledger,
        )
        actuation_wall_s = time.monotonic() - start
    except Exception as exc:  # noqa: BLE001 - preserve paid usage in the row
        if start:
            actuation_wall_s = time.monotonic() - start
            # A provider/network exception can arrive after the service accepted
            # a request but before its usage block reached the ledger. Known
            # usage remains recorded; the unreported marginal spend is unknown.
            spend_indeterminate = True
        error = f"{type(exc).__name__}: {exc}"
    evidence = _capture_post_evidence(
        oracle,
        fixture,
        before_counts=before_counts,
        before_non_target_loan_sha256=before_non_target_loan_sha256,
        arm="agent",
    )
    wall_s = time.monotonic() - start if start else 0.0
    try:
        fallback_screenshot = backend.screenshot()
    except Exception:  # noqa: BLE001 - outside timing; oracle row still survives
        fallback_screenshot = None
    try:
        close()
    except Exception as exc:  # noqa: BLE001 - outside shared timing boundary
        if error is None:
            error = f"browser teardown: {type(exc).__name__}: {exc}"
    artifact_metadata = _persist_evidence(
        evidence_dir,
        output_root=output_root,
        evidence=evidence,
        environment_identity=environment_identity,
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
            "spend_indeterminate": spend_indeterminate,
        },
    )


def _refuse_indeterminate_paid_continuation(row: TrialRow) -> None:
    """Halt the matrix when provider failure may have omitted billed usage."""
    if row.metadata.get("spend_indeterminate") is True:
        raise FixtureError(
            "agent provider spend is indeterminate; row/evidence and known "
            "usage were preserved, and all later paid calls are refused"
        )


def run_matrix(
    fixture: FrappeFixture,
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
            "and both cost caps after explicit spend approval"
        )
    if not model_free and (
        not math.isfinite(max_cost_per_run_usd)
        or max_cost_per_run_usd <= AGENT_MAX_SINGLE_CALL_RESERVE_USD
    ):
        raise FixtureError(
            "--max-cost-per-run-usd must exceed the fixed one-call reserve "
            f"(${AGENT_MAX_SINGLE_CALL_RESERVE_USD:.4f})"
        )
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
        raise FixtureError(
            f"output path exists; refusing to append/mix runs: {out_dir}"
        )
    fixture.up()
    # Free checks happen before any paid request. There is deliberately no
    # standalone paid preflight: the first measured agent response verifies
    # auth/credit and is fully included in that row's UsageLedger and caps.
    if not model_free:
        try:
            agent_baseline.load_api_key()
        except Exception as exc:  # noqa: BLE001 - free credential precondition
            raise FixtureError(f"agent credential preflight failed: {exc}") from exc
    baseline_hash = fixture.baseline_sha256()
    _validate_bundle_contract(bundle_dir)
    spec = LoanApplicationSpec()
    environment_identity = {
        "lock_sha256": hashlib.sha256(
            (REPO / "benchmark/frappe_lending/environment.lock.json").read_bytes()
        ).hexdigest(),
        "custom_image": fixture.image_identity(),
        "baseline_snapshot_sha256": baseline_hash,
        "bundle_manifest_sha256": _tree_manifest_sha256(bundle_dir),
        "task_contract": {
            "fixed_fields": spec.fixed_fields,
            "recorded_params": spec.params(),
            "persisted_fields": spec.fields,
            "browser_setup": (
                "supported Customer -> new Loan Application route with exact "
                "prepopulated Company"
            ),
        },
    }

    out_dir.mkdir(parents=True, mode=0o700)
    out_dir.chmod(0o700)
    rows_path = out_dir / "rows.jsonl"
    rows: list[TrialRow] = []

    def persist(row: TrialRow) -> None:
        raw = (json.dumps(asdict(row), sort_keys=True) + "\n").encode()
        if len(raw) > MAX_ROW_BYTES:
            metadata_raw = json.dumps(
                _json_safe(row.metadata), sort_keys=True, separators=(",", ":")
            ).encode()
            row.metadata = {
                "bounded_omission": True,
                "kind": "row-metadata",
                "original_bytes": len(metadata_raw),
                "original_sha256": hashlib.sha256(metadata_raw).hexdigest(),
            }
            raw = (json.dumps(asdict(row), sort_keys=True) + "\n").encode()
        if len(raw) > MAX_ROW_BYTES:
            raise FixtureError("bounded trial row still exceeds evidence limit")
        rows.append(row)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(rows_path, flags, 0o600)
        try:
            view = memoryview(raw)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short JSONL evidence write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)

    agent_stop_threshold = max_cost_per_run_usd - AGENT_MAX_SINGLE_CALL_RESERVE_USD

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
                    output_root=out_dir,
                    environment_identity=environment_identity,
                )
            )
            persist(
                run_api_trial(
                    fixture,
                    condition=condition,
                    trial=trial,
                    evidence_dir=out_dir / "evidence" / f"api-{trial_label}",
                    output_root=out_dir,
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
                    max_cost_usd=agent_stop_threshold,
                    evidence_dir=out_dir / "evidence" / f"agent-{trial_label}",
                    output_root=out_dir,
                    environment_identity=environment_identity,
                )
                persist(agent_row)
                _refuse_indeterminate_paid_continuation(agent_row)
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
                    "post_call_stop_threshold_usd": agent_stop_threshold,
                    "single_call_cost_reserve_usd": (AGENT_MAX_SINGLE_CALL_RESERVE_USD),
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
    spec = LoanApplicationSpec()
    return {
        "run_mode": "model_free" if model_free else "full",
        "arms": list(selected_arms),
        "required_comparative_arms": list(FULL_ARMS),
        "omitted_arms": [arm for arm in FULL_ARMS if arm not in selected_arms],
        "conditions": list(CONDITIONS),
        "trials_per_cell": n,
        "total_trials": len(selected_arms) * len(CONDITIONS) * n,
        "reset": "restore and verify one hashed SQL baseline before every trial",
        "oracle": "independent read-only Frappe REST plus direct SQL delta audit",
        "task_contract": {
            "fixed_fields": spec.fixed_fields,
            "recorded_params": spec.params(),
            "persisted_fields": spec.fields,
            "browser_setup": (
                "supported Customer -> new Loan Application route with exact "
                "prepopulated Company"
            ),
        },
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
        "--state", type=Path, default=REPO / "benchmark/frappe_lending/state"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight")
    sub.add_parser("prepare")
    sub.add_parser("build")
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
    run_parser.add_argument("--max-cost-per-run-usd", type=float, default=2.00)
    run_parser.add_argument("--max-total-agent-cost-usd", type=float)
    run_parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)
    fixture = FrappeFixture(args.state)

    try:
        if args.command == "preflight":
            fixture.runtime_preflight(
                require_build_space=True, engine=fixture.build_engine
            )
            fixture.prepare()
        elif args.command == "prepare":
            fixture.prepare()
        elif args.command == "build":
            fixture.build()
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
        print(f"frappe-lending benchmark refused: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
