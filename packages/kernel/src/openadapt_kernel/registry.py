"""Zero-PII content-addressed PortalPack registry and offline HTML audit card generator.

Enforces two commercial invariants:
  1. Shared PortalPack Network: any pack published for cross-tenant reuse is scanned
     for PII (emails, SSNs, phone numbers, DOBs) and signed with Ed25519 so tenants
     only pull verified, zero-PII structural updates.
  2. Proof-Carrying Automation: renders an offline, self-contained HTML audit card
     from an Ed25519-signed openadapt.kernel.receipt/v1 bundle for compliance review.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key

from openadapt_kernel.copilot import PortalPack, verify_receipt

_PII_PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}\b"),
    "dob_iso": re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"),
}


@dataclass
class SignedPackEnvelope:
    schema: str
    pack_sha256: str
    pack: Dict[str, Any]
    signature_b64: str


def scan_pack_for_pii(pack: PortalPack) -> List[str]:
    """Return any PII pattern hits inside a PortalPack before cross-tenant publishing."""
    blob = json.dumps(asdict(pack), sort_keys=True)
    hits: List[str] = []
    for label, pattern in _PII_PATTERNS.items():
        if pattern.search(blob):
            hits.append(label)
    return hits


def publish_signed_pack(
    pack: PortalPack,
    private_key_pem: bytes,
    registry_dir: Path,
) -> Path:
    """Validate zero PII, sign a PortalPack with Ed25519, and write to the local registry."""
    pii_hits = scan_pack_for_pii(pack)
    if pii_hits:
        raise ValueError(f"Refusing to publish PortalPack with PII patterns: {pii_hits}")

    pack_dict = asdict(pack)
    canonical = json.dumps(pack_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(canonical).hexdigest()

    priv = load_pem_private_key(private_key_pem, password=None)
    assert isinstance(priv, Ed25519PrivateKey)
    sig_b64 = base64.b64encode(priv.sign(canonical)).decode("ascii")

    envelope = SignedPackEnvelope(
        schema="openadapt.kernel.pack/v1",
        pack_sha256=digest,
        pack=pack_dict,
        signature_b64=sig_b64,
    )
    registry_dir.mkdir(parents=True, exist_ok=True)
    out = registry_dir / f"{pack.pack_id}-{digest[:12]}.pack.json"
    out.write_text(json.dumps(asdict(envelope), indent=2), encoding="utf-8")
    return out


def pull_verified_pack(pack_file: Path, public_key_pem: bytes) -> PortalPack:
    """Verify Ed25519 signature, SHA-256 digest, and zero-PII invariant before loading."""
    data = json.loads(pack_file.read_text(encoding="utf-8"))
    pack_dict = data["pack"]
    canonical = json.dumps(pack_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if hashlib.sha256(canonical).hexdigest() != data.get("pack_sha256"):
        raise ValueError("PortalPack SHA-256 digest mismatch")

    pub = load_pem_public_key(public_key_pem)
    assert isinstance(pub, Ed25519PublicKey)
    pub.verify(base64.b64decode(data["signature_b64"]), canonical)

    pack = PortalPack(
        pack_id=pack_dict["pack_id"],
        portal_origin=pack_dict["portal_origin"],
        variable_fields=list(pack_dict.get("variable_fields") or []),
        static_defaults=dict(pack_dict.get("static_defaults") or {}),
        provenance_map=dict(pack_dict.get("provenance_map") or {}),
        readback_url_pattern=str(pack_dict.get("readback_url_pattern") or ""),
        stage=pack_dict.get("stage", "SHADOW"),
        shadow_passes=int(pack_dict.get("shadow_passes", 0)),
        edge_branches=dict(pack_dict.get("edge_branches") or {}),
    )
    if scan_pack_for_pii(pack):
        raise ValueError("Pulled PortalPack failed zero-PII check")
    return pack


def render_audit_card_html(receipt: Dict[str, Any], public_key_pem: bytes) -> str:
    """Render a self-contained offline compliance HTML card for an Ed25519 receipt."""
    sig_ok = verify_receipt(receipt, public_key_pem)
    payload = receipt.get("payload", {})
    obligations = payload.get("obligations", {})
    rows = []
    for oid, ob in obligations.items():
        proof = ob.get("readback_proof") or ob.get("proof") or {}
        evidence = ", ".join(
            str(s) for s in (proof.get("cited_substrings") or proof.get("cited_value_sha256") or [])
        )
        locator = str(proof.get("origin_host") or proof.get("url_or_locator") or "")
        rows.append(
            f"<tr><td>{html.escape(str(oid))}</td>"
            f"<td>{html.escape(str(ob.get('state', ob.get('status', ''))))}</td>"
            f"<td>{html.escape(str(proof.get('channel', '')))}</td>"
            f"<td><code>{html.escape(evidence or locator)}</code></td></tr>"
        )
    status_badge = "SIGNATURE VERIFIED" if sig_ok else "SIGNATURE INVALID"
    color = "#0f766e" if sig_ok else "#b91c1c"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>OpenAdapt Kernel Audit Receipt</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:780px;margin:24px auto;color:#111}"
        "table{border-collapse:collapse;width:100%;margin-top:12px}"
        "th,td{border:1px solid #d1d5db;padding:8px;text-align:left;font-size:14px}"
        ".badge{display:inline-block;padding:4px 10px;border-radius:6px;color:#fff;font-weight:600}</style>"
        f"</head><body><h1>OpenAdapt Kernel Audit Receipt</h1>"
        f"<span class='badge' style='background:{color}'>{status_badge}</span>"
        f"<p><strong>Pack:</strong> {html.escape(str(payload.get('pack_id', '')))} | "
        f"<strong>Episode:</strong> {html.escape(str(payload.get('episode_id', '')))} | "
        f"<strong>Minted:</strong> {html.escape(str(payload.get('minted_at', '')))}</p>"
        f"<table><thead><tr><th>Obligation</th><th>Status</th><th>Read-Back Channel</th><th>Evidence Hash</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></body></html>"
    )
