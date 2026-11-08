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
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

import pytest

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import fhir, rest
from openadapt_flow.runtime.effects.effect import (
    Effect,
    EffectState,
    Verdict,
)
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
        # This test endpoint implements an immutable response view. Every
        # continuation names the server-side frozen map, not mutable source
        # rows with a constant version-looking decoration.
        views: dict[str, dict[int, tuple]] = {}
        lock = threading.Lock()

        def frozen(path: str, number: int, view: str | None) -> tuple:
            with lock:
                if view is None:
                    view = str(len(views) + 1)
                    pages: dict[int, tuple] = {}
                    for n in range(max(rest.MAX_PAGES, fhir.MAX_PAGES) + 1):
                        status, original, *extra = page(path, n)
                        body = deepcopy(original)
                        links = []
                        if isinstance(body, dict):
                            body["_snapshot"] = view
                            if isinstance(body.get("links"), dict):
                                links.append((body["links"], "next"))
                            for item in body.get("link", []):
                                if item.get("relation") == "next":
                                    links.append((item, "url"))
                        more = False
                        for holder, key in links:
                            following = holder.get(key)
                            if isinstance(following, str) and following:
                                more = True
                                parts = urlsplit(following)
                                query = (
                                    parts.query
                                    + ("&" if parts.query else "")
                                    + "view="
                                    + view
                                )
                                holder[key] = urlunsplit(parts._replace(query=query))
                        pages[n] = (status, body, *deepcopy(extra))
                        if status // 100 != 2 or not more:
                            break
                    views[view] = pages
                return deepcopy(views[view][number])

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                parts = urlsplit(self.path)
                query = parse_qs(parts.query)
                number = int(query.get("page", ["0"])[0])
                SEEN.append(dict(self.headers))
                status, body, *extra = frozen(
                    parts.path, number, query.get("view", [None])[0]
                )
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
        field_paths={"id": "id", "lname": "name.family"},
        access_token="test-token-not-a-secret",
        timeout_s=2.0,
        snapshot_path="_snapshot",
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
        snapshot_path="_snapshot",
    )


@pytest.mark.parametrize(
    "representation",
    ["same_row", "same_id_changed", "idless", "one_page", "numeric_id"],
)
def test_rest_overlapping_rows_cannot_fill_the_total_and_hide_an_extra_record(
    serve, representation: str
) -> None:
    """Independent HTTP probe for #2041; not the reviewer's unseen probe."""
    identity = "key" if representation == "idless" else "id"
    first: dict[str, Any] = {identity: "a", "kind": "wanted"}
    second = {identity: "b", "kind": "wanted"}
    hidden = {identity: "hidden", "kind": "unintended"}
    if representation == "numeric_id":
        first[identity] = 1
    actual_store = [first, second, hidden]
    repeated = dict(first)
    if representation == "numeric_id":
        repeated[identity] = 1.0
    if representation == "same_id_changed":
        repeated["revision"] = "later"
    after = False

    def page(_path: str, n: int) -> tuple[int, dict]:
        if not after:
            return 200, {"data": [], "total": 0, "links": {"next": None}}
        if representation == "one_page":
            rows, following = [first, repeated, second], None
        else:
            rows = [first] if n == 0 else [repeated, second]
            following = "/api/patient?page=1" if n == 0 else None
        return 200, {
            "data": rows,
            "total": len(actual_store),
            "links": {"next": following},
        }

    url = serve(page)
    verifier = RestRecordVerifier(
        url,
        records_path="/api/patient",
        records_key="data",
        next_path="links.next",
        total_path="total",
        timeout_s=1,
        snapshot_path="_snapshot",
    )
    before = verifier.capture_pre_state()
    assert before.reachable and before.readback_complete is True
    after = True
    expected = Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {},
            "new_records": [{"kind": "wanted"}] * 3,
            "expected_count": 3,
            "identity_field": identity,
            "timeout_s": 0,
        }
    )
    assert len(actual_store) == 3
    assert sum(row["kind"] == "wanted" for row in actual_store) == 2
    verdict = verifier.verify(expected, before)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert verdict.readback_complete is False


def test_rest_distinct_identities_with_equal_business_fields_are_not_collapsed(
    serve,
) -> None:
    rows = [{"id": str(index), "kind": "wanted"} for index in range(3)]
    after = False

    def page(_path: str, n: int) -> tuple[int, dict]:
        values = rows[n : n + 1] if after else []
        following = f"/api/patient?page={n + 1}" if after and n < 2 else None
        return 200, {
            "data": values,
            "total": len(rows) if after else 0,
            "links": {"next": following},
        }

    verifier = RestRecordVerifier(
        serve(page),
        records_path="/api/patient",
        records_key="data",
        next_path="links.next",
        total_path="total",
        timeout_s=1,
        snapshot_path="_snapshot",
    )
    before = verifier.capture_pre_state()
    assert before.reachable and before.readback_complete is True
    after = True
    effect = Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {},
            "new_records": [{"kind": "wanted"}] * 3,
            "expected_count": 3,
            "identity_field": "id",
            "timeout_s": 0,
        }
    )
    result = verifier.verify(effect, before)
    assert result.verdict is Verdict.CONFIRMED
    assert result.observed_count == 3
    assert result.readback_complete is True


