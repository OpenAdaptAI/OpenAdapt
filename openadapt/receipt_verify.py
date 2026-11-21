"""``openadapt verify <file|folder|link>``: check any receipt, of any format.

A receipt (``record/run/v1``, ``receipt.json``) is checked against the pinned
key registry: this computer's key, Cloud keys pinned with ``openadapt receipt
keys pin``, and any ``--keys FILE``. The verifier prints who signed it and
fails closed on an unknown key, schema or role.

Every older artifact still verifies, labelled "older format":
``local-seal/v1`` (``seal.json``), ``public-seal/v1``, ``seal-signature/v1``
(a Cloud-signed Execute receipt), ``run-receipt/v2`` (the run summary),
``write-receipt/v1`` and ``tx-receipt/v1``-``v3``.

Signing isn't grading: the outcome is printed as the run recorded it, and a
stopped run's receipt is still a valid receipt.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

OLDER = "older format"

Say = Callable[[str], None]


def _load_json(path: Path) -> Any:
    from openadapt_types.receipt import loads_strict

    return loads_strict(path.read_text(encoding="utf-8"))


def _registry(keys: Optional[Path]) -> list[Any]:
    from openadapt_flow.receipt_record import load_role_registry, local_registry

    extra = load_role_registry(keys) if keys is not None else []
    return local_registry(extra=extra)


def _describe(result: Any, say: Say, indent: str = "  ") -> None:
    for line in result.signer_lines:
        say(f"{indent}{line}")
    if not result.ok:
        if result.detail:
            say(f"{indent}({result.detail})")
        return
    statement = result.statement or {}
    predicate = statement.get("predicate", {})
    reason = predicate.get("reason_code")
    say(
        f"{indent}Outcome: {result.outcome}"
        + (f" ({reason})" if reason else "")
        + ", as the run recorded it. A receipt doesn't grade the run."
    )
    checks = predicate.get("checks", {})
    lines = predicate.get("lines", [])
    say(
        f"{indent}Writes: {len(lines)} "
        f"({checks.get('lines_confirmed', 0)} confirmed, "
        f"{checks.get('lines_refuted', 0)} refuted, "
        f"{checks.get('lines_not_checked', 0)} not checked)"
    )
    for name, value in sorted(result.disclosed.items()):
        say(f"{indent}{name}: {value}")


def verify_record_file(
    path: Path, registry: Sequence[Any], say: Say, *, openings: Optional[Path] = None
) -> int:
    from openadapt_flow.receipt_record import PRIVATE_FILE, verify_run_receipt
    from openadapt_types.receipt import verify_receipt

    if path.name == "receipt.json" and (path.parent / "report.json").is_file():
        result = verify_run_receipt(path.parent, registry)
    else:
        sidecar = openings or path.with_name(PRIVATE_FILE)
        doc = _load_json(path)
        opened = (
            _load_json(sidecar)
            if sidecar.is_file() and doc.get("schema") is None
            else None
        )
        result = verify_receipt(doc, registry, openings=opened)
    say(f"{path}  receipt (record/run/v1)")
    _describe(result, say)
    return 0 if result.ok else 1


def _legacy_label(say: Say, path: Path, schema: str) -> None:
    say(f"{path}  {schema} ({OLDER})")


def verify_path(
    target: str,
    *,
    keys: Optional[Path] = None,
    extra: Sequence[str] = (),
    say: Say = print,
    invoke_flow: Callable[[list[str]], int],
    invoke_agent: Callable[[list[str]], int],
    legacy_folder: Callable[[Path, list[str]], int],
    legacy_link: Callable[[str, list[str]], int],
    is_link: Callable[[str], bool],
) -> int:
    """Verify one target. Returns 0 only when every receipt in it verified."""
    if is_link(target):
        say(f"{target}  public-seal/v1 ({OLDER})")
        return legacy_link(target, list(extra))
    path = Path(target)
    if path.is_dir():
        return _verify_folder(
            path, keys=keys, extra=extra, say=say, legacy_folder=legacy_folder
        )
    if not path.is_file():
        say(f"Not verified: {target} is not a file, folder or receipt link")
        return 1
    data = path.read_bytes()
    if not data.lstrip().startswith(b"{"):
        return _verify_cose(path, data, extra=extra, say=say, invoke_flow=invoke_flow)
    try:
        doc = _load_json(path)
    except ValueError:
        say(f"{path}  Not verified: malformed receipt (not JSON)")
        return 1
    from openadapt_types.receipt import (
        OPENINGS_SCHEMA,
        is_receipt_card,
        is_receipt_envelope,
    )

    if is_receipt_envelope(doc) or is_receipt_card(doc):
        return verify_record_file(path, _registry(keys), say)
    schema = (
        (doc.get("schema") or doc.get("schema_version"))
        if isinstance(doc, dict)
        else None
    )
    if schema == OPENINGS_SCHEMA:
        say(
            f"{path} is the receipt's private openings, not a receipt. "
            "Verify receipt.json; keep this file private."
        )
        return 1
    if schema == "openadapt.run-receipt/v2":
        return _verify_run_summary(path, doc, say)
    if schema == "openadapt.local-seal/v1":
        # A Seal is over its run's report: check the folder it sits in.
        _legacy_label(say, path, "local-seal/v1")
        legacy_extra = list(extra) if keys is None else ["--keys", str(keys)]
        return legacy_folder(path.parent, legacy_extra)
    if isinstance(doc, dict) and {"card", "seal"} <= set(doc):
        return _verify_public_seal(path, data, keys, say)
    if isinstance(doc, dict) and "seal_signature" in doc:
        _legacy_label(say, path, "seal-signature/v1")
        return invoke_agent(["seal", "verify", str(path), *extra])
    say(f"{path}  Not verified: unknown receipt format ({schema or 'no schema'})")
    return 1


def _verify_public_seal(path: Path, data: bytes, keys: Optional[Path], say: Say) -> int:
    """``public-seal/v1``: its signature, and its key must be pinned here."""
    from openadapt_flow.public_seal import verify_public_seal

    _legacy_label(say, path, "public-seal/v1")
    result = verify_public_seal(data)
    if not result.ok:
        say(f"  Not verified: {result.reason}")
        return 1
    pinned = {k.key_id: k for k in _registry(keys)}
    key = pinned.get(str(result.key_id))
    if key is None:
        say("  Not verified: unknown signer")
        say(f"  (key {result.key_id} isn't pinned here; pin it with --keys FILE)")
        return 1
    from openadapt_types.receipt import SIGNER_LINES

    say("  " + SIGNER_LINES[key.role].format(org=key.org or "the customer"))
    return 0


def _verify_folder(
    folder: Path,
    *,
    keys: Optional[Path],
    extra: Sequence[str],
    say: Say,
    legacy_folder: Callable[[Path, list[str]], int],
) -> int:
    def signed(d: Path) -> bool:
        return (d / "receipt.json").is_file() or (d / "seal.json").is_file()

    runs = (
        [folder]
        if signed(folder)
        else sorted(c for c in folder.iterdir() if c.is_dir() and signed(c))
    )
    with_receipt = [r for r in runs if _is_record(r / "receipt.json")]
    # A receipt.json that is neither a receipt nor an older run summary is a
    # broken receipt: it fails, and a seal.json beside it doesn't rescue it.
    broken = [
        r
        for r in runs
        if r not in with_receipt
        and (r / "receipt.json").is_file()
        and not _is_summary(r / "receipt.json")
    ]
    sealed_only = [
        r
        for r in runs
        if r not in with_receipt and r not in broken and (r / "seal.json").is_file()
    ]
    unsigned = [r for r in runs if r not in with_receipt + broken + sealed_only]
    if not runs:
        say(f"Not verified: no receipt in {folder} (no receipt.json or seal.json)")
        return 1
    failed = 0
    for run in broken:
        say(f"{run / 'receipt.json'}  Not verified: malformed receipt")
        failed += 1
    for run in unsigned:
        say(f"{run}  Not verified: no receipt.json or seal.json")
        failed += 1
    if with_receipt:
        registry = _registry(keys)
        for run in with_receipt:
            failed += verify_record_file(run / "receipt.json", registry, say)
    if sealed_only:
        say(f"{len(sealed_only)} run(s) have only a seal.json ({OLDER})")
        legacy_extra = [*extra] if keys is None else ["--keys", str(keys)]
        for run in sealed_only:
            failed += 1 if legacy_folder(run, legacy_extra) else 0
    total = len(runs)
    say(f"{total - failed} of {total} verified")
    return 0 if failed == 0 else 1


def _is_record(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        from openadapt_types.receipt import is_receipt_envelope

        return is_receipt_envelope(_load_json(path))
    except (OSError, ValueError):
        return False


def _is_summary(path: Path) -> bool:
    try:
        doc = _load_json(path)
    except (OSError, ValueError):
        return False
    return isinstance(doc, dict) and str(doc.get("schema_version", "")).startswith(
        "openadapt.run-receipt/"
    )


def _verify_run_summary(path: Path, doc: Any, say: Say) -> int:
    """``run-receipt/v2``: unsigned; check its own digest only."""
    _legacy_label(say, path, "run-receipt/v2")
    try:
        from openadapt_flow.receipt import RunReceipt

        RunReceipt.model_validate(doc)
    except Exception as exc:  # noqa: BLE001 - any failure is a refusal
        say(
            f"  Not verified: the summary's digest doesn't match ({str(exc).splitlines()[0]})"
        )
        return 1
    say("  Digest matches. Not signed: this older summary proves no signer.")
    return 0


def _verify_cose(
    path: Path,
    data: bytes,
    *,
    extra: Sequence[str],
    say: Say,
    invoke_flow: Callable[[list[str]], int],
) -> int:
    try:
        from openadapt_flow.receipt_log import decode_sign1
        from openadapt_flow.tx.receipt import TX_RECEIPT_CONTENT_TYPE
        from openadapt_flow.write_receipt import CONTENT_TYPE

        _, headers, _, _, _ = decode_sign1(data)
        from openadapt_flow.receipt_log import CTY

        content_type = headers.get(CTY)
    except Exception:  # noqa: BLE001
        say(f"{path}  Not verified: unknown receipt format")
        return 1
    if content_type == CONTENT_TYPE:
        _legacy_label(say, path, "write-receipt/v1")
        return invoke_flow(["receipt", "verify", str(path), *extra])
    if content_type == TX_RECEIPT_CONTENT_TYPE:
        return _verify_tx(path, extra, say)
    say(f"{path}  Not verified: unknown receipt format")
    return 1


def _verify_tx(path: Path, extra: Sequence[str], say: Say) -> int:
    """``tx-receipt/v1``-``v3``: needs the signer's public key (``--public-key``)."""
    from openadapt_flow.tx.receipt import audit_legacy_tx_receipt, verify_tx_receipt

    args = list(extra)
    if "--public-key" not in args or args.index("--public-key") + 1 >= len(args):
        say(f"{path}  tx-receipt ({OLDER})")
        say(
            "  Not verified: unknown signer (pass --public-key FILE, the tx signer's key)"
        )
        return 1
    key_path = Path(args[args.index("--public-key") + 1])
    log_dir = args[args.index("--log-dir") + 1] if "--log-dir" in args else None
    from cryptography.hazmat.primitives import serialization

    raw = key_path.read_bytes()
    try:
        key: Any = serialization.load_pem_public_key(raw)
    except ValueError:
        key = raw
    try:
        payload = verify_tx_receipt(path, key, log_dir)
        schema = str(payload.get("schema_version", "tx-receipt/v3"))
    except ValueError as exc:
        if "legacy_tx_receipt" not in str(exc):
            say(f"{path}  tx-receipt ({OLDER})")
            say(f"  Not verified: {exc}")
            return 1
        try:
            audit = audit_legacy_tx_receipt(path, key, log_dir)
        except ValueError as again:
            say(f"{path}  tx-receipt ({OLDER})")
            say(f"  Not verified: {again}")
            return 1
        schema = str(audit["source_schema"])
    say(f"{path}  {schema.removeprefix('openadapt.')} ({OLDER})")
    say(f"  Signed by the tx signer key in {key_path}")
    return 0


def to_json(result: Any) -> str:
    return json.dumps(
        {
            "ok": result.ok,
            "reason": result.reason,
            "signer_lines": list(result.signer_lines),
            "outcome": result.outcome,
            "demo": result.demo,
        }
    )
