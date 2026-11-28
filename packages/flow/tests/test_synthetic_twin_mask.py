"""Unit tests for Synthetic-Twin visual masking and ZdrVlmGrounder."""

from __future__ import annotations

import io
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.runtime.grounder import (
    Grounder,
    GrounderMatch,
    component_may_egress,
)
from openadapt_flow.runtime.synthetic_twin_mask import (
    OcrTextBox,
    SyntheticTwinProjector,
    mask_screenshot_png,
    unmask_text,
)
from openadapt_flow.runtime.zdr_vlm_grounder import (
    HealedAnchorCache,
    ZdrVlmGrounder,
)


def _make_png(
    width: int, height: int, bg: tuple[int, int, int] = (255, 255, 255)
) -> bytes:
    img = Image.new("RGB", (width, height), bg)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_bijective_format_preserving_round_trip() -> None:
    dossier = {
        "patient_name": "Ada Lovelace",
        "dob": "1988-04-14",
        "us_date": "04/14/1988",
        "ssn": "123-45-6789",
        "phone": "(415) 555-0194",
        "email": "ada.lovelace@clinic.example",
        "mrn": "MRN-482910",
        "copay": "$1,245.50",
    }
    projector = SyntheticTwinProjector(dossier, seed=7)

    # Check format preservation for each field
    twin_dob = projector.project("1988-04-14")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", twin_dob)
    assert twin_dob != "1988-04-14"

    twin_us = projector.project("04/14/1988")
    assert re.fullmatch(r"\d{2}/\d{2}/\d{4}", twin_us)
    assert twin_us != "04/14/1988"

    twin_ssn = projector.project("123-45-6789")
    assert re.fullmatch(r"\d{3}-\d{2}-\d{4}", twin_ssn)
    assert twin_ssn != "123-45-6789"

    twin_phone = projector.project("(415) 555-0194")
    assert re.fullmatch(r"\(\d{3}\) \d{3}-\d{4}", twin_phone)
    assert twin_phone != "(415) 555-0194"

    twin_email = projector.project("ada.lovelace@clinic.example")
    assert "@" in twin_email and twin_email.endswith("@example.test")
    assert twin_email != "ada.lovelace@clinic.example"

    twin_mrn = projector.project("MRN-482910")
    assert re.fullmatch(r"MRN-\d{6}", twin_mrn)
    assert twin_mrn != "MRN-482910"

    twin_copay = projector.project("$1,245.50")
    assert re.fullmatch(r"\$\d,\d{3}\.\d{2}", twin_copay)
    assert twin_copay != "$1,245.50"

    twin_name = projector.project("Ada Lovelace")
    assert len(twin_name.split()) == 2
    assert twin_name != "Ada Lovelace"

    # Full sentence round-trip including inline detected PHI not in dossier
    raw_sentence = (
        "Patient Ada Lovelace (MRN-482910, DOB 1988-04-14, 04/14/1988, "
        "SSN 123-45-6789, tel (415) 555-0194, ada.lovelace@clinic.example) "
        "owes $1,245.50; secondary claim CLM-904412 on 2026-03-19 for $85.00."
    )
    masked, count = projector.mask_text(raw_sentence)
    assert count == 11
    for raw_val in dossier.values():
        assert raw_val not in masked
    assert "CLM-904412" not in masked
    assert "2026-03-19" not in masked
    assert "$85.00" not in masked

    restored = unmask_text(masked, projector)
    assert restored == raw_sentence


def test_projector_bijection_uniqueness() -> None:
    projector = SyntheticTwinProjector(seed=11)
    raws = [f"1988-04-{day:02d}" for day in range(1, 20)]
    twins = [projector.project(r) for r in raws]
    assert len(set(twins)) == len(raws)
    for r, t in zip(raws, twins):
        assert projector.unproject(t) == r


