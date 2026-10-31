"""Learn, then enforce: excuse the app's own per-request writes, by approval.

Some apps write on every request: the acting user's ``last_login``, a
token's ``last_seen``, a first-login row for the acting user. The
whole-database frame refutes every write when such a row moves. This module
watches checked writes, proposes the moves a person may excuse, and on one
approval writes them into the contracts (``frame.learned``, see
``runtime/effects/learned.py``). See ``docs/LEARNED_EXCUSES.md``.

    openadapt-flow learn collect --config shadow.yaml --out learn/
    openadapt-flow learn propose --episodes learn/episodes.jsonl \\
        --users app_users --schema schema.json --out learn/proposal.yaml
    openadapt-flow learn approve learn/proposal.yaml --by "Dana Reviewer" \\
        --config shadow.yaml

Fail closed, at every step:

- an episode is learned from only when its declared effects held and its
  frame was refuted by database rows alone; every other episode is ignored
  and counted;
- a moved row is excusable only as the write's own row (``target``) or the
  acting user's row (``actor_row``: the users table's row keyed by the
  actor's id, or a row whose user column holds it; the users table and its
  user columns come from the schema's foreign keys or a person's mapping);
  any other row stays a stop, however often it moves;
- an excuse names exact columns, a change (``changed`` or ``added``, never a
  removal) and a relation, and is proposed only when seen in enough of the
  kind's episodes (``K`` of ``N``);
- approval is one action by a person, recorded with the proposal's digest.

Nothing here reads or stores a record value: episodes and proposals hold
table and column names only.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import math
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

EPISODE_SCHEMA = "openadapt.learn-episode/v1"
PROPOSAL_SCHEMA = "openadapt.learn-proposal/v1"

#: Defaults: propose for a kind only after ``N`` learnable episodes, and an
#: excuse only when seen in at least ``K`` of each ``N`` (3 of 5: a clear
#: majority). Five is the smallest sample in which a majority still leaves
#: room for two episodes where a throttled column (a last-used time the app
#: writes at most once a minute) didn't move; a move seen once or twice in
#: five, a one-off, isn't proposed. The count is not the safety boundary:
#: the relation is. A row that isn't the write's own or the actor's is
#: never proposed, whatever the count.
DEFAULT_K = 3
DEFAULT_N = 5


class LearnError(ValueError):
    """The learner can't go on: a bad input, an edited proposal, a contract
    that can't take the excuses."""


# -- collect -------------------------------------------------------------------------


class Collector:
    """``Shadow.on_checked``: append one line per verifier call to a JSONL
    file. Names only: the kind, the contract, the outcome, the parts checked
    and the frame's ``moves`` (tables, columns, relations). Never a value."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.count = 0

    def __call__(self, episode: Any, result: Any) -> None:
        self.write(episode_record(episode, result))

    def write(self, record: Mapping[str, Any]) -> None:
        line = json.dumps(record, sort_keys=True, default=str)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            self.count += 1


def episode_record(episode: Any, result: Any) -> dict[str, Any]:
    """One episode's line: what the verifier said, by names only."""

    contract = getattr(episode, "contract", None)
    out: dict[str, Any] = {
        "schema_version": EPISODE_SCHEMA,
        "episode": getattr(episode, "id", None),
        "kind": str(getattr(episode, "name", "") or ""),
        "mode": str(getattr(episode, "kind", "") or ""),
        "contract": str(getattr(contract, "name", "") or ""),
        "contract_digest": str(getattr(contract, "digest", "") or ""),
        "writes": len(getattr(episode, "writes", None) or []),
        "at": round(time.time(), 3),
    }
    outcome = getattr(result, "outcome", None)
    out["outcome"] = str(getattr(outcome, "value", outcome) or "")
    checked = getattr(result, "checked", None)
    if isinstance(checked, Mapping):
        out["delta"] = str(checked.get("delta") or "")
        out["frame"] = str(checked.get("frame") or "")
    evidence = getattr(result, "evidence", None)
    frame = evidence.get("frame") if isinstance(evidence, Mapping) else None
    if isinstance(frame, Mapping):
        moves = frame.get("moves")
        if isinstance(moves, list):
            out["moves"] = [dict(m) for m in moves if isinstance(m, Mapping)]
        out["has_database"] = isinstance(frame.get("database"), Mapping)
    return out


