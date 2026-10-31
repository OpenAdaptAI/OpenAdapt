"""Shadow mode's report: ``report.json`` and a ``report.html`` a person reads
in two minutes. It holds the values of the writes it describes."""

from __future__ import annotations

import html
import json
import statistics
from pathlib import Path
from typing import Any

SCHEMA = "openadapt.shadow-report/v1"
ORDER = ("REFUTED", "UNCERTAIN", "CONFIRMED")


def _ms(values: list[float]) -> dict[str, float]:
    if not values:
        return {"median": 0.0, "max": 0.0}
    return {"median": round(statistics.median(values), 1), "max": round(max(values), 1)}


#: How a REFUTED write landed wrong, from the verifier's reasons (first match).
KINDS = (
    ("wrong or missing", "a value differs from what the agent sent"),
    ("not one", "saved more than once"),
    ("also the person", "saved more than once"),
    ("other resource", "another record changed"),
    ("outside the declared write", "another record changed"),
    ("not given by the task", "holds a value the agent didn't send"),
    ("someone else changed", "changed by someone else"),
    (
        "version in between was not the person",
        "saved as another record first, then corrected",
    ),
    ("after creating it", "changed after it was created"),
)


def kind_of(reasons: list[str]) -> str:
    text = " ".join(reasons)
    for needle, kind in KINDS:
        if needle in text:
            return kind
    return "other (see the reasons)"


def summarize(shadow: Any) -> dict[str, Any]:
    checks = []
    for ep in shadow.episodes:
        checks.append(
            {
                "id": ep.id,
                "kind": ep.kind,
                "name": ep.name,
                "verdict": ep.verdict or "PENDING",
                "reasons": list(ep.reasons),
                "sent": dict(ep.params),
                "records": sorted(ep.refs),
                "writes": [
                    {
                        "seq": w.seq,
                        "method": w.method,
                        "path": w.path,
                        "status": w.status,
                        "held_ms": round(w.held_ms, 1),
                        "proxy_ms": round(w.proxy_ms, 1),
                        "error": w.error,
                    }
                    for w in ep.writes
                ],
                "evidence": ep.evidence,
                "late": (
                    {"verdict": ep.late_verdict, "reasons": list(ep.late_reasons)}
                    if ep.late_verdict
                    else (
                        {"verdict": "PENDING", "reasons": ["recheck not yet due"]}
                        if ep.recheck_at
                        else None
                    )
                ),
            }
        )
    counts = {
        v: sum(1 for c in checks if c["verdict"] == v) for v in (*ORDER, "PENDING")
    }
    kinds: dict[str, int] = {}
    for c in checks:
        if c["verdict"] == "REFUTED":
            kinds[kind_of(c["reasons"])] = kinds.get(kind_of(c["reasons"]), 0) + 1
    held = [w.held_ms for w in shadow.writes]
    added = [w.held_ms + w.proxy_ms for w in shadow.writes]
    first = min((w.started for w in shadow.writes), default=None)
    return {
        "schema": SCHEMA,
        "mode": shadow.mode,
        "upstream": f"{shadow.upstream.scheme}://{shadow.upstream.netloc}",
        "started": shadow.started,
        "first_write_after_s": None
        if first is None
        else round(first - shadow.started, 1),
        "writes_seen": len(shadow.writes),
        "checks": len(checks),
        "counts": counts,
        "late_refuted": sum(
            1 for c in checks if (c["late"] or {}).get("verdict") == "REFUTED"
        ),
        "rechecks_pending": sum(
            1 for c in checks if (c["late"] or {}).get("verdict") == "PENDING"
        ),
        "refuted_by_kind": dict(sorted(kinds.items(), key=lambda kv: -kv[1])),
        "overhead_ms": {"held_for_baseline": _ms(held), "added_total": _ms(added)},
        "errors": list(shadow.errors),
        "items": sorted(
            checks, key=lambda c: (ORDER + ("PENDING",)).index(c["verdict"])
        ),
    }