def test_mask_screenshot_png_modifies_phi_and_preserves_static_labels() -> None:
    img = Image.new("RGB", (480, 200), (245, 247, 250))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    draw.text((20, 20), "Patient Name:", fill=(20, 20, 20), font=font)
    draw.text((150, 20), "Ada Lovelace", fill=(20, 20, 20), font=font)
    draw.text((20, 70), "DOB:", fill=(20, 20, 20), font=font)
    draw.text((150, 70), "1988-04-14", fill=(20, 20, 20), font=font)
    draw.rectangle((20, 130, 120, 165), fill=(30, 120, 70))
    draw.text((42, 142), "Submit", fill=(255, 255, 255), font=font)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    orig_png = buf.getvalue()

    static_label_box = (18, 18, 100, 20)
    phi_name_box = (148, 18, 120, 20)
    phi_dob_box = (148, 68, 110, 20)
    submit_btn_box = (20, 130, 100, 35)

    ocr_boxes = [
        OcrTextBox("Patient Name:", static_label_box),
        OcrTextBox("Ada Lovelace", phi_name_box),
        OcrTextBox("DOB:", (18, 68, 50, 20)),
        OcrTextBox("1988-04-14", phi_dob_box),
        OcrTextBox("Submit", submit_btn_box),
    ]
    projector = SyntheticTwinProjector(
        {"patient_name": "Ada Lovelace", "dob": "1988-04-14"}, seed=3
    )

    masked_png, report = mask_screenshot_png(orig_png, ocr_boxes, projector)
    assert report.total_boxes == 5
    assert report.masked_box_count == 2
    assert report.unmasked_box_count == 3
    assert report.masked_token_count == 2
    assert report.raw_dossier_leaks == 0
    assert report.leaked_keys == ()

    orig_img = Image.open(io.BytesIO(orig_png)).convert("RGB")
    masked_img = Image.open(io.BytesIO(masked_png)).convert("RGB")

    def _crop_bytes(im: Image.Image, box: tuple[int, int, int, int]) -> bytes:
        x, y, w, h = box
        return im.crop((x, y, x + w, y + h)).tobytes()

    # Static label and Submit button pixels must be completely untouched.
    assert _crop_bytes(orig_img, static_label_box) == _crop_bytes(
        masked_img, static_label_box
    )
    assert _crop_bytes(orig_img, submit_btn_box) == _crop_bytes(
        masked_img, submit_btn_box
    )

    # PHI boxes must have modified pixels.
    assert _crop_bytes(orig_img, phi_name_box) != _crop_bytes(masked_img, phi_name_box)
    assert _crop_bytes(orig_img, phi_dob_box) != _crop_bytes(masked_img, phi_dob_box)