@pytest.mark.parametrize("kind", ["field_equals", "record_written"])
def test_rest_complete_current_presence_refutes_despite_unknown_baseline(
    serve, kind: str
) -> None:
    rows = [{"id": "a", "kind": "wanted", "value": "wrong"}]
    if kind == "record_written":
        rows.append({"id": "b", "kind": "wanted", "value": "wrong"})
    url = serve(lambda _path, _n: (200, {"data": rows, "total": len(rows)}))
    verifier = RestRecordVerifier(
        url,
        records_path="/api/patient",
        records_key="data",
        total_path="total",
        timeout_s=1,
    )
    before = EffectState(
        substrate="rest", reachable=False, records=[], readback_complete=None
    )
    spec: dict = {"kind": kind, "match": {"kind": "wanted"}, "timeout_s": 0}
    if kind == "field_equals":
        spec.update(field="value", value="expected")
    result = verifier.verify(Effect.model_validate(spec), before)
    assert result.verdict is Verdict.REFUTED
    assert result.readback_complete is True
    assert result.observed_count == len(rows)


def test_rest_unknown_baseline_still_cannot_confirm_a_complete_current_read(
    serve,
) -> None:
    rows = [{"id": "a", "value": "expected"}]
    url = serve(lambda _path, _n: (200, {"data": rows, "total": len(rows)}))
    verifier = RestRecordVerifier(
        url,
        records_path="/api/patient",
        records_key="data",
        total_path="total",
        timeout_s=1,
    )
    before = EffectState(
        substrate="rest", reachable=False, records=[], readback_complete=None
    )
    effect = Effect.model_validate(
        {
            "kind": "field_equals",
            "match": {"id": "a"},
            "field": "value",
            "value": "expected",
            "timeout_s": 0,
        }
    )
    result = verifier.verify(effect, before)
    assert result.verdict is Verdict.INDETERMINATE
    assert not result.confirmed


@pytest.mark.parametrize("changed_payload", [False, True])
def test_fhir_overlapping_match_identities_cannot_hide_an_extra_record(
    serve, changed_payload: bool
) -> None:
    """Same coverage obligation as REST, using native FHIR identities."""
    first: dict[str, Any] = {
        "resourceType": "Patient",
        "id": "a",
        "name": [{"family": "wanted"}],
    }
    second = {"resourceType": "Patient", "id": "b", "name": [{"family": "wanted"}]}
    hidden = {
        "resourceType": "Patient",
        "id": "hidden",
        "name": [{"family": "unintended"}],
    }
    actual_store = [first, second, hidden]
    repeated = dict(first)
    if changed_payload:
        repeated["meta"] = {"versionId": "2"}
    after = False

    def page(_path: str, n: int) -> tuple[int, dict]:
        rows = ([first] if n == 0 else [repeated, second]) if after else []
        following = (
            [{"relation": "next", "url": "/fhir/Patient?page=1"}]
            if after and n == 0
            else []
        )
        return 200, {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(actual_store) if after else 0,
            "entry": [{"resource": row} for row in rows],
            "link": following,
        }

    verifier = _fhir(serve(page))
    before = verifier.capture_pre_state()
    assert before.reachable and before.records == []
    after = True
    expected = Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {},
            "new_records": [{"lname": "wanted"}] * 3,
            "expected_count": 3,
            "identity_field": "id",
            "timeout_s": 0,
        }
    )
    assert len({(row["resourceType"], row["id"]) for row in actual_store}) == 3
    result = verifier.verify(expected, before)
    assert result.verdict is Verdict.INDETERMINATE


def test_fhir_distinct_identities_with_equal_business_fields_are_not_collapsed(
    serve,
) -> None:
    rows = [
        {"resourceType": "Patient", "id": str(index), "name": [{"family": "wanted"}]}
        for index in range(3)
    ]
    after = False
    holder: dict[str, str] = {}

    def page(_path: str, n: int) -> tuple[int, dict]:
        values = rows[n : n + 1] if after else []
        following = (
            [{"relation": "next", "url": f"{holder['url']}/fhir/Patient?page={n + 1}"}]
            if after and n < 2
            else []
        )
        return 200, {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(rows) if after else 0,
            "entry": [{"resource": row} for row in values],
            "link": following,
        }

    holder["url"] = serve(page)
    verifier = _fhir(holder["url"])
    before = verifier.capture_pre_state()
    assert before.reachable
    after = True
    effect = Effect.model_validate(
        {
            "kind": "exact_new_set",
            "match": {},
            "new_records": [{"lname": "wanted"}] * 3,
            "expected_count": 3,
            "identity_field": "id",
            "timeout_s": 0,
        }
    )
    result = verifier.verify(effect, before)
    assert result.verdict is Verdict.CONFIRMED
    assert result.observed_count == 3


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


