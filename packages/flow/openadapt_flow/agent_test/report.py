"""The test run's outputs: ``results.json`` (written by the runner), ``report.html`` and ``receipt.json``.

The receipt is a small allow-listed summary that can travel without the
evidence: what ran (pack digests, image references, agent id), the counts
and rates, the gate's verdict when a gate ran, and SHA-256 digests of
``results.json`` and ``report.html`` that bind it to them. It holds no task
values, screenshots, transcripts or URLs. With ``--sign-key`` (an Ed25519
private key in PEM) it is signed over its canonical JSON;
``openadapt test verify-receipt`` checks the signature and the digests.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.agent_test.grade import OUTCOMES, RATES
from openadapt_flow.agent_test.packs import canonical_json

RECEIPT_SCHEMA = "openadapt.agent-test-receipt/v1"


def sha256_file(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def _pct(r: dict[str, Any]) -> str:
    lo, hi = r["ci95"]
    return f"{r['rate'] * 100:.1f}% ({r['count']}/{r['of']}; 95% CI {lo * 100:.0f}-{hi * 100:.0f}%)"


LABELS = {
    "completed_correctly": "Completed correctly",
    "silent_corruption": "Silent corruption",
    "collateral_edit": "Collateral edits",
    "false_success": "False claims of success",
    "safe_stop": "Safe stops",
    "unclaimed_success": "Done but not claimed",
    "fault_not_delivered": "Fault not delivered (left out)",
}


def render_html(doc: dict[str, Any], gate: Optional[dict[str, Any]] = None) -> str:
    m, s = doc["meta"], doc["summary"]
    e = html.escape
    rows = []
    for r in doc["runs"]:
        flags = [n for n in ("silent_corruption", "collateral_edit") if r.get(n)]
        rows.append(
            "<tr>"
            f"<td>{e(r['scenario'])} r{r['rep']}</td><td>{e(r['fault'])}</td>"
            f"<td>{e(r['claim']['status'])}</td>"
            f"<td>{'right' if r['store']['correct'] else e('; '.join(r['store']['reasons']))}</td>"
            f"<td class='o-{e(r['outcome'])}'>{e(LABELS.get(r['outcome'], r['outcome']))}</td>"
            f"<td>{e(', '.join(LABELS[f] for f in flags))}</td>"
            f"<td>{r.get('agent_s', 0):.0f}</td><td>{float(r.get('cost_usd') or 0):.3f}</td>"
            "</tr>"
        )
    tiles = "".join(
        f"<div class='tile'><div class='k'>{e(LABELS[n])}</div><div class='v'>{s['rates'][n]['rate'] * 100:.0f}%</div>"
        f"<div class='d'>{s['rates'][n]['count']} of {s['rates'][n]['of']}</div></div>"
        for n in RATES
    )
    gate_html = ""
    if gate:
        cls = "pass" if gate["verdict"] == "pass" else "fail"
        items = "".join(f"<li>{e(x)}</li>" for x in gate["lines"])
        gate_html = f"<section class='gate {cls}'><h2>Gate: {e(gate['verdict'].upper())}</h2><ul>{items}</ul></section>"
    excluded = s["outcomes"]["fault_not_delivered"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agent test report</title>
<style>
:root {{ --bg:#fff; --fg:#1d1d1f; --mut:#6e6e73; --line:#e5e5ea; --bad:#b3261e; --ok:#1b7f3b; --tile:#f5f5f7; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#111; --fg:#f2f2f2; --mut:#a1a1a6; --line:#333; --bad:#ff6b6b; --ok:#5fd38d; --tile:#1c1c1e; }} }}
body {{ background:var(--bg); color:var(--fg); font:15px/1.5 system-ui, sans-serif; margin:0 auto; max-width:1100px; padding:24px 16px; }}
h1 {{ font-size:22px; margin:0 0 4px; }} .meta {{ color:var(--mut); font-size:13px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:20px 0; }}
.tile {{ background:var(--tile); border-radius:10px; padding:12px; }} .k {{ color:var(--mut); font-size:13px; }}
.v {{ font-size:28px; font-weight:600; }} .d {{ color:var(--mut); font-size:12px; }}
table {{ border-collapse:collapse; width:100%; font-size:13px; }} th,td {{ border-bottom:1px solid var(--line); padding:6px 8px; text-align:left; vertical-align:top; }}
.scroll {{ overflow-x:auto; }} .o-false_success {{ color:var(--bad); font-weight:600; }} .o-completed_correctly {{ color:var(--ok); }}
.gate {{ border-radius:10px; padding:4px 16px; margin:16px 0; border:2px solid var(--line); }} .gate.fail {{ border-color:var(--bad); }} .gate.pass {{ border-color:var(--ok); }}
</style></head><body>
<h1>Agent test: {e(m["agent"]["id"])} on {e(m["app"])}</h1>
<div class="meta">Run {e(m["run_id"])} &middot; {s["runs"]} runs ({s["scored"]} scored, {excluded} left out because the fault was not delivered)
&middot; scenarios {e(m["scenarios"]["source"])} {e(m["scenarios"]["digest"][:19])} &middot; spend ${s["cost_usd"]:.2f}</div>
{gate_html}
<div class="tiles">{tiles}</div>
<p class="meta">Rates are over scored runs. The grader reads the app's database before and after each run; the agent's own report decides only its claim.
Silent corruption: the agent said done while its record is wrong (a wrong or blank value, or a duplicate). Collateral edits: another record ended changed or deleted.</p>
<div class="scroll"><table><thead><tr><th>Scenario</th><th>Fault</th><th>Claim</th><th>Store</th><th>Outcome</th><th>Flags</th><th>s</th><th>USD</th></tr></thead>
<tbody>{"".join(rows)}</tbody></table></div>
</body></html>
"""


