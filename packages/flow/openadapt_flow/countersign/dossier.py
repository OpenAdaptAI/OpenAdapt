"""Dispute dossier: one offline file a third party can check without OpenAdapt.

``openadapt-flow countersign dossier`` writes a self-contained HTML page (or a zip
with the same page plus the raw files ``openssl`` needs). It contains:

* the receipt and its evidence manifest, which are PHI-free by construction;
* the countersign block: statement, RFC 3161 tokens, countersignature;
* the verification report computed at export, with the pinned keys used;
* disclosed fields, only from a disclosure bundle the holder passed in;
* screenshots, only after automatic redaction by openadapt-privacy, and only
  when that provider is installed. Without it, screenshots are left out.

Nothing in the vault is read here. Disclosed values are PHI by definition, so
they appear only when the caller hands over a disclosure bundle.

The page re-checks itself in the reader's browser (WebCrypto): the receipt
hash, the Ed25519 Seal signature against the embedded key, the evidence
digest, and each disclosed commitment. RFC 3161 tokens need ``openssl ts
-verify`` or ``openadapt-flow countersign verify``; the page prints the command.
"""

from __future__ import annotations

import base64
import hashlib
import html
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from openadapt_flow.countersign.document import (
    EXTENSION_KEY,
    CountersignError,
    CountersignReport,
    locate,
    verify_countersigned,
)
from openadapt_flow.countersign.render import render_report, timing_line

__all__ = [
    "DISCLAIMER",
    "METERING_NOTE",
    "DOSSIER_SCHEMA",
    "build_dossier",
    "extract_dossier_data",
    "write_dossier",
]

DOSSIER_SCHEMA = "openadapt.seal-dossier/v1"
_DATA_ID = "openadapt-dossier-data"
DISCLAIMER = (
    "Neither a Countersigned Seal nor a Timestamped Seal is a notarial act,"
    " a government certification, or an eIDAS qualified electronic seal or "
    "qualified electronic time stamp. This verifier checks cryptographic "
    "integrity against pinned keys and RFC 3161 timestamp tokens; it does "
    "not consult EU Trusted Lists (EUTL) or attest to the accuracy of the "
    "underlying system of record. A Seal is a technical audit artifact and "
    "makes no representation as to legal admissibility or legal effect."
)
METERING_NOTE = (
    "OpenAdapt meters Seal issuance equally across all verification "
    "outcomes (VERIFIED, REFUTED, and INDETERMINATE); fees are never "
    "contingent on whether a workflow or verification check succeeds."
)


def _json_for_script(value: Any) -> str:
    text = json.dumps(value, indent=1, sort_keys=True, ensure_ascii=True)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def _redacted_screenshots(
    paths: Sequence[Path],
) -> tuple[list[dict[str, str]], list[str]]:
    """Redact each PNG with openadapt-privacy, or leave it out. Never embeds raw."""
    from openadapt_flow import privacy

    included: list[dict[str, str]] = []
    notes: list[str] = []
    if not paths:
        return included, notes
    if not privacy.scrubbing_available():
        notes.append(
            f"{len(paths)} screenshot(s) left out: no redaction provider is installed "
            "(pip install 'openadapt-flow[privacy]')."
        )
        return included, notes
    for i, path in enumerate(paths, 1):
        try:
            redacted = privacy.scrub_image_bytes(Path(path).read_bytes(), force=True)
        except Exception:  # noqa: BLE001 - a failed redaction means leave it out
            notes.append(f"screenshot {i} left out: redaction failed.")
            continue
        included.append(
            {
                "label": f"screenshot-{i}",
                "png_base64": base64.b64encode(redacted).decode("ascii"),
                "redacted_sha256": hashlib.sha256(redacted).hexdigest(),
            }
        )
    return included, notes


