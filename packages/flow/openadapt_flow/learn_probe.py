"""Learn the app's per-request bookkeeping from read-only requests.

Some apps write on every request: the acting user's ``last_login``, an API
token's last-used time, a property row added at the user's first sign-in.
The whole-database frame sees those rows move and refutes every write,
correct or not. :mod:`openadapt_flow.learn` learns them from checked writes
and a person approves. That needs writes the shadow can check, and on apps
whose requests name their target in the URL it learns nothing.

This module learns the same moves without a write. A read-only request
(``GET`` or ``HEAD``) carries no task, so a row it makes the app move is
the app's own bookkeeping, not something an agent asked for:

1. For each probe round, read the contract's whole-database frame
   (``begin_write`` with the probe's parameters), send the read-only
   requests as the agent, and judge the frame (``verify_write``). No write
   is sent, so the declared effect is absent; only the frame's moved rows
   are used. A control window of the same length follows each round with
   no request; a move it shows is not the request's and is not excused.
2. Propose from those moves with the learner's own rules
   (:func:`openadapt_flow.learn.propose`): only the acting user's own row
   (``actor_row``), exact columns, ``changed`` only, and never a column a
   probe request, a parameter or the contract names. An ``added`` row is
   held for a person's approval (its excuse would name columns, not
   values). Any other row that moved, another user's or one no user owns,
   stays a stop and is listed.
3. Write the excuses into each contract's ``frame.learned`` with
   ``source: read_only_probe``. At check time the frame derives the
   relation again from the run's actor, so an excuse never reaches another
   user's row or another column.

No person approves this, because nothing in it is a judgment: which rows
moved is read, the relation to the acting user is derived from the
schema's foreign keys, and the method of every request is checked. Where
the users table can't be derived (``users_for_actor_params``), nothing is
learned. Nothing here reads or stores a record value.
"""

from __future__ import annotations

import json
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from openadapt_flow import learn as _learn

#: Request methods a probe may send: they carry no task.
READ_ONLY_METHODS = frozenset({"GET", "HEAD"})
#: The ``frame.learned.approved_by`` of a probe's excuses.
PROBE_APPROVER = "read-only probe (openadapt-flow learn probe)"
DEFAULT_ROUNDS = 3
#: A move is excused only when every one of at least this many rounds shows
#: it: per-request bookkeeping moves on every request, while a one-off edit
#: (a person changing the acting user's own row during a round) does not.
MIN_ROUNDS = 2


class ProbeError(_learn.LearnError):
    """The probe can't run or its result can't be written."""


def check_read_only(requests: Sequence[tuple[str, str]]) -> None:
    """Refuse any request that isn't ``GET`` or ``HEAD``."""

    if not requests:
        raise ProbeError("a probe needs at least one read-only request")
    for method, _path in requests:
        if str(method).upper() not in READ_ONLY_METHODS:
            raise ProbeError(
                f"a probe sends read-only requests only (GET, HEAD), not {method!r}"
            )


def episode(
    kind: str,
    result: Any,
    *,
    contract: Any,
    params: Mapping[str, Any],
    sent_fields: set[str],
) -> dict[str, Any]:
    """One probe round as a learner episode (names only)."""

    evidence = getattr(result, "evidence", None) or {}
    frame = evidence.get("frame") if isinstance(evidence, Mapping) else None
    checked = getattr(result, "checked", None) or {}
    out: dict[str, Any] = {
        "schema_version": _learn.EPISODE_SCHEMA,
        "probe": True,
        "kind": kind,
        "mode": "sql",
        "contract": str(getattr(contract, "name", "") or ""),
        "frame": str(checked.get("frame") or ""),
        "sent_fields": sorted(sent_fields),
        "param_names": sorted(str(p) for p in params),
        "contract_fields": _learn.contract_fields(contract),
        "has_database": isinstance(frame, Mapping)
        and isinstance(frame.get("database"), Mapping),
    }
    moves = frame.get("moves") if isinstance(frame, Mapping) else None
    out["moves"] = [dict(m) for m in moves or [] if isinstance(m, Mapping)]
    return out


