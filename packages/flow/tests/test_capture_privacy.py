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
        json.dumps(value)[1:-1].encode(),
        json.dumps(value, ensure_ascii=True)[1:-1].encode(),
        base64.b64encode(raw),
        base64.urlsafe_b64encode(raw).rstrip(b"="),
    ]


def _plain(data: bytes) -> bytes:
    """``data`` decrypted (Flow's at-rest format) and gunzipped when it is."""
    if crypto.is_encrypted(data):
        for aad in (b"openadapt-flow/tx-private-state",):
            try:
                data = crypto.decrypt_bytes(
                    data, crypto.resolve_read_key(None), aad=aad
                )
                break
            except Exception:  # noqa: BLE001 - try the next context
                continue
    if data[:2] == b"\x1f\x8b":
        data = gzip.decompress(data)
    return data


def _leaks(root: Path) -> list[tuple[str, str]]:
    found = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        data = _plain(path.read_bytes())
        for value in PHI:
            for form in _forms(value):
                if form in data:
                    found.append((str(path.relative_to(root)), value))
    return found


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


def test_hash_key_is_per_capture_and_never_written() -> None:
    a, b = cp.BodyMinimizer(), cp.BodyMinimizer()
    assert a.tag(DOB) == a.tag(DOB)
    assert a.tag(DOB) != b.tag(DOB)
    out = a.body(json.dumps({"dob": DOB}).encode(), "application/json")
    assert out is not None and DOB not in json.dumps(out)


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
