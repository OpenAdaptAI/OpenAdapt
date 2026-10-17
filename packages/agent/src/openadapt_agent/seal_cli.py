"""``openadapt-agent seal``: pin Cloud's Seal keys and verify a Seal.

``seal keys pin`` fetches ``/api/public/seal-keys`` and pins it only after a
person confirms each new key (trust on first use). The person should compare
the printed key id and fingerprint with one OpenAdapt published somewhere
other than this download (for example the Seal key notice in the
openadapt-cloud repository's SECURITY.md) before typing it back. Later runs
handle rotation:

* a new key is added only with the same explicit confirmation;
* a pinned key that Cloud now lists as revoked is marked revoked (that only
  removes trust, so it needs no confirmation);
* a pinned key Cloud no longer lists, or tries to un-revoke, stays as pinned.

``seal verify <file|url>`` checks a Seal offline against the pinned keys. It
never fetches keys; a Seal from an unpinned key does not verify.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from openadapt_agent.seal import (
    load_pinned_registry,
    parse_registry,
    pinned_keys_path,
    verify_document,
)

DEFAULT_KEYS_URL = "https://app.openadapt.ai/api/public/seal-keys"
PIN_SCHEMA = "openadapt.seal-keys-pin/v1"
MAX_BYTES = 262144

REASONS = {
    "verified": "The Cloud signature verifies against a pinned OpenAdapt key.",
    "unsigned": "This receipt carries no Cloud signature. It is not a Seal.",
    "malformed": "The Seal signature is malformed.",
    "receipt_mismatch": "The receipt was changed after Cloud signed it.",
    "unknown_key": "Signed by a key you have not pinned. Run 'openadapt-agent seal keys pin' if you trust it.",
    "revoked_key": "Signed by a key that was revoked before this Seal was issued.",
    "bad_signature": "The signature does not match the receipt.",
    "no_pinned_keys": "No Seal keys are pinned. Run 'openadapt-agent seal keys pin' first.",
    "no_receipt": "No Execute receipt found in this document.",
}


def _https_get(url: str) -> bytes:
    if not url.startswith("https://") and not url.startswith(("http://127.0.0.1", "http://localhost")):
        raise ValueError("only https:// URLs are fetched")
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "openadapt-agent"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310 - scheme checked above
        body = response.read(MAX_BYTES + 1)
    if len(body) > MAX_BYTES:
        raise ValueError("response too large")
    return body


def fingerprint(public_key_b64: str) -> str:
    import base64

    digest = hashlib.sha256(base64.b64decode(public_key_b64, validate=True)).hexdigest()
    return ":".join(digest[i : i + 4] for i in range(0, 64, 4))


def _entries(document: Any) -> list[dict]:
    parse_registry(document)  # validates every entry (key ids bind the keys)
    raw = document.get("keys") if isinstance(document, dict) else document
    return [
        {k: v for k, v in dict(entry).items() if k in ("key_id", "public_key", "status", "revoked_at")}
        for entry in raw
    ]


def plan_update(pinned: list[dict], remote: list[dict]) -> dict:
    """Pure rotation plan: which keys are new, newly revoked, or kept."""
    by_id = {entry["key_id"]: dict(entry) for entry in pinned}
    new, revoked, kept_unrevoked, missing = [], [], [], []
    remote_ids = set()
    for entry in remote:
        remote_ids.add(entry["key_id"])
        current = by_id.get(entry["key_id"])
        if current is None:
            new.append(entry)
        elif entry["status"] == "revoked" and current["status"] != "revoked":
            revoked.append(entry)
        elif entry["status"] == "active" and current["status"] == "revoked":
            kept_unrevoked.append(current)
    for key_id, entry in by_id.items():
        if key_id not in remote_ids:
            missing.append(entry)
    return {"new": new, "revoked": revoked, "refused_unrevoke": kept_unrevoked, "missing": missing}


def apply_plan(pinned: list[dict], plan: dict, accepted_ids: Iterable[str]) -> list[dict]:
    accepted = set(accepted_ids)
    result = {entry["key_id"]: dict(entry) for entry in pinned}
    for entry in plan["revoked"]:
        result[entry["key_id"]] = dict(entry)
    for entry in plan["new"]:
        if entry["key_id"] in accepted:
            result[entry["key_id"]] = dict(entry)
    return list(result.values())


def _read_pinned(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return _entries(json.loads(path.read_text(encoding="utf-8")))


def _write_pinned(path: Path, keys: list[dict], source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": PIN_SCHEMA,
        "source": source,
        "pinned_at": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "keys": keys,
    }
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def cmd_keys_pin(
    args: argparse.Namespace,
    *,
    fetch: Callable[[str], bytes] = _https_get,
    ask: Callable[[str], str] = input,
    is_tty: Optional[Callable[[], bool]] = None,
    out=None,
) -> int:
    out = out or sys.stdout
    is_tty = is_tty or (lambda: sys.stdin.isatty())
    path = Path(args.path).expanduser() if args.path else pinned_keys_path()
    try:
        remote = _entries(json.loads(fetch(args.url).decode("utf-8")))
    except Exception as exc:  # noqa: BLE001 - report and refuse
        print(f"Could not read Seal keys from {args.url}: {exc}", file=out)
        return 1
    try:
        pinned = _read_pinned(path)
    except Exception as exc:  # noqa: BLE001
        print(f"The pinned file {path} is unreadable ({exc}). Fix or move it; nothing changed.", file=out)
        return 1
    plan = plan_update(pinned, remote)
    first_use = not pinned
    for entry in plan["revoked"]:
        print(f"Revoked by Cloud, now untrusted for Seals after {entry['revoked_at']}: {entry['key_id']}", file=out)
    for entry in plan["refused_unrevoke"]:
        print(f"Cloud lists {entry['key_id']} as active, but you pinned it revoked. Keeping it revoked.", file=out)
    for entry in plan["missing"]:
        print(f"Cloud no longer lists {entry['key_id']}; keeping your pinned entry.", file=out)
    accepted: list[str] = []
    offered = [e for e in plan["new"] if e["status"] == "active"]
    if offered:
        print(
            ("First use: no Seal keys pinned yet." if first_use else "Cloud offers a new Seal key (rotation).")
            + " Compare each fingerprint with the one OpenAdapt published outside this download"
            " before trusting it.",
            file=out,
        )
    for entry in offered:
        print(f"  key id:      {entry['key_id']}\n  fingerprint: {fingerprint(entry['public_key'])}", file=out)
        if entry["key_id"] in (args.confirm or []):
            accepted.append(entry["key_id"])
            print("  trusted (--confirm)", file=out)
            continue
        if not is_tty():
            print("  not trusted: confirm by a person at a terminal, or pass --confirm KEY_ID.", file=out)
            continue
        typed = ask("  Type the key id to trust it, or press Enter to skip: ").strip()
        if typed == entry["key_id"]:
            accepted.append(entry["key_id"])
            print("  trusted", file=out)
        else:
            print("  skipped", file=out)
    updated = apply_plan(pinned, plan, accepted)
    if updated == pinned:
        print(f"No change to {path}." if pinned else "Nothing pinned.", file=out)
        return 0 if pinned or not offered else 1
    _write_pinned(path, updated, args.url)
    print(f"Pinned {len(updated)} key(s) in {path}.", file=out)
    if path != pinned_keys_path():
        print(f"Point the agent at it: export OPENADAPT_SEAL_KEYS_PATH={path}", file=out)
    return 0


def cmd_keys_show(args: argparse.Namespace, *, out=None) -> int:
    out = out or sys.stdout
    path = Path(args.path).expanduser() if args.path else pinned_keys_path()
    try:
        keys = _read_pinned(path)
    except Exception as exc:  # noqa: BLE001
        print(f"{path} is unreadable: {exc}", file=out)
        return 1
    if not keys:
        print(f"No Seal keys pinned at {path}.", file=out)
        return 1
    for entry in keys:
        extra = f" (revoked {entry.get('revoked_at')})" if entry["status"] == "revoked" else ""
        print(f"{entry['key_id']}  {fingerprint(entry['public_key'])}  {entry['status']}{extra}", file=out)
    return 0


def cmd_verify(args: argparse.Namespace, *, fetch: Callable[[str], bytes] = _https_get, out=None) -> int:
    out = out or sys.stdout
    target = args.target
    try:
        if target.startswith(("https://", "http://")):
            raw = fetch(target)
        elif target == "-":
            raw = sys.stdin.buffer.read(MAX_BYTES + 1)
        else:
            raw = Path(target).expanduser().read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        verdict = {"verified": False, "reason": "unreadable", "detail": str(exc)}
    else:
        if args.keys:
            try:
                registry = parse_registry(json.loads(Path(args.keys).expanduser().read_text(encoding="utf-8")))
            except Exception:  # noqa: BLE001
                registry = []
        else:
            registry = load_pinned_registry()
        verdict = verify_document(document, registry)
    if args.json:
        print(json.dumps(verdict, indent=2, sort_keys=True), file=out)
    else:
        ok = verdict["verified"] and verdict.get("verified_outcome", False)
        print("SEAL VERIFIED" if ok else "NOT VERIFIED", file=out)
        print(REASONS.get(verdict["reason"], verdict.get("detail", verdict["reason"])), file=out)
        if verdict["verified"] and not verdict.get("verified_outcome", False):
            print("The signature is genuine, but the outcome is not verified.", file=out)
        for key in ("execution_id", "outcome", "key_id", "issued_at", "receipt_sha256"):
            if verdict.get(key):
                print(f"  {key}: {verdict[key]}", file=out)
    return 0 if verdict["verified"] and verdict.get("verified_outcome", False) else 1


def add_seal_parser(sub: Any) -> None:
    seal = sub.add_parser("seal", help="Pin OpenAdapt Cloud's Seal keys and verify a Seal offline.")
    seal_sub = seal.add_subparsers(dest="seal_command", required=True)

    verify = seal_sub.add_parser("verify", help="Check a Seal (receipt, run report, or webhook JSON).")
    verify.add_argument("target", help="Path, https:// URL, or - for stdin")
    verify.add_argument("--keys", default=None, help="Pinned key file (default: your pinned keys)")
    verify.add_argument("--json", action="store_true", help="Print the verdict as JSON")
    verify.set_defaults(func=cmd_verify)

    keys = seal_sub.add_parser("keys", help="Pin or show the Cloud Seal keys this computer trusts.")
    keys_sub = keys.add_subparsers(dest="keys_command", required=True)
    pin = keys_sub.add_parser(
        "pin",
        help="Fetch /api/public/seal-keys and pin new keys after a person confirms each one.",
    )
    pin.add_argument("--url", default=DEFAULT_KEYS_URL, help=f"Key registry URL (default {DEFAULT_KEYS_URL})")
    pin.add_argument("--path", default=None, help="Pinned key file (default ~/.openadapt/seal-keys.json)")
    pin.add_argument(
        "--confirm",
        action="append",
        metavar="KEY_ID",
        help="Trust this key id without a prompt, after checking its fingerprint out of band. Repeatable.",
    )
    pin.set_defaults(func=cmd_keys_pin)
    show = keys_sub.add_parser("show", help="List pinned keys with fingerprints.")
    show.add_argument("--path", default=None)
    show.set_defaults(func=cmd_keys_show)
