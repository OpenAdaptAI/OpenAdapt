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

Database mode (``mode: db``) has no upstream: the agent announces each action
(``POST /begin``, ``POST /end``) and each contract needs ``frame.database``::

    mode: db
    actor: "7"
    audit_sql: "SELECT id FROM audit_log WHERE user_id = %(actor)s"
    actions:
      - name: create_patient
        contract: patient.verify.yaml
        params: {lname: last_name}
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
        allow_abbrev=False,
    )
    p.add_argument("--config", help=f"{SCHEMA} YAML (flags override it)")
    p.add_argument(
        "--mode",
        choices=("fhir", "sql", "db"),
        help="db: no proxy; the agent announces each action (POST /begin, /end)",
    )
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
        "300; 0 turns it off); --until-idle waits for the rechecks",
    )
    p.add_argument(
        "--guard",
        action="store_true",
        default=None,
        help="db: refuse an action that can't be checked or that follows one not "
        "CONFIRMED, and answer /end with the verdict",
    )
    p.add_argument(
        "--receipts",
        action="store_true",
        default=None,
        help="sign each verdict as a write receipt under OUT/receipts "
        "(docs/WRITE_RECEIPTS.md)",
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
        "receipts",
        "guard",
    ):
        value = getattr(args, key, None)
        if value is not None:
            cfg[key] = value
    if args.contract:
        cfg["contracts"] = list(args.contract)
    cfg["_base"] = base
    if cfg.get("mode") == "db":
        if cfg.get("upstream"):
            raise ShadowConfigError(
                "db mode has no upstream: the agent announces actions"
            )
    elif not cfg.get("mode") or not cfg.get("upstream"):
        raise ShadowConfigError("give --mode and --upstream (or a --config with both)")
    if cfg.get("guard") and cfg.get("mode") != "db":
        raise ShadowConfigError("--guard applies to db mode")
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

        db = cfg["mode"] == "db"
        key = "name" if db else "write"
        for i, r in enumerate(cfg.get("actions" if db else "routes") or []):
            if not isinstance(r, dict) or not r.get(key) or not r.get("contract"):
                raise ShadowConfigError(
                    f"{'action' if db else 'route'} {i + 1} needs {key} and contract"
                )
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
                    None if db else re.compile(r["write"]),  # type: ignore[arg-type]
                    contract,
                    params,
                    r.get("name") or r["write"],
                )
            )
        if db:
            from openadapt_flow.shadow_db import DatabaseShadow

            if not routes:
                raise ShadowConfigError("db mode needs at least one action")
            if cfg.get("audit_sql") and not cfg.get("actor"):
                raise ShadowConfigError(
                    "audit_sql needs actor: the agent's own user id in the app"
                )
            return DatabaseShadow(
                routes=routes,
                actor_id=str(cfg.get("actor") or "agent"),
                out=out,
                listen=("127.0.0.1", int(cfg.get("listen") or 9670)),
                hold_ms=float(cfg.get("hold_ms", 30000)),
                quiet_s=float(cfg.get("quiet_s", 5)),
                receipts=bool(cfg.get("receipts", False)),
                guard=bool(cfg.get("guard", False)),
                audit_sql=cfg.get("audit_sql"),
                max_action_s=float(cfg.get("max_action_s", 600)),
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
        recheck_s=_recheck_s(cfg.get("recheck_s", 300)),
        quiet_s=float(cfg.get("quiet_s", 5)),
        receipts=bool(cfg.get("receipts", False)),
    )


def _recheck_s(value: Any) -> Any:
    """``None`` or 0 turns the fhir recheck off; a negative wait is an error."""

    from openadapt_flow.shadow import ShadowConfigError

    if value is None or float(value) == 0:
        return None
    if float(value) < 0:
        raise ShadowConfigError("recheck_s can't be negative")
    return float(value)


def cmd_shadow(args: argparse.Namespace) -> int:
    from openadapt_flow.shadow import ShadowConfigError
    from openadapt_flow.shadow_report import write_report

    out = Path(args.out)
    try:
        shadow = build(load(args), out).start()
    except (ShadowConfigError, OSError, ValueError) as exc:
        print(f"shadow: {exc}", file=sys.stderr)
        return 2
    if shadow.proxy:
        print(
            f"Shadow is listening at {shadow.url}; point the agent there instead of "
            f"{args.upstream or 'the server'}. Ctrl-C ends the run.",
            flush=True,
        )
    else:
        print(
            f"Shadow is watching {shadow.target()}. The agent announces each "
            f"action: POST {shadow.url}/begin and {shadow.url}/end. Ctrl-C ends "
            "the run.",
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
        f"{doc['writes_seen']} {'writes' if shadow.proxy else 'actions'}, {doc['checks']} checks: {n['CONFIRMED']} "
        f"confirmed, {n['REFUTED']} refuted, {n['UNCERTAIN']} uncertain"
        + (f", {n['PENDING']} never checked" if n["PENDING"] else "")
        + "".join(
            f"; {doc[k]} {label}"
            for k, label in (
                ("late_refuted", "refuted on recheck"),
                ("late_uncertain", "uncertain on recheck"),
                ("rechecks_pending", "confirmed writes not yet rechecked"),
            )
            if doc.get(k)
        )
        + ". "
        f"Report: {page}"
    )
    return 0