def probe(
    contract: Any,
    params: Mapping[str, Any],
    *,
    kind: str,
    actor: str,
    send: Callable[[], set[str]],
    rounds: int = DEFAULT_ROUNDS,
    workdir: Optional[Path] = None,
    idle: Callable[[float], None] = time.sleep,
) -> list[dict[str, Any]]:
    """Run ``rounds`` read-only windows under ``contract``'s frame.

    ``send`` sends the read-only requests as the agent and returns the
    query-parameter names it sent (``learn.query_fields``); it must not
    write. After each request window, a control window as long as it sends
    nothing (``idle``): a row that moves there moves without a request, so
    it is not the request's bookkeeping. Returns one request episode and
    one control episode (``control: True``) per round."""

    from openadapt_flow.verifier import Actor, begin_write, verify_write

    if rounds < MIN_ROUNDS:
        raise ProbeError(f"a probe runs at least {MIN_ROUNDS} rounds")
    config = getattr(contract, "config", None)
    frame = getattr(config, "frame", None)
    if frame is None or getattr(frame, "database", None) is None:
        raise ProbeError("a probe reads the whole-database frame: frame.database")
    out = []
    with tempfile.TemporaryDirectory(prefix="oa-probe-") as tmp:
        base = Path(workdir) if workdir is not None else Path(tmp)
        for i in range(rounds):
            pending = begin_write(
                contract,
                dict(params),
                actor=Actor("agent", str(actor)),
                ledger=None,
                write_key=f"read-only-probe-{kind}-{i}",
                workdir=base / f"round-{i}",
            )
            started = time.monotonic()
            sent = set(send())
            elapsed = time.monotonic() - started
            result = verify_write(pending)
            out.append(
                episode(
                    kind, result, contract=contract, params=params, sent_fields=sent
                )
            )
            control = begin_write(
                contract,
                dict(params),
                actor=Actor("agent", str(actor)),
                ledger=None,
                write_key=f"read-only-probe-{kind}-{i}-control",
                workdir=base / f"round-{i}-control",
            )
            idle(elapsed)
            ep = episode(
                kind,
                verify_write(control),
                contract=contract,
                params=params,
                sent_fields=set(),
            )
            ep["control"] = True
            out.append(ep)
    return out


def users_for_actor_params(manifest_path: Path, schema: Any) -> Optional[_learn.Users]:
    """The users table, derived from a generator manifest: the one table and
    key that every column bound to an actor parameter (``actor_params``)
    references by foreign key. None when there is no such column, or the
    columns reference more than one table (then nothing is learned)."""

    import yaml

    data = yaml.safe_load(Path(manifest_path).read_text(encoding="utf-8")) or {}
    actor_params = {str(p) for p in data.get("actor_params") or []}
    if not actor_params or schema is None:
        return None
    base = Path(manifest_path).resolve().parent
    targets: set[tuple[str, str]] = set()
    for body in (data.get("kinds") or {}).values():
        main = (body or {}).get("main_table")
        table = schema.tables.get(main) if main else None
        if table is None:
            continue
        for variant in (body or {}).get("variants") or []:
            for value in (variant or {}).values():
                if not (isinstance(value, str) and value.endswith((".yaml", ".yml"))):
                    continue
                try:
                    doc = yaml.safe_load((base / value).read_text(encoding="utf-8"))
                except (OSError, yaml.YAMLError):
                    continue
                for effect in (doc or {}).get("effects") or []:
                    for column, bound in ((effect or {}).get("match") or {}).items():
                        if not (
                            isinstance(bound, Mapping)
                            and str(bound.get("param")) in actor_params
                        ):
                            continue
                        for fk in table.foreign_keys:
                            if fk.column == column:
                                targets.add((fk.table, fk.ref_column))
    if len(targets) != 1:
        return None
    users_table, key = next(iter(targets))
    return _learn.users_from(users_table, key, schema)


