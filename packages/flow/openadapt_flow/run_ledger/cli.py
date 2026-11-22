"""``openadapt-flow ledger``: one accounting row per run, for any executor.

    openadapt-flow ledger add RUN_DIR [--task-type T] [--task-id ID]
    openadapt-flow ledger list [--json]
    openadapt-flow ledger show ROW_ID [--show-values]
    openadapt-flow ledger summary [--since ISO] [--task-type T] [--json]
    openadapt-flow ledger export --out FILE
    openadapt-flow ledger push [--host URL] [--dry-run]

``add`` reads the run's files and appends a value-free row. ``show
--show-values`` is the only command that prints values, and only from the
local private sidecar. ``export`` and ``push`` send the newest row per run,
value-free, and refuse if any value from a private sidecar appears in what
would leave. See ``docs/RUN_LEDGER.md``.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional, Sequence

if TYPE_CHECKING:
    from openadapt_flow.run_ledger.store import LedgerStore

API_KEY_ENV = "OPENADAPT_API_KEY"
INGEST_PATH = "/api/runs/ingest-agent-run"
_OUTCOMES = ("VERIFIED", "ESCALATED", "CAUGHT_ERROR", "FAILED", "UNKNOWN")


class LedgerRefused(Exception):
    """An export or push that would let a value leave. Nothing was written."""


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser(
        "ledger",
        help="Per-run accounting rows (outcome, time, cost, fields) for any executor",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--home",
        default=None,
        help="OpenAdapt home (default: $OPENADAPT_HOME, else ~/.openadapt)",
    )
    verbs = parser.add_subparsers(dest="ledger_cmd", required=True)

    add = verbs.add_parser(
        "add", help="Read a run's files and append its row", allow_abbrev=False
    )
    add.add_argument("run", help="Run directory (or one run record file)")
    add.add_argument("--task-type", default=None, help="Operator label, e.g. intake")
    add.add_argument("--task-id", default=None, help="Override the task id")
    add.set_defaults(func=cmd_add)

    lst = verbs.add_parser("list", help="List rows", allow_abbrev=False)
    lst.add_argument("--json", action="store_true", help="One JSON row per line")
    lst.set_defaults(func=cmd_list)

    show = verbs.add_parser("show", help="Show one row", allow_abbrev=False)
    show.add_argument("row_id")
    show.add_argument(
        "--show-values",
        action="store_true",
        help="Also print the field values from the local private sidecar",
    )
    show.set_defaults(func=cmd_show)

    summary = verbs.add_parser(
        "summary",
        help="Counts per outcome, minutes, cost, duration",
        allow_abbrev=False,
    )
    summary.add_argument("--since", default=None, help="ISO date or time")
    summary.add_argument("--task-type", default=None)
    summary.add_argument("--json", action="store_true")
    summary.set_defaults(func=cmd_summary)

    export = verbs.add_parser(
        "export", help="Write value-free rows as JSONL", allow_abbrev=False
    )
    export.add_argument("--out", required=True, help="Output file")
    export.add_argument(
        "--require-scrubber",
        action="store_true",
        help="Refuse when openadapt-privacy's text scrubber isn't installed",
    )
    export.set_defaults(func=cmd_export)

    push = verbs.add_parser(
        "push",
        help=f"Send value-free run summaries to OpenAdapt Cloud ({API_KEY_ENV})",
        allow_abbrev=False,
    )
    push.add_argument("--host", default=None, help="Cloud origin")
    push.add_argument(
        "--dry-run", action="store_true", help="Print what would be sent; send nothing"
    )
    push.set_defaults(func=cmd_push)


# -- add / list / show ------------------------------------------------------------------


def _store(args: argparse.Namespace) -> LedgerStore:
    # Imported here so that building the parser (``--version``, ``--help``)
    # never needs the ledger schema: an install whose openadapt-types predates
    # it still starts, and only ``ledger`` itself refuses.
    try:
        from openadapt_flow.run_ledger.store import LedgerStore
    except ImportError as exc:
        raise SystemExit(
            "ledger: this install's openadapt-types has no run ledger schema "
            f"({exc}); upgrade openadapt-types"
        ) from None
    return LedgerStore(args.home)


def cmd_add(args: argparse.Namespace) -> int:
    from pydantic import ValidationError

    run = Path(args.run)
    if not run.exists():
        print(f"ledger add: {run} does not exist", file=sys.stderr)
        return 2
    try:
        result = _store(args).add(run, task_type=args.task_type, task_id=args.task_id)
    except ValidationError as exc:
        bad = sorted({str(e["loc"][0]) for e in exc.errors() if e.get("loc")})
        print(
            "ledger add: refused a value that isn't a closed label: "
            + ", ".join(bad or ["row"]),
            file=sys.stderr,
        )
        return 2
    row = result.row
    state = "added" if result.added else "already in the ledger"
    print(
        f"{row.row_id}  {row.outcome.value} ({row.outcome_reason})  "
        f"task {row.task_id}  {state}"
    )
    return 0


def _money(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:.4f}"


def _secs(value: Optional[float]) -> str:
    return "-" if value is None else f"{value:.1f}s"


def cmd_list(args: argparse.Namespace) -> int:
    rows = _store(args).rows()
    if args.json:
        for row in rows:
            print(json.dumps(row.to_json(), sort_keys=True))
        return 0
    if not rows:
        print("No rows yet. Add one with: openadapt-flow ledger add RUN_DIR")
        return 0
    for row in rows:
        print(
            f"{row.row_id}  {row.outcome.value:<12} {row.outcome_reason:<26} "
            f"{row.task_type:<12} {row.task_id:<24} {_secs(row.duration_s):>8} "
            f"{_money(row.model.cost_usd):>10}"
        )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        row = store.find(args.row_id)
    except ValueError:
        row = None
    if row is None:
        print(f"ledger show: no row {args.row_id}", file=sys.stderr)
        return 2
    doc: dict[str, Any] = {"row": row.to_json()}
    if args.show_values:
        sidecar = store.sidecar(row.row_id)
        doc["values"] = (sidecar or {}).get("fields") or {}
    print(json.dumps(doc, indent=2, sort_keys=True))
    return 0


# -- summary ----------------------------------------------------------------------------


def _since(text: Optional[str]) -> Optional[datetime]:
    if not text:
        return None
    moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        from datetime import timezone

        moment = moment.replace(tzinfo=timezone.utc)
    return moment


def summarize(
    rows: Sequence[Any],
    *,
    since: Optional[datetime] = None,
    task_type: Optional[str] = None,
) -> dict[str, Any]:
    """Counts per outcome, a person's minutes, model cost, median duration.

    A row whose time or cost is unknown is counted as unknown, never as 0.
    A row with no ``started_at`` is kept by ``since`` only if it has none to
    compare (it is reported in ``undated``).
    """

    picked = []
    undated = 0
    for row in rows:
        if task_type is not None and row.task_type != task_type:
            continue
        if since is not None:
            if row.started_at is None:
                undated += 1
                continue
            if datetime.fromisoformat(row.started_at.replace("Z", "+00:00")) < since:
                continue
        picked.append(row)
    counts = {name: 0 for name in _OUTCOMES}
    for row in picked:
        counts[row.outcome.value] += 1
    minutes = [
        r.human.intervention_s for r in picked if r.human.intervention_s is not None
    ]
    costs = [r.model.cost_usd for r in picked if r.model.cost_usd is not None]
    durations = [r.duration_s for r in picked if r.duration_s is not None]
    return {
        "rows": len(picked),
        "outcomes": counts,
        "intervention_minutes": round(sum(minutes) / 60.0, 2),
        "intervention_unknown_rows": len(picked) - len(minutes),
        "model_cost_usd": round(sum(costs), 6),
        "model_cost_unknown_rows": len(picked) - len(costs),
        "median_duration_s": round(statistics.median(durations), 3)
        if durations
        else None,
        "duration_unknown_rows": len(picked) - len(durations),
        "fields_differing": sum(r.fields_differing for r in picked),
        "undated_rows_skipped": undated,
    }


def cmd_summary(args: argparse.Namespace) -> int:
    try:
        since = _since(args.since)
    except ValueError:
        print("ledger summary: --since must be an ISO date or time", file=sys.stderr)
        return 2
    report = summarize(_store(args).latest(), since=since, task_type=args.task_type)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    counts = report["outcomes"]
    print(f"{report['rows']} runs")
    for name in _OUTCOMES:
        print(f"  {name:<13}{counts[name]}")
    unknown_m = report["intervention_unknown_rows"]
    print(
        f"A person's time: {report['intervention_minutes']} min"
        + (f" (unknown for {unknown_m} runs)" if unknown_m else "")
    )
    unknown_c = report["model_cost_unknown_rows"]
    print(
        f"Model cost: ${report['model_cost_usd']:.4f}"
        + (f" (unknown for {unknown_c} runs)" if unknown_c else "")
    )
    median = report["median_duration_s"]
    print("Median duration: " + ("-" if median is None else f"{median}s"))
    return 0


# -- leaving this computer ----------------------------------------------------------------


def _label_values(doc: dict[str, Any]) -> list[tuple[str, str]]:
    labels = [
        ("task_id", doc["task_id"]),
        ("task_type", doc["task_type"]),
        ("executor_label", doc["executor_label"]),
    ]
    labels += [
        ("fields_written.name", f["name"]) for f in doc.get("fields_written", [])
    ]
    return labels


def outbound_lines(
    store: LedgerStore,
    *,
    summary: bool,
    scrubber: Optional[Any] = None,
) -> list[str]:
    """The newest row per run, serialised, after both guards.

    1. Every value in every local sidecar is looked for in the output; one
       hit refuses the whole export.
    2. When a text scrubber is given, each label (task id, task type,
       executor label, field names) must come back from it unchanged; a label
       the scrubber would rewrite looks like personal data and is refused.
    """

    from openadapt_types.run_ledger import AgentRunSummaryV1, RunLedgerRowV1

    values = store.sidecar_values()
    lines: list[str] = []
    for row in store.latest():
        doc = row.summary().to_json() if summary else row.to_json()
        model = AgentRunSummaryV1 if summary else RunLedgerRowV1
        allowed = {f.alias or name for name, f in model.model_fields.items()}
        if set(doc) - allowed:
            raise LedgerRefused(f"row {row.row_id} has a key outside the contract")
        model.model_validate(doc)
        if scrubber is not None:
            for where, label in _label_values(doc):
                try:
                    scrubbed = scrubber.scrub_text(label)
                except Exception as exc:  # a scrubber that can't run refuses
                    raise LedgerRefused(
                        f"the text scrubber could not run ({type(exc).__name__}); "
                        "nothing was written"
                    ) from exc
                if scrubbed != label:
                    raise LedgerRefused(
                        f"row {row.row_id}: the scrubber would rewrite its {where}"
                    )
        line = json.dumps(doc, sort_keys=True)
        for value in values:
            if value in line:
                raise LedgerRefused(
                    f"row {row.row_id} contains a value from a private sidecar"
                )
        lines.append(line)
    return lines


def _scrubber(require: bool) -> Optional[Any]:
    from openadapt_flow.privacy import PrivacyNotAvailable, get_text_scrubber

    try:
        scrubber = get_text_scrubber()
    except PrivacyNotAvailable as exc:
        raise LedgerRefused(str(exc)) from exc
    if scrubber is None and require:
        raise LedgerRefused(
            "openadapt-privacy's text scrubber isn't available (--require-scrubber)"
        )
    if scrubber is None:
        print(
            "ledger: openadapt-privacy's text scrubber isn't available; rows were "
            "checked against the local value sidecars only",
            file=sys.stderr,
        )
    return scrubber


def cmd_export(args: argparse.Namespace) -> int:
    try:
        scrubber = _scrubber(args.require_scrubber)
        lines = outbound_lines(_store(args), summary=False, scrubber=scrubber)
    except LedgerRefused as exc:
        print(f"ledger export: refused: {exc}", file=sys.stderr)
        return 2
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    print(f"Wrote {len(lines)} rows to {out}")
    return 0


Poster = Callable[[str, dict[str, str], dict[str, Any]], int]


def _post(url: str, headers: dict[str, str], payload: dict[str, Any]) -> int:
    import httpx

    response = httpx.post(
        url, headers=headers, json=payload, timeout=30.0, follow_redirects=False
    )
    return response.status_code


def push(
    store: LedgerStore,
    *,
    host: Optional[str] = None,
    dry_run: bool = False,
    poster: Poster = _post,
    environ: Optional[dict[str, str]] = None,
    scrubber: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """POST each newest row (minus ``sources``) to ``/api/runs/ingest-agent-run``.

    Refused, before anything else, when ``OPENADAPT_API_KEY`` isn't set.
    The destination must pass the same policy ``ingest-report`` uses.
    """

    env = os.environ if environ is None else environ
    key = env.get(API_KEY_ENV, "").strip()
    if not key and not dry_run:
        raise LedgerRefused(f"{API_KEY_ENV} isn't set; nothing was sent")
    lines = outbound_lines(store, summary=True, scrubber=scrubber)
    payloads = [json.loads(line) for line in lines]
    if dry_run:
        return payloads
    from openadapt_flow.hosted import (
        HostedError,
        resolve_destination_policy,
        resolve_host,
    )

    try:
        origin = resolve_host(host)
        resolve_destination_policy(origin)
    except HostedError as exc:
        raise LedgerRefused(f"destination refused: {exc}") from exc
    url = origin + INGEST_PATH
    headers = {"Authorization": f"Bearer {key}"}
    results = []
    for payload in payloads:
        try:
            status = poster(url, headers, payload)
        except Exception:  # noqa: BLE001 - a transport error never echoes the key
            status = 0
        results.append({"row_id": payload["row_id"], "status": status})
    return results


def cmd_push(args: argparse.Namespace) -> int:
    try:
        if not args.dry_run and not os.environ.get(API_KEY_ENV, "").strip():
            raise LedgerRefused(f"{API_KEY_ENV} isn't set; nothing was sent")
        results = push(
            _store(args),
            host=args.host,
            dry_run=args.dry_run,
            scrubber=_scrubber(False),
        )
    except LedgerRefused as exc:
        print(f"ledger push: refused: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        for payload in results:
            print(json.dumps(payload, sort_keys=True))
        return 0
    failed = [r for r in results if r["status"] not in (200, 201, 202)]
    print(f"Sent {len(results) - len(failed)} of {len(results)} rows")
    for item in failed:
        print(f"  {item['row_id']}: HTTP {item['status']}", file=sys.stderr)
    return 1 if failed else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="openadapt-flow", allow_abbrev=False)
    register(parser.add_subparsers(dest="command", required=True))
    args = parser.parse_args(argv)
    return int(args.func(args))


__all__ = [
    "API_KEY_ENV",
    "INGEST_PATH",
    "LedgerRefused",
    "main",
    "outbound_lines",
    "push",
    "register",
    "summarize",
]
