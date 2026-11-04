"""Run the pre-registered UI second-read faults on live OpenEMR and Dolibarr.

See ``PREREG.md`` (committed before the first run). The acting session is a
headless Playwright browser that writes through the app's forms. The verifier
is the shipped ``ui_readback`` verifier with its real Playwright driver; its
deployment has no REST, SQL or FHIR section, and its session aborts requests
to the apps' API paths. SQL is used only to seed records and plant faults.

Credentials and hosts come from the environment (``run_live.sh`` exports them
from the local fixture compose files); nothing secret is in this file.

    python benchmark/ui_second_read/live.py --app dolibarr --out results/
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from openadapt_flow.runtime.effects import Effect, EffectKind, ValueExpr
from openadapt_flow.runtime.effects.ui_readback import (
    PlaywrightReadbackDriver,
    UiReadbackSpec,
    build_ui_readback_verifier,
    settle_ui_readback,
)

API_PATHS = ("/apis/", "/api/")


class NoApiDriver(PlaywrightReadbackDriver):
    """The shipped driver, plus a guard: no request to an API path."""

    def open(self, target: Any, username: str, password: str) -> Any:
        ctx = super().open(target, username, password)
        guard = ctx._guard  # type: ignore[attr-defined]

        def no_api(route: Any, request: Any) -> None:
            if any(p in str(request.url) for p in API_PATHS):
                NoApiDriver.api_blocked += 1
                route.abort("blockedbyclient")
                return
            guard(route, request)

        ctx._guard = no_api  # type: ignore[attr-defined]
        return ctx

    api_blocked = 0


class ActingBackend:
    """What the verifier may see of the acting session: its URL."""

    def __init__(self, page: Any) -> None:
        self._page = page

    def url(self) -> str:
        return str(self._page.url)


@dataclass
class Case:
    id: str
    fault: str
    predicted: str
    run: Callable[["App"], dict[str, Any]]


@dataclass
class Outcome:
    case: str
    fault: str
    predicted: str
    verdict: str = ""
    final: Optional[str] = None
    reason: str = ""
    read_evidence: dict[str, Any] = field(default_factory=dict)
    seconds: float = 0.0
    note: str = ""


def verify(
    spec: UiReadbackSpec,
    params: dict[str, object],
    effect: Effect,
    act: Callable[[], Any],
    acting_page: Any,
) -> Any:
    """Pre-read, act, then read back through the separate session."""

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
    return verdict, time.monotonic() - started


def settle(spec: UiReadbackSpec) -> tuple[list[Any], float]:
    started = time.monotonic()
    out = settle_ui_readback(spec, driver=NoApiDriver())
    return out, time.monotonic() - started


def field_effect(match: dict[str, str], name: str, value: str) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={k: ValueExpr(literal=v) for k, v in match.items()},
        field=name,
        value=ValueExpr(literal=value),
        timeout_s=3.0,
    )


def written_effect(match: dict[str, str]) -> Effect:
    return Effect(
        kind=EffectKind.RECORD_WRITTEN,
        match={k: ValueExpr(literal=v) for k, v in match.items()},
        expected_count=1,
        forbid_collateral_loss=False,
        timeout_s=3.0,
    )


def token(n: int = 6) -> str:
    return secrets.token_hex(n // 2 + 1)[:n].upper()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", choices=["openemr", "dolibarr"], required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args(argv)
    if args.app == "openemr":
        from benchmark.ui_second_read import app_openemr as mod
    else:
        from benchmark.ui_second_read import app_dolibarr as mod
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    with mod.App(out) as app:
        for case in mod.CASES:
            if args.only and case.id not in args.only:
                continue
            started = time.monotonic()
            try:
                result = case.run(app)
            except Exception as exc:  # noqa: BLE001 - a harness error is a row
                result = {"verdict": "harness_error", "reason": repr(exc)[:400]}
            row = {
                "app": args.app,
                "case": case.id,
                "fault": case.fault,
                "predicted": case.predicted,
                "wall_seconds": round(time.monotonic() - started, 2),
                **result,
            }
            rows.append(row)
            print(json.dumps(row)[:600], flush=True)
    rows.append({"api_requests_blocked": NoApiDriver.api_blocked})
    (out / f"{args.app}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
