"""Publish and resume a live form reconciler through the local native-host mailbox."""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Optional

from openadapt_flow.copilot_bridge import CopilotSessionBridge, CopilotViewState
from openadapt_flow.copilot_protocol import CopilotRunBinding
from openadapt_flow.form_tx.receipt import verify_form_tx_receipt
from openadapt_flow.form_tx.reconciler import FormReconciler, ReconcilerSession
from openadapt_flow.form_tx.types import FormTxOutcome, FormTxResult


class CopilotFormRuntime:
    """Single-owner driver for an already authorized, authenticated form session.

    The caller keeps this object on the browser's owning thread and calls
    ``resume_pending`` while awaiting operator input. The native host may run
    in another process. Operator identity is a local assertion, not login proof.
    Restarting creates a new nonce; an interrupted decision is never replayed.
    """

    def __init__(
        self,
        reconciler: FormReconciler,
        session: ReconcilerSession,
        session_dir: Path,
        *,
        operator_id: str,
    ) -> None:
        if not operator_id.strip():
            raise ValueError("an explicit local operator identity is required")
        self.reconciler = reconciler
        self.session = session
        self.bridge = CopilotSessionBridge(session_dir)
        self._nonce = secrets.token_hex(32)
        self._operator = operator_id
        self._generation = 0
        self._result: Optional[FormTxResult] = None
        self._binding: Optional[CopilotRunBinding] = None

    def _publish(self, result: FormTxResult) -> FormTxResult:
        self._generation += 1
        binding = CopilotRunBinding(
            run_id=self.session.run_id,
            session_nonce=self._nonce,
            generation=self._generation,
            pack_digest=self.session.pack.digest(),
            dossier_digest=self.session.dossier.digest(),
            operator_id=self._operator,
            expires_at_unix=int(time.time()) + 600,
        )
        receipt_verified = bool(
            result.receipt is not None and verify_form_tx_receipt(result.receipt)
        )
        self.bridge.publish_owned_state(
            CopilotViewState(
                run_id=result.run_id,
                binding=binding,
                outcome=result.outcome,
                status_line=result.what_happened,
                fields_filled_auto=result.fields_filled_auto,
                total_bound_fields=len(self.session.pack.bindings),
                subroutines_completed=result.subroutines_completed,
                halts=result.halts,
                outputs=result.outputs,
                receipt_verified=receipt_verified,
                receipt_id=(
                    result.receipt.payload.receipt_id
                    if receipt_verified and result.receipt
                    else None
                ),
            ),
            expected_binding=self._binding,
        )
        self._binding, self._result = binding, result
        return result

    def start(self) -> FormTxResult:
        if self._result is not None:
            raise ValueError("copilot runtime already started")
        # Publish a no-action state first; a missing/unsafe mailbox fails before actuation.
        initial = FormTxResult(
            run_id=self.session.run_id,
            request_id=self.session.dossier.request_id,
            outcome=FormTxOutcome.HALTED_NEEDS_HUMAN,
            safe_to_retry=True,
            what_happened="Starting local form session",
        )
        self._publish(initial)
        return self._publish(self.reconciler.run_until_halt_or_done(self.session))

    def resume_pending(self) -> FormTxResult:
        if self._binding is None or self._result is None:
            raise ValueError("copilot runtime has not started")
        # Check the published state has not been replaced by another runner.
        state = self.bridge.read_state()
        if state is None or state.binding != self._binding:
            raise ValueError("copilot session ownership or generation changed")
        if self._binding.expires_at_unix <= time.time():
            return self._publish(self._result)
        if not self._result.halts:
            return self._result
        item = self.bridge.claim_bound_decision(self._binding)
        if item is None:
            return self._result
        # Claim is durable before any application mutation. Invalidate its view
        # before applying, so a second click cannot queue a new old-generation action.
        self._publish(self._result.model_copy(update={"halts": ()}))
        try:
            return self._publish(
                self.reconciler.apply_human_resolution(self.session, item.decision)
            )
        except BaseException:
            # Do not restore a pre-action view after an uncertain failure.
            self._result = None
            raise


__all__ = ["CopilotFormRuntime"]


def main(argv: Optional[list[str]] = None) -> int:
    """Attach to one explicitly selected existing page; never start a browser."""
    import argparse
    import json
    import time
    from urllib.parse import urlsplit

    from openadapt_flow.form_tx.types import FormPack, InputDossier, UIProfileSpec

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--pack", required=True, type=Path)
    parser.add_argument("--dossier", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--session-dir", required=True, type=Path)
    parser.add_argument("--operator-id", required=True)
    parser.add_argument("--cdp-endpoint", required=True)
    parser.add_argument("--page-url", required=True)
    parser.add_argument("--operator-timeout-seconds", type=int, default=3600)
    args = parser.parse_args(argv)
    endpoint = urlsplit(args.cdp_endpoint)
    if (
        endpoint.scheme != "http"
        or endpoint.hostname not in {"localhost", "127.0.0.1", "::1"}
        or endpoint.username
        or endpoint.password
        or endpoint.query
        or endpoint.fragment
        or endpoint.path not in {"", "/"}
        or endpoint.port is None
        or not 1 <= args.operator_timeout_seconds <= 86400
    ):
        parser.error(
            "CDP requires a credential-free loopback HTTP endpoint and a bounded timeout"
        )
    pack = FormPack.model_validate_json(args.pack.read_text())
    dossier = InputDossier.model_validate_json(args.dossier.read_text())
    profile = UIProfileSpec.model_validate_json(args.profile.read_text())
    if pack.ui_profile_id != profile.profile_id:
        parser.error("pack and profile identity differ")
    from playwright.sync_api import sync_playwright

    from openadapt_flow.form_tx.playwright_surface import PlaywrightFormSurface

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp_endpoint)
        pages = [
            page
            for context in browser.contexts
            for page in context.pages
            if page.url == args.page_url
        ]
        if len(pages) != 1:
            parser.error("page URL must select exactly one existing page")
        page = pages[0]
        surface = PlaywrightFormSurface(page, profile)
        reconciler = FormReconciler(surface)
        session = reconciler.create_session(pack, dossier)
        runtime = CopilotFormRuntime(
            reconciler, session, args.session_dir, operator_id=args.operator_id
        )
        result = runtime.start()
        deadline = time.monotonic() + args.operator_timeout_seconds
        while result.halts and time.monotonic() < deadline:
            page.wait_for_timeout(200)
            result = runtime.resume_pending()
        if result.halts:
            # Expire the operator capability before this owner stops polling.
            runtime._publish(result.model_copy(update={"halts": ()}))
        print(json.dumps({"run_id": result.run_id, "outcome": result.outcome.value}))
        return 0 if result.outcome == FormTxOutcome.VERIFIED_DONE else 2


if __name__ == "__main__":
    raise SystemExit(main())
