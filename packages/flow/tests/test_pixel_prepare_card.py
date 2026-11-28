"""Unit and integration tests for pixel-halt to zero-PHI PrepareCard extraction."""

from __future__ import annotations

import io
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from PIL import Image, ImageDraw, ImageFont

from openadapt_flow.cloud_prepare_bridge import (
    CloudPrepareClient,
    ZeroPhiViolationError,
    compute_resolution_sha256,
    prepare_resolution_to_human_input,
)
from openadapt_flow.copilot_protocol import HumanDecisionAction
from openadapt_flow.pixel_prepare_card import (
    OcrWord,
    diff_popup_region,
    extract_pixel_prepare_card,
    pixel_prepare_card_to_form_halt,
)


def _encode_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _render_base_citrix_screen(width: int = 800, height: int = 600) -> Image.Image:
    """Render a synthetic Citrix-style desktop claim form background."""
    img = Image.new("RGB", (width, height), color=(236, 239, 244))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()

    # Top title bar
    draw.rectangle((0, 0, width, 36), fill=(36, 62, 102))
    draw.text(
        (16, 12),
        "Citrix Claim Entry - Dental Benefits",
        fill=(255, 255, 255),
        font=font,
    )

    # Form container
    draw.rectangle(
        (40, 60, 760, 540), fill=(255, 255, 255), outline=(180, 190, 205), width=2
    )
    draw.text((64, 92), "Member ID:", fill=(30, 41, 59), font=font)
    draw.rectangle(
        (180, 84, 420, 112), fill=(248, 250, 252), outline=(148, 163, 184), width=1
    )
    draw.text((188, 92), "MBR-9981", fill=(15, 23, 42), font=font)

    draw.text((64, 146), "Assignment of Benefits:", fill=(30, 41, 59), font=font)
    draw.rectangle(
        (220, 138, 480, 168), fill=(248, 250, 252), outline=(100, 116, 139), width=1
    )
    draw.text((230, 148), "Select...", fill=(71, 85, 105), font=font)

    return img


def _make_citrix_modal_fixture(
    *,
    add_cursor_blink: bool = False,
) -> tuple[bytes, bytes, tuple[int, int, int, int]]:
    """Return ``(before_png, after_png, expected_modal_bbox)`` for a Citrix modal dialog."""
    base = _render_base_citrix_screen()
    before_png = _encode_png(base)

    after = base.copy()
    draw = ImageDraw.Draw(after)
    font = ImageFont.load_default()

    # Modal dialog rect: x=200, y=180, w=400, h=200
    mx, my, mw, mh = 200, 180, 400, 200
    draw.rectangle(
        (mx, my, mx + mw - 1, my + mh - 1),
        fill=(245, 247, 250),
        outline=(51, 65, 85),
        width=2,
    )
    # Modal title bar
    draw.rectangle((mx + 2, my + 2, mx + mw - 3, my + 34), fill=(30, 64, 175))
    draw.text((mx + 14, my + 12), "Coverage Notice", fill=(255, 255, 255), font=font)

    # Modal prompt text
    draw.text(
        (mx + 24, my + 66),
        "Secondary coverage detected",
        fill=(15, 23, 42),
        font=font,
    )

    # Two side-by-side buttons at the bottom of the modal
    draw.rectangle(
        (mx + 48, my + 136, mx + 204, my + 172),
        fill=(37, 99, 235),
        outline=(29, 78, 216),
    )
    draw.text((mx + 70, my + 148), "Continue Anyway", fill=(255, 255, 255), font=font)

    draw.rectangle(
        (mx + 244, my + 136, mx + 352, my + 172),
        fill=(226, 232, 240),
        outline=(100, 116, 139),
    )
    draw.text((mx + 278, my + 148), "Cancel", fill=(15, 23, 42), font=font)

    if add_cursor_blink:
        # Tiny distant blinking text caret near top-left form field
        draw.rectangle((196, 88, 198, 104), fill=(15, 23, 42))

    after_png = _encode_png(after)
    return before_png, after_png, (mx, my, mw, mh)