def build_dossier(
    document: Mapping[str, Any],
    *,
    registry: Sequence[Mapping[str, Any]],
    tsa_certificates_pem: Optional[bytes] = None,
    disclosures: Sequence[Mapping[str, Any]] = (),
    screenshots: Sequence[Path] = (),
    accept_non_flow: bool = False,
    now: Optional[datetime] = None,
    ledger: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, Any], CountersignReport]:
    """``ledger`` is :meth:`ExceptionQueue.ledger`; the CLI always passes it."""
    from openadapt_flow.countersign.tsa import load_pem_certificates

    certs = load_pem_certificates(tsa_certificates_pem) if tsa_certificates_pem else []
    report = verify_countersigned(
        document,
        registry=registry,
        tsa_certificates=certs,
        disclosures=disclosures,
        accept_non_flow=accept_non_flow,
    )
    located = locate(document)
    if disclosures and (
        report.commitments.get("status") != "bound"
        or any(d.get("status") != "matches" for d in report.disclosures)
    ):
        # Disclosed values are PHI. Publish them only when they verify against
        # commitments a timestamp or countersignature binds.
        raise CountersignError(
            "disclosure_rejected",
            "every disclosure must match commitments that a verified timestamp "
            "or countersignature binds",
        )
    shots, notes = _redacted_screenshots(list(screenshots))
    data = {
        "schema": DOSSIER_SCHEMA,
        "generated_at": (now or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "receipt": dict(located.receipt),
        "evidence": dict(located.evidence) if located.evidence is not None else None,
        "countersign": dict(located.block) if located.block is not None else None,
        "pinned_seal_keys": [dict(k) for k in registry],
        "pinned_tsa_certificates_pem": tsa_certificates_pem.decode("ascii")
        if tsa_certificates_pem
        else None,
        "disclosures": [_disclosure_copy(d) for d in disclosures],
        "screenshots": shots,
        "notes": notes,
        "report_at_export": report.as_dict(),
        "exception_ledger": _ledger_copy(ledger) if ledger is not None else None,
    }
    return data, report


_ENTRY_KEYS = (
    "receipt_sha256",
    "receipt_id",
    "execution_id",
    "workflow_digest",
    "outcome",
    "verdict",
    "category",
    "issued_at",
    "status",
    "queued_at",
    "terminal_at",
    "retain_until",
    "resolution_receipt_id",
    "resolution_receipt_sha256",
    "disposition",
    "note",
)


def _ledger_copy(ledger: Mapping[str, Any]) -> dict[str, Any]:
    """Counts and entries, with only the keys an entry defines. No notes, no values."""
    counts = {
        k: int((ledger.get("counts") or {}).get(k, 0))
        for k in ("verified", "resolved_by_seal", "closed_by_attestation", "open")
    }
    return {
        "total_seals": sum(counts.values()),
        "counts": counts,
        "exceptions": [
            {k: e.get(k) for k in _ENTRY_KEYS if k in e}
            for e in ledger.get("exceptions") or []
        ],
    }


def _ledger_html(ledger: Optional[Mapping[str, Any]]) -> str:
    esc = html.escape
    if ledger is None:
        return (
            "<h2>Exceptions</h2><p class='warn'>No exception ledger was supplied. "
            "Export with 'openadapt-flow countersign dossier', which includes it.</p>"
        )
    c = ledger["counts"]
    out = [
        "<h2>Exceptions</h2>",
        f"<p class='headline'>Total Seals {ledger['total_seals']} = Verified "
        f"{c['verified']} + Resolved_by_seal {c['resolved_by_seal']} + "
        f"Closed_by_attestation {c['closed_by_attestation']} + Open {c['open']}</p>",
        "<p>Every Seal in the holder's vault on a REFUTED or INDETERMINATE outcome "
        "is listed, open and closed. A closed entry shows how it ended: a later "
        "Seal, or a signed disposition.</p>",
    ]
    rows = []
    for e in ledger["exceptions"]:
        how = ""
        if e.get("resolution_receipt_id"):
            how = f"later Seal {e['resolution_receipt_id']}"
        elif e.get("disposition"):
            d = e["disposition"]
            approver = d.get("approver") or {}
            how = (
                f"{d.get('disposition_code')} by {approver.get('id')} "
                f"({approver.get('role')}), {d.get('verification_method')}, "
                f"{d.get('dispositioned_at')}"
            )
        elif e.get("note"):
            how = str(e["note"])
        rows.append(
            "<tr>"
            + "".join(
                f"<td>{esc(str(v))}</td>"
                for v in (
                    e.get("status"),
                    e.get("verdict"),
                    e.get("receipt_sha256"),
                    e.get("issued_at"),
                    how,
                )
            )
            + "</tr>"
        )
    if rows:
        out.append(
            "<table><tr><th>Status</th><th>Verdict</th><th>Receipt sha256</th>"
            "<th>Issued</th><th>How it ended</th></tr>" + "".join(rows) + "</table>"
        )
    return "".join(out)


def _disclosure_copy(disclosure: Mapping[str, Any]) -> dict[str, Any]:
    """Only the keys a disclosure defines, so nothing else rides into the page."""
    return {
        "schema": disclosure.get("schema"),
        "receipt_sha256": disclosure.get("receipt_sha256"),
        "commitments_sha256": disclosure.get("commitments_sha256"),
        "fields": [
            {k: f.get(k) for k in ("label", "name", "value", "salt")}
            for f in disclosure.get("fields") or []
        ],
    }


def _document_from_data(data: Mapping[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {"receipt": data["receipt"]}
    if data.get("evidence") is not None:
        doc["evidence"] = data["evidence"]
    if data.get("countersign") is not None:
        doc["extensions"] = {EXTENSION_KEY: data["countersign"]}
    return doc


def extract_dossier_data(html_text: str) -> dict[str, Any]:
    """The embedded data of a dossier page, for ``countersign verify dossier.html``."""
    marker = f'<script type="application/json" id="{_DATA_ID}">'
    start = html_text.find(marker)
    end = html_text.find("</script>", start)
    if start < 0 or end < 0:
        raise ValueError("not an OpenAdapt Seal dossier")
    data = json.loads(html_text[start + len(marker) : end])
    if data.get("schema") != DOSSIER_SCHEMA:
        raise ValueError("not an OpenAdapt Seal dossier")
    return {
        "document": _document_from_data(data),
        "disclosures": data.get("disclosures") or [],
        "exception_ledger": data.get("exception_ledger"),
    }


_JS = r"""
(async () => {
  const data = JSON.parse(document.getElementById('openadapt-dossier-data').textContent);
  const out = document.getElementById('live');
  const enc = new TextEncoder();
  const canon = (v) => Array.isArray(v) ? '[' + v.map(canon).join(',') + ']'
    : (v !== null && typeof v === 'object')
      ? '{' + Object.keys(v).sort().map(k => JSON.stringify(k) + ':' + canon(v[k])).join(',') + '}'
      : JSON.stringify(v);
  const hex = (buf) => Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  const sha = async (s) => hex(await crypto.subtle.digest('SHA-256', typeof s === 'string' ? enc.encode(s) : s));
  const b64 = (s) => Uint8Array.from(atob(s), c => c.charCodeAt(0));
  const row = (name, ok, text) => {
    const li = document.createElement('li');
    li.textContent = (ok ? 'PASS  ' : 'FAIL  ') + name + (text ? ': ' + text : '');
    li.className = ok ? 'pass' : 'fail';
    out.appendChild(li);
  };
  try {
    const receipt = Object.assign({}, data.receipt); const sig = receipt.seal_signature; delete receipt.seal_signature;
    const rsha = await sha(canon(receipt));
    row('receipt hash', !sig || sig.receipt_sha256 === rsha, rsha);
    if (data.evidence) {
      const ed = 'sha256:' + await sha(canon(data.evidence));
      row('evidence digest', ed === data.receipt.evidence_digest, ed);
    }
    if (sig) {
      const key = (data.pinned_seal_keys || []).find(k => k.key_id === sig.key_id);
      if (!key) { row('Seal signature', false, 'key ' + sig.key_id + ' is not in this dossier'); }
      else {
        const unsigned = Object.assign({}, sig); delete unsigned.signature;
        const payload = enc.encode('openadapt.seal-signature/v1\u0000' + canon(unsigned));
        try {
          const k = await crypto.subtle.importKey('raw', b64(key.public_key), {name: 'Ed25519'}, false, ['verify']);
          const sigOk = await crypto.subtle.verify({name: 'Ed25519'}, k, b64(sig.signature), payload);
          // The signature covers receipt_sha256, so it vouches for this receipt only if the hash matches.
          const ok = sigOk && sig.receipt_sha256 === rsha && sig.run_id === data.receipt.execution_id;
          row('Seal signature (Ed25519, key ' + sig.key_id + ')', ok, ok ? 'compare this key id with the one you pinned' : (sigOk ? 'genuine, but over a different receipt' : 'does not verify'));
        } catch (e) { row('Seal signature', false, 'this browser has no Ed25519 in WebCrypto'); }
      }
    } else { row('Seal signature', false, 'unsigned receipt'); }
    const fields = (data.countersign && data.countersign.commitments && data.countersign.commitments.fields) || {};
    for (const d of (data.disclosures || [])) {
      for (const f of (d.fields || [])) {
        const c = 'sha256:' + await sha('openadapt.field-commitment/v1\u0000' + canon({label: f.label, name: f.name, salt: f.salt, value: f.value}));
        row('disclosed ' + f.label + ' (' + f.name + ')', fields[f.label] === c, fields[f.label] === c ? 'matches its commitment' : 'does not match');
      }
    }
  } catch (e) { row('in-page check', false, String(e)); }
})();
"""

_CSS = """
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;max-width:60rem;margin:2rem auto;padding:0 1rem;color:#1b1b1b}
h1{font-size:1.5rem} h2{font-size:1.15rem;margin-top:2rem;border-bottom:1px solid #ccc}
pre{background:#f5f5f5;padding:.75rem;overflow:auto;font-size:.8rem;white-space:pre-wrap;word-break:break-all}
.headline{font-size:1.1rem;font-weight:600;padding:.75rem;border:2px solid #333}
.pass{color:#0a5c0a} .fail{color:#a00000;font-weight:600} .warn{background:#fff4d6;padding:.5rem;border-left:4px solid #c90}
img{max-width:100%;border:1px solid #999}
"""


def render_html(data: Mapping[str, Any], report: CountersignReport) -> str:
    esc = html.escape
    sections: list[str] = []
    sections.append(
        f"<p class='headline'>{esc(render_report(report).splitlines()[0])}</p>"
    )
    sections.append(
        "<p>This dossier records one statement OpenAdapt made about one write. "
        "A signature proves who made the statement. A trusted timestamp proves the "
        "statement existed by a given time. Neither proves the record in the system "
        "of record is correct.</p>"
        f"<p>{esc(DISCLAIMER)}</p><p>{esc(METERING_NOTE)}</p>"
    )
    if report.warnings:
        sections.append(f"<p class='warn'>WARNING: {esc(timing_line(report))}</p>")
    sections.append(
        "<h2>Verification at export</h2><pre>" + esc(render_report(report)) + "</pre>"
    )
    sections.append(_ledger_html(data.get("exception_ledger")))
    sections.append(
        "<h2>Checks your browser ran just now</h2>"
        "<p>These use only the data in this file. The Seal key is the one embedded below; "
        "compare its key id with the key you pinned from OpenAdapt's published registry.</p>"
        "<ul id='live'></ul><noscript>JavaScript is off; use the command line below.</noscript>"
    )
    block = data.get("countersign") or {}
    rts = block.get("receipt_timestamp")
    if rts:
        sections.append(
            "<h2>Check the trusted timestamp yourself</h2><p>Save the token and the TSA "
            "certificate, then run:</p><pre>"
            + esc(
                "base64 -d receipt-timestamp.b64 > receipt-timestamp.tsr\n"
                f"openssl ts -verify -digest {rts.get('imprint_sha256')} -in receipt-timestamp.tsr "
                "-token_in -CAfile tsa-ca.pem\n\n"
                "Or: openadapt-flow countersign verify this-dossier.html --tsa-cert tsa-ca.pem"
            )
            + "</pre><p>The digest is SHA-256 of the countersign statement below, which names "
            "the receipt hash.</p>"
        )
    for note in data.get("notes") or []:
        sections.append(f"<p class='warn'>{esc(str(note))}</p>")
    if data.get("disclosures"):
        sections.append(
            "<h2>Disclosed fields</h2><p class='warn'>The holder chose to reveal these "
            "values. They may be personal or health information. Share this file only "
            "with someone entitled to see them.</p>"
        )
        for d in data["disclosures"]:
            rows = "".join(
                f"<tr><td>{esc(str(f.get('label')))}</td><td>{esc(str(f.get('name')))}</td>"
                f"<td>{esc(str(f.get('value')))}</td></tr>"
                for f in d.get("fields", [])
            )
            sections.append(
                f"<table><tr><th>Label</th><th>Field</th><th>Value</th></tr>{rows}</table>"
            )
    for shot in data.get("screenshots") or []:
        sections.append(
            f"<h2>{esc(shot['label'])} (automatically redacted)</h2>"
            "<p class='warn'>Redacted by openadapt-privacy. Automatic redaction can miss "
            "text; review before sharing. Screenshots are not bound to the receipt.</p>"
            f"<img alt='{esc(shot['label'])}' src='data:image/png;base64,{shot['png_base64']}'>"
        )
    for title, key in (
        ("Receipt", "receipt"),
        ("Evidence manifest", "evidence"),
        ("Countersign block", "countersign"),
    ):
        if data.get(key) is not None:
            sections.append(
                f"<h2>{title}</h2><pre>"
                + esc(json.dumps(data[key], indent=2, sort_keys=True))
                + "</pre>"
            )
    body = "\n".join(sections)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta http-equiv='Content-Security-Policy' content=\"default-src 'none'; "
        "img-src data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'\">"
        f"<title>OpenAdapt Seal dossier {esc(str(report.execution_id))}</title>"
        f"<style>{_CSS}</style></head><body><h1>OpenAdapt Seal dossier</h1>"
        f"<p>Execution {esc(str(report.execution_id))}. Generated {esc(str(data.get('generated_at')))} UTC.</p>"
        f"{body}"
        f'<script type="application/json" id="{_DATA_ID}">{_json_for_script(data)}</script>'
        f"<script>{_JS}</script></body></html>"
    )


def write_dossier(
    data: Mapping[str, Any], report: CountersignReport, out: Path, *, fmt: str = "html"
) -> Path:
    page = render_html(data, report)
    if fmt == "html":
        out.write_text(page, encoding="utf-8")
        return out
    if fmt != "zip":
        raise ValueError("format is html or zip")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("dossier.html", page)
        zf.writestr(
            "document.json",
            json.dumps(_document_from_data(data), indent=2, sort_keys=True),
        )
        if data.get("disclosures"):
            zf.writestr(
                "disclosures.json",
                json.dumps(data["disclosures"], indent=2, sort_keys=True),
            )
        block = data.get("countersign") or {}
        commands = [
            "# Verify offline",
            "",
            "openadapt-flow countersign verify document.json --tsa-cert tsa-ca.pem --keys seal-keys.json",
            "",
        ]
        for name in ("receipt_timestamp", "baseline_timestamp"):
            entry = block.get(name)
            if entry:
                stem = name.replace("_", "-")
                zf.writestr(f"{stem}.tsr", base64.b64decode(entry["token"]))
                commands.append(
                    f"openssl ts -verify -digest {entry['imprint_sha256']} -in {stem}.tsr -token_in -CAfile tsa-ca.pem"
                )
        if data.get("pinned_tsa_certificates_pem"):
            zf.writestr("tsa-ca.pem", data["pinned_tsa_certificates_pem"])
        zf.writestr(
            "seal-keys.json",
            json.dumps({"keys": data.get("pinned_seal_keys") or []}, indent=2),
        )
        commands += [
            "",
            "tsa-ca.pem and seal-keys.json are what the exporter pinned. Check them",
            "against the TSA's and OpenAdapt's published fingerprints before relying on them.",
        ]
        zf.writestr("VERIFY.md", "\n".join(commands) + "\n")
    out.write_bytes(buf.getvalue())
    return out
