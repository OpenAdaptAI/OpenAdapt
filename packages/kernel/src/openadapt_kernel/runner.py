"""Live CDP / Playwright runner for Draft-to-Commit and Verified Autopilot modes."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Literal, Optional

from openadapt_kernel.copilot import PortalPack, mint_receipt
from openadapt_kernel.interceptor import CommitInterceptor
from openadapt_kernel.ledger import ObligationLedger, ReadBackProof
from openadapt_kernel.macro import stage_form

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


def _token_boundary_matches(fields: Dict[str, str], text: str) -> List[str]:
    compact = " ".join((text or "").split())
    cited: List[str] = []
    for val in fields.values():
        v = str(val).strip()
        if not v:
            continue
        if re.search(rf"(?<!\w){re.escape(v)}(?!\w)", compact):
            cited.append(v)
    return cited


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
    private_key_path: Optional[Path] = None,
    receipt_out: Optional[Path] = None,
    episode_id: str = "ep-live-1",
    headless: bool = True,
) -> RunResult:
    """Execute a PortalPack in Draft-to-Commit (stop before save) or Verified Autopilot mode."""
    from playwright.sync_api import sync_playwright

    pack = _load_portal_pack(pack_path)
    bindings = _compute_bindings(pack, dossier)
    opaque_fields = sorted(pack.provenance_map.keys())

    ledger = ObligationLedger(episode_id=episode_id)
    ledger.add_obligation("primary_commit", f"Execute {pack.pack_id}", target_system=pack.portal_origin)
    for k, v in dossier.items():
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
            if not staged.ok:
                ledger.finalize("error", dirty_fields=list(interceptor.dirty_fields.keys()), error_detail="stage_mismatch")
                return RunResult(
                    mode=mode,
                    outcome="ESCALATED",
                    staged_fields=staged.staged,
                    reason=f"stage_failed(unmatched={staged.unmatched}, mismatched={staged.mismatched})",
                )

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

            interceptor.begin_step()
            page.locator(commit_selector).first.click()
            page.wait_for_timeout(300)
            classification = interceptor.classify_step(form_submitted=True)

            # Independent fresh-reload read-back against persisted server state
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(200)
            body_text = str(page.evaluate("() => (document.body && document.body.innerText) || ''") or "")
            expected_vars = {k: bindings[k] for k in pack.variable_fields if k in bindings}
            cited = _token_boundary_matches(expected_vars, body_text)
            all_verified = bool(expected_vars) and len(cited) == len(expected_vars)

            if not all_verified:
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
                )

            interceptor.clear_dirty_fields()
            ledger.mark_committed(
                "primary_commit",
                ReadBackProof(
                    channel="fresh_reload",
                    url_or_locator=str(page.url),
                    cited_substrings=cited,
                ),
            )
            decision = ledger.finalize("done", dirty_fields=list(interceptor.dirty_fields.keys()))

            saved_receipt: Optional[str] = None
            if decision.outcome == "VERIFIED" and private_key_path and receipt_out:
                receipt = mint_receipt(ledger, pack.pack_id, private_key_path.read_bytes())
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
            )
        finally:
            if close_browser:
                browser.close()


def format_run_result_json(result: RunResult) -> str:
    return json.dumps(asdict(result), indent=2)