def _make_dropdown_fixture() -> tuple[bytes, bytes, tuple[int, int, int, int]]:
    """Return ``(before_png, after_png, expected_dropdown_bbox)`` for a clicked-open dropdown."""
    base = _render_base_citrix_screen()
    before_png = _encode_png(base)

    after = base.copy()
    draw = ImageDraw.Draw(after)
    font = ImageFont.load_default()

    # Dropdown menu rect directly below the Assignment of Benefits field
    dx, dy, dw, dh = 220, 170, 260, 88
    draw.rectangle(
        (dx, dy, dx + dw - 1, dy + dh - 1),
        fill=(255, 255, 255),
        outline=(51, 65, 85),
        width=2,
    )
    # Option 1 (highlighted)
    draw.rectangle((dx + 2, dy + 2, dx + dw - 3, dy + 42), fill=(219, 234, 254))
    draw.text(
        (dx + 14, dy + 16), "Assignment to Provider", fill=(15, 23, 42), font=font
    )

    # Option 2
    draw.rectangle((dx + 2, dy + 44, dx + dw - 3, dy + dh - 3), fill=(248, 250, 252))
    draw.text((dx + 14, dy + 58), "Pay Subscriber", fill=(15, 23, 42), font=font)

    after_png = _encode_png(after)
    return before_png, after_png, (dx, dy, dw, dh)


def test_diff_popup_region_isolates_citrix_modal_and_ignores_distant_caret():
    before_png, after_png, (mx, my, mw, mh) = _make_citrix_modal_fixture(
        add_cursor_blink=True
    )
    bbox = diff_popup_region(before_png, after_png)
    assert bbox is not None
    x, y, w, h = bbox

    # Default pad_px is 2; isolated box should tightly wrap the modal dialog.
    assert abs(x - (mx - 2)) <= 2
    assert abs(y - (my - 2)) <= 2
    assert abs(w - (mw + 4)) <= 4
    assert abs(h - (mh + 4)) <= 4


def test_diff_popup_region_isolates_clicked_open_dropdown():
    before_png, after_png, (dx, dy, dw, dh) = _make_dropdown_fixture()
    bbox = diff_popup_region(before_png, after_png)
    assert bbox is not None
    x, y, w, h = bbox

    assert abs(x - (dx - 2)) <= 2
    assert abs(y - (dy - 2)) <= 2
    assert abs(w - (dw + 4)) <= 4
    assert abs(h - (dh + 4)) <= 4


def test_diff_popup_region_returns_none_for_identical_small_or_mismatched_frames():
    before_png, _, _ = _make_citrix_modal_fixture()
    assert diff_popup_region(before_png, before_png) is None

    # Tiny 8x8 change (area 64 < min_area_px 400)
    base = _render_base_citrix_screen()
    tiny = base.copy()
    ImageDraw.Draw(tiny).rectangle((100, 100, 107, 107), fill=(0, 0, 0))
    assert (
        diff_popup_region(_encode_png(base), _encode_png(tiny), min_area_px=400) is None
    )

    # Mismatched dimensions
    smaller = Image.new("RGB", (320, 240), color=(255, 255, 255))
    assert diff_popup_region(before_png, _encode_png(smaller)) is None