def propose(
    episodes: Sequence[Mapping[str, Any]], users: Optional[_learn.Users]
) -> dict[str, Any]:
    """The learner's proposal from probe episodes: a ``changed`` move must
    show in every read-only round (at least :data:`MIN_ROUNDS`), so a
    one-off edit to the acting user's own row during one round is not
    excused. The relation to the acting user then decides.

    An ``added`` row is never admitted this way. Its excuse names columns,
    not values, so it would also cover a row the agent itself adds for its
    own user with any values (a first-login row the probe saw once, then a
    different property the agent sets). Such moves go under
    ``needs_approval``: a person approves them with ``learn approve``."""

    probes = [e for e in episodes if e.get("probe")]
    controls = [e for e in probes if e.get("control")]
    eps = [dict(e, kind="read_only_probe") for e in probes if not e.get("control")]
    # fail-open class: correlation-as-attribution. A move seen in every
    # request round is only timing; it is the request's bookkeeping only
    # when the control windows (no request) show it never moving on its own.
    background = {
        (str(m.get("table")), str(c))
        for e in controls
        for m in e.get("moves") or []
        for c in m.get("columns") or [None]
    }
    learnable = [e for e in eps if _learn._why_ignored(e) is None]
    n = max(1, len(learnable))
    proposal = _learn.propose(eps, users=users, k=n, n=n, source="read-only probe")
    # A round that showed no usable move (nothing moved, or no frame) means
    # the move isn't per-request bookkeeping; too few rounds can't tell.
    every_round = len(learnable) >= MIN_ROUNDS and len(learnable) == len(eps)
    for body in (proposal.get("kinds") or {}).values():
        if not every_round:
            body["excuses"] = []
            body["not_learned"] = (
                f"a move is excused only when all of at least {MIN_ROUNDS} "
                f"read-only rounds show it ({len(learnable)} of {len(eps)} did)"
            )
        kept: list[dict[str, Any]] = []
        held: list[dict[str, Any]] = []
        for e in body.get("excuses") or []:
            if e.get("change") == "added":
                e["why_not"] = (
                    "an added row: its excuse names columns, not values, so a "
                    "person approves it (learn approve)"
                )
            elif len(controls) < len(eps) or not all(
                c.get("has_database") and c.get("frame") in ("held", "refuted")
                for c in controls
            ):
                # A control window whose frame wasn't read shows nothing,
                # which is no evidence that nothing moved.
                e["why_not"] = (
                    "no readable control window without requests for every "
                    "round: nothing shows the move comes from the request, "
                    "so a person approves it (learn approve)"
                )
            elif any(
                (str(e.get("table")), str(c)) in background
                or (str(e.get("table")), "None") in background
                for c in e.get("columns") or []
            ):
                e["why_not"] = (
                    "it also moved in a control window with no request: a "
                    "background writer, not the request's bookkeeping, so a "
                    "person approves it (learn approve)"
                )
            else:
                kept.append(e)
                continue
            held.append(e)
        for e in held:
            e.pop("id", None)
        body["excuses"] = kept
        body["needs_approval"] = held
    proposal.pop("proposal_sha256", None)
    proposal["proposal_sha256"] = _learn.proposal_digest(proposal)
    return proposal


