"""``openadapt-flow learn entities``: scope a setup's frames to its entities.

A whole-database frame stops a correct write when any row anywhere moves:
a scheduler's heartbeat, a task queue, prices the app recomputes, a
notification for each watcher. ``frame.database.entity_tables`` scopes the
hard check to the business-entity tables; rows elsewhere are logged as side
effects and offered to the learner (``docs/VERDICT_TIERS.md``).

The entity tables are derived from the setup, never named by hand:

- each kind's main table (the manifest's ``main_table``);
- every table a contract's own read selects from, which covers the child
  tables a write adds and the tables a field's location reads through (a
  project's customer);
- the tables the frame already names (``views``, ``judged_by``,
  ``new_row_fields``);
- the acting user's table: the one table every column bound to an actor
  parameter references (``learn_probe.users_for_actor_params``). Without it
  no scope is proposed, since another user's row must stay a stop.

Three steps, so the scope is admitted only on evidence (``admission``):

    openadapt-flow learn entities propose --manifest m.yaml --read read.yaml --out scope.json
    openadapt-flow learn entities stage scope.json --out candidate/   # a scoped copy
    # run the planted faults against candidate/ -> faults.json
    openadapt-flow learn entities apply scope.json --faults faults.json --by "A. Reviewer"

``apply`` refuses unless every class of the safety bar, and the actor-table
attacks, ran against the staged scope and were all caught.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

SCHEMA = "openadapt.entity-scope/v1"

_FROM = re.compile(
    r"\b(?:FROM|JOIN)\s+((?:[`\"]?[A-Za-z_][\w$]*[`\"]?\.)?[`\"]?[A-Za-z_][\w$]*[`\"]?)",
    re.IGNORECASE,
)


class EntityScopeError(ValueError):
    pass


def tables_in_sql(sql: str) -> set[str]:
    """Table names a read selects from (``FROM`` / ``JOIN``), unquoted."""

    out = set()
    for m in _FROM.finditer(sql or ""):
        name = m.group(1).replace("`", "").replace('"', "")
        out.add(name)
    return out


def _sql_of(doc: Any) -> list[str]:
    found: list[str] = []

    def walk(v: Any) -> None:
        if isinstance(v, Mapping):
            q = v.get("sql_query")
            if isinstance(q, str):
                found.append(q)
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(doc)
    return found


def _contracts(manifest: Path) -> list[Path]:
    from openadapt_flow.learn import contracts_from_manifest

    return sorted({p for ps in contracts_from_manifest(manifest).values() for p in ps})


def derive(manifest: Path, schema: Any) -> dict[str, Any]:
    """The proposal: entity tables and the contracts they apply to."""

    from openadapt_flow.app_profiles import table_name
    from openadapt_flow.learn_probe import users_for_actor_params

    data = yaml.safe_load(Path(manifest).read_text(encoding="utf-8")) or {}
    tables: set[str] = set()
    for body in (data.get("kinds") or {}).values():
        main = (body or {}).get("main_table")
        if main:
            tables.add(str(main))
    files = _contracts(manifest)
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for q in _sql_of(doc.get("verifier") or {}):
            tables |= tables_in_sql(q)
        db = ((doc.get("frame") or {}).get("database")) or {}
        for key in ("views", "judged_by", "new_row_fields"):
            tables |= {str(t) for t in (db.get(key) or {})}
    users = users_for_actor_params(Path(manifest), schema)
    if users is None:
        raise EntityScopeError(
            "the acting user's table can't be derived from the manifest's actor "
            "parameters, so no scope is proposed: another user's row must stay a stop"
        )
    tables.add(str(users.table))
    known = set(getattr(schema, "tables", {}) or {})
    if known:
        tables = {t for t in tables if t in known or table_name(t) in known}
    payload = {
        "schema": SCHEMA,
        "manifest": str(Path(manifest).resolve()),
        "entity_tables": sorted(tables),
        "actor_table": str(users.table),
        "contracts": [str(p) for p in files],
    }
    from openadapt_flow.runtime.effects.admission import digest

    payload["proposal_sha256"] = digest(
        {k: payload[k] for k in ("entity_tables", "actor_table")}
    )
    return payload


def _scoped(doc: Mapping[str, Any], tables: Sequence[str]) -> dict[str, Any]:
    frame = doc.get("frame") if isinstance(doc, Mapping) else None
    if not isinstance(frame, Mapping) or not frame.get("database"):
        raise EntityScopeError("an entity scope needs frame.database")
    new = dict(doc)
    new["frame"] = {
        **frame,
        "database": {**frame["database"], "entity_tables": list(tables)},
    }
    return new


def _rewrite(files: Sequence[Path], tables: Sequence[str]) -> list[tuple[Path, str]]:
    from openadapt_flow.verifier import Contract, VerifierError

    out = []
    for path in files:
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        new = _scoped(doc, tables)
        try:
            Contract.from_dict(new, name=Path(path).stem)
        except VerifierError as exc:
            raise EntityScopeError(f"{path}: {exc}") from exc
        out.append(
            (Path(path), yaml.safe_dump(new, sort_keys=False, allow_unicode=True))
        )
    return out


def stage(proposal: Mapping[str, Any], out: Path) -> Path:
    """A copy of the manifest's folder with the scope written in, for the
    planted faults to run against. Returns the copied manifest."""

    manifest = Path(str(proposal["manifest"]))
    src = manifest.parent
    out = Path(out)
    if out.exists():
        raise EntityScopeError(f"{out} exists")
    shutil.copytree(src, out)
    files = [
        out / Path(p).resolve().relative_to(src.resolve())
        for p in proposal["contracts"]
    ]
    for path, text in _rewrite(files, proposal["entity_tables"]):
        path.write_text(text, encoding="utf-8")
    return out / manifest.name


def apply(proposal: Mapping[str, Any], faults: Path, by: str) -> Any:
    """Write the scope into the setup's contracts, only when the planted
    faults run against the staged scope were all caught."""

    from openadapt_flow.learn import _write_all
    from openadapt_flow.runtime.effects.admission import (
        ACTOR_ATTACKS,
        SAFETY_BAR,
        AdmissionRefused,
        admit,
        load_results,
    )

    if not by.strip():
        raise EntityScopeError("applying a scope names the person (--by)")
    ran_under, results = load_results(Path(faults))
    if ran_under != proposal.get("proposal_sha256"):
        raise AdmissionRefused(
            "the fault results were not run under this proposal "
            f"({ran_under or 'no digest'} != {proposal.get('proposal_sha256')})"
        )
    decision = admit("entity_tables", results, required=(*SAFETY_BAR, *ACTOR_ATTACKS))
    if not decision.admitted:
        raise AdmissionRefused("; ".join(decision.reasons))
    _write_all(
        _rewrite([Path(p) for p in proposal["contracts"]], proposal["entity_tables"])
    )
    return decision


# -- CLI -----------------------------------------------------------------------------


def register(steps: argparse._SubParsersAction) -> None:
    e = steps.add_parser(
        "entities",
        help="scope the whole-database frame to the setup's business entities",
        allow_abbrev=False,
    )
    esub = e.add_subparsers(dest="entities_step", required=True)
    p = esub.add_parser(
        "propose", help="derive the entity tables from the setup", allow_abbrev=False
    )
    p.add_argument("--manifest", required=True)
    p.add_argument("--read", default=None, help="read.yaml (the read-only account)")
    p.add_argument("--schema", default=None, help="schema.json from `generate schema`")
    p.add_argument("--out", default="entity_scope.json")
    p.set_defaults(func=cmd_entities)
    s = esub.add_parser(
        "stage", help="a scoped copy of the setup, for the faults", allow_abbrev=False
    )
    s.add_argument("proposal")
    s.add_argument("--out", required=True)
    s.set_defaults(func=cmd_entities)
    a = esub.add_parser(
        "apply",
        help="write the scope in, if the faults were all caught",
        allow_abbrev=False,
    )
    a.add_argument("proposal")
    a.add_argument(
        "--faults", required=True, help="openadapt.relaxation-faults/v1 JSON"
    )
    a.add_argument("--by", required=True)
    a.set_defaults(func=cmd_entities)


def cmd_entities(args: argparse.Namespace) -> int:
    from openadapt_flow.runtime.effects.admission import AdmissionRefused

    try:
        if args.entities_step == "propose":
            from openadapt_flow.learn import _schema

            schema = _schema(args)
            proposal = derive(Path(args.manifest), schema)
            Path(args.out).write_text(json.dumps(proposal, indent=2), encoding="utf-8")
            print(
                f"{len(proposal['entity_tables'])} entity table(s) for "
                f"{len(proposal['contracts'])} contract(s); written to {args.out}"
            )
            return 0
        proposal = json.loads(Path(args.proposal).read_text(encoding="utf-8"))
        if proposal.get("schema") != SCHEMA:
            raise EntityScopeError(f"{args.proposal} is not a {SCHEMA} proposal")
        if args.entities_step == "stage":
            print(stage(proposal, Path(args.out)))
            return 0
        decision = apply(proposal, Path(args.faults), args.by)
        print(f"admitted: {json.dumps(decision.per_class, sort_keys=True)}")
        return 0
    except (EntityScopeError, AdmissionRefused, OSError, ValueError) as exc:
        print(f"learn entities: {exc}", file=sys.stderr)
        return 2


def main(argv: Optional[Sequence[str]] = None) -> int:  # pragma: no cover
    ap = argparse.ArgumentParser(prog="learn-entities", allow_abbrev=False)
    register(ap.add_subparsers(dest="step", required=True))
    args = ap.parse_args(argv)
    return int(args.func(args))
