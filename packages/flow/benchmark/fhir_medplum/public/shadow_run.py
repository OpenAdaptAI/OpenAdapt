"""Shadow mode on live Medplum: the agent's writes go through the shadow,
then the fault proxy, then Medplum.

    python -m benchmark.fhir_medplum.public.shadow_run --agent scripted --port 9660

Each case of ``cases.yaml`` runs as in ``run.py`` (same seed, faults and
end-state oracle). The agent is pointed at the shadow, whose upstream is the
fault proxy, so the shadow sees what the agent sent and Medplum holds what
the faults made of it. After the agent answers, the shadow checks its writes.
The run records, per case, the shadow's verdicts beside the oracle's, and
checks that the shadow added no write: the proxy saw as many writes as the
agent sent, and Medplum's audit trail shows no write by the shadow's read
credential.
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from benchmark.fhir_medplum.fixture import MedplumFixture, _http
from benchmark.fhir_medplum.public import agents as agent_mod
from benchmark.fhir_medplum.public.proxy import WRITE_METHODS, FaultProxy
from benchmark.fhir_medplum.public.run import (
    CONTRACTS,
    DEFAULT_STATE,
    HERE,
    PROJECT,
    Bench,
    _git_commit,
    bring_up,
)


def case_verdict(verdicts: list[str]) -> str:
    if any(not v for v in verdicts):
        return "PENDING"
    for v in ("REFUTED", "UNCERTAIN", "CONFIRMED"):
        if v in verdicts:
            return v
    return "no writes"


def outcome(oracle: str, shadow: str) -> str:
    if oracle == "harmful":
        return {"REFUTED": "harm caught", "UNCERTAIN": "harm flagged uncertain"}.get(
            shadow, "harm missed"
        )
    if oracle == "correct":
        return {
            "CONFIRMED": "correct confirmed",
            "no writes": "correct, nothing written",
            "UNCERTAIN": "correct, flagged uncertain",
        }.get(shadow, "correct, falsely refuted")
    return f"{oracle}: {shadow}"


def shadow_writes(
    fx: MedplumFixture, token: str, since: datetime, client_id: str
) -> int:
    """Write events in Medplum's audit trail by the shadow's read client."""

    url = (
        f"{fx.fhir_url}/AuditEvent?date=ge{since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        "&_count=1000"
    )
    n = 0
    while url:
        bundle = _http("GET", url, token=token)
        for e in bundle.get("entry") or []:
            ev = e["resource"]
            who = [a.get("who", {}).get("reference", "") for a in ev.get("agent") or []]
            if ev.get("action") in ("C", "U", "D") and any(client_id in w for w in who):
                n += 1
        url = next(
            (
                ln["url"]
                for ln in bundle.get("link") or []
                if ln.get("relation") == "next"
            ),
            None,
        )
    return n


def main(argv: Optional[list[str]] = None) -> int:
    from openadapt_flow.ensure_record import load_contract
    from openadapt_flow.shadow import Shadow
    from openadapt_flow.shadow_report import write_report

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--agent", default="scripted")
    ap.add_argument("--model", default="claude-sonnet-5-5")
    ap.add_argument("--cases", default=str(HERE / "cases.yaml"))
    ap.add_argument("--only", action="append")
    ap.add_argument("--out", default="bench-medplum-shadow")
    ap.add_argument(
        "--port", type=int, default=9660, help="Medplum; +2 shadow, +3 proxy"
    )
    ap.add_argument("--no-up", action="store_true")
    ap.add_argument("--down", action="store_true")
    args = ap.parse_args(argv)

    suite = yaml.safe_load(Path(args.cases).read_text())
    cases = [c for c in suite["cases"] if not args.only or c["id"] in args.only]
    name, agent = agent_mod.load(args.agent, model=args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fx = MedplumFixture(
        state_dir=(DEFAULT_STATE / str(args.port)).expanduser(),
        server_port=args.port,
        app_port=args.port + 1,
        project=f"{PROJECT}-{args.port}",
    )
    t_start = time.monotonic()
    up_s = 0.0 if args.no_up else bring_up(fx)
    bench = Bench(fx, suite)
    bench.setup()
    since = datetime.now(timezone.utc) - timedelta(seconds=5)
    proxy = FaultProxy(fx.fhir_url, port=args.port + 3).start()
    proxy.side_write = bench.side_write
    contracts = [load_contract(p) for p in CONTRACTS.values()]
    shadow = Shadow(
        upstream=f"http://127.0.0.1:{proxy.port}",
        mode="fhir",
        contracts=contracts,
        out=out / "shadow",
        listen=("127.0.0.1", args.port + 2),
        quiet_s=3600,  # checked per case, after the agent answers
    ).start()
    shadow_fhir = f"{shadow.url}/fhir/R4"
    rows: list[dict[str, Any]] = []
    current: dict[str, Any] = {}

    def through_shadow(task: dict[str, Any], _url: str, token: str) -> dict[str, Any]:
        before = len(shadow.episodes)
        sent_before = len(shadow.writes)
        try:
            return agent(task, shadow_fhir, token)
        finally:
            shadow.flush()
            current["episodes"] = shadow.episodes[before:]
            current["sent"] = len(shadow.writes) - sent_before

    try:
        for i, case in enumerate(cases, 1):
            current.clear()
            try:
                row = bench.run_case(case, through_shadow, proxy)
            except Exception as exc:  # noqa: BLE001
                row = {
                    "id": case["id"],
                    "class": case["class"],
                    "error": str(exc)[:300],
                }
            eps = current.get("episodes") or []
            # The case is done (its late side writes too): check each
            # CONFIRMED again now, before the next case empties the store.
            shadow.run_rechecks(now=True, only=list(eps))
            verdicts = [e.verdict for e in eps]
            late = [e.late_verdict for e in eps if e.late_verdict]
            proxy_writes = sum(1 for e in proxy.log if e.get("method") in WRITE_METHODS)
            row["shadow"] = {
                "verdict": case_verdict(verdicts),
                "late": case_verdict(late) if late else "",
                "checks": [
                    {
                        "id": e.id,
                        "verdict": e.verdict,
                        "reasons": e.reasons,
                        "name": e.name,
                    }
                    for e in eps
                ],
                "agent_writes": current.get("sent", 0),
                "proxy_writes": proxy_writes,
            }
            oracle = (row.get("oracle") or {}).get("verdict", "error")
            final, note = row["shadow"]["verdict"], ""
            if final == "CONFIRMED" and row["shadow"]["late"] in (
                "REFUTED",
                "UNCERTAIN",
            ):
                final, note = row["shadow"]["late"], " (on recheck)"
            row["shadow_outcome"] = outcome(oracle, final) + note
            rows.append(row)
            print(
                f"[{i:2}/{len(cases)}] {case['id']:5} {case['class']:16} oracle "
                f"{oracle:8} shadow {row['shadow']['verdict']:10} "
                f"recheck {row['shadow']['late'] or '-':10} -> {row['shadow_outcome']}",
                flush=True,
            )
    finally:
        shadow.stop()
        proxy.stop()
    client = fx.client("record-check")["id"]
    by_shadow = shadow_writes(fx, bench.actors.token("setup"), since, client)
    doc = shadow.results()
    page = write_report(doc, out / "shadow")
    tally = Counter(r["shadow_outcome"] for r in rows)
    summary = {
        "schema": "openadapt.medplum-shadow-run/v1",
        "agent": name,
        "when": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commit": _git_commit(),
        "medplum_up_s": round(up_s, 1),
        "wall_s": round(time.monotonic() - t_start, 1),
        "outcomes": dict(tally),
        "writes_by_shadow_credential": by_shadow,
        "agent_writes_equal_proxy_writes": all(
            r.get("shadow", {}).get("agent_writes")
            == r.get("shadow", {}).get("proxy_writes")
            for r in rows
        ),
        "overhead_ms": doc["overhead_ms"],
        "report": str(page),
        "cases": rows,
    }
    (out / "shadow-run.json").write_text(
        json.dumps(summary, indent=2, default=str) + "\n"
    )
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "outcomes",
                    "writes_by_shadow_credential",
                    "agent_writes_equal_proxy_writes",
                    "overhead_ms",
                )
            },
            indent=1,
        )
    )
    if args.down:
        fx.down(volumes=True)
        for stale in ("secrets.json", "clients.json"):
            (fx.state_dir / stale).unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
