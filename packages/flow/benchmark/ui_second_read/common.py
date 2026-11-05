"""Shared plumbing for the live UI second-read cases (see PREREG.md)."""

from __future__ import annotations

import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from benchmark.lab_chaos.chaos import active_profile, chaos_url
from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.effects.deferred_report import PendingLedger
from openadapt_flow.runtime.effects.ui_readback import (
    PlaywrightReadbackDriver,
    UiReadbackSpec,
    build_ui_readback_verifier,
    settle_ui_readback,
)

#: The verifier's session may not touch an API path: the read comes from
#: HTML screens only.
API_PATHS = ("/apis/", "/api/")


class NoApiDriver(PlaywrightReadbackDriver):
    """The shipped driver, plus a guard that aborts API requests."""

    blocked = 0

    def open(self, target: Any, username: str, password: str) -> Any:
        session = super().open(target, username, password)
        guard = session._guard  # type: ignore[attr-defined]

        def no_api(route: Any, request: Any) -> None:
            if any(p in str(request.url) for p in API_PATHS):
                NoApiDriver.blocked += 1
                route.abort("blockedbyclient")
                return
            guard(route, request)

        session._guard = no_api  # type: ignore[attr-defined]
        return session


class ActingBackend:
    """What the verifier sees of the acting session: its page URL."""

    def __init__(self, page: Any) -> None:
        self._page = page

    def url(self) -> str:
        return str(self._page.url)


def base(url: str) -> str:
    host = urlsplit(url).hostname
    if host is None or host.rstrip(".").endswith(".invalid"):
        raise ValueError(
            "configure the synthetic fixture URL before running the benchmark"
        )
    return chaos_url(url)


def token(n: int = 6) -> str:
    return secrets.token_hex(n)[:n].upper()


def written(match: dict[str, str]) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: ValueExpr(literal=v) for k, v in match.items()},
        expected_count=1,
        forbid_collateral_loss=False,
        timeout_s=3.0,
    )


@dataclass
class Read:
    verdict: str
    reason: str
    evidence: dict[str, Any]
    seconds: float
    deferred: Optional[str]


def check(
    spec: UiReadbackSpec,
    params: dict[str, object],
    effect: Effect,
    act: Callable[[], None],
    acting_page: Any,
) -> Read:
    """Pre-read, act, read back in a separate session."""

    verifier = build_ui_readback_verifier(
        spec,
        params,
        timeout_s=5.0,
        driver=NoApiDriver(),
        backend=ActingBackend(acting_page),
    )
    before = verifier.capture_pre_state()
    act()
    started = time.monotonic()
    verdict = verifier.verify(effect, before)
    return Read(
        verdict.verdict.value,
        verdict.reason[:400],
        verdict.read_evidence or {},
        round(time.monotonic() - started, 2),
        verdict.deferred_check,
    )


def settle(spec: UiReadbackSpec) -> tuple[list[Any], float]:
    started = time.monotonic()
    out = settle_ui_readback(spec, driver=NoApiDriver())
    return out, round(time.monotonic() - started, 2)


def final_section(**extra: Any) -> dict[str, Any]:
    ledger = tempfile.mkdtemp(prefix="ui2read-ledger-")
    return {"ledger": ledger, "after_s": 0, "deadline_hours": 24, **extra}


def ledger_state(spec: UiReadbackSpec, entry_id: str) -> Optional[str]:
    entry = PendingLedger(spec.final.ledger).get(entry_id) if spec.final else None
    return entry.state if entry else None


def env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise SystemExit(f"set {name} (see run_live.sh)")
    return value


def profile() -> str:
    return active_profile() or "direct"
