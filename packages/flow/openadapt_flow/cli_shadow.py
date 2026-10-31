"""CLI: ``openadapt-flow shadow`` -- check an agent's writes beside it, never
blocking one. See ``docs/SHADOW_MODE.md``.

    openadapt shadow --mode fhir --upstream http://127.0.0.1:8103 \\
        --contract allergy.yaml --listen 9670 --out shadow-report

Config file (``--config shadow.yaml``, ``openadapt.shadow/v1``)::

    mode: sql
    upstream: http://127.0.0.1:8080
    routes:
      - write: "POST /api/v1/Contact"
        contract: contact.verify.yaml
        params: {email: emailAddress, lname: lastName}
"""

from __future__ import annotations

import argparse
import re
import signal
import sys
import time
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.shadow/v1"


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "shadow",
        help="Check each write an agent makes against the system of record, "
        "without blocking or changing it, and write a report",
    )
    p.add_argument("--config", help=f"{SCHEMA} YAML (flags override it)")
    p.add_argument("--mode", choices=("fhir", "sql"))
    p.add_argument("--upstream", help="the server the agent writes to")
    p.add_argument(
        "--contract",
        action="append",
        default=[],
        help="fhir: an openadapt.ensure-record/v1 contract (repeatable)",
    )
    p.add_argument(
        "--listen", type=int, default=None, help="loopback port (default 9670)"
    )
    p.add_argument("--out", default="shadow-report")
    p.add_argument("--actor", default=None, help="sql: the agent's id in the receipt")
    p.add_argument(
        "--hold-ms",
        type=float,
        default=None,
        help="longest a write waits for the read before it (default 2000)",
    )
    p.add_argument(
        "--quiet-s",
        type=float,
        default=None,
        help="check once the agent has written nothing this long (default 5)",
    )
    p.add_argument(
        "--until-idle",
        type=float,
        default=None,
        metavar="SECONDS",
        help="stop after this long with no write (default: run until Ctrl-C)",
    )
    p.add_argument(
        "--recheck-s",
        type=float,
        default=None,
        help="fhir: check each CONFIRMED write again this long after (default "
        "300); --until-idle waits for the rechecks",
    )
    p.set_defaults(func=cmd_shadow)


def load(args: argparse.Namespace) -> dict[str, Any]:
    import yaml

    from openadapt_flow.shadow import ShadowConfigError

    cfg: dict[str, Any] = {}
    base = Path.cwd()
    if args.config:
        path = Path(args.config)
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if cfg.get("schema_version", SCHEMA) != SCHEMA:
            raise ShadowConfigError(f"{path} is not {SCHEMA}")
        base = path.resolve().parent
    for key in (
        "mode",
        "upstream",
        "listen",
        "actor",
        "hold_ms",
        "quiet_s",
        "recheck_s",
    ):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    if args.contract:
        cfg["contracts"] = list(args.contract)
    cfg["_base"] = base
    if not cfg.get("mode") or not cfg.get("upstream"):
        raise ShadowConfigError("give --mode and --upstream (or a --config with both)")
    return cfg


def build(cfg: dict[str, Any], out: Path) -> Any:
    from openadapt_flow.shadow import Route, Shadow, ShadowConfigError

    base: Path = cfg["_base"]
    contracts, routes = [], []
    if cfg["mode"] == "fhir":
        from openadapt_flow.ensure_record import load_contract

        contracts = [load_contract(base / c) for c in cfg.get("contracts") or []]
    else:
        from openadapt_flow.verifier import Contract

        for i, r in enumerate(cfg.get("routes") or []):
            if not isinstance(r, dict) or not r.get("write") or not r.get("contract"):
                raise ShadowConfigError(f"route {i + 1} needs write and contract")
            contract = Contract.load(base / r["contract"])
            params = dict(r.get("params") or {})
            for name in contract.params:
                params.setdefault(name, name)
            for name, where in params.items():
                if isinstance(where, dict) and set(where) != {"literal"}:
                    raise ShadowConfigError(
                        f"route {i + 1}: {name} is a body path or {{literal: value}}"
                    )
            routes.append(
                Route(
                    re.compile(r["write"]),
                    contract,
                    params,
                    r.get("name") or r["write"],
                )
            )
    return Shadow(
        upstream=str(cfg["upstream"]),
        mode=cfg["mode"],
        contracts=contracts,
        routes=routes,
        actor_id=str(cfg.get("actor") or "agent"),
        out=out,
        listen=("127.0.0.1", int(cfg.get("listen") or 9670)),
        hold_ms=float(cfg.get("hold_ms", 2000)),
        upstream_timeout_s=float(cfg.get("upstream_timeout_s", 300)),
        recheck_s=None
        if cfg.get("recheck_s", 300) is None
        else float(cfg.get("recheck_s", 300)),
        quiet_s=float(cfg.get("quiet_s", 5)),
    )


def cmd_shadow(args: argparse.Namespace) -> int:
    from openadapt_flow.shadow import ShadowConfigError
    from openadapt_flow.shadow_report import write_report

    out = Path(args.out)
    try:
        shadow = build(load(args), out).start()
    except (ShadowConfigError, OSError, ValueError) as exc:
        print(f"shadow: {exc}", file=sys.stderr)
        return 2
    print(
        f"Shadow is listening at {shadow.url}; point the agent there instead of "
        f"{args.upstream or 'the server'}. Ctrl-C ends the run.",
        flush=True,
    )
    stopping = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    try:
        while not stopping:
            time.sleep(0.5)
            if (
                args.until_idle is not None
                and shadow.idle_s() >= args.until_idle
                and not shadow.pending_rechecks()
            ):
                break
    except KeyboardInterrupt:
        pass
    print("Checking the last writes...", flush=True)
    shadow.stop()
    doc = shadow.results()
    page = write_report(doc, out)
    n = doc["counts"]
    print(
        f"{doc['writes_seen']} writes, {doc['checks']} checks: {n['CONFIRMED']} "
        f"confirmed, {n['REFUTED']} refuted, {n['UNCERTAIN']} uncertain"
        + (f", {n['PENDING']} never checked" if n["PENDING"] else "")
        + (
            f"; {doc['late_refuted']} refuted on recheck"
            if doc.get("late_refuted")
            else ""
        )
        + ". "
        f"Report: {page}"
    )
    return 0