def read_episodes(paths: Iterable[Path]) -> list[dict[str, Any]]:
    out = []
    for path in paths:
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise LearnError(f"can't read {path}: {exc}") from exc
        for n, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise LearnError(f"{path}:{n}: not JSON ({exc})") from exc
            if record.get("schema_version") != EPISODE_SCHEMA:
                raise LearnError(f"{path}:{n}: not an {EPISODE_SCHEMA} line")
            out.append(record)
    return out


# -- propose -------------------------------------------------------------------------


@dataclass
class Users:
    """Where the acting user's rows are: the users table and its key, and
    each other table's columns that hold a user id (from the schema's
    foreign keys to that key, or named by a person)."""

    table: str
    key: str
    columns: dict[str, dict[str, str]] = field(default_factory=dict)

    def actor_columns(self, table: str) -> dict[str, str]:
        """Column -> where it came from, for a row of ``table``."""

        out = dict(self.columns.get(table, {}))
        if table == self.table:
            out[self.key] = "users_key"
        return out

    def to_json(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "key": self.key,
            "user_columns": [
                {"table": t, "column": c, "source": s}
                for t in sorted(self.columns)
                for c, s in sorted(self.columns[t].items())
            ],
        }


def users_from(
    table: str,
    key: Optional[str],
    schema: Optional[Any],
    person_columns: Sequence[str] = (),
) -> Users:
    """The users mapping: the table a person names, its key (given, or its
    one-column primary key), the foreign keys to that key in ``schema``
    (``generate.sql_store.Schema``) and the ``table.column`` pairs a person
    confirms."""

    if schema is not None:
        found = schema.tables.get(table)
        if found is None:
            raise LearnError(f"users table {table!r} is not in the schema")
        if key is None:
            if len(found.primary_key) != 1:
                raise LearnError(
                    f"users table {table!r} has no one-column primary key: "
                    "name the key (--users TABLE.KEY)"
                )
            key = found.primary_key[0]
        elif found.column(key) is None:
            raise LearnError(f"users table {table!r} has no column {key!r}")
    if not key:
        raise LearnError("without a schema, name the users key: --users TABLE.KEY")
    users = Users(table, key)
    if schema is not None:
        for name, t in schema.tables.items():
            for fk in t.foreign_keys:
                if fk.table == table and fk.ref_column == key:
                    users.columns.setdefault(name, {})[fk.column] = "foreign_key"
    for pair in person_columns:
        t, sep, c = pair.rpartition(".")
        if not sep or not t or not c:
            raise LearnError(f"--user-column {pair!r} is not TABLE.COLUMN")
        if schema is not None:
            found = schema.tables.get(t)
            if found is None or found.column(c) is None:
                raise LearnError(f"--user-column {pair!r} is not in the schema")
        users.columns.setdefault(t, {}).setdefault(c, "person")
    return users


def _why_ignored(ep: Mapping[str, Any]) -> Optional[str]:
    """Why an episode teaches nothing, or None when it is learnable."""

    if ep.get("mode") not in ("sql", ""):
        return "not a SQL check"
    outcome = ep.get("outcome")
    if outcome == "CONFIRMED":
        return "confirmed: nothing moved that needs an excuse"
    if ep.get("delta") != "held":
        return "the declared effects didn't hold"
    if ep.get("frame") != "refuted":
        return "the frame wasn't refuted (not checked)"
    if outcome != "REFUTED":
        return f"outcome {outcome}"
    moves = ep.get("moves")
    if not isinstance(moves, list) or not moves:
        return "no moved rows recorded"
    if any(m.get("truncated") for m in moves):
        return "too many moved rows to list"
    if not all(m.get("row") is True for m in moves):
        return "refuted by more than rows (a log entry, a table no row explains, a side record)"
    return None


