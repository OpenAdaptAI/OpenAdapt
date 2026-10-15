"""Attach a Cloud-signed Execute receipt (the Seal) to a local run report.

When a governed run is connected to OpenAdapt Cloud Execute, Cloud issues an
``ExecuteEvidenceReceiptV1`` for the execution and, when its signing key is
configured, a ``seal_signature`` (``openadapt.seal-signature/v1``, Ed25519).
This module fetches that receipt after a VERIFIED local outcome and writes it
into the run's ``report.json`` as ``execute_receipt`` with the Cloud
``cloud_execution_id`` it belongs to. ``openadapt-agent`` then reports
``sealed: true`` only if the signature verifies against keys a person pinned.

Fail closed, never upgrade:

* Nothing is fetched unless the local report is ``VERIFIED``.
* Cloud must report the execution terminal with a verified outcome, and the
  receipt must name the same execution, carry a verified outcome, and carry a
  ``seal_signature``. Anything else attaches nothing.
* This module does not decide whether the Seal is genuine. It never
  verifies or trusts the signature; verification belongs to the holder's
  pinned keys (``openadapt-agent seal verify``).
* The report as it was before attaching is kept as ``report.pre-seal.json``
  so any earlier digest of the report stays checkable.

Configuration (or the matching keyword arguments):

* ``OPENADAPT_CLOUD_EXECUTION_ID`` - the Cloud execution this run belongs to.
* ``OPENADAPT_EXECUTE_API_KEY`` - an Execute API key for that org.
* ``OPENADAPT_CLOUD_URL`` - default ``https://app.openadapt.ai``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_CLOUD_URL = "https://app.openadapt.ai"
RECEIPT_SCHEMA = "openadapt.execute-evidence-receipt/v1"
SEAL_SIGNATURE_SCHEMA = "openadapt.seal-signature/v1"
_SEALABLE = {"verified", "rolled_back_verified"}
_EXECUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
MAX_RECEIPT_BYTES = 65536

#: (method, url, headers) -> (status, body bytes). Injected by tests.
Transport = Callable[[str, str, Mapping[str, str]], "tuple[int, bytes]"]


class CloudSealError(RuntimeError):
    """A closed reason the receipt was not attached."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class AttachResult:
    attached: bool
    code: str
    execution_id: Optional[str] = None


def _default_transport(
    method: str, url: str, headers: Mapping[str, str]
) -> "tuple[int, bytes]":
    import httpx

    with httpx.Client(timeout=20.0, follow_redirects=False) as client:
        response = client.request(method, url, headers=dict(headers))
        return response.status_code, response.content[: MAX_RECEIPT_BYTES + 1]


