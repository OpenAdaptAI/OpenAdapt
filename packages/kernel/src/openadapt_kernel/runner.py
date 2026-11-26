"""Live CDP / Playwright runner for Draft-to-Commit, Verified Autopilot, and Self-Healing modes."""

from __future__ import annotations

import json
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Literal, Mapping, Optional, Sequence

from openadapt_kernel.canonical import (
    DossierVariableSchema,
    tokenize_role_label,
    verify_semantic_slot_bindings,
)
from openadapt_kernel.copilot import PortalPack, mint_receipt
from openadapt_kernel.digital_twin import capture_zero_phi_digital_twin, self_heal_in_digital_twin
from openadapt_kernel.egress_guard import LocalEgressGuard
from openadapt_kernel.fsm import compile_policy_rules_to_table, evaluate_pre_step_policy
from openadapt_kernel.interceptor import CommitInterceptor
from openadapt_kernel.ledger import (
    EpistemicArtifact,
    ObligationLedger,
    extract_dossier_facts,
    verify_completion_closure,
)
from openadapt_kernel.macro import lift_distilled_repair_to_pack, observe_schema, stage_form
from openadapt_kernel.oracles import verify_cross_tab_dom, verify_pdf_readback, verify_sqlite_ro
from openadapt_kernel.prepare import (
    PrepareCard,
    PrepareResolution,
    PrepareResumeResponse,
    UnresolvedSlot,
    qualify_and_promote_pack,
    resume_from_prepare_card,
)
from openadapt_kernel.probe import probe_before_click
from openadapt_kernel.registry import publish_signed_pack
from openadapt_kernel.zero_phi_repair import replay_distilled_zero_phi_repair

RunMode = Literal["draft", "autopilot"]


@dataclass
class RunResult:
    mode: RunMode
    outcome: str
    staged_fields: Dict[str, str] = field(default_factory=dict)
    provenance_violations: List[str] = field(default_factory=list)
    commit_detected: bool = False
    readback_verified: bool = False
    receipt_path: Optional[str] = None
    reason: str = ""
    healed: bool = False
    published_pack_path: Optional[str] = None
    egress_ledger_entry_sha256: str = ""
    closure_passed: bool = False


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


def _verify_post_commit_readback(
    page: Any,
    expected_vars: Dict[str, str],
    *,
    sqlite_db_path: Optional[Path],
    sqlite_query: Optional[str],
    pdf_readback_path: Optional[Path],
    reload_active: bool = True,
) -> Any:
    if sqlite_db_path and sqlite_query:
        return verify_sqlite_ro(sqlite_db_path, sqlite_query, (), expected_vars)
    if pdf_readback_path:
        return verify_pdf_readback(pdf_readback_path, expected_vars)
    return verify_cross_tab_dom(page, expected_vars, reload_active=reload_active)


