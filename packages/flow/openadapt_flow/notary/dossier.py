"""Dispute dossier: one offline file a third party can check without OpenAdapt.

``openadapt-flow notary dossier`` writes a self-contained HTML page (or a zip
with the same page plus the raw files ``openssl`` needs). It contains:

* the receipt and its evidence manifest, which are PHI-free by construction;
* the notary block: statement, RFC 3161 tokens, countersignature;
* the verification report computed at export, with the pinned keys used;
* disclosed fields, only from a disclosure bundle the holder passed in;
* screenshots, only after automatic redaction by openadapt-privacy, and only
  when that provider is installed. Without it, screenshots are left out.

Nothing in the vault is read here. Disclosed values are PHI by definition, so
they appear only when the caller hands over a disclosure bundle.

The page re-checks itself in the reader's browser (WebCrypto): the receipt
hash, the Ed25519 Seal signature against the embedded key, the evidence
digest, and each disclosed commitment. RFC 3161 tokens need ``openssl ts
-verify`` or ``openadapt-flow notary verify``; the page prints the command.
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

from openadapt_flow.notary.notarize import (
    EXTENSION_KEY,
    NotaryReport,
    locate,
    verify_notarized,
)
from openadapt_flow.notary.render import render_report

__all__ = ["DOSSIER_SCHEMA", "build_dossier", "extract_dossier_data", "write_dossier"]

DOSSIER_SCHEMA = "openadapt.seal-dossier/v1"
_DATA_ID = "openadapt-dossier-data"


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
) -> tuple[dict[str, Any], NotaryReport]:
    from openadapt_flow.notary.tsa import load_pem_certificates

    certs = load_pem_certificates(tsa_certificates_pem) if tsa_certificates_pem else []
    report = verify_notarized(
        document,
        registry=registry,
        tsa_certificates=certs,
        disclosures=disclosures,
        accept_non_flow=accept_non_flow,
    )
    located = locate(document)
    shots, notes = _redacted_screenshots(list(screenshots))
    data = {
        "schema": DOSSIER_SCHEMA,
        "generated_at": (now or datetime.now(timezone.utc)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "receipt": dict(located.receipt),
        "evidence": dict(located.evidence) if located.evidence is not None else None,
        "notary": dict(located.notary) if located.notary is not None else None,
        "pinned_seal_keys": [dict(k) for k in registry],
        "pinned_tsa_certificates_pem": tsa_certificates_pem.decode("ascii")
        if tsa_certificates_pem
        else None,
        "disclosures": [dict(d) for d in disclosures],
        "screenshots": shots,
        "notes": notes,
        "report_at_export": report.as_dict(),
    }
    return data, report


def _document_from_data(data: Mapping[str, Any]) -> dict[str, Any]:
    doc: dict[str, Any] = {"receipt": data["receipt"]}
    if data.get("evidence") is not None:
        doc["evidence"] = data["evidence"]
    if data.get("notary") is not None:
        doc["extensions"] = {EXTENSION_KEY: data["notary"]}
    return doc


def extract_dossier_data(html_text: str) -> dict[str, Any]:
    """The embedded data of a dossier page, for ``notary verify dossier.html``."""
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
    const fields = (data.notary && data.notary.commitments && data.notary.commitments.fields) || {};
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


def render_html(data: Mapping[str, Any], report: NotaryReport) -> str:
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
    )
    sections.append(
        "<h2>Verification at export</h2><pre>" + esc(render_report(report)) + "</pre>"
    )
    sections.append(
        "<h2>Checks your browser ran just now</h2>"
        "<p>These use only the data in this file. The Seal key is the one embedded below; "
        "compare its key id with the key you pinned from OpenAdapt's published registry.</p>"
        "<ul id='live'></ul><noscript>JavaScript is off; use the command line below.</noscript>"
    )
    notary = data.get("notary") or {}
    rts = notary.get("receipt_timestamp")
    if rts:
        sections.append(
            "<h2>Check the trusted timestamp yourself</h2><p>Save the token and the TSA "
            "certificate, then run:</p><pre>"
            + esc(
                "base64 -d receipt-timestamp.b64 > receipt-timestamp.tsr\n"
                f"openssl ts -verify -digest {rts.get('imprint_sha256')} -in receipt-timestamp.tsr "
                "-token_in -CAfile tsa-ca.pem\n\n"
                "Or: openadapt-flow notary verify this-dossier.html --tsa-cert tsa-ca.pem"
            )
            + "</pre><p>The digest is SHA-256 of the notary statement below, which names "
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
        ("Notary block", "notary"),
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
    data: Mapping[str, Any], report: NotaryReport, out: Path, *, fmt: str = "html"
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
        notary = data.get("notary") or {}
        commands = [
            "# Verify offline",
            "",
            "openadapt-flow notary verify document.json --tsa-cert tsa-ca.pem --keys seal-keys.json",
            "",
        ]
        for name in ("receipt_timestamp", "baseline_timestamp"):
            entry = notary.get(name)
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
