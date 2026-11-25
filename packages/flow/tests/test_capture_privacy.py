"""Synthetic PHI in a captured body never reaches disk unless raw is opted in.

Covers both capture paths that write bodies: the recorder's
``network.jsonl`` (:mod:`openadapt_flow.network_capture`) and the wire tap's
saved HAR (:func:`openadapt_flow.tx.wire_tap.dump_har`). The PHI here
appears only in response or request bodies and URL queries, never in a
declared field, so a hit on disk is a leak. Every file under the test
directory is scanned after decryption, in raw, percent-encoded,
JSON-escaped, base64 and gzip forms. Made-up values only.
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus

import pytest

from openadapt_flow import at_rest, crypto
from openadapt_flow import capture_privacy as cp
from openadapt_flow import network_capture as nc
from openadapt_flow.tx.wire_tap import SessionWireTap, dump_har, load_har

# Built at run time so the literals are clearly synthetic.
PATIENT = " ".join(["Zelda", "Quixotewright"])
DOB = "-".join(["1931", "02", "29"])
MRN = "".join(["MRN", "880", "417", "Q"])
DIAGNOSIS = " ".join(["synthetic", "glimmerpox", "stage", "IV"])
PHI = (PATIENT, DOB, MRN, DIAGNOSIS)


def _forms(value: str) -> list[bytes]:
    raw = value.encode("utf-8")
    return [
        raw,
        quote(value, safe="").encode(),
        quote_plus(value).encode(),
        json.dumps(value, ensure_ascii=False)[1:-1].encode(),
        json.dumps(value, ensure_ascii=True)[1:-1].encode(),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw).rstrip(b"="),
    ]


def _plain(data: bytes) -> bytes:
    """``data`` decrypted (Flow's at-rest format) and gunzipped when it is.

    A file that is encrypted but doesn't decrypt fails the test: the scan
    must never pass by looking at ciphertext.
    """
    if crypto.is_encrypted(data):
        data = crypto.decrypt_bytes(
            data, crypto.resolve_read_key(None), aad=b"openadapt-flow/tx-private-state"
        )
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _decoded(data: bytes) -> list[bytes]:
    """``data`` and every body inside it: JSON strings, base64 and gzip."""
    out = [data]
    try:
        document = json.loads(data)
    except ValueError:
        return out
    stack = [document]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
        elif isinstance(node, str):
            out.append(node.encode("utf-8", "surrogatepass"))
            try:
                blob = base64.b64decode(node, validate=True)
            except ValueError:
                continue
            out.append(blob)
            if blob[:2] == b"\x1f\x8b":
                out.append(gzip.decompress(blob))
    return out


def _leaks(root: Path, values: tuple[str, ...] = PHI) -> list[tuple[str, str]]:
    from openadapt_flow import sealed_set

    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if (
            sealed_set.is_sealed_path(path)
            and path.name != sealed_set.HEADER
            and path.name.endswith(".enc")
        ):
            raw = sealed_set.read_bytes(path.with_name(path.name[:-4]))
        else:
            raw = path.read_bytes()
        blobs = _decoded(_plain(raw))
        for value in values:
            if any(form in blob for blob in blobs for form in _forms(value)):
                found.append((str(path.relative_to(root)), value))
    return found


# A canary whose raw, percent, JSON-escaped and base64 spellings all differ.
CANARY = 'Zoë "Q" Ångström/1931'


@pytest.mark.parametrize("index", range(7))
def test_scanner_finds_each_encoding(tmp_path: Path, index: int) -> None:
    form = _forms(CANARY)[index]
    others = [f for i, f in enumerate(_forms(CANARY)) if i != index]
    assert form not in others  # each control checks a distinct spelling
    (tmp_path / "f.bin").write_bytes(b"x" + form + b"y")
    assert _leaks(tmp_path, (CANARY,)) == [("f.bin", CANARY)]


def test_scanner_finds_gzip_inside_a_har_body_and_refuses_ciphertext(
    tmp_path: Path,
) -> None:
    body = base64.b64encode(gzip.compress(CANARY.encode())).decode()
    har = {"log": {"entries": [{"response": {"content": {"text": body}}}]}}
    (tmp_path / "s.har").write_text(json.dumps(har))
    assert _leaks(tmp_path, (CANARY,)) == [("s.har", CANARY)]
    sealed = crypto.encrypt_bytes(b"{}", "a-different-key", aad=b"other")
    (tmp_path / "s.har").write_bytes(sealed)
    with pytest.raises(Exception):
        _leaks(tmp_path, (CANARY,))


# -- network.jsonl ------------------------------------------------------------


class _Response:
    def __init__(self, status: int, content_type: str, body: bytes) -> None:
        self.status = status
        self.headers = {"content-type": content_type}
        self._body = body

    def body(self) -> bytes:
        return self._body

    def headers_array(self) -> list[dict[str, str]]:
        return [{"name": "Content-Type", "value": self.headers["content-type"]}]


class _Request:
    def __init__(self, url, method, body, content_type, response) -> None:
        self.url, self.method, self.resource_type = url, method, "fetch"
        self.post_data_buffer = body
        self.headers = {"content-type": content_type} if content_type else {}
        self.frame = type("F", (), {"url": url})()
        self._response = response

    def all_headers(self) -> dict[str, str]:
        return dict(self.headers)

    def response(self) -> _Response:
        return self._response


class _Context:
    def __init__(self) -> None:
        self.handlers: dict[str, Any] = {}

    def on(self, event, handler) -> None:
        self.handlers[event] = handler

    def remove_listener(self, event, handler) -> None:
        self.handlers.pop(event, None)


def _record(tmp_path: Path, **kwargs: Any) -> Path:
    ctx = _Context()
    cap = nc.NetworkCapture(ctx, keep_fields=("note", "patient_id"), **kwargs)
    cap.start()
    finished = ctx.handlers["requestfinished"]
    # A search: the name is in the query, the chart page holds every value.
    finished(
        _Request(
            f"http://app.test/patients?q={quote_plus(PATIENT)}",
            "GET",
            None,
            None,
            _Response(
                200,
                "text/html",
                (
                    f"<table><tr><td>{PATIENT}</td><td>{DOB}</td>"
                    f"<td>{MRN}</td><td>{DIAGNOSIS}</td></tr></table>"
                ).encode(),
            ),
        )
    )
    # A JSON save: one declared field typed, the response echoes the chart.
    finished(
        _Request(
            "http://app.test/api/notes",
            "POST",
            json.dumps({"patient_id": "4417", "note": "follow up"}).encode(),
            "application/json",
            _Response(
                201,
                "application/json",
                json.dumps(
                    {
                        "id": 91,
                        "patient_id": "4417",
                        "note": "follow up",
                        "patient": {"name": PATIENT, "dob": DOB, "mrn": MRN},
                        "problems": [{"text": DIAGNOSIS}],
                    }
                ).encode(),
            ),
        )
    )
    # A form post that carries an undeclared value.
    finished(
        _Request(
            "http://app.test/intake",
            "POST",
            f"note=ok&diagnosis={quote_plus(DIAGNOSIS)}".encode(),
            "application/x-www-form-urlencoded",
            _Response(302, "text/html", b""),
        )
    )
    cap.stop()
    cap.write(tmp_path, t0_monotonic=0.0)
    return tmp_path / nc.NETWORK_FILE


def test_default_network_capture_writes_no_phi(tmp_path: Path) -> None:
    path = _record(tmp_path)
    assert _leaks(tmp_path) == []
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0]["bodies"] == "minimized"
    assert nc.bodies_mode(tmp_path) == "minimized"
    for row in rows[1:]:
        assert "body" not in row and "response_text" not in row
        assert "form" not in row


def test_declared_fields_survive_and_the_rest_is_hashed(tmp_path: Path) -> None:
    _record(tmp_path)
    rows = nc.load(tmp_path)
    save = rows[1]
    assert ["patient_id", "4417"] in save["body_minimized"]["kept"]
    assert ["note", "follow up"] in save["response_minimized"]["kept"]
    withheld = dict(save["response_minimized"]["withheld"])
    assert withheld["patient.name"].startswith("hmac-sha256:")
    assert "id" in withheld  # not declared here, so not kept
    page = rows[0]["response_minimized"]
    assert page["format"] == "opaque" and page["bytes"] > 0
    form = rows[2]["form_minimized"]
    assert ["note", "ok"] in form["kept"]
    assert [name for name, _ in form["withheld"]] == ["diagnosis"]


def test_raw_bodies_need_encryption_or_an_explicit_plaintext_opt_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(at_rest.OPT_OUT_ENV, raising=False)
    with pytest.raises(at_rest.AtRestRefused):
        _record(tmp_path, bodies="raw")
    assert not (tmp_path / nc.NETWORK_FILE).exists()


def test_raw_bodies_opt_in_keeps_them_for_debugging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Positive control: the scanner does see PHI when it is written.
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "test: synthetic data")
    _record(tmp_path, bodies="raw")
    assert nc.bodies_mode(tmp_path) == "raw"
    assert {value for _, value in _leaks(tmp_path)} == set(PHI)


def test_raw_bodies_in_an_encrypted_recording_are_ciphertext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import sealed_set

    monkeypatch.delenv(at_rest.OPT_OUT_ENV, raising=False)
    root = tmp_path / "rec"
    with sealed_set.writing(root, "recording", "synthetic-passphrase-for-tests"):
        _record(root, bodies="raw")
    on_disk = b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())
    for value in PHI:
        for form in _forms(value):
            assert form not in on_disk
    reader = sealed_set.open_set(root, "synthetic-passphrase-for-tests")
    assert PATIENT in reader.read_bytes(nc.NETWORK_FILE).decode()


def test_record_cli_refuses_raw_bodies_before_recording_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import argparse

    parser = argparse.ArgumentParser()
    nc.add_cli_arguments(parser)
    monkeypatch.delenv(at_rest.OPT_OUT_ENV, raising=False)
    assert nc.cli_mode(parser.parse_args([]), None) is False
    on = parser.parse_args(["--capture-network"])
    assert nc.cli_mode(on, None) == "minimized"
    raw = parser.parse_args(["--capture-network", "--capture-network-bodies", "raw"])
    assert nc.cli_mode(raw, "recording-passphrase") == "raw"
    with pytest.raises(SystemExit, match="encrypted"):
        nc.cli_mode(raw, None)


def test_recorder_capture_keeps_its_declared_fields() -> None:
    cap = nc.NetworkCapture.for_recording(_Context(), {"pw"}, {"mrn_param"}, True)
    assert cap._bodies == "minimized" and "mrn_param" in cap._keep_fields
    assert nc.NetworkCapture.for_recording(_Context(), (), (), "raw")._bodies == "raw"


def test_bad_body_mode_is_refused() -> None:
    with pytest.raises(ValueError):
        nc.NetworkCapture(_Context(), bodies="everything")


# -- wire-tap HAR ---------------------------------------------------------------


def _tap() -> SessionWireTap:
    tap = SessionWireTap("http://app.test")
    tap.record_exchange(
        method="POST",
        url=f"http://app.test/api/records?lookup={quote_plus(MRN)}",
        request_body=json.dumps({"name": PATIENT, "dob": DOB}).encode(),
        response_status=201,
        response_body=json.dumps(
            {"id": "51", "version": "3", "chart": {"dx": DIAGNOSIS, "mrn": MRN}}
        ).encode(),
        request_headers={"Content-Type": "application/json"},
        response_headers={"Content-Type": "application/json"},
        duration_ms=1.0,
    )
    tap.record_exchange(
        method="GET",
        url="http://app.test/chart/51",
        request_body=b"",
        response_status=200,
        response_body=f"<h1>{PATIENT}</h1><p>{DIAGNOSIS}</p>".encode(),
        request_headers={},
        response_headers={"Content-Type": "text/html"},
        duration_ms=1.0,
    )
    return tap


def test_saved_har_holds_no_phi_even_after_decryption(tmp_path: Path) -> None:
    tap = _tap()
    path = tap.save_har(tmp_path / "session.har")
    assert crypto.is_encrypted(path.read_bytes())
    assert _leaks(tmp_path) == []
    text = dump_har(tap)
    for value in PHI:
        for form in _forms(value):
            assert form.decode("latin-1") not in text


def test_minimized_har_replays_declared_ids_and_fails_closed(tmp_path: Path) -> None:
    path = _tap().save_har(tmp_path / "session.har")
    har = load_har(path)
    assert cp.is_minimized_har(har)
    restored = SessionWireTap.from_har(path)
    summary = restored.summary()
    assert summary.mutations_allowed == 1
    assert summary.mutations[0].extracted_entity_id == "51"
    assert summary.mutations[0].version == "3"


def test_raw_har_opt_in_is_sealed_on_disk(tmp_path: Path) -> None:
    path = _tap().save_har(tmp_path / "raw.har", raw_bodies=True)
    on_disk = path.read_bytes()
    for value in PHI:
        for form in _forms(value):
            assert form not in on_disk
    # Positive control: decrypted, the raw bodies are there.
    assert {value for _, value in _leaks(tmp_path)} == set(PHI)


# -- minimizer -------------------------------------------------------------------


def test_hash_key_is_per_capture_and_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = cp.BodyMinimizer(), cp.BodyMinimizer()
    assert a.tag(DOB) == a.tag(DOB)
    assert a.tag(DOB) != b.tag(DOB)
    out = a.body(json.dumps({"dob": DOB}).encode(), "application/json")
    assert out is not None and DOB not in json.dumps(out)
    # A known key, injected where captures make theirs, is in no output.
    key = bytes(range(32))
    monkeypatch.setattr(cp.secrets, "token_bytes", lambda n: key[:n])
    (tmp_path / "rec").mkdir()
    _record(tmp_path / "rec")
    _tap().save_har(tmp_path / "rec" / "s.har")
    spellings = [key, key.hex().encode(), base64.b64encode(key)]
    spellings += [base64.urlsafe_b64encode(key).rstrip(b"=")]
    for path in (tmp_path / "rec").rglob("*"):
        if path.is_file():
            for blob in _decoded(_plain(path.read_bytes())):
                assert not any(k in blob for k in spellings), path


def test_keys_that_are_not_field_names_are_hashed() -> None:
    m = cp.BodyMinimizer(("status",))
    out = m.body(json.dumps({PATIENT: {"status": "seen"}}).encode(), "application/json")
    assert out is not None
    assert PATIENT not in json.dumps(out)
    assert out["kept"][0][1] == "seen"


def test_unparsed_and_truncated_bodies_keep_only_size_and_hash() -> None:
    m = cp.BodyMinimizer(("note",))
    out = m.body(
        json.dumps({"note": PATIENT}).encode(), "application/json", truncated=True
    )
    assert out is not None and out["format"] == "opaque" and "kept" not in out
    binary = m.body(b"\xff\xfe" + PATIENT.encode(), "application/octet-stream")
    assert binary is not None and binary["format"] == "opaque"
    assert m.body(b"", "text/html") is None


def test_url_query_values_are_hashed_unless_declared() -> None:
    m = cp.BodyMinimizer(("page",))
    url = m.url(f"http://app.test/s?q={quote_plus(PATIENT)}&page=2")
    assert quote_plus(PATIENT) not in url and PATIENT not in url
    assert "page=2" in url


# -- review regressions ------------------------------------------------------------


def _replayed(tap: SessionWireTap, tmp_path: Path, **kw: Any) -> Any:
    path = tmp_path / "r.har"
    dump_har(tap, path, **kw)
    return SessionWireTap.from_har(path).summary()


def test_replay_keeps_writes_whose_evidence_was_withheld(tmp_path: Path) -> None:
    tap = SessionWireTap("http://app.test")
    batch = [{"query": "query A { a }"}, {"query": "mutation B { b }"}]
    tap.record_exchange(
        method="POST",
        url="http://app.test/graphql",
        request_body=json.dumps(batch).encode(),
        request_headers={"Content-Type": "application/json"},
        response_body=b"{}",
    )
    tap.record_exchange(
        method="GET", url="http://app.test/api?query=" + quote("mutation { x }")
    )
    live = tap.summary()
    # Default fields hash the GraphQL documents; "query" keeps them.
    for kw in ({}, {"keep_fields": ("query",)}):
        replay = _replayed(tap, tmp_path, **kw)
        assert replay.mutations_allowed + replay.mutations_blocked == (
            live.mutations_allowed + live.mutations_blocked
        )
        assert replay.read_requests == live.read_requests


def test_reloading_a_minimized_har_keeps_its_descriptions(tmp_path: Path) -> None:
    first = json.loads(dump_har(_tap()))
    second = json.loads(dump_har(load_har(json.dumps(first))))
    for a, b in zip(first["log"]["entries"], second["log"]["entries"]):
        for side, key in (("request", "postData"), ("response", "content")):
            if key in a[side]:
                assert a[side][key][cp.HAR_FIELD] == b[side][key][cp.HAR_FIELD]


def test_raw_har_is_not_returned_in_memory(tmp_path: Path) -> None:
    assert dump_har(_tap(), tmp_path / "raw.har", raw_bodies=True) == ""


def test_floor_without_tap_metadata() -> None:
    m = cp.BodyMinimizer()
    entry = {"request": {"method": "GET", "url": m.url("http://a.test/x?query=m")}}
    assert cp.mutation_floor(entry)
    post = {"method": "POST", "url": "http://a.test/x", "postData": {}}
    post["postData"][cp.HAR_FIELD] = m.body(b'{"a": 1}', "application/json")
    assert cp.mutation_floor({"request": post})


def test_raw_entry_added_to_a_minimized_har_is_minimized(tmp_path: Path) -> None:
    har = load_har(dump_har(_tap()))
    raw = json.loads(json.dumps(_tap().export_har()))["log"]["entries"][0]
    har["log"]["entries"].append(raw)
    har["log"]["pages"] = [{"title": f"http://app.test/?q={quote_plus(PATIENT)}"}]
    (tmp_path / "s.har").write_text(dump_har(har))
    assert _leaks(tmp_path) == []
    narrowed = json.loads(dump_har(har, keep_fields=()))
    for entry in narrowed["log"]["entries"]:
        assert entry["_tx"].get("extracted_entity_id") != "51"
        assert entry["_tx"].get("version") != "3"


def test_truncated_forms_and_non_json_types_keep_nothing() -> None:
    m = cp.BodyMinimizer(("id", "note"))
    line = cp.minimize_line({"form": [["note", PATIENT]], "request_truncated": True}, m)
    assert PATIENT not in json.dumps(line)
    for ctype in ("text/plain", "text/html", "image/png"):
        out = m.body(json.dumps({"id": PATIENT}).encode(), ctype)
        assert out is not None and out["format"] == "opaque"
        assert PATIENT not in json.dumps(out)


def test_declared_secret_is_withheld_whatever_its_case(tmp_path: Path) -> None:
    ctx = _Context()
    cap = nc.NetworkCapture(ctx, secret_names={"code"}, keep_fields={"CODE"})
    cap.start()
    ctx.handlers["requestfinished"](
        _Request(
            "http://app.test/cb?code=xy",
            "GET",
            None,
            None,
            _Response(200, "text/html", b""),
        )
    )
    cap.write(tmp_path, t0_monotonic=0.0)
    assert "code=xy" not in (tmp_path / nc.NETWORK_FILE).read_text()


def test_fragment_queries_and_bare_terms_are_hashed() -> None:
    m = cp.BodyMinimizer(("page",))
    for url in (
        f"http://a.test/#/search?q={quote_plus(PATIENT)}&page=2",
        f"http://a.test/#q={quote_plus(PATIENT)}",
        f"http://a.test/s?{quote_plus(PATIENT)}",
    ):
        out = m.url(url)
        assert quote_plus(PATIENT) not in out and PATIENT not in out
    assert "page=2" in m.url("http://a.test/#/s?q=x&page=2")
    once = m.url(f"http://a.test/s?q={quote_plus(PATIENT)}")
    assert m.url(once) == once


def test_restored_json_keeps_lists_and_dotted_versions() -> None:
    m = cp.BodyMinimizer(("id", "meta.versionId"))
    doc = {"data": {"id": ["one", "two"]}, "record": {"meta": {"versionId": "7"}}}
    out = m.body(json.dumps(doc).encode(), "application/json")
    assert out is not None
    restored = json.loads(cp.restore_kept_json(out))
    assert restored == doc


def test_params_only_har_form_is_minimized() -> None:
    har = {
        "log": {
            "entries": [
                {
                    "request": {
                        "method": "POST",
                        "url": "http://a.test/f",
                        "postData": {
                            "mimeType": "application/x-www-form-urlencoded",
                            "params": [
                                {"name": "id", "value": "9"},
                                {"name": "dx", "value": DIAGNOSIS},
                            ],
                        },
                    }
                }
            ]
        }
    }
    out = cp.minimize_har(har, cp.BodyMinimizer(("id",)))
    post = out["log"]["entries"][0]["request"]["postData"]
    assert post["params"] == [] and DIAGNOSIS not in json.dumps(out)
    assert ["id", "9"] in post[cp.HAR_FIELD]["kept"]
    assert [n for n, _ in post[cp.HAR_FIELD]["withheld"]] == ["dx"]


def test_lone_surrogate_is_hashed_not_fatal() -> None:
    out = cp.BodyMinimizer().body(b'{"a": "\\ud800"}', "application/json")
    assert out is not None and out["withheld"][0][1].startswith("hmac-sha256:")


def test_raw_har_without_a_path_is_refused() -> None:
    with pytest.raises(ValueError):
        dump_har(_tap(), raw_bodies=True)


def test_raw_network_write_never_falls_back_to_plaintext(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openadapt_flow import sealed_set

    monkeypatch.delenv(at_rest.OPT_OUT_ENV, raising=False)
    with pytest.raises(PermissionError):
        sealed_set.write_bytes(tmp_path / "x", b"raw", sealed_only=True)
    assert not (tmp_path / "x").exists()
    with pytest.raises(at_rest.AtRestRefused):
        nc.require_raw_ready(tmp_path)
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "test: synthetic data")
    nc.require_raw_ready(tmp_path)


# -- second review: the four blockers left open after eb95a37dd ------------------


def _capture(**kw: Any) -> tuple[nc.NetworkCapture, Any, Any]:
    ctx = _Context()
    cap = nc.NetworkCapture(ctx, keep_fields=("note", "id"), **kw).start()
    return cap, ctx.handlers["requestfinished"], ctx.handlers["requestfailed"]


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    from openadapt_flow import sealed_set

    text = sealed_set.read_text(tmp_path / nc.NETWORK_FILE)
    return [json.loads(line) for line in text.splitlines()[1:]]


def _multipart(boundary: str, filename: str, payload: bytes) -> bytes:
    return (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="note"\r\n\r\nok\r\n'
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="scan"; filename="{filename}"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )


def _opaque_bodies() -> list[tuple[str, bytes, str, bytes, str]]:
    """(method, request body, request type, response body, response type)."""
    binary = b"\xff\xfe" + PATIENT.encode("utf-16-le") + b"\x00\x81" + MRN.encode()
    return [
        ("POST", b"\xff\xfe", "application/octet-stream", b"", "text/html"),
        ("POST", binary, "application/octet-stream", b"", "text/html"),
        ("GET", b"", "", b"%PDF-1.7\n\xe2\xe3" + DIAGNOSIS.encode(), "application/pdf"),
        (
            "POST",
            _multipart("bX", "chart.bin", b"\x89" + DOB.encode()),
            "multipart/form-data; boundary=bX",
            b"",
            "text/html",
        ),
    ]


def test_binary_and_file_bodies_are_measured_and_hashed_as_sent(
    tmp_path: Path,
) -> None:
    """#10: size and hash come from the original bytes; binary responses and
    multipart files get a descriptor; nothing of their content is kept."""
    cap, finished, _ = _capture()
    for method, body, rtype, resp, resp_type in _opaque_bodies():
        finished(
            _Request(
                "http://app.test/upload",
                method,
                body or None,
                rtype,
                _Response(200, resp_type, resp),
            )
        )
    cap.write(tmp_path, t0_monotonic=0.0)
    assert _leaks(tmp_path) == []
    rows = _rows(tmp_path)
    for row, (_, body, rtype, resp, _) in zip(rows, _opaque_bodies()):
        assert not any(k.startswith("_") for k in row)
        if body:
            desc = row["body_minimized"]
            assert desc["format"] == "opaque"
            assert desc["bytes"] == len(body)
            assert desc["hmac"] == cap._minimizer.tag(body)
            assert "body" not in row and "form" not in row
        if resp:
            desc = row["response_minimized"]
            assert desc["format"] == "opaque"
            assert desc["bytes"] == len(resp)
            assert desc["hmac"] == cap._minimizer.tag(resp)
    assert rows[0]["body_minimized"]["bytes"] == 2  # not 6 replacement bytes
    assert rows[3]["body_omitted"] == "multipart"


def test_text_bodies_report_the_size_and_hash_sent_not_the_redacted_text(
    tmp_path: Path,
) -> None:
    cap, finished, _ = _capture(secret_names={"pin"})
    body = json.dumps({"note": "ok", "pin": "pin-Quokka", "dx": DIAGNOSIS}).encode()
    big = ("x" * nc.MAX_REQUEST_BYTES + DIAGNOSIS).encode()
    for payload in (body, big):
        finished(
            _Request(
                "http://app.test/save",
                "POST",
                payload,
                "application/json",
                _Response(204, "text/html", b""),
            )
        )
    cap.write(tmp_path, t0_monotonic=0.0)
    assert _leaks(tmp_path) == []
    assert "Quokka" not in (tmp_path / nc.NETWORK_FILE).read_text()
    saved, cut = (row["body_minimized"] for row in _rows(tmp_path))
    assert saved["bytes"] == len(body) and saved["hmac"] == cap._minimizer.tag(body)
    assert ["note", "ok"] in saved["kept"]
    assert cut["truncated"] and cut["bytes"] == len(big)
    assert cut["hmac"] == cap._minimizer.tag(big)


def test_url_encoded_responses_are_captured_and_minimized(tmp_path: Path) -> None:
    cap, finished, _ = _capture()
    finished(
        _Request(
            "http://app.test/lookup",
            "GET",
            None,
            None,
            _Response(
                200,
                "application/x-www-form-urlencoded",
                f"id=51&dx={quote_plus(DIAGNOSIS)}".encode(),
            ),
        )
    )
    cap.write(tmp_path, t0_monotonic=0.0)
    assert _leaks(tmp_path) == []
    desc = _rows(tmp_path)[0]["response_minimized"]
    assert desc["format"] == "form" and ["id", "51"] in desc["kept"]
    assert [n for n, _ in desc["withheld"]] == ["dx"]


def test_raw_mode_keeps_binary_bodies_out_and_writes_valid_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Raw mode has no in-memory keys to trip ``json.dumps`` (an error path
    that would lose the capture) and writes binary bodies as descriptors."""
    monkeypatch.setenv(at_rest.OPT_OUT_ENV, "test: synthetic data")
    cap, finished, _ = _capture(bodies="raw")
    for method, body, rtype, resp, resp_type in _opaque_bodies():
        finished(
            _Request(
                "http://app.test/u",
                method,
                body or None,
                rtype,
                _Response(200, resp_type, resp),
            )
        )
    cap.write(tmp_path, t0_monotonic=0.0)
    rows = _rows(tmp_path)
    assert not any(k.startswith("_") for row in rows for k in row)
    assert rows[0]["body_minimized"]["bytes"] == 2 and "body" not in rows[0]
    assert rows[2]["response_minimized"]["format"] == "opaque"
    assert _leaks(tmp_path) == []  # binary content is never kept, even raw


def _recorder_backend() -> Any:
    class Backend:
        viewport = (320, 200)

        def screenshot(self) -> bytes:
            return b""

    return Backend()


def test_recording_meta_app_url_query_is_minimized(tmp_path: Path) -> None:
    """#6: the starting URL in meta.json gets the capture's query policy, so
    no plaintext copy of an undeclared query value sits beside network.jsonl."""
    from openadapt_flow.recorder import Recorder

    cap, finished, _ = _capture()
    url = f"http://app.test/search?q={quote_plus(PATIENT)}&id=51#/r?dob={DOB}"
    rec = Recorder(
        _recorder_backend(), tmp_path / "rec", app_url=url, network_capture=cap
    )
    finished(_Request(url, "GET", None, None, _Response(200, "text/html", b"")))
    out = rec.finish()
    assert _leaks(out) == []
    from openadapt_flow import sealed_set

    meta = json.loads(sealed_set.read_text(out / "meta.json"))
    assert "id=51" in meta["app_url"]
    assert meta["app_url"] == _rows(out)[0]["url"]  # same key as network.jsonl
    assert urlsplit_host(meta["app_url"]) == "app.test"
    plain = Recorder(_recorder_backend(), tmp_path / "plain", app_url=url).finish()
    assert json.loads(sealed_set.read_text(plain / "meta.json"))["app_url"] == url


def urlsplit_host(url: str) -> str:
    from urllib.parse import urlsplit

    return urlsplit(url).hostname or ""


def test_explicit_octet_stream_json_is_opaque_and_unlabelled_json_parses(
    tmp_path: Path,
) -> None:
    """#4: JSON is parsed only when labelled JSON or with no type at all; the
    wire tap records a missing header as "" so the two stay apart."""
    m = cp.BodyMinimizer(("id",))
    body = json.dumps({"id": PATIENT}).encode()
    out = m.body(body, "application/octet-stream")
    assert out is not None and out["format"] == "opaque"
    assert PATIENT not in json.dumps(out)
    unlabelled = m.body(json.dumps({"id": "51"}).encode(), "")
    assert unlabelled is not None and ["id", "51"] in unlabelled["kept"]

    from openadapt_flow.tx.wire_tap import WirePolicy

    tap = SessionWireTap("http://app.test", policy=WirePolicy(max_mutations=None))
    tap.record_exchange(
        method="POST",
        url="http://app.test/api/a",
        request_body=json.dumps({"id": MRN}).encode(),
        response_status=201,
        response_body=json.dumps({"id": PATIENT}).encode(),
        request_headers={"Content-Type": "application/octet-stream"},
        response_headers={"Content-Type": "application/octet-stream"},
    )
    tap.record_exchange(
        method="POST",
        url="http://app.test/api/b",
        request_body=b'{"note": "x"}',
        response_status=201,
        response_body=b'{"id": "52"}',
    )
    path = tap.save_har(tmp_path / "s.har")
    assert _leaks(tmp_path) == []
    entries = load_har(path)["log"]["entries"]
    assert entries[0]["response"]["content"][cp.HAR_FIELD]["format"] == "opaque"
    assert entries[1]["response"]["content"]["mimeType"] == ""
    replay = SessionWireTap.from_har(path, policy=WirePolicy(max_mutations=None))
    ids = [m.extracted_entity_id for m in replay.summary().mutations]
    assert ids == [None, "52"]  # live read "PATIENT" from the octet-stream body


def test_marked_har_with_a_raw_body_is_refused_on_load(tmp_path: Path) -> None:
    """#2: a document marked minimized must be minimized in every entry."""
    good = load_har(dump_har(_tap()))
    assert load_har(load_har(good)) == load_har(good)  # a reload still loads
    raw_entry = json.loads(json.dumps(_tap().export_har()))["log"]["entries"][0]

    def doctored(change: Any) -> dict[str, Any]:
        doc = json.loads(dump_har(_tap()))
        change(doc["log"]["entries"])
        return doc

    cases = [
        lambda e: e.append(raw_entry),  # an entry with no descriptor
        lambda e: e[0]["request"]["postData"].update(text=json.dumps({"n": PATIENT})),
        lambda e: e[1]["response"]["content"].update(
            text=base64.b64encode(PATIENT.encode()).decode(), encoding="base64"
        ),
        lambda e: e[0]["request"]["postData"].update(
            params=[{"name": "dx", "value": DIAGNOSIS}]
        ),
    ]
    for change in cases:
        doc = doctored(change)
        for source in (doc, json.dumps(doc), json.dumps(doc).encode()):
            with pytest.raises(ValueError) as err:
                load_har(source)
            assert not any(v in str(err.value) for v in PHI)
            with pytest.raises(ValueError):
                SessionWireTap.from_har(source)


def test_url_valued_har_headers_are_minimized(tmp_path: Path) -> None:
    tap = SessionWireTap("http://app.test")
    search = f"http://app.test/s?q={quote_plus(PATIENT)}"
    tap.record_exchange(
        method="GET",
        url="http://app.test/chart",
        request_headers={"Referer": search},
        response_status=200,
        response_headers={"Content-Location": search, "Refresh": f"0; url={search}"},
        response_body=b"",
    )
    tap.save_har(tmp_path / "s.har")
    assert _leaks(tmp_path) == []


def test_no_phi_reaches_disk_on_error_and_debug_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every capture path, with DEBUG logging to a file in the scanned tree:
    failed requests, refused raw writes, refused loads and raw exports."""
    import logging

    monkeypatch.delenv(at_rest.OPT_OUT_ENV, raising=False)
    handler = logging.FileHandler(tmp_path / "debug.log")
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        cap, finished, failed = _capture()
        failed(
            _Request(
                f"http://app.test/s?q={quote_plus(PATIENT)}",
                "POST",
                json.dumps({"dx": DIAGNOSIS, "mrn": MRN}).encode(),
                "application/json",
                None,
            )
        )
        for method, body, rtype, resp, resp_type in _opaque_bodies():
            finished(
                _Request(
                    "http://app.test/u",
                    method,
                    body or None,
                    rtype,
                    _Response(200, resp_type, resp),
                )
            )
        (tmp_path / "rec").mkdir()
        cap.write(tmp_path / "rec", t0_monotonic=0.0)
        raw_cap, raw_finished, _ = _capture(bodies="raw")
        raw_finished(
            _Request(
                "http://app.test/x",
                "POST",
                DOB.encode(),
                "text/plain",
                _Response(200, "text/plain", PATIENT.encode()),
            )
        )
        (tmp_path / "raw").mkdir()
        with pytest.raises(at_rest.AtRestRefused) as refused:
            raw_cap.write(tmp_path / "raw", t0_monotonic=0.0)
        assert not any(v in str(refused.value) for v in PHI)
        with pytest.raises(ValueError):
            dump_har(_tap(), raw_bodies=True)
        _tap().save_har(tmp_path / "s.har")
        doc = json.loads(dump_har(_tap()))
        doc["log"]["entries"][0]["request"]["postData"]["text"] = PATIENT
        with pytest.raises(ValueError):
            load_har(doc)
        dump_har(_tap(), tmp_path / "again.har")
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)
        handler.close()
    assert not (tmp_path / "raw" / nc.NETWORK_FILE).exists()
    assert _leaks(tmp_path) == []
