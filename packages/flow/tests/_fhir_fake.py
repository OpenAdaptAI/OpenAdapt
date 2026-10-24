"""A faithful in-repo fake of the OpenEMR FHIR R4 API for CI.

It mirrors the REAL FHIR wire contract -- ``GET {base}/Observation?patient=..``
returns an HL7 FHIR R4 ``Bundle`` search-set of nested ``Observation``
resources, and a missing/invalid bearer token returns ``401`` -- so the
:class:`FhirEffectVerifier` is exercised against the exact response SHAPE a
live OpenEMR emits, never against MockMed's screen. It also lets a test inject
the transactional fault classes at the RECORD level (duplicate resource,
dropped ``valueString``, phantom write, deleted concurrent resource) to prove
the effect verifier is substrate-agnostic across FHIR and REST.

This is a test helper (leading underscore -> pytest does not collect it).
"""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse


class FhirStore:
    """In-memory FHIR resource store (the system of record ground truth)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.resources: list[dict] = []

    def add_observation(
        self,
        *,
        patient: str,
        note: Optional[str],
        status: str = "final",
        code_text: str = "Clinical note",
        source: str = "replay",
    ) -> dict:
        """Add an Observation, mirroring OpenEMR's real resource shape.

        ``note=None`` models a partial save (the row persists but the
        ``valueString`` field is dropped).
        """
        with self._lock:
            res = {
                "resourceType": "Observation",
                "id": uuid.uuid4().hex,
                "meta": {"versionId": "1"},
                "status": status,
                "category": [
                    {
                        "coding": [
                            {
                                "system": (
                                    "http://terminology.hl7.org/CodeSystem/"
                                    "observation-category"
                                ),
                                "code": "social-history",
                            }
                        ]
                    }
                ],
                "code": {"text": code_text},
                "subject": {"reference": f"Patient/{patient}"},
                "effectiveDateTime": "2026-07-13T00:00:00+00:00",
                "_source": source,
            }
            if note is not None:
                res["valueString"] = note
            self.resources.append(res)
            return res

    def delete_where(self, *, patient: str, source: str) -> int:
        with self._lock:
            before = len(self.resources)
            self.resources = [
                r
                for r in self.resources
                if not (
                    r["subject"]["reference"] == f"Patient/{patient}"
                    and r.get("_source") == source
                )
            ]
            return before - len(self.resources)

    def search(self, patient: Optional[str]) -> list[dict]:
        with self._lock:
            out = []
            for r in self.resources:
                if patient is None or r["subject"]["reference"] == (
                    f"Patient/{patient}"
                ):
                    # Strip the internal _source marker from the wire form.
                    out.append({k: v for k, v in r.items() if k != "_source"})
            return out


def _bundle(
    resources: list[dict],
    *,
    count: Optional[int] = None,
    offset: int = 0,
    base_url: str = "http://fake/Observation",
    extra_params: Optional[dict[str, str]] = None,
    total_override: Optional[int] = None,
) -> dict:
    total = len(resources) if total_override is None else total_override
    if count is not None and count > 0:
        paged = resources[offset : offset + count]
    else:
        paged = resources[offset:]

    links = [{"relation": "self", "url": base_url}]
    if count is not None and offset + count < len(resources):
        params = dict(extra_params or {})
        params["_count"] = str(count)
        params["_offset"] = str(offset + count)
        query_str = "&".join(f"{k}={v}" for k, v in params.items())
        next_url = f"{base_url}?{query_str}" if query_str else base_url
        links.append({"relation": "next", "url": next_url})

    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": total,
        "link": links,
        "entry": [{"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in paged],
    }


def _make_handler(store: FhirStore, token: Optional[str]):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:  # noqa: A002
            pass

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            if token is None:
                return True
            return self.headers.get("Authorization") == f"Bearer {token}"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if not self._authed():
                self._send(
                    401,
                    {
                        "resourceType": "OperationOutcome",
                        "issue": [{"severity": "error", "code": "login"}],
                    },
                )
                return
            if parsed.path.rstrip("/").endswith("/Observation"):
                qs = parse_qs(parsed.query)
                patient = (qs.get("patient", [None]) or [None])[0]
                count_raw = (qs.get("_count", [None]) or [None])[0]
                offset_raw = (qs.get("_offset", [None]) or [None])[0]
                bad_total = (qs.get("_bad_total", [None]) or [None])[0]
                count = int(count_raw) if count_raw is not None else None
                offset = int(offset_raw) if offset_raw is not None else 0
                total_override = int(bad_total) if bad_total is not None else None
                all_res = store.search(patient)
                host = self.headers.get("Host", "127.0.0.1")
                extra = {"patient": patient} if patient else {}
                self._send(
                    200,
                    _bundle(
                        all_res,
                        count=count,
                        offset=offset,
                        base_url=f"http://{host}{parsed.path}",
                        extra_params=extra,
                        total_override=total_override,
                    ),
                )
                return
            self._send(
                404,
                {"resourceType": "OperationOutcome", "issue": []},
            )

    return _Handler


def serve(
    *, token: Optional[str] = None, host: str = "127.0.0.1"
) -> tuple[str, FhirStore, Callable[[], None]]:
    """Serve the fake FHIR R4 API in a background thread.

    Returns ``(base_url, store, stop)`` where ``base_url`` is the FHIR base
    (no trailing slash), ``store`` is the ground-truth resource store, and
    ``stop()`` shuts it down.
    """
    store = FhirStore()
    httpd = ThreadingHTTPServer((host, 0), _make_handler(store, token))
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, name="fake-fhir", daemon=True)
    thread.start()

    def stop() -> None:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)

    return f"http://{host}:{port}", store, stop
