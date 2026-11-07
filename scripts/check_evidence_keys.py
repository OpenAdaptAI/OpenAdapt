#!/usr/bin/env python3
"""Reject tracked key material before opening any denied artifact.

Exact source-defined public verification registries and CA certificates also
need a closed schema. No other key-shaped filename receives that exception.
Diagnostics contain paths only, never candidate contents or parser errors.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import runpy
import ssl
import subprocess
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
_policy = runpy.run_path(str(ROOT / "packages/flow/openadapt_flow/evidence_files.py"))
is_key_material_path = _policy["is_key_material_path"]

# Source contracts: public_key_entry, parse_key_registry, checkWitnesses and
# write_seal_page. These exceptions never apply to arbitrary export inputs.
PUBLIC_REGISTRIES = frozenset(
    {
        "packages/flow/benchmark/scoreboard/raw/openemr_referral/certified/2026-10-20/seal-key.json",
        "packages/flow/tests/fixtures/rekor_real/seal-key.json",
        "packages/seal-view/test/fixtures/openemr-r3/site-keys.json",
        "tests/fixtures/seal_view/keys.json",
    }
)
PUBLIC_CERTIFICATES = frozenset(
    {
        "packages/flow/openadapt_flow/countersign/trust/sigstore-tsa-root.pem",
        "packages/flow/tests/fixtures/countersign/digicert-trusted-root-g4.pem",
    }
)
_REVOCATION_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def is_public_registry(payload: str) -> bool:
    """Only public Ed25519 verification entries; unknown fields refuse."""
    try:
        entries = json.loads(payload, object_pairs_hook=_unique_object)
        if isinstance(entries, dict):
            if set(entries) != {"keys"}:
                return False
            entries = entries["keys"]
        if not isinstance(entries, list) or not entries:
            return False
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                return False
            required = {"key_id", "public_key", "status"}
            if not required <= entry.keys() or entry.keys() - required - {"revoked_at"}:
                return False
            if not isinstance(entry["public_key"], str):
                return False
            raw = base64.b64decode(entry["public_key"], validate=True)
            if len(raw) != 32:
                return False
            expected_id = "seal-ed25519-" + hashlib.sha256(raw).hexdigest()[:16]
            if entry["key_id"] != expected_id or expected_id in seen:
                return False
            seen.add(expected_id)
            if entry["status"] not in ("active", "revoked"):
                return False
            revoked_at = entry.get("revoked_at")
            if revoked_at is not None and (
                not isinstance(revoked_at, str)
                or _REVOCATION_TIME.fullmatch(revoked_at) is None
            ):
                return False
            if entry["status"] == "revoked" and revoked_at is None:
                return False
        return True
    except (ValueError, TypeError, UnicodeError):
        return False


def is_public_certificate(payload: str) -> bool:
    """A complete certificate container cannot include a private-key block."""
    pem = payload.strip()
    if re.fullmatch(
        r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+-----END CERTIFICATE-----",
        pem,
    ) is None:
        return False
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
        return bool(context.get_ca_certs())
    except (ValueError, ssl.SSLError):
        return False


def _is_evidence_path(name: str) -> bool:
    return bool(
        {part.casefold() for part in Path(name).parts}
        & {"benchmark", "evidence", "evidence-packs", "fixtures", "public-demo", "showcase"}
    )


def tracked_key_path_kind(name: str) -> str | None:
    """Pure filename classification; a public candidate still needs validation."""
    key_container = any(
        re.search(r"\.(?:key|p12|pfx|jks|keystore)(?:$|[.~_-])", part, re.I)
        for part in Path(name).parts
    )
    if not key_container and not _is_evidence_path(name) and name not in PUBLIC_CERTIFICATES:
        return None
    if not is_key_material_path(name):
        return None
    if name in PUBLIC_REGISTRIES:
        return "public_registry_candidate"
    if name in PUBLIC_CERTIFICATES:
        return "public_certificate_candidate"
    return "excluded"


def violations(root: Path, names: Iterable[str]) -> list[str]:
    denied: list[str] = []
    for name in names:
        kind = tracked_key_path_kind(name)
        if kind is None:
            continue
        validator = (
            is_public_registry if kind == "public_registry_candidate" else
            is_public_certificate if kind == "public_certificate_candidate" else None
        )
        if validator is None:
            denied.append(name)  # Never open or hash a denied candidate.
            continue
        candidate = root / name
        try:
            cursor = root
            linked = root.is_symlink()
            for part in Path(name).parts:
                cursor /= part
                linked = linked or cursor.is_symlink()
            if linked or candidate.stat().st_size > 64 * 1024:
                denied.append(name)
            elif not validator(candidate.read_text(encoding="utf-8")):
                denied.append(name)
        except (OSError, UnicodeError):
            denied.append(name)
    return sorted(denied)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"], cwd=args.root,
            check=True, stdout=subprocess.PIPE,
        )
        names = result.stdout.decode("utf-8", errors="strict").split("\0")
        denied = violations(args.root, (name for name in names if name))
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        print("EVIDENCE_KEYS_ENUMERATION_FAILED")
        return 2
    if denied:
        print("EVIDENCE_KEYS_DENIED")
        for name in denied:
            print(f"  {name}")
        return 1
    print("Tracked evidence key-material check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