def test_extract_citrix_modal_prepare_card_crops_and_extracts_options():
    before_png, after_png, (_, _, mw, mh) = _make_citrix_modal_fixture()
    captured_sizes: list[tuple[int, int]] = []

    def fake_ocr(cropped_png: bytes) -> list[OcrWord]:
        with Image.open(io.BytesIO(cropped_png)) as img:
            captured_sizes.append(img.size)
        return [
            OcrWord(
                text="Secondary coverage detected", x=24, y=66, width=210, height=18
            ),
            OcrWord(text="Continue", x=70, y=148, width=56, height=18),
            OcrWord(text="Anyway", x=132, y=148, width=50, height=18),
            OcrWord(text="Cancel", x=278, y=148, width=48, height=18),
        ]

    card = extract_pixel_prepare_card(
        before_png=before_png,
        after_png=after_png,
        portal_origin="https://citrix.clinic.org",
        field_label="Coverage Confirmation",
        card_id="prep_citrix_modal_001",
        ocr_fn=fake_ocr,
        staged_bindings={0: "$member_id_masked"},
        workflow_summary="Dental claim entry over Citrix",
    )

    # OCR received the cropped popup, not the full 800x600 screen.
    assert len(captured_sizes) == 1
    crop_w, crop_h = captured_sizes[0]
    assert abs(crop_w - (mw + 4)) <= 4
    assert abs(crop_h - (mh + 4)) <= 4

    assert card["card_id"] == "prep_citrix_modal_001"
    assert card["portal_origin"] == "https://citrix.clinic.org"
    assert card["staged_bindings"] == {0: "$member_id_masked"}
    assert card["control_labels"] == {1: "Coverage Confirmation"}
    assert card["control_selectors"] == {}
    assert card["commit_mode"] == "DRAFT_TO_COMMIT"
    assert card["redacted_preview"] is True
    assert card["pause_reason"] == "high_risk_approval"
    assert card["pause_category"] == "Policy Sign-Off"
    assert card["session_state"] == "live"
    assert card["workflow_summary"] == "Dental claim entry over Citrix"
    assert card["created_at_utc"].endswith("Z")

    assert len(card["unresolved_slots"]) == 1
    slot = card["unresolved_slots"][0]
    assert slot["control_idx"] == 1
    assert slot["control_label"] == "Coverage Confirmation"
    assert slot["reason"] == "Secondary coverage detected"
    assert slot["allowed_enum_options"] == ["Continue Anyway", "Cancel"]
    assert slot["input_mode"] == "choice"
    assert slot["candidate_vars"] == []
    assert slot["expected_field_type"] == "exact"


def test_extract_dropdown_prepare_card_from_clicked_open_menu():
    before_png, after_png, (_, _, dw, dh) = _make_dropdown_fixture()
    captured_sizes: list[tuple[int, int]] = []

    def fake_ocr(cropped_png: bytes) -> list[OcrWord]:
        with Image.open(io.BytesIO(cropped_png)) as img:
            captured_sizes.append(img.size)
        return [
            OcrWord(text="Assignment to Provider", x=14, y=16, width=180, height=18),
            OcrWord(text="Pay Subscriber", x=14, y=58, width=120, height=18),
        ]

    card = extract_pixel_prepare_card(
        before_png=before_png,
        after_png=after_png,
        portal_origin="https://rdp.dentalhub.example",
        field_label="Assignment of Benefits",
        ocr_fn=fake_ocr,
    )

    assert len(captured_sizes) == 1
    crop_w, crop_h = captured_sizes[0]
    assert abs(crop_w - (dw + 4)) <= 4
    assert abs(crop_h - (dh + 4)) <= 4

    assert card["card_id"].startswith("prep_pixel_")
    assert card["pause_reason"] == "policy_conflict"
    assert card["pause_category"] == "Policy Sign-Off"
    slot = card["unresolved_slots"][0]
    assert slot["control_idx"] == 0
    assert slot["control_label"] == "Assignment of Benefits"
    assert slot["allowed_enum_options"] == [
        "Assignment to Provider",
        "Pay Subscriber",
    ]
    assert slot["input_mode"] == "choice"


def test_extract_without_before_png_runs_on_full_after_png_and_supports_typed_mode():
    _, after_png, _ = _make_citrix_modal_fixture()
    captured_sizes: list[tuple[int, int]] = []

    def fake_ocr(png: bytes) -> list[OcrWord]:
        with Image.open(io.BytesIO(png)) as img:
            captured_sizes.append(img.size)
        return [OcrWord(text="Enter pre-authorization number to proceed.")]

    card = extract_pixel_prepare_card(
        before_png=None,
        after_png=after_png,
        portal_origin="https://citrix.clinic.org",
        field_label="Pre-Authorization Number",
        ocr_fn=fake_ocr,
    )

    assert captured_sizes == [(800, 600)]
    assert card["pause_reason"] == "missing_required_field"
    assert card["pause_category"] == "Missing Info"
    slot = card["unresolved_slots"][0]
    assert slot["input_mode"] == "typed"
    assert slot["allowed_enum_options"] == []
    assert slot["reason"] == "Enter pre-authorization number to proceed."


