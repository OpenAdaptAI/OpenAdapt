"""Run 1's read-back on a FHIR store whose resources change on every read.

``RecordReadBack`` compares whole records. Some FHIR servers set a
resource's ``meta.lastUpdated`` (or ``meta.versionId``) when it is read, not
when it is written. This pins what the read-back does then, on the real
``load_observer`` path over HTTP: a field the operator's ``field_paths`` does
not map is never seen; a mapped one stops run 1 as ``wrong_record`` although
only a read changed; ``records_drop_fields`` removes it. The whole-database
snapshot (``db_snapshot``) is not a read-back source: ``load_observer`` has no
kind for it, so it is checked only by the contract frame.
"""

from __future__ import annotations

import itertools
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest
import yaml

from openadapt_flow.compile_agent.harness import record_readback
from openadapt_flow.record_sor import RecordSorError, load_observer

NOTE = "Synthetic follow-up in two weeks"


class _Store:
    def __init__(self) -> None:
        self.resources = [
            {"id": "1", "subject": {"reference": "Patient/8"}, "valueString": "old"}
        ]
        self.reads = itertools.count()

    def bundle(self) -> dict[str, Any]:
        n = next(self.reads)
        entries = [
            {
                "resource": {
                    "resourceType": "Observation",
                    "status": "final",
                    # Changes on every read, as on servers that stamp it then.
                    "meta": {
                        "lastUpdated": f"2026-11-03T12:00:{n:02d}Z",
                        "versionId": str(n),
                    },
                    **r,
                }
            }
            for r in self.resources
        ]
        return {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(entries),
            "entry": entries,
        }


@pytest.fixture
def fhir() -> Iterator[tuple[str, _Store]]:
    store = _Store()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            body = json.dumps(store.bundle()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/fhir+json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/fhir", store
    finally:
        server.shutdown()
        server.server_close()


def _run1_save(tmp_path: Path, base: str, store: _Store, **extra: Any) -> str:
    """One save in run 1, read back as the pipeline does; the outcome."""

    del store.resources[1:]
    verify = tmp_path / "verify.yaml"
    verify.write_text(
        yaml.safe_dump(
            {
                "verifier": {
                    "kind": "fhir",
                    "base_url": base,
                    "resource_type": "Observation",
                    "timeout_s": 2.0,
                    **extra,
                }
            }
        )
    )
    observer, _ = load_observer(verify)
    baseline = observer.system_of_record
    readback = record_readback(
        lambda: observer.system_of_record,
        {"patient": "Patient/9"},
        lambda: baseline,
        values={"note": NOTE},
    )
    readback.start()
    readback.mark()
    store.resources.append(
        {"id": "2", "subject": {"reference": "Patient/9"}, "valueString": NOTE}
    )
    return readback().outcome


def test_an_unmapped_volatile_field_is_never_seen(fhir, tmp_path: Path) -> None:
    base, store = fhir
    assert _run1_save(tmp_path, base, store) == "landed"


@pytest.mark.parametrize("path", ["meta.lastUpdated", "meta.versionId"])
def test_a_mapped_volatile_field_stops_run_1(fhir, tmp_path: Path, path: str) -> None:
    # A false stop: only a read changed the starting record. It fails closed.
    base, store = fhir
    paths = {
        "id": "id",
        "patient": "subject.reference",
        "note": "valueString",
        "updated": path,
    }
    assert _run1_save(tmp_path, base, store, field_paths=paths) == "wrong_record"
    assert (
        _run1_save(
            tmp_path,
            base,
            store,
            field_paths=paths,
            records_drop_fields=["updated"],
        )
        == "landed"
    )


@pytest.mark.parametrize("kind", ["db_snapshot", "database"])
def test_the_database_snapshot_is_not_a_readback_source(
    tmp_path: Path, kind: str
) -> None:
    verify = tmp_path / "verify.yaml"
    verify.write_text(yaml.safe_dump({"verifier": {"kind": kind}}))
    with pytest.raises(RecordSorError):
        load_observer(verify)