def test_rest_without_next_path_refuses_one_incomplete_response(serve) -> None:
    holder: dict = {}
    holder["url"] = serve(
        lambda path, n: (
            200,
            _rest_page(n, next_url=lambda m: f"/api/patient?page={m}"),
        )
    )
    request_count = len(SEEN)
    state = _rest(holder["url"], next_path=None).capture_pre_state()
    assert not state.reachable
    assert state.records == []
    assert state.readback_complete is None
    assert len(SEEN) - request_count == 1


def test_rest_unconfigured_single_page_cannot_confirm_or_refute(serve) -> None:
    from openadapt_flow.runtime.effects import Effect, EffectKind

    holder: dict = {}
    holder["url"] = serve(
        lambda path, n: (
            200,
            _rest_page(n, next_url=lambda m: f"/api/patient?page={m}"),
        )
    )
    verifier = _rest(holder["url"], next_path=None)
    request_count = len(SEEN)
    before = verifier.capture_pre_state()
    assert not before.reachable
    assert before.records == []
    assert before.readback_complete is None
    assert len(SEEN) - request_count == 1

    absent = Effect(
        kind=EffectKind.EXACT_NEW_SET,
        match={"id": "5"},
        new_records=[{"id": "5", "fname": "F5", "lname": "L5"}],
        expected_count=1,
        identity_field="id",
        timeout_s=0,
    )
    verdict = verifier.verify(absent, before)
    assert verdict.verdict is rest.Verdict.INDETERMINATE
    assert verdict.readback_complete is None
    assert len(SEEN) - request_count == 2


def test_rest_explicit_complete_single_response_can_establish_absence(serve) -> None:
    from openadapt_flow.runtime.effects import Effect, EffectKind

    holder: dict = {}
    holder["url"] = serve(lambda path, n: (200, {"data": PATIENTS, "links": {}}))
    verifier = RestRecordVerifier(
        holder["url"],
        records_path="/api/patient",
        records_key="data",
        records_complete=True,
        timeout_s=1.0,
    )
    before = verifier.capture_pre_state()
    assert before.readback_complete is True
    absent = Effect(
        kind=EffectKind.EXACT_NEW_SET,
        match={"id": "6"},
        new_records=[{"id": "6", "fname": "F6", "lname": "L6"}],
        expected_count=1,
        identity_field="id",
        timeout_s=0,
    )
    verdict = verifier.verify(absent, before)
    assert verdict.verdict is rest.Verdict.REFUTED
    assert verdict.readback_complete is True


def test_rest_malformed_record_entry_is_incomplete(serve) -> None:
    holder: dict = {}
    holder["url"] = serve(lambda path, n: (200, {"data": [PATIENTS[0], None]}))
    verifier = RestRecordVerifier(
        holder["url"],
        records_path="/api/patient",
        records_key="data",
        records_complete=True,
    )
    state = verifier.capture_pre_state()
    assert not state.reachable
    assert state.readback_complete is False
    assert "non-object" in state.detail["read_problem"]


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
        "records_snapshot_path": "_snapshot",
    }
    verifier = cast(
        rest.RestRecordVerifier,
        build_effect_verifier(EffectsConfig.model_validate(section)),
    )
    assert len(_read(verifier) or []) == 5
    assert verifier.records_complete is None
    # Unset, the config serializes as before, so existing digests hold.
    plain = EffectsConfig.model_validate(
        {
            k: v
            for k, v in section.items()
            if k not in {"records_next_path", "records_snapshot_path"}
        }
    )
    assert "records_next_path" not in plain.model_dump(mode="json")
    assert "records_complete" not in plain.model_dump(mode="json")
    assert "records_snapshot_path" not in plain.model_dump(mode="json")


def test_records_complete_declaration_is_explicit_in_config_identity() -> None:
    from openadapt_flow.deployment import _effect_verifier_config_identity

    plain = EffectsConfig.model_validate(
        {"kind": "rest", "base_url": "http://sor", "records_path": "/records"}
    )
    declared = EffectsConfig.model_validate(
        {
            "kind": "rest",
            "base_url": "http://sor",
            "records_path": "/records",
            "records_complete": True,
        }
    )
    assert "records_complete" not in plain.model_dump(mode="json")
    assert declared.model_dump(mode="json")["records_complete"] is True
    assert _effect_verifier_config_identity(plain, object()) != (
        _effect_verifier_config_identity(declared, object())
    )


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
                    "issue": [
                        {
                            "severity": severity,
                            "code": (
                                "informational"
                                if severity in ("warning", "information")
                                else "processing"
                            ),
                        }
                    ],
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
        snapshot_path="_snapshot",
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
    assert state.readback_complete is ok
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