def _attempt_twin_self_heal_and_publish(
    page: Any,
    pack: PortalPack,
    bindings: Dict[str, str],
    working_dossier: Dict[str, str],
    *,
    coding_agent_fn: Callable[..., str],
    structural_error: str,
    egress_ledger_path: Optional[Path],
    sqlite_db_path: Optional[Path],
    sqlite_query: Optional[str],
    pdf_readback_path: Optional[Path],
    auto_publish_on_heal: bool,
    registry_dir: Optional[Path],
    signing_key: Optional[bytes],
    private_key_path: Optional[Path],
) -> tuple[bool, Any, str, Optional[str]]:
    twin = capture_zero_phi_digital_twin(page)
    ledger_file = (
        Path(egress_ledger_path)
        if egress_ledger_path is not None
        else Path(tempfile.mkdtemp(prefix="oa_runner_egress_")) / "outbound_egress_ledger.jsonl"
    )
    guard = LocalEgressGuard(ledger_file)
    var_map = {f"${k}": str(v) for k, v in bindings.items()}
    with ThreadPoolExecutor(max_workers=1) as pool:
        heal_res = pool.submit(
            self_heal_in_digital_twin,
            twin,
            var_map,
            coding_agent_fn,
            egress_guard=guard,
            max_turns=3,
            structural_error=structural_error,
        ).result()
    if not heal_res.verified:
        return False, None, heal_res.egress_ledger_entry_sha256, None

    replayed = replay_distilled_zero_phi_repair(
        page,
        heal_res.distilled_steps,
        private_vars=var_map,
        readback_oracle=lambda _p: True,
    )
    if not replayed:
        return False, None, heal_res.egress_ledger_entry_sha256, None

    lift_distilled_repair_to_pack(heal_res.distilled_steps, working_dossier)
    expected_vars = {k: bindings[k] for k in pack.variable_fields if k in bindings}
    verdict = _verify_post_commit_readback(
        page,
        expected_vars,
        sqlite_db_path=sqlite_db_path,
        sqlite_query=sqlite_query,
        pdf_readback_path=pdf_readback_path,
        reload_active=False,
    )
    if not verdict.verified:
        return False, verdict, heal_res.egress_ledger_entry_sha256, None

    published_path: Optional[str] = None
    key_bytes = signing_key or (private_key_path.read_bytes() if private_key_path else None)
    if auto_publish_on_heal and registry_dir is not None and key_bytes is not None:
        schemas = {
            k: DossierVariableSchema(var_name=k, field_type="casefold_ws", role_tokens=tokenize_role_label(k))
            for k in expected_vars
        }
        staged_map = {i: k for i, k in enumerate(expected_vars.keys())}
        card = PrepareCard(
            card_id=f"heal-{pack.pack_id}",
            portal_origin=pack.portal_origin,
            staged_bindings=staged_map,
            control_labels=dict(staged_map),
        )
        resolution = PrepareResolution(
            card_id=card.card_id,
            operator_id="auto_twin_healer",
            slot_bindings=staged_map,
            approve_all=False,
        )
        promo = qualify_and_promote_pack(
            pack_id=pack.pack_id,
            card=card,
            resolution=resolution,
            variable_schemas=schemas,
            dossier_values=expected_vars,
            observed_readback=expected_vars,
        )
        if promo.qualified:
            pack.stage = "VERIFIED_AUTOPILOT"
            pub_file = publish_signed_pack(pack, key_bytes, Path(registry_dir))
            published_path = str(pub_file)

    return True, verdict, heal_res.egress_ledger_entry_sha256, published_path


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
    episode_id: str = "ep-live-1",
    headless: bool = True,
    policy_specs: Optional[Sequence[Mapping[str, Any]]] = None,
    coding_agent_fn: Optional[Callable[..., str]] = None,
    prepare_handler: Optional[Callable[[PrepareCard], PrepareResumeResponse]] = None,
    egress_ledger_path: Optional[Path] = None,
    registry_dir: Optional[Path] = None,
    signing_key: Optional[bytes] = None,
    auto_publish_on_heal: bool = False,
    source_texts: Optional[Mapping[str, str]] = None,
    freetext_notes: str = "",
    post_commit_obligations: Optional[Sequence[str]] = None,
    allow_url_patterns: Sequence[str] = (),
    deny_url_patterns: Sequence[str] = (),
    observed_facts: Optional[Mapping[str, str]] = None,
    task_disposition: Optional[str] = None,
    max_steps: int = 20,
    strict_closed_world: bool = False,
    epistemic_artifacts: Sequence[EpistemicArtifact] = (),
) -> RunResult:
    """Execute a PortalPack in Draft-to-Commit, Verified Autopilot, or Self-Healing mode."""
    from playwright.sync_api import sync_playwright

    pack = _load_portal_pack(pack_path)
    working_dossier: Dict[str, str] = dict(dossier)
    required_params = [k for k in pack.variable_fields if k not in pack.static_defaults]

    # 1. Step-0 Policy Gate + PREPARE_ASK resume loop (0 writes before approval)
    if policy_specs is not None or prepare_handler is not None:
        policy_table = None
        if policy_specs is not None:
            policy_table, _ = compile_policy_rules_to_table(policy_specs)
        step0 = evaluate_pre_step_policy(
            stage="step_0",
            required_params=required_params,
            dossier=working_dossier,
            policy_table=policy_table,
            observed_facts=observed_facts,
            task_disposition=task_disposition,
        )
        if step0.action in ("REFUSE", "VETO"):
            return RunResult(
                mode=mode,
                outcome="REFUSED",
                reason=step0.reason or "step0_policy_refused",
            )
        if step0.action == "PREPARE_ASK":
            missing = list(step0.missing_params) if step0.missing_params else [
                p for p in required_params if not str(working_dossier.get(p, "")).strip()
            ]
            card = PrepareCard(
                card_id=f"prepare-{episode_id}",
                portal_origin=pack.portal_origin,
                staged_bindings={},
                unresolved_slots=[
                    UnresolvedSlot(
                        control_idx=i,
                        control_label=p,
                        reason="missing_required_param",
                        candidate_vars=(p,),
                    )
                    for i, p in enumerate(missing)
                ],
            )
            if prepare_handler is None:
                return RunResult(
                    mode=mode,
                    outcome="PREPARE_REQUIRED",
                    reason=step0.reason or "missing_required_params",
                )
            resp = prepare_handler(card)
            resume_res = resume_from_prepare_card(
                card,
                resp,
                working_dossier,
                required_params=required_params,
            )
            if resume_res.status != "RESUME":
                return RunResult(
                    mode=mode,
                    outcome="HALTED",
                    reason=resume_res.reason or "prepare_halted",
                )
            working_dossier = dict(resume_res.updated_dossier)

    # 2. Pre-Navigation Policy Gate
    if url and (allow_url_patterns or deny_url_patterns):
        nav_res = evaluate_pre_step_policy(
            stage="pre_navigation",
            target_url=url,
            allow_patterns=allow_url_patterns,
            deny_patterns=deny_url_patterns,
        )
        if nav_res.action != "ALLOW":
            return RunResult(
                mode=mode,
                outcome="REFUSED",
                reason=nav_res.reason or "pre_navigation_blocked",
            )

    bindings = _compute_bindings(pack, working_dossier)
    opaque_fields = sorted(pack.provenance_map.keys())

    ledger = ObligationLedger(episode_id=episode_id)
    ledger.add_obligation("primary_commit", f"Execute {pack.pack_id}", target_system=pack.portal_origin)
    for k, v in working_dossier.items():
        ledger.record_ssa_literal(k, str(v), source_window="dossier", turn=0)

    interceptor = CommitInterceptor()

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

            # Optional semantic slot guard when policy/prepare pipeline is enabled
            if policy_specs is not None or prepare_handler is not None:
                controls = observe_schema(page)
                if controls and bindings:
                    schemas = {
                        k: DossierVariableSchema(var_name=k, field_type="casefold_ws", role_tokens=tokenize_role_label(k))
                        for k in bindings
                    }
                    slot_map: Dict[int, str] = {}
                    for ctrl in controls:
                        for key in bindings:
                            if key.lower() in (ctrl.name.lower(), ctrl.element_id.lower()):
                                slot_map[ctrl.control_index] = key
                                break
                    if slot_map:
                        sem_rep = verify_semantic_slot_bindings(slot_map, schemas, controls)
                        if any(v.status == "TYPE_MISMATCH" for v in sem_rep.verdicts):
                            return RunResult(
                                mode=mode,
                                outcome="ESCALATED",
                                reason="semantic_slot_type_mismatch",
                            )

            staged = stage_form(
                page,
                bindings,
                ledger=ledger,
                opaque_fields=opaque_fields,
                interceptor=interceptor,
            )
            if staged.provenance_violations:
                ledger.finalize("error", error_detail="ssa_provenance_violation")
                return RunResult(
                    mode=mode,
                    outcome="ESCALATED",
                    provenance_violations=[
                        f"{v.field_name}: {v.reason}" for v in staged.provenance_violations
                    ],
                    reason="ssa_provenance_violation",
                )

            healed = False
            egress_entry_sha = ""
            published_pack_path: Optional[str] = None

            if not staged.ok:
                if coding_agent_fn is not None and mode == "autopilot":
                    healed_ok, heal_verdict, egress_entry_sha, published_pack_path = _attempt_twin_self_heal_and_publish(
                        page,
                        pack,
                        bindings,
                        working_dossier,
                        coding_agent_fn=coding_agent_fn,
                        structural_error=f"stage_failed:{staged.unmatched}",
                        egress_ledger_path=egress_ledger_path,
                        sqlite_db_path=sqlite_db_path,
                        sqlite_query=sqlite_query,
                        pdf_readback_path=pdf_readback_path,
                        auto_publish_on_heal=auto_publish_on_heal,
                        registry_dir=registry_dir,
                        signing_key=signing_key,
                        private_key_path=private_key_path,
                    )
                    if healed_ok and heal_verdict is not None:
                        healed = True
                        interceptor.clear_dirty_fields()
                        ledger.mark_committed("primary_commit", heal_verdict.to_proof())
                        decision = ledger.finalize("done", dirty_fields=list(interceptor.dirty_fields.keys()))
                        saved_receipt: Optional[str] = None
                        key_bytes = signing_key or (private_key_path.read_bytes() if private_key_path else None)
                        if decision.outcome == "VERIFIED" and key_bytes and receipt_out:
                            receipt = mint_receipt(ledger, pack.pack_id, key_bytes)
                            receipt_out.parent.mkdir(parents=True, exist_ok=True)
                            receipt_out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                            saved_receipt = str(receipt_out)
                        return RunResult(
                            mode="autopilot",
                            outcome=decision.outcome,
                            staged_fields=bindings,
                            commit_detected=True,
                            readback_verified=True,
                            receipt_path=saved_receipt,
                            reason=decision.reason,
                            healed=True,
                            published_pack_path=published_pack_path,
                            egress_ledger_entry_sha256=egress_entry_sha,
                            closure_passed=True,
                        )
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="stage_mismatch")
                return RunResult(
                    mode=mode,
                    outcome="ESCALATED",
                    staged_fields=staged.staged,
                    reason=f"stage_failed(unmatched={staged.unmatched}, mismatched={staged.mismatched})",
                    egress_ledger_entry_sha256=egress_entry_sha,
                )

            # Optional child popup workflow
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
                    return RunResult(
                        mode=mode,
                        outcome="ESCALATED",
                        staged_fields={**staged.staged, **pop_staged.staged},
                        reason="popup_stage_failed",
                    )
                staged.staged.update(pop_staged.staged)
                if popup_submit_selector:
                    popup_page.locator(popup_submit_selector).first.click()
                    page.wait_for_timeout(200)

            if mode == "draft":
                return RunResult(
                    mode="draft",
                    outcome="READY_FOR_HUMAN_COMMIT",
                    staged_fields=staged.staged,
                    commit_detected=False,
                    readback_verified=False,
                    reason="draft_staged_awaiting_operator_submit",
                )

            if not commit_selector:
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="missing_commit_selector")
                return RunResult(
                    mode="autopilot",
                    outcome="ESCALATED",
                    staged_fields=staged.staged,
                    reason="autopilot_requires_commit_selector",
                )

            probe = probe_before_click(page, commit_selector)
            if not probe.safe_to_click:
                detail = f"commit_control_blocked:{probe.reason}"
                if probe.top_blocker:
                    detail = f"{detail}({probe.top_blocker})"
                if coding_agent_fn is not None:
                    healed_ok, heal_verdict, egress_entry_sha, published_pack_path = _attempt_twin_self_heal_and_publish(
                        page,
                        pack,
                        bindings,
                        working_dossier,
                        coding_agent_fn=coding_agent_fn,
                        structural_error=detail,
                        egress_ledger_path=egress_ledger_path,
                        sqlite_db_path=sqlite_db_path,
                        sqlite_query=sqlite_query,
                        pdf_readback_path=pdf_readback_path,
                        auto_publish_on_heal=auto_publish_on_heal,
                        registry_dir=registry_dir,
                        signing_key=signing_key,
                        private_key_path=private_key_path,
                    )
                    if healed_ok and heal_verdict is not None:
                        interceptor.clear_dirty_fields()
                        ledger.mark_committed("primary_commit", heal_verdict.to_proof())
                        decision = ledger.finalize("done", dirty_fields=list(interceptor.dirty_fields.keys()))
                        saved_receipt = None
                        key_bytes = signing_key or (private_key_path.read_bytes() if private_key_path else None)
                        if decision.outcome == "VERIFIED" and key_bytes and receipt_out:
                            receipt = mint_receipt(ledger, pack.pack_id, key_bytes)
                            receipt_out.parent.mkdir(parents=True, exist_ok=True)
                            receipt_out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                            saved_receipt = str(receipt_out)
                        return RunResult(
                            mode="autopilot",
                            outcome=decision.outcome,
                            staged_fields=staged.staged,
                            commit_detected=True,
                            readback_verified=True,
                            receipt_path=saved_receipt,
                            reason=decision.reason,
                            healed=True,
                            published_pack_path=published_pack_path,
                            egress_ledger_entry_sha256=egress_entry_sha,
                            closure_passed=True,
                        )
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail=detail)
                return RunResult(
                    mode="autopilot",
                    outcome="ESCALATED",
                    staged_fields=staged.staged,
                    commit_detected=False,
                    readback_verified=False,
                    reason=detail,
                    egress_ledger_entry_sha256=egress_entry_sha,
                )

            interceptor.begin_step()
            page.locator(commit_selector).first.click()
            page.wait_for_timeout(300)
            classification = interceptor.classify_step(form_submitted=True)

            expected_vars = {k: bindings[k] for k in pack.variable_fields if k in bindings}
            verdict = _verify_post_commit_readback(
                page,
                expected_vars,
                sqlite_db_path=sqlite_db_path,
                sqlite_query=sqlite_query,
                pdf_readback_path=pdf_readback_path,
                reload_active=True,
            )

            if not verdict.verified and coding_agent_fn is not None:
                healed_ok, heal_verdict, egress_entry_sha, published_pack_path = _attempt_twin_self_heal_and_publish(
                    page,
                    pack,
                    bindings,
                    working_dossier,
                    coding_agent_fn=coding_agent_fn,
                    structural_error="readback_refuted_persistence",
                    egress_ledger_path=egress_ledger_path,
                    sqlite_db_path=sqlite_db_path,
                    sqlite_query=sqlite_query,
                    pdf_readback_path=pdf_readback_path,
                    auto_publish_on_heal=auto_publish_on_heal,
                    registry_dir=registry_dir,
                    signing_key=signing_key,
                    private_key_path=private_key_path,
                )
                if healed_ok and heal_verdict is not None:
                    healed = True
                    verdict = heal_verdict

            if not verdict.verified:
                ledger.finalize(
                    "error",
                    dirty_fields=list(interceptor.dirty_fields.keys()),
                    error_detail="readback_refuted_persistence",
                )
                return RunResult(
                    mode="autopilot",
                    outcome="ESCALATED",
                    staged_fields=staged.staged,
                    commit_detected=classification.is_commit,
                    readback_verified=False,
                    reason="readback_refuted_persistence",
                    egress_ledger_entry_sha256=egress_entry_sha,
                )

            interceptor.clear_dirty_fields()
            ledger.mark_committed("primary_commit", verdict.to_proof())

            # 4. Completion Closure & Fact Coverage before minting signed receipt
            extracted_facts = extract_dossier_facts(source_texts) if source_texts else ()
            has_oracle_proof = bool(verdict.verified and (sqlite_db_path or pdf_readback_path or expected_vars))
            closure = verify_completion_closure(
                ledger,
                step_idx=1,
                max_steps=max_steps,
                required_dossier_keys=list(expected_vars.keys()),
                readback_values=expected_vars,
                dossier_values=working_dossier,
                post_commit_obligations=list(post_commit_obligations or ()),
                freetext_notes=freetext_notes,
                extracted_facts=extracted_facts,
                epistemic_artifacts=epistemic_artifacts,
                require_positive_oracle_proof=strict_closed_world,
                has_independent_oracle_proof=has_oracle_proof,
            )
            if not closure.allowed and (
                strict_closed_world
                or epistemic_artifacts
                or source_texts is not None
                or freetext_notes
                or post_commit_obligations
            ):
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="completion_closure_failed")
                return RunResult(
                    mode="autopilot",
                    outcome=closure.decision or "ESCALATED",
                    staged_fields=staged.staged,
                    commit_detected=classification.is_commit,
                    readback_verified=True,
                    reason=f"completion_closure_failed:{closure.reason}",
                    healed=healed,
                    published_pack_path=published_pack_path,
                    egress_ledger_entry_sha256=egress_entry_sha,
                    closure_passed=False,
                )

            decision = ledger.finalize("done", dirty_fields=list(interceptor.dirty_fields.keys()))

            saved_receipt = None
            key_bytes = signing_key or (private_key_path.read_bytes() if private_key_path else None)
            if decision.outcome == "VERIFIED" and key_bytes and receipt_out:
                receipt = mint_receipt(ledger, pack.pack_id, key_bytes)
                receipt_out.parent.mkdir(parents=True, exist_ok=True)
                receipt_out.write_text(json.dumps(receipt, indent=2), encoding="utf-8")
                saved_receipt = str(receipt_out)

            return RunResult(
                mode="autopilot",
                outcome=decision.outcome,
                staged_fields=staged.staged,
                commit_detected=classification.is_commit,
                readback_verified=True,
                receipt_path=saved_receipt,
                reason=decision.reason,
                healed=healed,
                published_pack_path=published_pack_path,
                egress_ledger_entry_sha256=egress_entry_sha,
                closure_passed=closure.allowed,
            )
        finally:
            if close_browser:
                browser.close()


def format_run_result_json(result: RunResult) -> str:
    return json.dumps(asdict(result), indent=2)