def test_phi_scrubbing_redacts_known_patient_dob_ssn_email_and_phone():
    before_png, after_png, _ = _make_citrix_modal_fixture()

    def fake_ocr(_png: bytes) -> list[OcrWord]:
        return [
            OcrWord(
                text=(
                    "Secondary coverage detected for JANE DOE (DOB 1985-04-12), "
                    "SSN 123-45-6789, email jane.doe@clinic.org, phone (416) 555-0199"
                ),
                x=20,
                y=60,
                width=350,
                height=18,
            ),
            OcrWord(text="Continue Anyway", x=50, y=150, width=120, height=18),
            OcrWord(text="Cancel", x=240, y=150, width=60, height=18),
        ]

    card = extract_pixel_prepare_card(
        before_png=before_png,
        after_png=after_png,
        portal_origin="https://citrix.clinic.org",
        field_label="Coverage Confirmation",
        ocr_fn=fake_ocr,
        known_phi_values=("Jane Doe", "1985-04-12"),
    )

    serialized = json.dumps(card)
    lowered = serialized.casefold()
    assert "jane doe" not in lowered
    assert "1985-04-12" not in lowered
    assert "123-45-6789" not in lowered
    assert "jane.doe@clinic.org" not in lowered
    assert "555-0199" not in lowered

    slot = card["unresolved_slots"][0]
    assert "<redacted>" in slot["reason"]
    assert slot["allowed_enum_options"] == ["Continue Anyway", "Cancel"]


@pytest.mark.parametrize(
    "ocr_lines,extra_kwargs",
    [
        (
            [
                OcrWord(
                    text="API error with key sk-live_abcdefghijklmnopqrst",
                    x=20,
                    y=60,
                    width=300,
                    height=18,
                ),
                OcrWord(text="Retry", x=50, y=150, width=60, height=18),
                OcrWord(text="Cancel", x=200, y=150, width=60, height=18),
            ],
            {},
        ),
        (
            [
                OcrWord(
                    text="Session failed: Bearer secret_token_1234567890",
                    x=20,
                    y=60,
                    width=300,
                    height=18,
                ),
            ],
            {},
        ),
        (
            [
                OcrWord(
                    text="Auth URL https://citrix.clinic.org/cb?csrf_token=tok_987654321",
                    x=20,
                    y=60,
                    width=300,
                    height=18,
                ),
            ],
            {},
        ),
        (
            [
                OcrWord(
                    text="Secondary coverage detected", x=20, y=60, width=200, height=18
                )
            ],
            {"field_label": "Portal Password"},
        ),
        (
            [
                OcrWord(
                    text="Secondary coverage detected", x=20, y=60, width=200, height=18
                )
            ],
            {"portal_origin": "https://admin:supersecret@citrix.clinic.org"},
        ),
        (
            [
                OcrWord(
                    text="Secondary coverage detected", x=20, y=60, width=200, height=18
                )
            ],
            {"staged_bindings": {0: "sk-live_abcdefghijklmnopqrst"}},
        ),
        (
            [
                OcrWord(
                    text="Secondary coverage detected", x=20, y=60, width=200, height=18
                )
            ],
            {"workflow_summary": "Using key AKIAIOSFODNN7EXAMPLE"},
        ),
    ],
)
def test_unscrubbable_secrets_are_rejected(
    ocr_lines: list[OcrWord],
    extra_kwargs: dict[str, Any],
):
    before_png, after_png, _ = _make_citrix_modal_fixture()
    kwargs: dict[str, Any] = {
        "before_png": before_png,
        "after_png": after_png,
        "portal_origin": "https://citrix.clinic.org",
        "field_label": "Coverage Confirmation",
        "ocr_fn": lambda _png: ocr_lines,
    }
    kwargs.update(extra_kwargs)

    with pytest.raises(ZeroPhiViolationError):
        extract_pixel_prepare_card(**kwargs)


