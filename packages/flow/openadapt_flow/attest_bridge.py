"""Opt-in bridge to the ``openadapt-attest`` proof sidecar.

``openadapt-attest`` is a SEPARATE, privately distributed package: after a run
it verifies the run's claimed effect against the system of record and writes a
SIGNED receipt. The public engine must never hard-depend on it, so this module
holds the entire coupling behind two rules:

* **Lazy import.** ``openadapt_attest`` is imported only inside the hook
  bodies, only after the operator opted in by configuring a contract. When the
  package is not installed the hooks print a single notice and do nothing.
* **Wrap, never rewrite.** Both hooks are fully best-effort: every exception
  is caught and printed as a warning, so attestation can NEVER change the
  run's outcome, its report, or its exit code (mirrors
  ``_maybe_report_break`` / ``_maybe_report_run`` in ``__main__``).

Opt-in is a contract path from the CLI (``--attest-contract``) or the
environment (``OPENADAPT_FLOW_ATTEST_CONTRACT``); the CLI wins. With no
contract configured everything here is a silent no-op.

Artifacts (all inside the run directory, names chosen NOT to collide with the
engine's own unsigned ``receipt.json``):

* ``attest_pre_state.json`` — pre-actuation system-of-record snapshot,
  captured by :func:`maybe_capture_pre_state` (baseline for delta-style
  checks such as ``count_new_only``).
* ``attest_claim.json`` / ``attest_receipt.json`` — written by the sidecar's
  ``check_flow_run`` from :func:`maybe_attest_run`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

CONTRACT_ENV = "OPENADAPT_FLOW_ATTEST_CONTRACT"
SIGN_KEY_ENV = "OPENADAPT_FLOW_ATTEST_SIGN_KEY"
AUDIT_LOG_ENV = "OPENADAPT_FLOW_ATTEST_AUDIT_LOG"
PRE_STATE_ENV = "OPENADAPT_FLOW_ATTEST_PRE_STATE"

PRE_STATE_FILENAME = "attest_pre_state.json"
CLAIM_FILENAME = "attest_claim.json"
RECEIPT_FILENAME = "attest_receipt.json"

_NOT_INSTALLED = "attest: openadapt-attest is not installed; skipping"


@dataclass(frozen=True)
class AttestConfig:
    """Resolved attest wiring for one invocation (CLI over environment)."""

    contract: Optional[Path]
    sign_key: Optional[Path]
    audit_log: Optional[Path]
    pre_state: Optional[Path]

    @property
    def enabled(self) -> bool:
        return self.contract is not None


def _pick(args: Any, attr: str, env: str) -> Optional[Path]:
    """One option: an explicit CLI flag wins over its environment fallback."""
    cli = getattr(args, attr, None) if args is not None else None
    if cli:
        return Path(str(cli))
    value = os.environ.get(env, "").strip()
    return Path(value) if value else None


def resolve_attest_config(args: Any = None) -> AttestConfig:
    """Resolve the four attest options from ``args`` and the environment."""
    return AttestConfig(
        contract=_pick(args, "attest_contract", CONTRACT_ENV),
        sign_key=_pick(args, "attest_sign_key", SIGN_KEY_ENV),
        audit_log=_pick(args, "attest_audit_log", AUDIT_LOG_ENV),
        pre_state=_pick(args, "attest_pre_state", PRE_STATE_ENV),
    )


def maybe_capture_pre_state(run_dir: Path, args: Any = None) -> None:
    """Opt-in pre-actuation snapshot: write ``attest_pre_state.json``.

    Fires only when an attest contract is configured AND no explicit
    pre-state path was given (an explicit ``--attest-pre-state`` means the
    operator already has a baseline — capture would overwrite intent).
    Fully best-effort: any failure (including an absent ``openadapt_attest``)
    prints one line and the run proceeds untouched.
    """
    config = resolve_attest_config(args)
    if not config.enabled or config.pre_state is not None:
        return
    try:
        from openadapt_attest.check import capture_snapshot
    except ModuleNotFoundError:
        print(f"{_NOT_INSTALLED} pre-state snapshot")
        return
    except Exception as e:  # noqa: BLE001 — a proof hook must never fail a run
        print(f"(attest pre-state capture skipped: {e})")
        return
    try:
        snapshot = capture_snapshot(config.contract)
        path = Path(run_dir) / PRE_STATE_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(snapshot.model_dump_json() + "\n", encoding="utf-8")
        reachable = (
            ""
            if getattr(snapshot, "reachable", True)
            else " (system of record UNREACHABLE)"
        )
        print(f"attest: pre-state snapshot written to {path}{reachable}")
    except Exception as e:  # noqa: BLE001 — a proof hook must never fail a run
        print(f"(attest pre-state capture skipped: {e})")


def maybe_attest_run(run_dir: Path, report: Any, args: Any = None) -> None:
    """Opt-in post-run hook: verify the run's claimed effect, write a receipt.

    When a contract is configured, hand the finished run directory to the
    sidecar's ``check_flow_run`` (it reads ``report.json``, auto-uses
    ``attest_pre_state.json`` when present, and writes ``attest_claim.json``
    plus a signed ``attest_receipt.json``), then print a compact summary.
    Fully best-effort — every exception is caught so the hook NEVER changes
    the run's outcome, report, or exit code.
    """
    del report  # the sidecar reads <run_dir>/report.json itself
    config = resolve_attest_config(args)
    if not config.enabled:
        return
    try:
        from openadapt_attest.flow_run import check_flow_run
    except ModuleNotFoundError:
        print(f"{_NOT_INSTALLED} receipt")
        return
    except Exception as e:  # noqa: BLE001 — a proof hook must never fail a run
        print(f"(attest receipt skipped: {e})")
        return
    try:
        result = check_flow_run(
            Path(run_dir),
            config.contract,
            sign_key=config.sign_key,
            audit_log=config.audit_log,
            pre_state_path=config.pre_state,
        )
        verdict = getattr(result.verdict, "value", result.verdict)
        tier = getattr(result, "tier", None)
        tier_text = f"evidence tier {tier}" if tier is not None else "no evidence tier"
        print(
            f"attest: verdict {verdict} ({tier_text}); "
            f"receipt {Path(run_dir) / RECEIPT_FILENAME}"
        )
    except Exception as e:  # noqa: BLE001 — a proof hook must never fail a run
        print(f"(attest receipt skipped: {e})")