class _MockVlmServer:
    """Local HTTP server for exercising ZdrVlmGrounder end-to-end."""

    def __init__(self) -> None:
        self.mode: str = "ok"
        self.reply_payload: dict[str, Any] = {"x": 200, "y": 150, "confidence": 0.91}
        self.zdr_header: bool = True
        self.delay_s: float = 0.0
        self.requests: list[dict[str, Any]] = []
        self.request_headers: list[dict[str, str]] = []
        self._server: ThreadingHTTPServer
        self._thread: threading.Thread

    def __enter__(self) -> "_MockVlmServer":
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return None

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                try:
                    outer.requests.append(json.loads(raw.decode("utf-8")))
                except ValueError:
                    outer.requests.append({})
                outer.request_headers.append(
                    {k.lower(): v for k, v in self.headers.items()}
                )

                if outer.delay_s > 0:
                    time.sleep(outer.delay_s)

                if outer.mode == "500":
                    self.send_response(500)
                    self.end_headers()
                    self.wfile.write(b"internal error")
                    return

                if outer.mode == "malformed_http_json":
                    self.send_response(200)
                    if outer.zdr_header:
                        self.send_header("X-Zero-Data-Retention", "true")
                    self.end_headers()
                    self.wfile.write(b"not-json-at-all")
                    return

                if outer.mode == "malformed_content":
                    content = "I cannot find JSON here"
                else:
                    content = json.dumps(outer.reply_payload)

                body = json.dumps(
                    {
                        "choices": [
                            {"message": {"role": "assistant", "content": content}}
                        ]
                    }
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if outer.zdr_header:
                    self.send_header("X-Zero-Data-Retention", "true")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    @property
    def url(self) -> str:
        host, port = self._server.server_address
        return f"http://{host}:{port}/v1"


def test_zdr_grounder_valid_and_anchor_cache_hit(tmp_path: Path) -> None:
    png = _make_png(800, 600)
    cache_file = tmp_path / "anchors.json"
    cache = HealedAnchorCache(cache_path=cache_file)

    with _MockVlmServer() as srv:
        srv.reply_payload = {"x": 320, "y": 240, "confidence": 0.93}
        grounder = ZdrVlmGrounder(
            endpoint_url=srv.url,
            model="test-vision-model",
            api_key="test-key",
            healed_anchor_cache=cache,
        )
        assert isinstance(grounder, Grounder)
        assert component_may_egress(grounder) is True

        # 1st call hits mock HTTP server
        match1 = grounder.locate(png, "Click Submit", ocr_text="Submit")
        assert isinstance(match1, GrounderMatch)
        assert match1.point == (320, 240)
        assert match1.confidence == 0.93
        assert len(srv.requests) == 1
        assert srv.request_headers[0]["x-zero-data-retention"] == "true"
        assert srv.request_headers[0]["authorization"] == "Bearer test-key"

        # 2nd call hits HealedAnchorCache with 0 additional HTTP calls
        match2 = grounder.locate(png, "Click Submit", ocr_text="Submit")
        assert match2 == match1
        assert len(srv.requests) == 1
        assert grounder.stats.http_calls == 1
        assert grounder.stats.cache_hits == 1
        assert cache.hit_rate == 0.5

        # Reloading cache from disk also hits with 0 HTTP calls
        reloaded = HealedAnchorCache(cache_path=cache_file)
        grounder2 = ZdrVlmGrounder(
            endpoint_url=srv.url,
            model="test-vision-model",
            healed_anchor_cache=reloaded,
        )
        match3 = grounder2.locate(png, "Click Submit", ocr_text="Submit")
        assert match3 == match1
        assert len(srv.requests) == 1


def test_zdr_grounder_downscales_large_png_and_maps_coordinates() -> None:
    # 3200x1600 -> downscaled by 0.5 to 1600x800
    large_png = _make_png(3200, 1600)
    with _MockVlmServer() as srv:
        srv.reply_payload = {"x": 800, "y": 400, "confidence": 0.88}
        grounder = ZdrVlmGrounder(
            endpoint_url=srv.url, model="test-vision", max_image_dim=1600
        )
        match = grounder.locate(large_png, "Click Submit")
        assert match is not None
        assert match.point == (1600, 800)


def test_zdr_grounder_with_synthetic_twin_masking_scrubs_request() -> None:
    png = _make_png(640, 400)
    dossier = {"patient_name": "Ada Lovelace", "dob": "1988-04-14", "mrn": "MRN-482910"}
    projector = SyntheticTwinProjector(dossier, seed=5)
    boxes = [
        OcrTextBox("Ada Lovelace", (20, 20, 140, 24)),
        OcrTextBox("1988-04-14", (180, 20, 110, 24)),
        OcrTextBox("MRN-482910", (310, 20, 110, 24)),
    ]
    twin_name = projector.project("Ada Lovelace")

    with _MockVlmServer() as srv:
        srv.reply_payload = {
            "x": 220,
            "y": 140,
            "confidence": 0.90,
            "note": f"Found row for {twin_name}",
        }
        grounder = ZdrVlmGrounder(
            endpoint_url=srv.url,
            model="test-vision",
            projector=projector,
            ocr_provider=lambda _png: boxes,
        )
        match = grounder.locate(
            png,
            "Click Open in the row for Ada Lovelace (MRN-482910)",
            ocr_text="Open",
        )
        assert match is not None
        assert match.point == (220, 140)
        assert grounder.last_mask_report is not None
        assert grounder.last_mask_report.raw_dossier_leaks == 0

        # Ensure raw PHI never appeared in the prompt sent to the server.
        sent_text = srv.requests[0]["messages"][0]["content"][1]["text"]
        assert "Ada Lovelace" not in sent_text
        assert "MRN-482910" not in sent_text
        assert twin_name in sent_text

        # Unmasked reply restores original patient name locally.
        assert grounder.last_unmasked_reply is not None
        assert "Ada Lovelace" in grounder.last_unmasked_reply


def test_zdr_grounder_fail_safe_abstentions() -> None:
    png = _make_png(640, 400)
    with _MockVlmServer() as srv:
        grounder = ZdrVlmGrounder(
            endpoint_url=srv.url,
            model="test-vision",
            require_zdr_header=True,
            timeout=0.3,
            min_confidence=0.6,
        )

        # 1. Missing ZDR confirmation header -> abstain
        srv.mode = "ok"
        srv.zdr_header = False
        srv.reply_payload = {"x": 100, "y": 100, "confidence": 0.95}
        assert grounder.locate(png, "Click Submit") is None
        srv.zdr_header = True

        # 2. HTTP 500 -> abstain
        srv.mode = "500"
        assert grounder.locate(png, "Click Submit") is None

        # 3. Malformed HTTP JSON -> abstain
        srv.mode = "malformed_http_json"
        assert grounder.locate(png, "Click Submit") is None

        # 4. Malformed assistant content -> abstain
        srv.mode = "malformed_content"
        assert grounder.locate(png, "Click Submit") is None

        # 5. Low confidence -> abstain
        srv.mode = "ok"
        srv.reply_payload = {"x": 100, "y": 100, "confidence": 0.35}
        assert grounder.locate(png, "Click Submit") is None

        # 6. Null coordinates -> abstain
        srv.reply_payload = {"x": None, "y": None, "confidence": 0.0}
        assert grounder.locate(png, "Click Submit") is None

        # 7. Out-of-bounds coordinates -> abstain
        srv.reply_payload = {"x": 9999, "y": 100, "confidence": 0.95}
        assert grounder.locate(png, "Click Submit") is None

        # 8. Timeout -> abstain
        srv.delay_s = 0.6
        srv.reply_payload = {"x": 100, "y": 100, "confidence": 0.95}
        assert grounder.locate(png, "Click Submit") is None
        srv.delay_s = 0.0