def apply(
    proposal: Mapping[str, Any],
    contracts: Sequence[Path],
) -> list[tuple[Path, int]]:
    """Write the probe's excuses into each contract's ``frame.learned``, all
    or none. A contract gets only the excuses whose columns it doesn't name
    itself, and only when it creates new records or reads the target's
    versions (:func:`_sees_its_own_harm`); one with a ``frame.learned``
    already is refused (a person reconciles two sets of excuses)."""

    import yaml

    from openadapt_flow.verifier import Contract, VerifierError

    block = _learn.learned_block(proposal, "read_only_probe", PROBE_APPROVER)
    if block is None:
        return []
    plan: list[tuple[Path, dict[str, Any], int]] = []
    for path in contracts:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise ProbeError(f"can't read {path}: {exc}") from exc
        frame = data.get("frame") if isinstance(data, dict) else None
        if not isinstance(frame, dict) or not frame.get("database"):
            continue  # no whole-database frame: nothing to excuse
        if frame.get("learned"):
            raise ProbeError(
                f"{path}: frame.learned is already set; reconcile it by hand"
            )
        try:
            names: set[str] = set()
            loaded = Contract.from_dict(data, name=Path(path).stem)
            for n in _learn.contract_fields(loaded):
                names |= _learn._names(n)
        except VerifierError as exc:
            raise ProbeError(f"{path}: {exc}") from exc
        row_versions = None
        if not _sees_its_own_harm(loaded):
            # An update (or a found check) with no version source: another
            # user's edit that the agent's stale save reverts leaves nothing
            # changed, so before learning only the bookkeeping move stopped
            # it. Such a contract gets an excuse only with a version source
            # derived here, written beside it.
            row_versions = detect_row_versions(data)
            if row_versions is None:
                continue
        excuses = [
            e
            for e in block["excuses"]
            if not any(_learn._names(c) & names for c in e["columns"])
        ]
        if not excuses:
            continue
        new = dict(data)
        new["frame"] = {
            **frame,
            "learned": {**block, "excuses": excuses, "source": "read_only_probe"},
        }
        if row_versions is not None:
            new["frame"]["row_versions"] = row_versions
        try:
            checked = Contract.from_dict(new, name=Path(path).stem)
        except VerifierError as exc:
            raise ProbeError(f"{path}: {exc}") from exc
        if row_versions is not None and not _sees_its_own_harm(checked):
            continue  # the derived source doesn't cover every effect
        plan.append((Path(path), new, len(excuses)))
    _learn._write_all(
        [
            (path, yaml.safe_dump(new, sort_keys=False, allow_unicode=True))
            for path, new, _n in plan
        ]
    )
    return [(path, n) for path, _new, n in plan]


def _sees_its_own_harm(contract: Any) -> bool:
    """May a probe excuse go into this (loaded) contract? Only when the
    frame can see a lost update without the bookkeeping move.

    Either every declared effect creates new records (``record_written``
    with ``count_new_only``; a zero-count guard writes nothing), or the
    contract reads the target's versions and they cover every other effect:
    ``frame.versions`` (with a ``target_surface``, each effect that isn't a
    create must be read on that surface), or ``frame.row_versions`` bound to
    the target surface's own table (:func:`row_versions.binding_problem`)
    with every other effect on that surface. Anything else, such as a create
    plus a ``field_equals`` update, gets no excuse."""

    from openadapt_flow.runtime.effects.row_versions import binding_problem
    from openadapt_flow.runtime.effects.surfaces import surface_of

    config = getattr(contract, "config", None)
    frame = getattr(config, "frame", None)
    effects = list(getattr(config, "effects", None) or [])

    def kind(effect: Any) -> str:
        k = getattr(effect, "kind", "")
        return str(getattr(k, "value", k))

    def creates(effect: Any) -> bool:
        return (
            kind(effect) == "record_written"
            and getattr(effect, "count_new_only", None) is True
            and getattr(effect, "expected_count", 1) != 0
        )

    def guard(effect: Any) -> bool:
        return (
            kind(effect) == "record_written"
            and getattr(effect, "expected_count", 1) == 0
        )

    others = [e for e in effects if not creates(e) and not guard(e)]
    if not others:
        return any(creates(e) for e in effects)
    if frame is None:
        return False
    versions = getattr(frame, "versions", None)
    if versions is not None:
        surface = getattr(versions, "target_surface", None)
        if surface is None:
            # fail-open class: ambiguous-subject-binding. An unscoped version
            # read judges targets by their local id alone, so two records
            # with one id on two surfaces would share one version check.
            if len({surface_of(e) for e in others}) == 1:
                return True
        elif all(surface_of(e) == surface for e in others):
            return True
    rows = getattr(frame, "row_versions", None)
    database = getattr(frame, "database", None)
    verifier = getattr(config, "verifier", None)
    if rows is None or database is None or verifier is None:
        return False
    if binding_problem(
        rows,
        getattr(verifier, "surfaces", None) or {},
        getattr(database, "read", None) or {},
        getattr(frame, "identity_field", None),
    ):
        return False
    return all(surface_of(e) == rows.target_surface for e in others)


