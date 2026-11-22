"""The one-page Readiness Scorecard: ``scorecard.html`` and ``scorecard.json``.

``scorecard.json`` (``openadapt.readiness-scorecard/v1``) carries aggregates
only: counts, field names with the kind of error, times, costs and the
savings estimate. It holds no field values and no record values, so it can
leave the machine. ``scorecard.html`` is for the person on this machine and
shows the values of each field error side by side.

The savings estimate is shown with its formula::

    minutes saved per task = (human median - agent median) / 60
    estimated value        = minutes saved per task x matched cases / 60
                             x hourly cost - model cost

The value is given only when the operator passes ``--hourly-cost`` and every
task's model cost is known; it is marked as an estimate.

``task_type`` names the kind of task (``--task-type``, an identifier;
``unspecified`` when not given). ``model_cost_usd.source`` is ``metered``
when every known cost was measured by the flight proxy, ``reported`` when
any came from the agent's own ``usage.json``, and ``unknown`` when no cost
is known. ``egress`` says how the agent's model traffic left the machine
(``scrubbed``, ``blocked`` or ``unscrubbed``), whether that was enforced by
the OS block, and how many model calls the proxy saw.

``scope_ok`` is false and ``failed_scope`` counts the case when a write
reached the app (``FAILED_SCOPE``) or the run couldn't vouch for the other
tabs, popups, contexts and workers (``NOT_CHECKED``, reason
``extra_target``).
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import statistics
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.readiness.runner import SCOPE_STATEMENT

__all__ = ["REPLAY_STATE", "SCORECARD_SCHEMA", "build_scorecard", "render_html", "write_scorecard"]

SCORECARD_SCHEMA = "openadapt.readiness-scorecard/v1"
REPLAY_STATE = {
    "sandbox": (
        "Sandbox: each task ran on a sandbox or test tenant reset to synthetic "
        "\"before\" state; the person's saved values are the reference."
    ),
    "run-ahead": (
        "Run-ahead: the agent worked each live queue item first and stopped "
        "before save; the person then did the item, paired by task id."
    ),
}
SAVINGS_FORMULA = (
    "(human_median_s - agent_median_s) / 60 = minutes saved per task; "
    "x matched cases / 60 x hourly cost - model cost = estimated value"
)
_ERROR_KINDS = ("mismatched", "missing", "extra", "uncompared", "wrong_record")
TASK_TYPE_DEFAULT = "unspecified"
_TASK_TYPE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
EGRESS_MODES = ("scrubbed", "blocked", "unscrubbed")


def check_task_type(value: Optional[str]) -> str:
    """``--task-type``: a lowercase identifier, or ``unspecified``."""

    if value is None or value == "":
        return TASK_TYPE_DEFAULT
    if not _TASK_TYPE.match(value):
        raise ValueError(
            "task type must be a lowercase identifier (letters, digits, '_', '.', '-')"
        )
    return value


def _money(value: float) -> str:
    """``-$0.75``, not ``$-0.75``."""

    return f"-${abs(value):,.2f}" if value < 0 else f"${value:,.2f}"


def _cost_source(cases: list[Mapping[str, Any]]) -> str:
    sources = [
        c.get("model_cost_source")
        for c in cases
        if isinstance(c.get("model_cost_usd"), (int, float))
    ]
    if not sources:
        return "unknown"
    return "metered" if all(s == "metered" for s in sources) else "reported"


def _egress(cases_doc: Mapping[str, Any], cases: list[Mapping[str, Any]]) -> dict[str, Any]:
    run = dict(cases_doc.get("egress") or {})
    mode = run.get("mode")
    calls: Optional[int] = 0
    for case in cases:
        n = case.get("model_calls_metered")
        if case.get("reason") == "not_run":
            continue
        if not isinstance(n, int) or isinstance(n, bool):
            calls = None
            break
        calls += n
    if mode not in EGRESS_MODES:
        # No flight proxy ran: nothing was scrubbed or metered.
        return {"mode": "unscrubbed", "enforced": False, "model_calls": None}
    return {
        "mode": mode,
        "enforced": bool(run.get("enforced")),
        "model_calls": calls,
    }


def _stats(values: list[float]) -> dict[str, Optional[float]]:
    if not values:
        return {"median": None, "mean": None}
    return {
        "median": round(statistics.median(values), 3),
        "mean": round(statistics.fmean(values), 3),
    }


def build_scorecard(
    cases_doc: Mapping[str, Any],
    *,
    hourly_cost: Optional[float] = None,
    agent_label: Optional[str] = None,
    task_type: Optional[str] = None,
    now: Optional[_dt.datetime] = None,
) -> dict[str, Any]:
    """The aggregate scorecard (no values) from ``compare_runs``'s output."""

    task_type = check_task_type(task_type)
    cases = list(cases_doc.get("cases") or [])
    status = [c["status"] for c in cases]
    extra_target = sum(
        1 for c in cases if c["status"] == "NOT_CHECKED" and c.get("reason") == "extra_target"
    )
    failed_scope = status.count("FAILED_SCOPE") + extra_target
    field_errors = []
    for case in cases:
        if case["status"] == "WRONG_RECORD":
            rows = [r for r in case["fields"] if r["kind"] == "wrong_record"]
            names = [r["name"] for r in rows] or ["<record>"]
            field_errors += [
                {"task_id": case["task_id"], "field": n, "kind": "wrong_record"}
                for n in names
            ]
            continue
        for row in case.get("fields") or []:
            if row["kind"] in _ERROR_KINDS:
                field_errors.append(
                    {"task_id": case["task_id"], "field": row["name"], "kind": row["kind"]}
                )
    agent_times = [
        float(c["agent_time_s"]) for c in cases if isinstance(c.get("agent_time_s"), (int, float))
    ]
    human_times = [
        float(c["human_time_s"]) for c in cases if isinstance(c.get("human_time_s"), (int, float))
    ]
    ran = [c for c in cases if c.get("reason") != "not_run"]
    known = [
        float(c["model_cost_usd"])
        for c in ran
        if isinstance(c.get("model_cost_usd"), (int, float))
    ]
    unknown = len(ran) - len(known)
    cost_total = round(sum(known), 6) if known else (0.0 if not ran else None)
    agent = _stats(agent_times)
    human = _stats(human_times)
    matched = status.count("MATCHED")
    minutes = None
    if agent["median"] is not None and human["median"] is not None:
        minutes = round((human["median"] - agent["median"]) / 60.0, 3)
    value = None
    why = None
    if hourly_cost is None:
        why = "no --hourly-cost given"
    elif minutes is None:
        why = "no agent or human times"
    elif unknown:
        why = f"model cost unknown for {unknown} task(s)"
    else:
        value = round(minutes * matched / 60.0 * hourly_cost - (cost_total or 0.0), 2)
    landed = sum(int(c.get("landed_writes") or 0) for c in cases)
    landed = max(landed, int(cases_doc.get("runs_landed_writes") or 0))
    mode = str(cases_doc.get("mode") or "sandbox")
    return {
        "schema": SCORECARD_SCHEMA,
        "generated_at": (now or _dt.datetime.now(_dt.timezone.utc)).isoformat(
            timespec="seconds"
        ),
        "agent_label": agent_label or cases_doc.get("agent_label") or "agent",
        "task_type": task_type,
        "mode": mode,
        "replay_state": REPLAY_STATE.get(mode, mode),
        "scope_statement": SCOPE_STATEMENT,
        "cases_total": len(cases),
        "matched": matched,
        "mismatched": status.count("MISMATCHED") + status.count("WRONG_RECORD"),
        "not_comparable": status.count("NOT_COMPARABLE"),
        "not_checked": status.count("NOT_CHECKED") + status.count("FAILED_SCOPE"),
        "failed_scope": failed_scope,
        "scope_ok": landed == 0 and failed_scope == 0,
        "risks": {
            "wrong_record": status.count("WRONG_RECORD"),
            "out_of_scope_request": sum(
                int(c.get("out_of_scope_requests") or 0) for c in cases
            ),
            "double_save": sum(1 for c in cases if c.get("double_save")),
            "undeclared_field": sum(
                1 for c in cases if int(c.get("undeclared_fields") or 0) > 0
            ),
        },
        "field_errors": field_errors,
        "agent_time_s": agent,
        "human_time_s": human,
        "model_cost_usd": {
            "total": cost_total,
            "per_task": round(cost_total / len(known), 6) if known and cost_total is not None else None,
            "unknown_count": unknown,
            "source": _cost_source(ran),
        },
        "egress": _egress(cases_doc, ran),
        "savings": {
            "formula": SAVINGS_FORMULA,
            "minutes_saved_per_task": minutes,
            "est_value_usd": value,
            "estimate": True,
            "inputs": {
                "human_median_s": human["median"],
                "agent_median_s": agent["median"],
                "matched_cases": matched,
                "hourly_cost_usd": hourly_cost,
                "model_cost_usd": cost_total,
                "why_no_value": why,
            },
        },
        "landed_writes": landed,
    }


CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mute:#59636e;--line:#d1d9e0;--bad:#b42318;--warn:#9a6700;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mute:#9198a1;--line:#3d444d;--bad:#f85149;--warn:#d29922;--ok:#3fb950}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px}
h1{font-size:22px;margin-bottom:4px}h2{font-size:17px;margin-top:24px}.mute{color:var(--mute)}
.tiles{display:flex;gap:12px;flex-wrap:wrap}.tile{border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}
.tile b{display:block;font-size:26px}.bad{color:var(--bad)}.ok{color:var(--ok)}.warn{color:var(--warn)}
table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
.flag{border:2px solid var(--bad);border-radius:8px;padding:8px 12px;color:var(--bad);font-weight:600}
@media print{body{margin:0;font-size:12px}.tile b{font-size:20px}}
"""


def _v(value: Any) -> str:
    if value is None:
        return "<span class=mute>(none)</span>"
    return html.escape(str(value))


def _num(value: Any, unit: str = "") -> str:
    if value is None:
        return "unknown"
    return f"{value:g}{unit}" if isinstance(value, (int, float)) else html.escape(str(value))


def render_html(card: Mapping[str, Any], cases_doc: Mapping[str, Any]) -> str:
    """One page. It shows field values, so it stays on this machine."""

    risks = card["risks"]
    tiles = [
        ("Matched", f"{card['matched']} of {card['cases_total']}", "ok"),
        ("Mismatched", card["mismatched"], "bad" if card["mismatched"] else ""),
        ("Wrong record", risks["wrong_record"], "bad" if risks["wrong_record"] else ""),
        ("Not comparable", card["not_comparable"], "warn" if card["not_comparable"] else ""),
        ("Not checked", card["not_checked"], "warn" if card["not_checked"] else ""),
        ("Writes that reached the app", card["landed_writes"], "bad" if card["landed_writes"] else "ok"),
    ]
    tile_html = "".join(
        f"<div class=tile><span class=mute>{html.escape(name)}</span><b class='{cls}'>{_v(val)}</b></div>"
        for name, val, cls in tiles
    )
    flag = (
        ""
        if card["scope_ok"]
        else f"<p class=flag>{card['failed_scope']} case(s) failed the scope: a write "
        "reached the app, or the run couldn't vouch for another tab, popup, "
        "browser context or worker. The scorecard is not a clean shadow run.</p>"
    )
    diffs = []
    for case in cases_doc.get("cases") or []:
        if case["status"] in ("MATCHED",):
            continue
        rows = [r for r in case.get("fields") or [] if r["kind"] != "matched"]
        reason = case.get("reason") or ""
        if not rows:
            diffs.append(
                f"<tr><td>{html.escape(case['task_id'])}</td><td>{case['status']}</td>"
                f"<td colspan=3 class=mute>{html.escape(str(reason))}</td></tr>"
            )
        for row in rows:
            diffs.append(
                f"<tr><td>{html.escape(case['task_id'])}</td><td>{case['status']}</td>"
                f"<td>{html.escape(row['name'])} <span class=mute>{row['kind']}</span></td>"
                f"<td>{_v(row.get('human'))}</td><td class=bad>{_v(row.get('proposed'))}</td></tr>"
            )
    diff_table = (
        "<table><tr><th>Task</th><th>Case</th><th>Field</th><th>Person saved</th>"
        "<th>Agent would save</th></tr>" + "".join(diffs) + "</table>"
        if diffs
        else "<p class=mute>No field errors.</p>"
    )
    cost = card["model_cost_usd"]
    savings = card["savings"]
    value = savings["est_value_usd"]
    value_text = (
        f"{_money(value)} (estimate)"
        if value is not None
        else f"not estimated: {html.escape(str(savings['inputs']['why_no_value']))}"
    )
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Readiness Scorecard</title><style>{CSS}</style></head><body>"
        f"<h1>Readiness Scorecard: {html.escape(card['agent_label'])}</h1>"
        f"<p class=mute>{html.escape(card['generated_at'])}. {html.escape(card['replay_state'])}</p>"
        f"<p class=mute>Scope: {html.escape(card['scope_statement'])}</p>"
        f"<p class=mute>Task type: {html.escape(card['task_type'])}. Model traffic: "
        f"{html.escape(card['egress']['mode'])}"
        f"{'' if card['egress']['enforced'] else ' (not enforced by the OS block)'}.</p>"
        f"{flag}<div class=tiles>{tile_html}</div>"
        "<h2>Risks caught</h2><table>"
        f"<tr><td>Wrong record (the agent was on another record at save)</td><td>{risks['wrong_record']}</td></tr>"
        f"<tr><td>Requests outside the app, refused</td><td>{risks['out_of_scope_request']}</td></tr>"
        f"<tr><td>The same save sent twice</td><td>{risks['double_save']}</td></tr>"
        f"<tr><td>A field the person did not save</td><td>{risks['undeclared_field']}</td></tr>"
        "</table>"
        f"<h2>Field errors</h2>{diff_table}"
        "<h2>Time and cost</h2><table>"
        f"<tr><td>Agent time to the save (median, mean)</td><td>{_num(card['agent_time_s']['median'], ' s')}, {_num(card['agent_time_s']['mean'], ' s')}</td></tr>"
        f"<tr><td>Person's time, first input to save (median, mean)</td><td>{_num(card['human_time_s']['median'], ' s')}, {_num(card['human_time_s']['mean'], ' s')}</td></tr>"
        f"<tr><td>Model cost (total, per task; {html.escape(cost['source'])})</td><td>{_num(cost['total'])} USD, {_num(cost['per_task'])} USD; unknown for {cost['unknown_count']} task(s)</td></tr>"
        f"<tr><td>Minutes saved per task</td><td>{_num(savings['minutes_saved_per_task'])}</td></tr>"
        f"<tr><td>Estimated value</td><td>{value_text}</td></tr>"
        f"<tr><td class=mute colspan=2>Formula: {html.escape(savings['formula'])}</td></tr>"
        "</table></body></html>"
    )


def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def write_scorecard(
    cases_doc: Mapping[str, Any],
    out: Path | str,
    *,
    hourly_cost: Optional[float] = None,
    agent_label: Optional[str] = None,
    task_type: Optional[str] = None,
) -> dict[str, Any]:
    """Write ``scorecard.json`` (aggregates) and ``scorecard.html`` (0600)."""

    out = Path(out)
    card = build_scorecard(
        cases_doc, hourly_cost=hourly_cost, agent_label=agent_label, task_type=task_type
    )
    _write(out / "scorecard.json", json.dumps(card, indent=2, sort_keys=True), 0o644)
    _write(out / "scorecard.html", render_html(card, cases_doc), 0o600)
    return card