def relation_of(move: Mapping[str, Any], users: Optional[Users]) -> tuple[str, str]:
    """(relation, actor column) for one moved row, or ("", why)."""

    change = move.get("change")
    columns = list(move.get("columns") or [])
    if change not in ("changed", "added"):
        return "", f"a row {change}: only a change or an addition can be excused"
    if not columns or any(not c or c.startswith("(") for c in columns):
        return "", "its moved columns can't be named"
    if move.get("target"):
        return "target", ""
    if users is None:
        return "", "not the write's own row, and no users table was named"
    candidates = users.actor_columns(str(move.get("table")))
    held = set(move.get("actor_columns") or [])
    usable = sorted(
        c for c in candidates if c in held and (change == "added" or c not in columns)
    )
    if not usable:
        return "", "not the write's own row, and not the acting user's row"
    return "actor_row", usable[0]


def propose(
    episodes: Sequence[Mapping[str, Any]],
    *,
    users: Optional[Users] = None,
    k: int = DEFAULT_K,
    n: int = DEFAULT_N,
    source: str = "",
) -> dict[str, Any]:
    """The proposal: per kind, the excuses seen often enough, the moves that
    stay a stop, and every ignored episode by reason."""

    if not 1 <= k <= n:
        raise LearnError(f"K must be between 1 and N (got K={k}, N={n})")
    by_kind: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    ignored: dict[str, Counter[str]] = defaultdict(Counter)
    for ep in episodes:
        kind = str(ep.get("kind") or "")
        why = _why_ignored(ep)
        if why is not None:
            ignored[kind][why] += 1
        else:
            by_kind[kind].append(ep)
    kinds: dict[str, Any] = {}
    next_id = 1
    for kind in sorted(set(by_kind) | set(ignored)):
        eps = by_kind.get(kind, [])
        total = len(eps)
        need = math.ceil(k * total / n) if total else 0
        groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        stays: dict[tuple[str, str, str], set[int]] = defaultdict(set)
        for i, ep in enumerate(eps):
            for move in ep.get("moves") or []:
                relation, extra = relation_of(move, users)
                table, change = str(move.get("table")), str(move.get("change"))
                if not relation:
                    stays[(table, change, extra)].add(i)
                    continue
                g = groups.setdefault(
                    (table, change, relation, extra),
                    {"episodes": set(), "columns": defaultdict(set)},
                )
                g["episodes"].add(i)
                for c in move.get("columns") or []:
                    g["columns"][c].add(i)
        excuses, below = [], []
        for (table, change, relation, actor_col), g in sorted(groups.items()):
            seen = len(g["episodes"])
            cols = sorted(c for c, s in g["columns"].items() if len(s) >= need)
            entry: dict[str, Any] = {
                "table": table,
                "columns": cols,
                "change": change,
                "relation": relation,
                **({"actor_column": actor_col} if actor_col else {}),
                "seen": seen,
                "of": total,
            }
            dropped = sorted(c for c, s in g["columns"].items() if len(s) < need)
            if dropped:
                entry["columns_below_threshold"] = dropped
            ok = (
                total >= n
                and seen >= need
                and cols
                and (
                    change == "changed" or relation != "actor_row" or actor_col in cols
                )
            )
            if ok:
                entry["id"] = f"E{next_id}"
                next_id += 1
                excuses.append(entry)
            else:
                entry["why_not"] = (
                    f"only {total} learnable episode(s); at least {n} are needed"
                    if total < n
                    else f"seen in {seen} of {total}; at least {need} are needed"
                )
                entry["columns"] = sorted(g["columns"])
                below.append(entry)
        kinds[kind] = {
            "learnable_episodes": total,
            "required": need,
            "ignored": sum(ignored[kind].values()),
            "ignored_by_reason": dict(sorted(ignored[kind].items())),
            "excuses": excuses,
            "below_threshold": below,
            "stays_a_stop": [
                {"table": t, "change": c, "why": w, "seen": len(s), "of": total}
                for (t, c, w), s in sorted(stays.items())
            ],
        }
    proposal: dict[str, Any] = {
        "schema_version": PROPOSAL_SCHEMA,
        "created_at": _now(),
        "source": source,
        "settings": {"k": k, "n": n},
        "actor": "the actor's id (Actor.id, the shadow's --actor) is the user's id",
        "users": users.to_json() if users is not None else None,
        "episodes": len(episodes),
        "ignored": sum(sum(c.values()) for c in ignored.values()),
        "kinds": kinds,
    }
    proposal["proposal_sha256"] = proposal_digest(proposal)
    return proposal


