"""Repository fixture composition for the generic record-check proof runner.

Run ``python -m benchmark.record_check_proof scaffold-verifier ... --prove NAME``.
Only this qualification entry point resolves fixture names; core Flow requires
an explicitly injected source and never silently omits a requested proof.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from openadapt_flow.record_check_proof import Fixture, ProofUnavailable
from openadapt_flow.runtime.effects.effect import Effect

# -- MockMed -----------------------------------------------------------------


def _mockmed_serve() -> tuple[str, Optional[Path], Callable[[], None], Any]:
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    return url.rstrip("/"), None, stop, db


def _mockmed_judge(
    before: list[dict[str, Any]], after: list[dict[str, Any]], params: Mapping[str, str]
) -> tuple[bool, str]:
    patient = params.get("patient_id", "p1")
    note = params.get("note", "")
    before_ids = {r["id"] for r in before}
    after_ids = {r["id"] for r in after}
    if any(r["id"] not in after_ids for r in before):
        return False, "collateral_loss"
    new = [r for r in after if r["id"] not in before_ids]
    mine = [r for r in new if str(r.get("patient_id")) == patient]
    if not mine:
        return False, "wrong_record" if new else "absent"
    if len(mine) > 1:
        return False, "duplicate"
    if str(mine[0].get("note")) != note:
        return False, "partial"
    if len(new) > 1:
        return False, "collateral_write"
    return True, "correct"


MOCKMED = Fixture(
    name="mockmed",
    serve=_mockmed_serve,
    reset=lambda db, seed: db.reset(seed_concurrent=seed),
    snapshot=lambda db: [dict(r) for r in db.snapshot()["records"]],
    judge=_mockmed_judge,
    scenarios=(
        ("ok", False),
        ("no_persist", False),
        ("partial", False),
        ("wrong_record", False),
        ("stale", True),
        ("optimistic", False),
        ("session", False),
    ),
    write_path="/api/encounter",
    default_body={"patient_id": "{patient_id}", "type": "Triage", "note": "{note}"},
)


def fixture(name: str) -> Fixture:
    if name == "mockmed":
        return MOCKMED
    if name in ("effect-e2e", "effect_e2e"):
        try:
            from benchmark.effect_e2e.prove_check import FIXTURE
        except ImportError as exc:  # pragma: no cover - wheel installs
            raise ProofUnavailable(
                "the effect-e2e fixture lives in the repository "
                "(packages/flow/benchmark); run from a checkout"
            ) from exc
        return FIXTURE
    raise ProofUnavailable(f"unknown fault fixture {name!r} (mockmed, effect-e2e)")


def prove_live(
    fixture_name: str,
    section: Mapping[str, Any],
    effects: list[Effect],
    params: Mapping[str, str],
    *,
    write_requests: Optional[list[dict[str, Any]]] = None,
    work_dir: Path,
) -> dict[str, Any]:
    """Compatibility composition for the existing repository proof cases."""
    from openadapt_flow.record_check_proof import prove_live as run_proof

    source = fixture(fixture_name)
    return run_proof(
        source.name,
        section,
        effects,
        params,
        write_requests=write_requests,
        work_dir=work_dir,
        proof_source=source,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Bind the requested fixture, then invoke the unchanged normal CLI route."""
    from openadapt_flow.__main__ import build_parser
    from openadapt_flow.__main__ import main as flow_main

    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    if getattr(args, "command", None) != "scaffold-verifier":
        print(
            "REFUSED: this fixture entry point requires scaffold-verifier",
            file=sys.stderr,
        )
        return 2
    requested = getattr(args, "prove", None)
    source = fixture(requested) if requested is not None else None
    if source is not None:
        # The normal parser stores the last --prove value. Append the trusted
        # source name before a literal delimiter without rewriting any existing
        # option value or positional text that merely resembles an option.
        delimiter = raw.index("--") if "--" in raw else len(raw)
        raw[delimiter:delimiter] = ["--prove", source.name]
    return flow_main(raw, record_check_proof_source=source)


if __name__ == "__main__":
    raise SystemExit(main())
