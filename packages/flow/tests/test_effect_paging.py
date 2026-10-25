"""A paged read is the whole read, or unreadable: never its first page.

The FHIR verifier follows a Bundle's ``next`` links and checks ``total``; the
REST verifier follows ``records_next_path`` when it is set. A page on another
origin, a failed page, a chain longer than ``MAX_PAGES`` or a ``total`` above
what was read makes the read unreadable, so no check confirms or refutes from
part of the store. Records are made up.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import fhir, rest
from openadapt_flow.runtime.effects.fhir import FhirEffectVerifier
from openadapt_flow.runtime.effects.rest import RestRecordVerifier

PATIENTS = [{"id": str(i), "fname": f"F{i}", "lname": f"L{i}"} for i in range(1, 6)]

Page = Callable[[str, int], tuple[int, dict]]


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start(page: Page) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urlsplit(self.path)
                number = int(parse_qs(parts.query).get("page", ["0"])[0])
                status, body = page(parts.path, number)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *_args) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


# -- FHIR ------------------------------------------------------------------------


def _bundle(url: str, number: int, *, per: int = 2, total: object = None) -> dict:
    rows = PATIENTS[number * per : number * per + per]
    bundle: dict = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [
            {
                "resource": {
                    "resourceType": "Patient",
                    "id": p["id"],
                    "name": [{"family": p["lname"], "given": [p["fname"]]}],
                }
            }
            for p in rows
        ],
        "link": [{"relation": "self", "url": f"{url}/fhir/Patient?page={number}"}],
    }
    if (number + 1) * per < len(PATIENTS):
        bundle["link"].append(
            {"relation": "next", "url": f"{url}/fhir/Patient?page={number + 1}"}
        )
    if total is not None:
        bundle["total"] = total
    return bundle


def _fhir(url: str) -> FhirEffectVerifier:
    return FhirEffectVerifier(
        f"{url}/fhir",
        resource_type="Patient",
        field_paths={"id": "id", "lname": "name.0.family"},
        access_token="test-token-not-a-secret",
        timeout_s=2.0,
    )


def _read(verifier) -> list | None:
    state = verifier.capture_pre_state()
    return list(state.records) if state.reachable else None


def test_fhir_reads_every_page(serve) -> None:
    holder: dict = {}
    url = serve(lambda path, n: (200, _bundle(holder["url"], n, total=5)))
    holder["url"] = url
    records = _read(_fhir(url))
    assert records is not None
    assert [r["id"] for r in records] == ["1", "2", "3", "4", "5"]


def test_fhir_one_page_without_a_next_link_is_the_whole_read(serve) -> None:
    holder: dict = {}
    url = serve(lambda path, n: (200, _bundle(holder["url"], n, per=10)))
    holder["url"] = url
    assert len(_read(_fhir(url)) or []) == 5


def test_fhir_next_page_on_another_origin_is_unreadable(serve) -> None:
    def page(path: str, n: int) -> tuple[int, dict]:
        bundle = _bundle("http://203.0.113.9", n)
        return 200, bundle

    url = serve(page)
    assert _read(_fhir(url)) is None


def test_fhir_failed_later_page_is_unreadable(serve) -> None:
    holder: dict = {}

    def page(path: str, n: int) -> tuple[int, dict]:
        if n == 2:
            return 503, {"resourceType": "OperationOutcome"}
        return 200, _bundle(holder["url"], n)

    holder["url"] = serve(page)
    assert _read(_fhir(holder["url"])) is None


def test_fhir_total_above_the_read_is_unreadable(serve) -> None:
    holder: dict = {}
    url = serve(lambda path, n: (200, _bundle(holder["url"], n, per=10, total=7)))
    holder["url"] = url
    assert _read(_fhir(url)) is None


def test_fhir_endless_next_links_are_unreadable(serve, monkeypatch) -> None:
    monkeypatch.setattr(fhir, "MAX_PAGES", 4)
    holder: dict = {}

    def page(path: str, n: int) -> tuple[int, dict]:
        bundle = _bundle(holder["url"], 0)
        bundle["link"][-1]["url"] = f"{holder['url']}/fhir/Patient?page={n + 1}"
        return 200, bundle

    holder["url"] = serve(page)
    assert _read(_fhir(holder["url"])) is None


# -- REST ------------------------------------------------------------------------


def _rest_page(
    n: int, *, per: int = 2, next_url: Callable[[int], str] | None = None
) -> dict:
    rows = PATIENTS[n * per : n * per + per]
    more = (n + 1) * per < len(PATIENTS)
    nxt = next_url(n + 1) if (more and next_url) else None
    return {"data": rows, "links": {"next": nxt}}


def _rest(url: str, next_path: str | None = "links.next") -> RestRecordVerifier:
    return RestRecordVerifier(
        url,
        records_path="/api/patient",
        records_key="data",
        timeout_s=2.0,
        next_path=next_path,
    )


def test_rest_follows_relative_and_absolute_next_links(serve) -> None:
    holder: dict = {}

    def page(path: str, n: int) -> tuple[int, dict]:
        # Page 1 links absolutely, page 2 relatively.
        return 200, _rest_page(
            n,
            next_url=lambda m: (
                f"{holder['url']}/api/patient?page={m}"
                if m == 1
                else f"/api/patient?page={m}"
            ),
        )

    holder["url"] = serve(page)
    records = _read(_rest(holder["url"]))
    assert records is not None and [r["id"] for r in records] == list("12345")


def test_rest_without_next_path_reads_one_response(serve) -> None:
    holder: dict = {}
    holder["url"] = serve(
        lambda path, n: (
            200,
            _rest_page(n, next_url=lambda m: f"/api/patient?page={m}"),
        )
    )
    records = _read(_rest(holder["url"], next_path=None))
    assert records is not None and len(records) == 2


def test_rest_next_page_on_another_origin_is_unreadable(serve) -> None:
    url = serve(
        lambda path, n: (
            200,
            _rest_page(
                n, next_url=lambda m: f"http://203.0.113.9/api/patient?page={m}"
            ),
        )
    )
    assert _read(_rest(url)) is None


def test_rest_failed_later_page_is_unreadable(serve) -> None:
    def page(path: str, n: int) -> tuple[int, dict]:
        if n == 1:
            return 500, {"error": "unavailable"}
        return 200, _rest_page(n, next_url=lambda m: f"/api/patient?page={m}")

    assert _read(_rest(serve(page))) is None


def test_rest_a_next_link_that_is_not_text_is_unreadable(serve) -> None:
    url = serve(lambda path, n: (200, {"data": PATIENTS[:2], "links": {"next": 7}}))
    assert _read(_rest(url)) is None


def test_rest_endless_next_links_are_unreadable(serve, monkeypatch) -> None:
    monkeypatch.setattr(rest, "MAX_PAGES", 4)
    url = serve(
        lambda path, n: (
            200,
            {"data": PATIENTS[:1], "links": {"next": f"/api/patient?page={n + 1}"}},
        )
    )
    assert _read(_rest(url)) is None


def test_records_next_path_in_a_config(serve) -> None:
    holder: dict = {}
    holder["url"] = serve(
        lambda path, n: (
            200,
            _rest_page(n, next_url=lambda m: f"/api/patient?page={m}"),
        )
    )
    section = {
        "kind": "rest",
        "base_url": holder["url"],
        "records_path": "/api/patient",
        "records_key": "data",
        "records_next_path": "links.next",
    }
    verifier = build_effect_verifier(EffectsConfig.model_validate(section))
    assert len(_read(verifier) or []) == 5
    # Unset, the config serializes as before, so existing digests hold.
    plain = EffectsConfig.model_validate(
        {k: v for k, v in section.items() if k != "records_next_path"}
    )
    assert "records_next_path" not in plain.model_dump(mode="json")