def detect_row_versions(data: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """A version source for an update or found check, from the contract
    alone: on PostgreSQL, the target's own row versions
    (``frame.row_versions``). Found only when every write effect reads one
    surface, that surface's read is a plain ``SELECT ... FROM <table> t``
    with no other ``FROM`` over the frame's own connection, and it selects the identity field
    straight from one column. Anything else: None (no excuse).

    A MySQL or MariaDB target has no such source: ``updated_at`` and
    ``ON UPDATE`` columns read the same after one save as after two, so they
    can't count saves. Those need a history read (``frame.versions``) or the
    database's change log."""

    frame = data.get("frame") if isinstance(data, Mapping) else None
    if not isinstance(frame, Mapping):
        return None
    database = frame.get("database")
    if not isinstance(database, Mapping) or database.get("dialect") != "postgresql":
        return None
    names: set[str] = set()
    for effect in data.get("effects") or []:
        if not isinstance(effect, Mapping):
            return None
        if effect.get("kind") not in ("record_written", "field_equals"):
            continue
        if effect.get("kind") == "record_written" and effect.get("expected_count") == 0:
            continue  # a guard reads; it has no target
        probe = str(effect.get("probe") or "")
        if not probe.startswith("surface="):
            return None
        names.add(probe[len("surface=") :].split("|", 1)[0].strip())
    if len(names) != 1:
        return None
    (surface,) = names
    verifier = data.get("verifier")
    surfaces = verifier.get("surfaces") if isinstance(verifier, Mapping) else None
    source = surfaces.get(surface) if isinstance(surfaces, Mapping) else None
    if not isinstance(source, Mapping) or source.get("kind") != "sql":
        return None
    read = database.get("read") or {}
    if any(source.get(k) != read.get(k) for k in ("sql_driver", "sql_connect_args")):
        return None  # another database: its rows aren't the frame's
    from openadapt_flow.runtime.effects.row_versions import single_table_read

    # One SELECT over one table: the declared record is one row, so the
    # row's versions are the record's. A read that also looks into other
    # tables (a list of tags, a child row, a UNION) declares rows this
    # source doesn't count.
    found = single_table_read(
        str(source.get("sql_query") or ""), str(frame.get("identity_field") or "id")
    )
    if found is None:
        return None
    table, key_column = found
    return {
        "source": "postgresql_xmin",
        "table": table,
        "key_column": key_column,
        "target_surface": surface,
    }


def http_sender(
    base_url: str, headers: Mapping[str, str], requests: Sequence[tuple[str, str]]
) -> Callable[[], set[str]]:
    """A ``send`` for :func:`probe`: each read-only request, in order."""

    import requests as _requests

    check_read_only(requests)

    def send() -> set[str]:
        names: set[str] = set()
        for method, path in requests:
            url = base_url.rstrip("/") + "/" + path.lstrip("/")
            _requests.request(method.upper(), url, headers=dict(headers), timeout=60)
            names |= _learn.query_fields(path)
        return names

    return send


def write_report(proposal: Mapping[str, Any], out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "probe_proposal.json"
    path.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n")
    return path


def cmd_probe(args: Any) -> int:
    """``openadapt-flow learn probe``."""

    import os

    import yaml

    from openadapt_flow.generate.verify_contract import choose

    try:
        requests = []
        for spec in args.get or []:
            requests.append(("GET", spec))
        for spec in args.head or []:
            requests.append(("HEAD", spec))
        check_read_only(requests)
        headers = (
            json.loads(os.environ.get(args.headers_env, "{}"))
            if args.headers_env
            else {}
        )
        if not isinstance(headers, dict):
            raise ProbeError(f"{args.headers_env} must hold a JSON object of headers")
        values = yaml.safe_load(Path(args.values).read_text(encoding="utf-8")) or {}
        extra = dict(yaml.safe_load(args.extra) or {}) if args.extra else {}
        contract, params = choose(Path(args.manifest), args.kind, values, extra)
        schema = _learn._schema(args)
        users: Optional[_learn.Users]
        if args.users:
            table, _, key = args.users.partition(".")
            users = _learn.users_from(table, key or None, schema, args.user_column)
        else:
            users = users_for_actor_params(Path(args.manifest), schema)
        send = http_sender(args.base_url, headers, requests)
        eps = probe(contract, params, kind=args.kind, actor=args.actor, send=send,
                    rounds=args.rounds)  # fmt: skip
        proposal = propose(eps, users)
        report = write_report(proposal, Path(args.out))
        written: list[tuple[Path, int]] = []
        if args.apply:
            files = [p for ps in _learn.contracts_from_manifest(Path(args.manifest)).values()
                     for p in ps]  # fmt: skip
            written = apply(proposal, sorted(set(files)))
    except (_learn.LearnError, OSError, ValueError) as exc:
        print(f"learn probe: {exc}", file=__import__("sys").stderr)
        return 2
    body = (proposal.get("kinds") or {}).get("read_only_probe") or {}
    print(
        f"{len(body.get('excuses') or [])} excuse(s) learned from {len(eps)} read-only "
        f"round(s); {len(body.get('stays_a_stop') or [])} move(s) stay a stop. "
        f"Report: {report}."
        + (
            f" Written into {len(written)} contract(s)."
            if args.apply
            else " Run again with --apply to write them into the contracts."
        )
    )
    return 0


def register_parser(steps: Any) -> None:
    """The ``probe`` step of ``openadapt-flow learn``."""

    p = steps.add_parser(
        "probe",
        help="learn per-request bookkeeping from read-only requests (no approval)",
        allow_abbrev=False,
    )
    p.add_argument("--manifest", required=True, help="the generator's manifest.yaml")
    p.add_argument(
        "--kind", required=True, help="the kind whose contract frames the probe"
    )
    p.add_argument(
        "--values", required=True, help="YAML: one task's values for that kind"
    )
    p.add_argument("--extra", default=None, help="YAML: extra parameters (actor ids)")
    p.add_argument("--actor", required=True, help="the agent's user id in the app")
    p.add_argument("--base-url", required=True)
    p.add_argument("--headers-env", default=None,
                   help="environment variable holding the agent's request headers as JSON")  # fmt: skip
    p.add_argument("--get", action="append", default=[], metavar="PATH")
    p.add_argument("--head", action="append", default=[], metavar="PATH")
    p.add_argument("--rounds", type=int, default=DEFAULT_ROUNDS)
    p.add_argument(
        "--users", default=None, help="TABLE or TABLE.KEY (default: derived)"
    )
    p.add_argument("--user-column", action="append", default=[], metavar="TABLE.COLUMN")
    p.add_argument("--schema", default=None)
    p.add_argument("--read", default=None)
    p.add_argument("--out", default="learn")
    p.add_argument("--apply", action="store_true",
                   help="write the excuses into every contract the manifest lists")  # fmt: skip
    p.set_defaults(func=cmd_probe)