def _get_json(transport: Transport, url: str, api_key: str) -> "tuple[int, Any]":
    status, body = transport(
        "GET", url, {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    )
    if len(body) > MAX_RECEIPT_BYTES:
        raise CloudSealError("response_too_large")
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CloudSealError("malformed_response") from exc


def fetch_sealed_receipt(
    execution_id: str,
    *,
    api_key: str,
    cloud_url: str = DEFAULT_CLOUD_URL,
    transport: Optional[Transport] = None,
    attempts: int = 6,
    delay_s: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Return Cloud's signed receipt for a verified execution, or raise."""
    if not isinstance(execution_id, str) or not _EXECUTION_ID.match(execution_id):
        raise CloudSealError("invalid_execution_id")
    if not api_key:
        raise CloudSealError("missing_api_key")
    base = cloud_url.rstrip("/")
    if not base.startswith("https://") and not re.match(
        r"^http://(127\.0\.0\.1|localhost)(:\d+)?$", base
    ):
        raise CloudSealError("insecure_cloud_url")
    transport = transport or _default_transport
    for attempt in range(max(1, attempts)):
        status, payload = _get_json(
            transport, f"{base}/api/v1/executions/{execution_id}/receipt", api_key
        )
        if status == 200:
            break
        # Cloud says the evidence is still being verified: retry briefly.
        if status == 409 and isinstance(payload, Mapping) and attempt + 1 < attempts:
            sleep(delay_s)
            continue
        code = (
            (payload.get("code") or payload.get("error"))
            if isinstance(payload, Mapping)
            else None
        )
        raise CloudSealError(
            "receipt_unavailable", f"HTTP {status} {code or ''}".strip()
        )
    else:  # pragma: no cover - loop always breaks or raises
        raise CloudSealError("receipt_unavailable")
    if not isinstance(payload, dict):
        raise CloudSealError("malformed_receipt")
    if payload.get("schema_version") != RECEIPT_SCHEMA:
        raise CloudSealError("malformed_receipt", "unexpected schema_version")
    if payload.get("execution_id") != execution_id:
        raise CloudSealError("execution_mismatch")
    if payload.get("outcome") not in _SEALABLE:
        raise CloudSealError("not_verified")
    sig = payload.get("seal_signature")
    if not isinstance(sig, Mapping) or sig.get("schema") != SEAL_SIGNATURE_SCHEMA:
        raise CloudSealError(
            "unsigned", "Cloud issued no Seal signature for this receipt"
        )
    if sig.get("run_id") != execution_id:
        raise CloudSealError("execution_mismatch")
    return payload


def attach_receipt(
    run_dir: Path, receipt: Mapping[str, Any], execution_id: str
) -> Path:
    """Write ``receipt`` into ``run_dir/report.json``; keep the prior report."""
    report_path = Path(run_dir) / "report.json"
    raw = report_path.read_text(encoding="utf-8")
    report = json.loads(raw)
    if not isinstance(report, dict) or report.get("execution_outcome") != "VERIFIED":
        raise CloudSealError("local_not_verified")
    if receipt.get("execution_id") != execution_id:
        raise CloudSealError("execution_mismatch")
    previous = report.get("cloud_execution_id")
    if previous is not None and previous != execution_id:
        raise CloudSealError(
            "execution_mismatch", "report already bound to another execution"
        )
    pre = report_path.with_name("report.pre-seal.json")
    if not pre.exists():
        shutil.copy2(report_path, pre)
    report["cloud_execution_id"] = execution_id
    report["execute_receipt"] = dict(receipt)
    tmp = report_path.with_name("report.json.seal-tmp")
    tmp.write_text(
        json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8"
    )
    os.chmod(tmp, 0o600)
    os.replace(tmp, report_path)
    return report_path


def attach_from_cloud(
    run_dir: Path,
    *,
    execution_id: Optional[str] = None,
    api_key: Optional[str] = None,
    cloud_url: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
    transport: Optional[Transport] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> AttachResult:
    """Fetch and attach the Seal for a connected, VERIFIED run. Never raises."""
    env = os.environ if env is None else env
    execution_id = (execution_id or env.get("OPENADAPT_CLOUD_EXECUTION_ID", "")).strip()
    if not execution_id:
        return AttachResult(False, "not_connected")
    api_key = (api_key or env.get("OPENADAPT_EXECUTE_API_KEY", "")).strip()
    cloud_url = (
        cloud_url or env.get("OPENADAPT_CLOUD_URL", "") or DEFAULT_CLOUD_URL
    ).strip()
    try:
        report = json.loads((Path(run_dir) / "report.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AttachResult(False, "report_unreadable", execution_id)
    if not isinstance(report, dict) or report.get("execution_outcome") != "VERIFIED":
        return AttachResult(False, "local_not_verified", execution_id)
    try:
        receipt = fetch_sealed_receipt(
            execution_id,
            api_key=api_key,
            cloud_url=cloud_url,
            transport=transport,
            sleep=sleep,
        )
    except CloudSealError as exc:
        return AttachResult(False, exc.code, execution_id)
    except Exception:  # network errors must never change the run's result
        return AttachResult(False, "cloud_unreachable", execution_id)
    try:
        attach_receipt(Path(run_dir), receipt, execution_id)
    except CloudSealError as exc:
        return AttachResult(False, exc.code, execution_id)
    except (OSError, ValueError):
        return AttachResult(False, "report_unreadable", execution_id)
    return AttachResult(True, "attached", execution_id)