def _value(v: Any) -> str:
    if v is None:
        return "<i>none</i>"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, sort_keys=True, default=str)
    return html.escape(str(v))


CSS = """
:root{--bg:#fff;--fg:#1b1f24;--mute:#59636e;--line:#d1d9e0;--bad:#b42318;--warn:#9a6700;--ok:#1a7f37}
@media (prefers-color-scheme:dark){:root{--bg:#0d1117;--fg:#e6edf3;--mute:#9198a1;--line:#3d444d;--bad:#f85149;--warn:#d29922;--ok:#3fb950}}
body{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;max-width:960px;margin:24px auto;padding:0 16px}
h1{font-size:22px}h2{font-size:18px;margin-top:28px}.mute{color:var(--mute)}
.tiles{display:flex;gap:12px;flex-wrap:wrap}.tile{border:1px solid var(--line);border-radius:8px;padding:10px 14px;min-width:120px}
.tile b{display:block;font-size:26px}.REFUTED{color:var(--bad)}.UNCERTAIN{color:var(--warn)}.CONFIRMED{color:var(--ok)}
table{border-collapse:collapse;width:100%;margin:8px 0}td,th{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left;vertical-align:top;overflow-wrap:anywhere}
.item{border:1px solid var(--line);border-radius:8px;padding:10px 14px;margin:12px 0}.diff{color:var(--bad);font-weight:600}
details{margin-top:6px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px}
"""


def _item(c: dict[str, Any]) -> str:
    ev = c.get("evidence") or {}
    writes = "".join(
        f"<tr><td>{w['method']}</td><td>{html.escape(w['path'])}</td><td>{w['status']}</td>"
        f"<td>{w['held_ms'] + w['proxy_ms']:.0f} ms</td></tr>"
        for w in c["writes"]
    )
    fields = ev.get("fields") or []
    differ = [f for f in fields if not f["same"]]
    rows = "".join(
        f"<tr class=diff><td>{html.escape(f['record'])}</td>"
        f"<td>{html.escape(f['field'])}</td><td>{_value(f['sent'])}</td>"
        f"<td>{_value(f['stored'])}</td></tr>"
        for f in differ
    )
    if fields:
        rows += (
            f"<tr><td colspan=4 class=mute>{len(fields) - len(differ)} other "
            "fields hold what the agent sent (all fields are under Evidence)</td></tr>"
        )
    if not rows and c["sent"]:
        rows = "".join(
            f"<tr><td></td><td>{html.escape(k)}</td><td>{_value(v)}</td><td></td></tr>"
            for k, v in c["sent"].items()
        )
    reasons = "".join(f"<li>{html.escape(r)}</li>" for r in c["reasons"])
    late = c.get("late") or {}
    if late:
        reasons += (
            f"<li>Checked again: <b class={late['verdict']}>{late['verdict']}</b>"
            + "".join(f"; {html.escape(r)}" for r in late["reasons"])
            + "</li>"
        )
    before = ", ".join(ev.get("before") or []) or "none"
    return (
        f"<div class=item><b class={c['verdict']}>{c['verdict']}</b> "
        f"<b>{html.escape(c['name'])}</b> <span class=mute>check {c['id']}, "
        f"records: {html.escape(', '.join(c['records']) or 'none')}</span>"
        f"<ul>{reasons}</ul>"
        + (
            "<table><tr><th>Record</th><th>Field</th><th>Agent sent</th>"
            f"<th>Store holds now</th></tr>{rows}</table>"
            if rows
            else ""
        )
        + (
            f"<p class=mute>Before the write, matching records: {html.escape(before)}</p>"
            if c["kind"] == "fhir"
            else ""
        )
        + f"<table><tr><th>Write</th><th>Path</th><th>Reply</th><th>Added</th></tr>{writes}</table>"
        + "<details><summary>Evidence</summary><pre>"
        + html.escape(json.dumps(ev, indent=1, sort_keys=True, default=str)[:20000])
        + "</pre></details></div>"
    )


