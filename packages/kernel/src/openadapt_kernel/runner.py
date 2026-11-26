"""Live CDP / Playwright runner for Draft-to-Commit and Verified Autopilot modes."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

from openadapt_kernel.aci import OnlineACICalibrator
from openadapt_kernel.copilot import PortalPack, mint_receipt
from openadapt_kernel.interceptor import CommitInterceptor
from openadapt_kernel.ledger import ObligationLedger
from openadapt_kernel.macro import stage_form
from openadapt_kernel.oracles import verify_cross_tab_dom, verify_pdf_readback, verify_sqlite_ro
from openadapt_kernel.probe import probe_before_click

RunMode = Literal["draft", "autopilot"]


@dataclass
class RunResult:
    mode: RunMode
    outcome: str
    episode_id: str = ""
    staged_fields: Dict[str, str] = field(default_factory=dict)
    provenance_violations: List[str] = field(default_factory=list)
    commit_detected: bool = False
    readback_verified: bool = False
    receipt_path: Optional[str] = None
    reason: str = ""
    aci_alpha_t: Optional[float] = None
    aci_threshold: Optional[float] = None


def _sign_terminal_receipt(
    ledger: ObligationLedger, pack_id: str, private_key_path: Optional[Path], receipt_out: Optional[Path],
    *, outcome_override: Optional[str] = None, include_plaintext_values: bool = False,
) -> Optional[str]:
    if not private_key_path or not receipt_out:
        return None
    receipt = mint_receipt(
        ledger, pack_id, private_key_path.read_bytes(),
        include_plaintext_values=include_plaintext_values, outcome_override=outcome_override,
    )
    receipt_out.parent.mkdir(parents=True, exist_ok=True)
    receipt_out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
    return str(receipt_out)


def _load_portal_pack(path: Path) -> PortalPack:
    data = json.loads(path.read_text(encoding="utf-8"))
    return PortalPack(
        pack_id=data["pack_id"],
        portal_origin=data["portal_origin"],
        variable_fields=list(data.get("variable_fields") or []),
        static_defaults=dict(data.get("static_defaults") or {}),
        provenance_map=dict(data.get("provenance_map") or {}),
        readback_url_pattern=str(data.get("readback_url_pattern") or ""),
        stage=data.get("stage", "SHADOW"),
        shadow_passes=int(data.get("shadow_passes", 0)),
        edge_branches=dict(data.get("edge_branches") or {}),
    )


def _compute_bindings(pack: PortalPack, dossier: Dict[str, str]) -> Dict[str, str]:
    bindings: Dict[str, str] = dict(pack.static_defaults)
    for target_field, source_key in pack.provenance_map.items():
        if source_key in dossier:
            bindings[target_field] = str(dossier[source_key])
    for fname in pack.variable_fields:
        if fname not in bindings and fname in dossier:
            bindings[fname] = str(dossier[fname])
    return bindings


def run_portal_pack(
    *,
    pack_path: Path,
    dossier: Dict[str, str],
    mode: RunMode = "draft",
    url: Optional[str] = None,
    cdp_url: Optional[str] = None,
    commit_selector: Optional[str] = None,
    popup_trigger_selector: Optional[str] = None,
    popup_bindings: Optional[Dict[str, str]] = None,
    popup_submit_selector: Optional[str] = None,
    sqlite_db_path: Optional[Path] = None,
    sqlite_query: Optional[str] = None,
    pdf_readback_path: Optional[Path] = None,
    private_key_path: Optional[Path] = None,
    receipt_out: Optional[Path] = None,
    episode_id: Optional[str] = None,
    headless: bool = True,
    settle_timeout_ms: int = 5000,
    aci_calibrator: Optional[OnlineACICalibrator] = None,
    include_plaintext_values: bool = False,
) -> RunResult:
    """Execute a PortalPack in Draft-to-Commit (stop before save) or Verified Autopilot mode."""
    from playwright.sync_api import sync_playwright

    pack = _load_portal_pack(pack_path)
    bindings = _compute_bindings(pack, dossier)
    opaque_fields = sorted(pack.provenance_map.keys())
    calibrator = aci_calibrator if aci_calibrator is not None else OnlineACICalibrator()

    ledger = ObligationLedger(episode_id=episode_id)
    ep_id = ledger.episode_id
    ledger.add_obligation("primary_commit", f"Execute {pack.pack_id}", target_system=pack.portal_origin)
    for k, v in dossier.items():
        ledger.record_ssa_literal(k, str(v), source_window="dossier", turn=0)

    interceptor = CommitInterceptor()

    def _finish(
        outcome: str, *, staged_fields: Optional[Dict[str, str]] = None,
        provenance_violations: Optional[List[str]] = None, commit_detected: bool = False,
        readback_verified: bool = False, reason: str = "",
        receipt_outcome: Optional[str] = None, with_aci: bool = False,
    ) -> RunResult:
        rpath = _sign_terminal_receipt(
            ledger, pack.pack_id, private_key_path, receipt_out,
            outcome_override=receipt_outcome, include_plaintext_values=include_plaintext_values,
        )
        return RunResult(
            mode=mode, outcome=outcome, episode_id=ep_id, staged_fields=staged_fields or {},
            provenance_violations=provenance_violations or [], commit_detected=commit_detected,
            readback_verified=readback_verified, receipt_path=rpath, reason=reason,
            aci_alpha_t=calibrator.alpha_t if with_aci else None,
            aci_threshold=calibrator.threshold() if with_aci else None,
        )

    with sync_playwright() as pw:
        if cdp_url:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            close_browser = False
        else:
            browser = pw.chromium.launch(headless=headless)
            page = browser.new_page()
            close_browser = True

        try:
            interceptor.attach_playwright_page(page)
            if url:
                page.goto(url, wait_until="domcontentloaded")

            target_origin = pack.portal_origin or str(getattr(page, "url", "") or "")
            expected_vars = {k: bindings[k] for k in pack.variable_fields if k in bindings}
            pre_present = False
            if not sqlite_db_path and not pdf_readback_path and expected_vars:
                pre_present = verify_cross_tab_dom(
                    page, expected_vars, reload_active=False, target_origin=target_origin
                ).verified

            staged = stage_form(
                page,
                bindings,
                ledger=ledger,
                opaque_fields=opaque_fields,
                interceptor=interceptor,
            )
            if staged.provenance_violations:
                ledger.finalize("error", error_detail="ssa_provenance_violation")
                return _finish(
                    "ESCALATED",
                    provenance_violations=[f"{v.field_name}: {v.reason}" for v in staged.provenance_violations],
                    reason="ssa_provenance_violation",
                )
            if not staged.ok:
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="stage_mismatch")
                return _finish(
                    "ESCALATED",
                    staged_fields=staged.staged,
                    reason=f"stage_failed(unmatched={staged.unmatched}, mismatched={staged.mismatched})",
                )

            if popup_trigger_selector and popup_bindings:
                with page.expect_popup(timeout=5000) as pop_info:
                    page.locator(popup_trigger_selector).first.click()
                popup_page = pop_info.value
                popup_page.wait_for_load_state("domcontentloaded")
                pop_staged = stage_form(
                    popup_page,
                    popup_bindings,
                    ledger=ledger,
                    opaque_fields=opaque_fields,
                    interceptor=interceptor,
                )
                if not pop_staged.ok:
                    ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="popup_stage_failed")
                    return _finish(
                        "ESCALATED",
                        staged_fields={**staged.staged, **pop_staged.staged},
                        reason="popup_stage_failed",
                    )
                staged.staged.update(pop_staged.staged)
                if popup_submit_selector:
                    popup_page.locator(popup_submit_selector).first.click()
                    page.wait_for_timeout(200)

            if mode == "draft":
                return _finish(
                    "READY_FOR_HUMAN_COMMIT",
                    staged_fields=staged.staged,
                    reason="draft_staged_awaiting_operator_submit",
                    receipt_outcome="PREPARE_REQUIRED",
                )

            if not commit_selector:
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="missing_commit_selector")
                return _finish("ESCALATED", staged_fields=staged.staged, reason="autopilot_requires_commit_selector")

            probe = probe_before_click(page, commit_selector)
            if not probe.safe_to_click or not probe.focus_responded:
                detail = f"commit_control_blocked:{probe.reason}"
                if probe.top_blocker:
                    detail = f"{detail}({probe.top_blocker})"
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail=detail)
                return _finish("ESCALATED", staged_fields=staged.staged, reason=detail)

            interceptor.begin_step()
            page.locator(commit_selector).first.click()
            interceptor.wait_for_commit_settle(
                page, target_origin=target_origin, timeout_ms=settle_timeout_ms
            )
            has_2xx_mut = interceptor.has_target_origin_2xx_mutation(target_origin)

            # Independent post-commit read-back (SQLite RO -> PDF stream -> target-origin DOM)
            if sqlite_db_path and sqlite_query:
                verdict = verify_sqlite_ro(sqlite_db_path, sqlite_query, (), expected_vars)
            elif pdf_readback_path:
                verdict = verify_pdf_readback(pdf_readback_path, expected_vars)
            else:
                verdict = verify_cross_tab_dom(
                    page, expected_vars, reload_active=True, target_origin=target_origin
                )

            has_transition = (not pre_present) and verdict.verified
            commit_confirmed = has_2xx_mut or has_transition

            # Online ACI update from Level-2/3 read-back ground truth Y_t
            anomaly_score = 0.0 if (has_2xx_mut and probe.focus_responded) else (1.0 if has_transition else 3.0)
            flagged = calibrator.should_flag(anomaly_score)
            realized_error = flagged != (not (verdict.verified and commit_confirmed))
            calibrator.update_from_readback(anomaly_score, realized_error=realized_error)

            if not commit_confirmed:
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="commit_not_observed")
                return _finish(
                    "ESCALATED",
                    staged_fields=staged.staged,
                    commit_detected=False,
                    readback_verified=verdict.verified,
                    reason="commit_not_observed",
                    with_aci=True,
                )

            if not verdict.verified:
                ledger.finalize(
                    "error",
                    dirty_fields=list(interceptor.dirty_fields.keys()),
                    error_detail="readback_refuted_persistence",
                )
                return _finish(
                    "ESCALATED",
                    staged_fields=staged.staged,
                    commit_detected=commit_confirmed,
                    readback_verified=False,
                    reason="readback_refuted_persistence",
                    with_aci=True,
                )

            interceptor.clear_dirty_fields()
            ledger.mark_committed("primary_commit", verdict.to_proof())
            decision = ledger.finalize("done", dirty_fields=list(interceptor.dirty_fields.keys()))
            return _finish(
                decision.outcome,
                staged_fields=staged.staged,
                commit_detected=commit_confirmed,
                readback_verified=True,
                reason=decision.reason,
                with_aci=True,
            )
        finally:
            if close_browser:
                browser.close()


def format_run_result_json(result: RunResult) -> str:
    return json.dumps(asdict(result), indent=2)