def proposal_digest(proposal: Mapping[str, Any]) -> str:
    body = {k: v for k, v in proposal.items() if k != "proposal_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def render_markdown(proposal: Mapping[str, Any]) -> str:
    """The proposal as a person reads it before approving."""

    lines = [
        "# Proposed excuses",
        "",
        f"Proposal digest: `{proposal['proposal_sha256']}`",
        "",
        f"{proposal['episodes']} checked write(s) read; {proposal['ignored']} ignored "
        "(listed per kind). An excuse is proposed only when seen in at least "
        f"{proposal['settings']['k']} of every {proposal['settings']['n']} learnable "
        "writes of a kind.",
        "",
    ]
    users = proposal.get("users")
    if users:
        lines += [
            f"Acting user's rows: `{users['table']}` keyed by `{users['key']}`"
            + (
                "; user columns: "
                + ", ".join(
                    f"`{c['table']}.{c['column']}` ({c['source'].replace('_', ' ')})"
                    for c in users["user_columns"]
                )
                if users["user_columns"]
                else ""
            )
            + ".",
            "",
        ]
    else:
        lines += ["No users table named: only the write's own rows can be excused.", ""]
    for kind, k in proposal["kinds"].items():
        lines += [f"## {kind or '(no kind)'}", ""]
        lines.append(
            f"{k['learnable_episodes']} learnable, {k['ignored']} ignored"
            + (
                " ("
                + "; ".join(f"{v}: {r}" for r, v in k["ignored_by_reason"].items())
                + ")"
                if k["ignored_by_reason"]
                else ""
            )
            + "."
        )
        lines.append("")
        if k["excuses"]:
            lines += [
                "| Id | Table | Columns | Change | Row | Seen |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
            for e in k["excuses"]:
                row = (
                    "the write's own row"
                    if e["relation"] == "target"
                    else f"the acting user's row (`{e['actor_column']}`)"
                )
                lines.append(
                    f"| {e['id']} | `{e['table']}` | {', '.join(e['columns'])} | "
                    f"{e['change']} | {row} | {e['seen']} of {e['of']} |"
                )
            lines.append("")
        else:
            lines += ["No excuse proposed.", ""]
        for e in k["below_threshold"]:
            lines.append(
                f"- Not proposed: `{e['table']}` {e['change']} "
                f"({', '.join(e['columns'])}): {e['why_not']}."
            )
        for s in k["stays_a_stop"]:
            lines.append(
                f"- Stays a stop: `{s['table']}` {s['change']}, seen in {s['seen']} "
                f"of {s['of']}: {s['why']}."
            )
        if k["below_threshold"] or k["stays_a_stop"]:
            lines.append("")
    lines += [
        "Approve all of it with one command (it records this digest):",
        "",
        "    openadapt-flow learn approve <this proposal.yaml> --by <your name> ...",
        "",
    ]
    return "\n".join(lines)


def write_proposal(proposal: Mapping[str, Any], out: Path) -> tuple[Path, Path]:
    import yaml

    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        yaml.safe_dump(dict(proposal), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    md = out.with_suffix(".md")
    md.write_text(render_markdown(proposal), encoding="utf-8")
    return out, md


def load_proposal(path: Path) -> dict[str, Any]:
    """A proposal as written, with its digest checked: an edited proposal
    is refused (propose again instead)."""

    import yaml

    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise LearnError(f"can't read {path}: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != PROPOSAL_SCHEMA:
        raise LearnError(f"{path} is not an {PROPOSAL_SCHEMA} proposal")
    if data.get("proposal_sha256") != proposal_digest(data):
        raise LearnError(
            f"{path} was changed after it was proposed (its digest doesn't match); "
            "propose again, and leave out excuses with --reject at approval"
        )
    return data


# -- approve -------------------------------------------------------------------------


def contracts_from_shadow(config: Path) -> dict[str, list[Path]]:
    """Route name -> its contract file, from an ``openadapt.shadow/v1`` config."""

    import yaml

    data = yaml.safe_load(Path(config).read_text(encoding="utf-8")) or {}
    base = Path(config).resolve().parent
    out: dict[str, list[Path]] = defaultdict(list)
    for r in data.get("routes") or []:
        if isinstance(r, dict) and r.get("contract"):
            out[str(r.get("name") or r.get("write"))].append(base / r["contract"])
    return dict(out)


def contracts_from_manifest(manifest: Path) -> dict[str, list[Path]]:
    """Kind -> every contract file the generator's manifest lists for it."""

    import yaml

    data = yaml.safe_load(Path(manifest).read_text(encoding="utf-8")) or {}
    base = Path(manifest).resolve().parent
    out: dict[str, list[Path]] = defaultdict(list)
    for kind, body in (data.get("kinds") or {}).items():
        for v in (body or {}).get("variants") or []:
            # Every contract file a variant names (create, update, found).
            for value in (v or {}).values():
                if isinstance(value, str) and value.endswith((".yaml", ".yml")):
                    out[str(kind)].append(base / value)
    return dict(out)


def learned_block(
    proposal: Mapping[str, Any], kind: str, by: str, reject: Sequence[str] = ()
) -> Optional[dict[str, Any]]:
    """The ``frame.learned`` mapping for one kind, or None when it has no
    excuse left."""

    body = (proposal.get("kinds") or {}).get(kind)
    if not body:
        return None
    excuses = []
    for e in body.get("excuses") or []:
        if e.get("id") in reject:
            continue
        excuses.append(
            {
                "table": e["table"],
                "columns": list(e["columns"]),
                "change": e["change"],
                "relation": e["relation"],
                **(
                    {"actor_column": e["actor_column"]} if e.get("actor_column") else {}
                ),
            }
        )
    if not excuses:
        return None
    return {
        "excuses": excuses,
        "proposal_sha256": proposal["proposal_sha256"],
        "approved_by": by,
        "approved_at": _now(),
    }


def approve(
    proposal: Mapping[str, Any],
    *,
    by: str,
    contracts: Mapping[str, Sequence[Path]],
    reject: Sequence[str] = (),
    kinds: Optional[Sequence[str]] = None,
) -> list[tuple[str, Path, int]]:
    """Write each kind's approved excuses into its contracts as
    ``frame.learned``, all or none. Returns (kind, contract, excuses)."""

    import yaml

    from openadapt_flow.verifier import Contract, VerifierError

    if not by.strip():
        raise LearnError("approval names the person approving (--by)")
    known = {
        e.get("id")
        for body in (proposal.get("kinds") or {}).values()
        for e in body.get("excuses") or []
    }
    unknown = sorted(set(reject) - known)
    if unknown:
        raise LearnError(f"--reject names no proposed excuse: {', '.join(unknown)}")
    wanted = list(kinds) if kinds else sorted(proposal.get("kinds") or {})
    plan: list[tuple[str, Path, dict[str, Any], int]] = []
    for kind in wanted:
        if kind not in (proposal.get("kinds") or {}):
            raise LearnError(f"the proposal has no kind {kind!r}")
        block = learned_block(proposal, kind, by.strip(), reject)
        if block is None:
            continue
        files = list(contracts.get(kind) or [])
        if not files:
            raise LearnError(
                f"no contract given for kind {kind!r} (--config, --manifest or "
                f"--contract {kind}=PATH)"
            )
        for path in files:
            try:
                data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise LearnError(f"can't read {path}: {exc}") from exc
            frame = data.get("frame") if isinstance(data, dict) else None
            if not isinstance(frame, dict) or not frame.get("database"):
                raise LearnError(
                    f"{path}: learned excuses need frame.database (the "
                    "whole-database frame)"
                )
            new = dict(data)
            new["frame"] = {**frame, "learned": block}
            try:
                Contract.from_dict(new, name=Path(path).stem)
            except VerifierError as exc:
                raise LearnError(f"{path}: {exc}") from exc
            plan.append((kind, Path(path), new, len(block["excuses"])))
    for _kind, path, new, _n in plan:
        path.write_text(
            yaml.safe_dump(new, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    return [(kind, path, n) for kind, path, _new, n in plan]


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- CLI -----------------------------------------------------------------------------


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "learn",
        help="Learn the app's own per-request writes from checked traffic, "
        "propose excuses, and approve them into the contracts",
    )
    build_parser(p)


def build_parser(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
    steps = p.add_subparsers(dest="learn_step", required=True)

    c = steps.add_parser("collect", help="run a shadow and record each check")
    c.add_argument("--config", required=True, help="openadapt.shadow/v1 YAML (sql)")
    c.add_argument("--out", default="learn", help="folder for episodes.jsonl")
    c.add_argument("--actor", default=None, help="the agent's user id in the app")
    c.add_argument("--listen", type=int, default=None)
    c.add_argument("--until-idle", type=float, default=None, metavar="SECONDS")
    c.set_defaults(func=cmd_collect)

    q = steps.add_parser("propose", help="write a proposal a person can read")
    q.add_argument("--episodes", action="append", required=True, help="episodes.jsonl")
    q.add_argument(
        "--users",
        default=None,
        help="the users table, as TABLE or TABLE.KEY (without it only the "
        "write's own rows can be excused)",
    )
    q.add_argument(
        "--user-column",
        action="append",
        default=[],
        metavar="TABLE.COLUMN",
        help="a column holding a user id, confirmed by you (repeatable)",
    )
    q.add_argument("--schema", default=None, help="schema.json from `generate schema`")
    q.add_argument(
        "--read", default=None, help="read.yaml: read the schema's foreign keys live"
    )
    q.add_argument("--k", type=int, default=DEFAULT_K)
    q.add_argument("--n", type=int, default=DEFAULT_N)
    q.add_argument("--out", default="learn/proposal.yaml")
    q.set_defaults(func=cmd_propose)

    a = steps.add_parser("approve", help="write the approved excuses into contracts")
    a.add_argument("proposal")
    a.add_argument("--by", required=True, help="who approves (recorded)")
    a.add_argument("--config", default=None, help="the shadow config's routes")
    a.add_argument("--manifest", default=None, help="a generator manifest.yaml")
    a.add_argument(
        "--contract",
        action="append",
        default=[],
        metavar="KIND=PATH",
        help="a contract file for a kind (repeatable)",
    )
    a.add_argument("--kind", action="append", default=[], help="approve only these")
    a.add_argument(
        "--reject", action="append", default=[], metavar="ID", help="leave out E<n>"
    )
    a.set_defaults(func=cmd_approve)
    return p


def _schema(args: argparse.Namespace) -> Optional[Any]:
    from openadapt_flow.generate.sql_store import (
        Schema,
        StoreError,
        connector,
        dialect_of,
        read_schema,
    )

    if args.schema:
        try:
            data = json.loads(Path(args.schema).read_text(encoding="utf-8"))
            return Schema.from_json(data)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LearnError(f"can't read the schema {args.schema}: {exc}") from exc
    if args.read:
        import yaml

        read = yaml.safe_load(Path(args.read).read_text(encoding="utf-8")) or {}
        try:
            return read_schema(connector(read), dialect_of(read))
        except (StoreError, Exception) as exc:  # noqa: BLE001 - reported plainly
            raise LearnError(f"can't read the schema: {exc}") from exc
    return None


def cmd_collect(args: argparse.Namespace) -> int:
    import signal

    from openadapt_flow import cli_shadow
    from openadapt_flow.shadow import ShadowConfigError
    from openadapt_flow.shadow_report import write_report

    out = Path(args.out)
    ns = argparse.Namespace(
        config=args.config,
        mode=None,
        upstream=None,
        listen=args.listen,
        actor=args.actor,
        hold_ms=None,
        quiet_s=None,
        contract=[],
    )
    try:
        cfg = cli_shadow.load(ns)
        if cfg.get("mode") != "sql":
            raise ShadowConfigError("learning reads the whole-database frame: mode sql")
        shadow = cli_shadow.build(cfg, out / "shadow")
    except (ShadowConfigError, OSError, ValueError) as exc:
        print(f"learn collect: {exc}", file=sys.stderr)
        return 2
    collector = Collector(out / "episodes.jsonl")
    shadow.on_checked = collector
    shadow.start()
    print(f"Shadow is listening at {shadow.url}. Ctrl-C ends the run.", flush=True)
    stopping: list[int] = []
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    try:
        while not stopping:
            time.sleep(0.5)
            if args.until_idle is not None and shadow.idle_s() >= args.until_idle:
                break
    except KeyboardInterrupt:
        pass
    shadow.stop()
    write_report(shadow.results(), out / "shadow")
    print(f"{collector.count} check(s) recorded in {collector.path}")
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    try:
        episodes = read_episodes(Path(p) for p in args.episodes)
        users = None
        if args.users:
            table, _, key = args.users.partition(".")
            users = users_from(table, key or None, _schema(args), args.user_column)
        elif args.user_column:
            raise LearnError("--user-column needs --users")
        proposal = propose(
            episodes,
            users=users,
            k=args.k,
            n=args.n,
            source=", ".join(str(p) for p in args.episodes),
        )
        path, md = write_proposal(proposal, Path(args.out))
    except LearnError as exc:
        print(f"learn propose: {exc}", file=sys.stderr)
        return 2
    from openadapt_flow.cli_hints import command

    count = sum(len(k["excuses"]) for k in proposal["kinds"].values())
    approve_hint = command(f"learn approve {path} --by <name> ...")
    print(
        f"{count} excuse(s) proposed from {proposal['episodes']} check(s) "
        f"({proposal['ignored']} ignored). Read {md}; approve with "
        f"`{approve_hint}`. Digest {proposal['proposal_sha256']}"
    )
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    try:
        proposal = load_proposal(Path(args.proposal))
        contracts: dict[str, list[Path]] = defaultdict(list)
        if args.config:
            for k, v in contracts_from_shadow(Path(args.config)).items():
                contracts[k].extend(v)
        if args.manifest:
            for k, v in contracts_from_manifest(Path(args.manifest)).items():
                contracts[k].extend(v)
        for pair in args.contract:
            kind, sep, path = pair.partition("=")
            if not sep or not kind or not path:
                raise LearnError(f"--contract {pair!r} is not KIND=PATH")
            p = Path(path)
            contracts[kind].extend(sorted(p.glob("*.yaml")) if p.is_dir() else [p])
        done = approve(
            proposal,
            by=args.by,
            contracts=contracts,
            reject=args.reject,
            kinds=args.kind or None,
        )
    except LearnError as exc:
        print(f"learn approve: {exc}", file=sys.stderr)
        return 2
    if not done:
        print("Nothing to approve: no excuse was proposed for these kinds.")
        return 0
    per_kind: dict[str, tuple[int, int]] = {}
    for kind, _path, n in done:
        files, _ = per_kind.get(kind, (0, n))
        per_kind[kind] = (files + 1, n)
    for kind, (files, n) in sorted(per_kind.items()):
        print(f"{kind}: {n} excuse(s) written into {files} contract(s)")
    print(f"Approved by {args.by}; proposal {proposal['proposal_sha256']}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser(
        argparse.ArgumentParser(
            prog="python -m openadapt_flow.learn",
            description="Learn, then enforce: excuse the app's own per-request writes.",
        )
    )
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
