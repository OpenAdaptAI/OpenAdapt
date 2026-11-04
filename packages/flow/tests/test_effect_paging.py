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

Page = Callable[[str, int], tuple]
#: Request headers every test server saw, in order.
SEEN: list[dict] = []


@pytest.fixture
def serve():
    servers: list[ThreadingHTTPServer] = []

    def start(page: Page) -> str:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urlsplit(self.path)
                number = int(parse_qs(parts.query).get("page", ["0"])[0])
                SEEN.append(dict(self.headers))
                status, body, *extra = page(parts.path, number)
                data = json.dumps(body).encode()
                self.send_response(status)
                for name, value in (extra[0] if extra else {}).items():
                    self.send_header(name, value)
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


# -- review of #1538 ---------------------------------------------------------------


def test_fhir_total_below_the_read_is_unreadable(serve) -> None:
    holder: dict = {}
    url = serve(lambda path, n: (200, _bundle(holder["url"], n, per=10, total=3)))
    holder["url"] = url
    verifier = _fhir(url)
    state = verifier.capture_pre_state()
    assert not state.reachable
    assert state.detail["read_problem"] == (
        "the Bundle's total is 3, the read returned 5 matches"
    )


def test_fhir_included_resources_are_not_matches(serve) -> None:
    holder: dict = {}

    def page(path: str, n: int) -> tuple:
        bundle = _bundle(holder["url"], n, per=10, total=5)
        bundle["entry"].append(
            {
                "resource": {"resourceType": "Practitioner", "id": "dr1"},
                "search": {"mode": "include"},
            }
        )
        return 200, bundle

    holder["url"] = serve(page)
    assert len(_read(_fhir(holder["url"])) or []) == 6


def _empty_with_outcome(severity: str) -> dict:
    """Epic's empty search: no matches and an OperationOutcome entry."""

    return {
        "resourceType": "Bundle",
        "type": "searchset",
        "total": 0,
        "entry": [
            {
                "resource": {
                    "resourceType": "OperationOutcome",
                    "issue": [{"severity": severity, "code": "processing"}],
                },
                "search": {"mode": "outcome"},
            }
        ],
    }


def test_fhir_a_warning_outcome_is_not_a_record(serve) -> None:
    """Counted as a record, the outcome vanished once the patient was
    created, and the frame refused a clean write as a removed record."""

    url = serve(lambda path, n: (200, _empty_with_outcome("warning")))
    assert _read(_fhir(url)) == []


def test_fhir_an_error_outcome_is_unreadable(serve) -> None:
    url = serve(lambda path, n: (200, _empty_with_outcome("error")))
    state = _fhir(url).capture_pre_state()
    assert not state.reachable
    assert state.detail["read_problem"] == "the search returned an error outcome"


def test_fhir_an_outcome_without_a_mode_is_not_a_record(serve) -> None:
    def page(path: str, n: int) -> tuple:
        bundle = _empty_with_outcome("information")
        bundle["entry"][0].pop("search")
        return 200, bundle

    assert _read(_fhir(serve(page))) == []


def test_fhir_an_outcome_beside_matches_on_a_later_page(serve) -> None:
    """Matches, a warning outcome on page two, and a total that counts only
    the matches: every match is read, the outcome is not."""

    holder: dict = {}

    def page(path: str, n: int) -> tuple:
        bundle = _bundle(holder["url"], n, per=3, total=5)
        if n == 1:
            bundle["entry"] += _empty_with_outcome("warning")["entry"]
        return 200, bundle

    holder["url"] = serve(page)
    records = _read(_fhir(holder["url"]))
    assert [r["id"] for r in records or []] == ["1", "2", "3", "4", "5"]


def test_fhir_next_origin_is_named(serve) -> None:
    url = serve(lambda path, n: (200, _bundle("http://203.0.113.9", n)))
    state = _fhir(url).capture_pre_state()
    assert state.detail["read_problem"] == (
        f"next link origin http://203.0.113.9 != base_url {url}"
    )


def test_openemr_next_links_name_the_site_address(serve) -> None:
    """OpenEMR builds REST next links from its configured site address. Read
    through a mapped port, the link names another origin: unreadable, with
    both origins in the reason, never page one alone."""

    site = "https://localhost:9300"

    def page(path: str, n: int) -> tuple:
        return 200, {
            "validationErrors": [],
            "internalErrors": [],
            "data": PATIENTS[n * 2 : n * 2 + 2],
            "links": {"next": f"{site}/apis/default/api/patient?page={n + 1}"},
        }

    url = serve(page)
    verifier = _rest(url)
    state = verifier.capture_pre_state()
    assert not state.reachable
    assert state.detail["read_problem"] == (
        f"next link origin {site} != base_url {url}"
    )


def _rest_total(url: str, path: str = "meta.total") -> RestRecordVerifier:
    return RestRecordVerifier(
        url,
        records_path="/api/patient",
        records_key="data",
        timeout_s=2.0,
        next_path="links.next",
        total_path=path,
    )


@pytest.mark.parametrize("total,ok", [(5, True), (6, False), (4, False), (None, False)])
def test_rest_total_path(serve, total, ok) -> None:
    def page(path: str, n: int) -> tuple:
        body = _rest_page(n, next_url=lambda m: f"/api/patient?page={m}")
        if total is not None:
            body["meta"] = {"total": total}
        return 200, body

    verifier = _rest_total(serve(page))
    state = verifier.capture_pre_state()
    assert state.reachable is ok
    if not ok:
        assert "total" in state.detail["read_problem"]


def test_a_redirect_never_carries_the_auth_headers(serve) -> None:
    """A 3xx is a failed read; the configured headers never reach the other
    host."""

    SEEN.clear()
    other = serve(lambda path, n: (200, {"data": PATIENTS, "links": {}}))
    url = serve(lambda path, n: (302, {}, {"Location": f"{other}/api/patient"}))
    verifier = RestRecordVerifier(
        url,
        records_path="/api/patient",
        records_key="data",
        headers={"X-Api-Key": "test-key-not-a-secret"},
        timeout_s=2.0,
    )
    state = verifier.capture_pre_state()
    assert not state.reachable
    assert state.detail["read_problem"].endswith("returned HTTP 302")
    assert len(SEEN) == 1  # the other host was never asked

    SEEN.clear()
    fhir_url = serve(lambda path, n: (302, {}, {"Location": f"{other}/fhir/Patient"}))
    state = _fhir(fhir_url).capture_pre_state()
    assert not state.reachable
    assert len(SEEN) == 1


def test_the_verdict_names_why_the_read_failed(serve) -> None:
    from openadapt_flow.runtime.effects import Effect, EffectKind

    url = serve(
        lambda path, n: (
            200,
            _rest_page(
                n, next_url=lambda m: f"http://203.0.113.9/api/patient?page={m}"
            ),
        )
    )
    verifier = _rest(url)
    before = verifier.capture_pre_state()
    effect = Effect(kind=EffectKind.RECORD_WRITTEN, match={"lname": "L5"}, timeout_s=0)
    verdict = verifier.verify(effect, before)
    assert "next link origin http://203.0.113.9" in verdict.reason