def test_end_to_end_pixel_prepare_card_cloud_roundtrip():
    before_png, after_png, _ = _make_citrix_modal_fixture()
    state: dict[str, Any] = {
        "enqueued_cards": [],
        "poll_count": 0,
    }

    class _MockPrepareHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_json(self, status: int, body: dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_POST(self) -> None:
            if self.path != "/api/prepare":
                self._send_json(404, {"error": "not_found"})
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            state["enqueued_cards"].append(payload["card"])
            self._send_json(201, {"accepted": True, "card": payload["card"]})

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/api/prepare":
                self._send_json(404, {"error": "not_found"})
                return
            params = urllib.parse.parse_qs(parsed.query)
            card_id = params.get("card_id", [""])[0]
            state["poll_count"] += 1

            if state["poll_count"] < 2:
                self._send_json(200, {"cards": [], "resolutions": []})
                return

            resolution_body: dict[str, Any] = {
                "schema_version": "prepare_signoff_v1",
                "card_id": card_id,
                "portal_origin": "https://citrix.clinic.org",
                "action": "approve",
                "resolution": {
                    "card_id": card_id,
                    "operator_id": "citrix_ops_lead",
                    "slot_bindings": {"1": "Continue Anyway"},
                    "approve_all": False,
                    "resolved_at_utc": "2026-11-28T16:45:00Z",
                },
                "merged_bindings": {
                    "0": "$member_id_masked",
                    "1": "Continue Anyway",
                },
                "zero_phi_verified": True,
            }
            resolution_body["payload_sha256"] = compute_resolution_sha256(
                resolution_body
            )
            self._send_json(200, {"cards": [], "resolutions": [resolution_body]})

    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockPrepareHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        card = extract_pixel_prepare_card(
            before_png=before_png,
            after_png=after_png,
            portal_origin="https://citrix.clinic.org",
            field_label="Coverage Confirmation",
            card_id="prep_pixel_e2e_001",
            ocr_fn=lambda _png: [
                OcrWord(
                    text="Secondary coverage detected for Jane Doe (123-45-6789)",
                    x=24,
                    y=66,
                    width=320,
                    height=18,
                ),
                OcrWord(text="Continue Anyway", x=60, y=148, width=120, height=18),
                OcrWord(text="Cancel", x=260, y=148, width=60, height=18),
            ],
            known_phi_values=("Jane Doe",),
            staged_bindings={0: "$member_id_masked"},
            workflow_summary="Citrix dental claim submission",
        )

        client = CloudPrepareClient(
            f"http://127.0.0.1:{port}",
            api_token="oa_pixel_test_token",
            request_timeout_seconds=5.0,
        )
        enqueue_resp = client.enqueue(card)
        assert enqueue_resp["accepted"] is True

        # Confirm zero PHI was sent over the wire.
        wire_json = json.dumps(state["enqueued_cards"][0]).casefold()
        assert "jane doe" not in wire_json
        assert "123-45-6789" not in wire_json

        resolution_payload = client.poll_resolution(
            card["card_id"],
            timeout_seconds=5.0,
            poll_interval_seconds=0.05,
        )
        halt = pixel_prepare_card_to_form_halt(card)
        decision = prepare_resolution_to_human_input(halt, resolution_payload)

        assert state["poll_count"] == 2
        assert decision.halt_id == "prep_pixel_e2e_001"
        assert decision.action == HumanDecisionAction.PROVIDE_VALUE
        assert decision.provided_value == "Continue Anyway"
        assert decision.actor_id == "citrix_ops_lead"
        assert decision.idempotency_key.startswith("cloud-prepare:prep_pixel_e2e_001:")
    finally:
        server.shutdown()
        server.server_close()
