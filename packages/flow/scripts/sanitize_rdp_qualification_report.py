#!/usr/bin/env python3
"""Create or verify a deterministic public RDP qualification derivative."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

_UUID_RE = re.compile(
    r"\{?[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\}?"
)
_PRIVATE_IP_RE = re.compile(
    r"\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
    r"172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b"
)


def _encoded(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize(source_bytes: bytes) -> dict[str, Any]:
    """Return a deterministic derivative without local machine identifiers."""
    report: dict[str, Any] = deepcopy(json.loads(source_bytes))
    environment = report["environment"]
    cleanup = report["cleanup"]

    before_count = len(environment.pop("snapshot_ids_before"))
    after_count = len(cleanup.pop("snapshot_ids_after"))
    environment["snapshot_inventory_before_count"] = before_count
    cleanup["snapshot_inventory_after_count"] = after_count

    replacements = {
        "environment.base_snapshot_id": "retained-clean-qualification-base",
        "environment.computer_name": "redacted-local-windows-guest",
        "environment.guest_ip": "redacted-private-guest-address",
        "environment.vm": "redacted-local-parallels-vm",
        "owned_snapshot_id": "redacted-batch-owned-snapshot",
    }
    environment["base_snapshot_id"] = replacements["environment.base_snapshot_id"]
    environment["computer_name"] = replacements["environment.computer_name"]
    environment["guest_ip"] = replacements["environment.guest_ip"]
    environment["vm"] = replacements["environment.vm"]
    report["owned_snapshot_id"] = replacements["owned_snapshot_id"]

    report["source_report_sha256"] = _sha256(source_bytes)
    report["redaction_manifest"] = {
        "policy": "openadapt.rdp-public-evidence.v1",
        "replacements": replacements,
        "removed_identifier_arrays": {
            "cleanup.snapshot_ids_after": after_count,
            "environment.snapshot_ids_before": before_count,
        },
    }
    report["derivative_payload_sha256"] = _sha256(_encoded(report))
    return report


def verify(report: dict[str, Any]) -> None:
    """Reject a corrupt derivative or one containing raw local identifiers."""
    expected = report.get("derivative_payload_sha256")
    payload = deepcopy(report)
    payload.pop("derivative_payload_sha256", None)
    actual = _sha256(_encoded(payload))
    if expected != actual:
        raise ValueError(f"derivative hash mismatch: expected {expected}, got {actual}")

    manifest = report.get("redaction_manifest", {})
    if manifest.get("policy") != "openadapt.rdp-public-evidence.v1":
        raise ValueError("missing public-evidence redaction policy")
    if not re.fullmatch(r"[0-9a-f]{64}", report.get("source_report_sha256", "")):
        raise ValueError("missing source report SHA-256")

    serialized = _encoded(report).decode()
    if _UUID_RE.search(serialized):
        raise ValueError("raw UUID found in sanitized derivative")
    if _PRIVATE_IP_RE.search(serialized):
        raise ValueError("private IP address found in sanitized derivative")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args()

    if args.verify:
        verify(json.loads(args.verify.read_text()))
        print(f"verified {args.verify}")
        return
    if not args.source or not args.output:
        parser.error("--source and --output are required when not using --verify")

    derivative = sanitize(args.source.read_bytes())
    verify(derivative)
    args.output.write_bytes(_encoded(derivative))
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