def render_html(doc: dict[str, Any], setup: dict[str, Any] | None = None) -> str:
    n = doc["counts"]
    tiles = "".join(
        f"<div class=tile><b class={v}>{n[v]}</b>{v.title()}</div>"
        for v in (*ORDER, "PENDING")
        if v != "PENDING" or n[v]
    )
    oh = doc["overhead_ms"]["added_total"]
    if doc.get("late_refuted"):
        tiles += (
            f"<div class=tile><b class=REFUTED>{doc['late_refuted']}</b>"
            "Refuted on recheck</div>"
        )
    sections = []
    for v, title in (
        ("REFUTED", "Landed wrong"),
        ("UNCERTAIN", "Couldn't be confirmed"),
        ("PENDING", "Never checked (the shadow failed; see its errors)"),
    ):
        items = [c for c in doc["items"] if c["verdict"] == v]
        if items:
            sections.append(
                f"<h2>{title} ({len(items)})</h2>" + "".join(map(_item, items))
            )
    late = [
        c for c in doc["items"] if (c.get("late") or {}).get("verdict") == "REFUTED"
    ]
    if late:
        sections.insert(
            0,
            f"<h2>Landed wrong after the check ({len(late)})</h2>"
            "<p class=mute>Confirmed at first, then refuted when checked again: "
            "something changed after the write.</p>" + "".join(map(_item, late)),
        )
    ok = [c for c in doc["items"] if c["verdict"] == "CONFIRMED"]
    if ok:
        sections.append(
            f"<h2>Confirmed ({len(ok)})</h2><details><summary>Show</summary>"
            + "".join(map(_item, ok))
            + "</details>"
        )
    setup_html = ""
    if setup:
        decisions = "".join(
            f"<li>{html.escape(str(d))}</li>" for d in setup.get("decisions", [])
        )
        setup_html = (
            f"<h2>Setup</h2><p>{html.escape(str(setup.get('minutes', '?')))} minutes "
            f"to the first checked write, {len(setup.get('decisions', []))} decisions:</p>"
            f"<ul>{decisions}</ul>"
        )
    errors = ""
    if doc["errors"]:
        errors = (
            "<h2>Shadow errors</h2><pre>"
            + html.escape("\n".join(doc["errors"]))
            + "</pre>"
        )
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>Shadow report</title><style>{CSS}</style></head><body>"
        "<h1>Shadow report</h1>"
        f"<p class=mute>{doc['mode'].upper()} writes to {html.escape(doc['upstream'])}. "
        "Each write was checked against the system of record. The shadow sent "
        "each one on as the agent sent it and blocked none.</p>"
        f"<p>{doc['writes_seen']} writes seen, in {doc['checks']} checks "
        "(a record's create, retries and corrections are one check).</p>"
        f"<div class=tiles>{tiles}</div>"
        + (
            "<p>How they landed wrong:</p><ul>"
            + "".join(
                f"<li>{html.escape(k)}: {v}</li>"
                for k, v in (doc.get("refuted_by_kind") or {}).items()
            )
            + "</ul>"
            if doc.get("refuted_by_kind")
            else ""
        )
        + f"<p class=mute>Added latency per write: median {oh['median']} ms, "
        f"largest {oh['max']} ms.</p>"
        + "".join(sections)
        + setup_html
        + errors
        + "</body></html>"
    )


def write_report(
    doc: dict[str, Any], out: Path, setup: dict[str, Any] | None = None
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    if setup:
        doc = {**doc, "setup": setup}
    (out / "report.json").write_text(json.dumps(doc, indent=2, default=str) + "\n")
    page = out / "report.html"
    page.write_text(render_html(doc, setup))
    return page
