"""A run's Seal, read back for the finale, and a link that carries it.

The finale draws the first checked run's Seal from two small files the engine
wrote: ``receipt.json`` (an allow-listed summary: counts, outcomes and
digests, no typed value, URL or screenshot) and ``seal.json`` (the key and the
signature over that run's ``report.json``).

:func:`seal_view_url` packs those two files into the URL *fragment* of an
online Seal viewer. Browsers never send a fragment to the server, so opening
the link uploads nothing. The finale prints it only when :data:`ONLINE_VIEWER`
names a page that reads the fragment; until then the clickable link is the
run's own offline ``seal.html``. The receipt alone can't prove the signature
(the Seal signs ``report.json``, which stays on this computer), so an online
page shows what the Seal says; ``openadapt seal verify`` and ``seal.html``
check it.
"""

from __future__ import annotations

import base64
import json
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

#: The online viewer that reads :func:`seal_view_url`'s fragment, or None while
#: no deployed page does (then no online link is printed).
ONLINE_VIEWER: Optional[str] = None
#: The fragment's version prefix: ``#v1.<base64url(deflate-raw(json))>``.
FRAGMENT_VERSION = "v1"
#: Longest fragment the finale prints; terminals and chat apps cut longer links.
MAX_FRAGMENT = 4096

RECEIPT_SCHEMA = "openadapt.run-receipt/v2"
SEAL_SCHEMA = "openadapt.local-seal/v1"

#: Only these receipt fields go in a link: the counts and outcomes the card
#: shows, and the digests that name the run. All are numbers, enums or hex.
RECEIPT_FIELDS = (
    "schema_version",
    "outcome",
    "transaction_outcome",
    "profile",
    "steps_ok",
    "steps_total",
    "effects_confirmed",
    "effects_required",
    "postconditions_confirmed",
    "postconditions_required",
    "identity_confirmed",
    "identity_required",
    "model_calls",
    "est_cost_usd",
    "duration_ms",
    "heals",
    "external_network_calls",
    "provenance",
    "receipt_builder_version",
    "bundle_digest",
    "receipt_digest",
    "generated_at",
)
SEAL_FIELDS = (
    "schema",
    "algorithm",
    "issuer",
    "key_id",
    "public_key",
    "outcome",
    "run_id",
    "subject_kind",
    "subject_sha256",
    "issued_at",
    "signature",
)


@dataclass(frozen=True)
class SealCard:
    """What the finale's Seal card shows, read from one run's files."""

    outcome: str
    steps_ok: int
    steps_total: int
    effects_confirmed: int
    effects_required: int
    checks_confirmed: int
    checks_required: int
    model_calls: int
    key_id: str
    report_sha256: str
    receipt: Mapping[str, Any]
    seal: Mapping[str, Any]

    @property
    def short_digest(self) -> str:
        return self.report_sha256[:12]


def read_card(run_dir: Path) -> Optional[SealCard]:
    """The Seal card for ``run_dir``, or None when its files are missing or odd."""

    try:
        receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
        seal = json.loads((run_dir / "seal.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(receipt, dict) or not isinstance(seal, dict):
        return None
    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        return None
    if seal.get("schema") != SEAL_SCHEMA:
        return None
    try:
        return SealCard(
            outcome=str(receipt["outcome"]),
            steps_ok=int(receipt["steps_ok"]),
            steps_total=int(receipt["steps_total"]),
            effects_confirmed=int(receipt["effects_confirmed"]),
            effects_required=int(receipt["effects_required"]),
            checks_confirmed=int(receipt.get("postconditions_confirmed", 0)),
            checks_required=int(receipt.get("postconditions_required", 0)),
            model_calls=int(receipt["model_calls"]),
            key_id=str(seal["key_id"]),
            report_sha256=str(seal["subject_sha256"]),
            receipt=receipt,
            seal=seal,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _deflate_raw(data: bytes) -> bytes:
    # wbits=-15: raw DEFLATE, what a browser's DecompressionStream('deflate-raw')
    # reads.
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    return compressor.compress(data) + compressor.flush()


def fragment_payload(
    receipt: Mapping[str, Any], seal: Mapping[str, Any]
) -> dict[str, Any]:
    """The allow-listed receipt and Seal fields, as the fragment carries them."""

    if receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ValueError(f"not a {RECEIPT_SCHEMA} receipt")
    if seal.get("schema") != SEAL_SCHEMA:
        raise ValueError(f"not a {SEAL_SCHEMA} Seal")
    return {
        "receipt": {k: receipt[k] for k in RECEIPT_FIELDS if k in receipt},
        "seal": {k: seal[k] for k in SEAL_FIELDS if k in seal},
    }


def encode_fragment(receipt: Mapping[str, Any], seal: Mapping[str, Any]) -> str:
    """``v1.<base64url(deflate-raw(canonical json))>``, without the ``#``."""

    payload = fragment_payload(receipt, seal)
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"{FRAGMENT_VERSION}.{_b64url(_deflate_raw(text.encode('utf-8')))}"


def decode_fragment(fragment: str) -> dict[str, Any]:
    """The inverse of :func:`encode_fragment` (what the online page does)."""

    fragment = fragment.lstrip("#")
    version, _, body = fragment.partition(".")
    if version != FRAGMENT_VERSION or not body:
        raise ValueError(f"not a {FRAGMENT_VERSION} Seal fragment")
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    return json.loads(zlib.decompress(raw, -15).decode("utf-8"))


def seal_view_url(
    receipt: Mapping[str, Any],
    seal: Mapping[str, Any],
    base: Optional[str] = None,
) -> Optional[str]:
    """``base#v1.…``: the Seal in the fragment, so opening it uploads nothing.

    None when no viewer is deployed (``base`` and :data:`ONLINE_VIEWER` both
    unset), when ``base`` already has a fragment, or when the link would be
    longer than :data:`MAX_FRAGMENT`.
    """

    base = base if base is not None else ONLINE_VIEWER
    if not base or "#" in base:
        return None
    fragment = encode_fragment(receipt, seal)
    if len(fragment) > MAX_FRAGMENT:
        return None
    return f"{base}#{fragment}"


__all__ = [
    "MAX_FRAGMENT",
    "ONLINE_VIEWER",
    "RECEIPT_FIELDS",
    "SEAL_FIELDS",
    "SealCard",
    "decode_fragment",
    "encode_fragment",
    "fragment_payload",
    "read_card",
    "seal_view_url",
]