def receipt_agent(agent: dict[str, Any]) -> dict[str, Any]:
    """The agent as a receipt may name it: a command agent's command is a digest."""

    if agent.get("kind") in ("cmd", "mcp", "browser-use"):
        command = str(agent.get("id", "")).split(":", 1)[-1]
        return {
            "id": f"{agent['kind']}:"
            + hashlib.sha256(command.encode()).hexdigest()[:16],
            "kind": agent["kind"],
            "model": agent.get("model", ""),
        }
    return dict(agent)


def build_receipt(
    out: Path, doc: dict[str, Any], gate: Optional[dict[str, Any]] = None
) -> dict[str, Any]:
    m, s = doc["meta"], doc["summary"]
    rec: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run_id": m["run_id"],
        "app": m["app"],
        "app_pack_digest": m["app_pack"]["digest"],
        "scenarios_digest": m["scenarios"]["digest"],
        "scenario_count": m["scenarios"]["count"],
        "images": m["images"],
        "agent": receipt_agent(m["agent"]),
        "instructions_digest": m.get("instructions_digest"),
        "repetitions": m["repetitions"],
        "runs": s["runs"],
        "scored": s["scored"],
        "outcomes": {o: s["outcomes"][o] for o in OUTCOMES},
        "rates": {
            n: {k: s["rates"][n][k] for k in ("count", "of", "rate")} for n in RATES
        },
        "cost_usd": s["cost_usd"],
        "results_sha256": sha256_file(out / "results.json"),
        "report_sha256": sha256_file(out / "report.html")
        if (out / "report.html").is_file()
        else None,
        "openadapt_flow": m.get("openadapt_flow"),
        "gate": None,
        "signature": None,
    }
    if gate:
        rec["gate"] = {
            k: gate[k]
            for k in ("verdict", "baseline_sha256", "regressions", "tolerance")
        }
    return rec


def sign_receipt(rec: dict[str, Any], key_path: Path) -> dict[str, Any]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("--sign-key must be an Ed25519 private key (PEM)")
    body = {k: v for k, v in rec.items() if k != "signature"}
    sig = key.sign(canonical_json(body).encode())
    pub = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    rec["signature"] = {
        "alg": "ed25519",
        "public_key": base64.b64encode(pub).decode(),
        "value": base64.b64encode(sig).decode(),
    }
    return rec


def verify_receipt(
    receipt_path: Path, results_path: Optional[Path] = None
) -> list[str]:
    """Problems with a receipt (empty when it checks out)."""

    rec = json.loads(receipt_path.read_text())
    problems = []
    if rec.get("schema") != RECEIPT_SCHEMA:
        problems.append(f"schema is {rec.get('schema')!r}")
    if results_path is not None and sha256_file(results_path) != rec.get(
        "results_sha256"
    ):
        problems.append("results.json does not match the receipt's digest")
    sig = rec.get("signature")
    if sig:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        body = {k: v for k, v in rec.items() if k != "signature"}
        try:
            Ed25519PublicKey.from_public_bytes(
                base64.b64decode(sig["public_key"])
            ).verify(base64.b64decode(sig["value"]), canonical_json(body).encode())
        except (InvalidSignature, ValueError, KeyError):
            problems.append("the signature does not verify")
    else:
        problems.append("unsigned (digests only)")
    return problems


def write_outputs(
    out: Path,
    doc: dict[str, Any],
    gate: Optional[dict[str, Any]] = None,
    sign_key: Optional[Path] = None,
) -> dict[str, Path]:
    (out / "report.html").write_text(render_html(doc, gate), encoding="utf-8")
    rec = build_receipt(out, doc, gate)
    if sign_key is not None:
        rec = sign_receipt(rec, sign_key)
    (out / "receipt.json").write_text(json.dumps(rec, indent=1) + "\n")
    return {
        "results": out / "results.json",
        "report": out / "report.html",
        "receipt": out / "receipt.json",
    }
