"""Deterministic synthetic patient-administration fixture for the gate campaign.

The fixture renders PHI-free synthetic pixels with Pillow, persists the one
qualified business write to a local SQLite system of record, and exposes the
guarded actuation surface (``GuardedCoordinateActionBackend`` plus
``GuardedKeyboardActionBackend``) that the real governed runtime drives.

No model, network, Docker, or browser is involved.  Every fault is injected at
the same boundary the hosted RDP campaign injects it: the observation stream,
the application state before a consequential input edge, or the transport
after a real delivery.  The runtime under test is unmodified.
"""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageOps

from openadapt_flow.backend import ActionDeliveryUncertain
from openadapt_flow.ir import ActionDeliveryReceipt

VIEWPORT = (1280, 800)

TARGET_RECORD = "REC-200"
OTHER_RECORD = "REC-100"
TARGET_NAME = "Brian Brooks Cardiology"
OTHER_NAME = "Alice Adams Neurology"
NOTE_PARAM = "note"
NOTE_VALUE = "Follow-up scheduled"

ROW_100_POINT = (120, 148)
ROW_200_POINT = (120, 248)
NOTE_FIELD_POINT = (760, 352)
SAVE_BUTTON_POINT = (1060, 656)
SAVE_BUTTON_REGION = (940, 620, 240, 72)
DUPLICATE_SAVE_BUTTON_POINT = (1060, 416)

_BACKGROUND = "white"
_TEXT = (17, 17, 17)
_ROW_ALT = (238, 240, 244)
_SELECTED = (208, 224, 250)
_BORDER = (120, 124, 132)
_BANNER_BG = (222, 240, 222)


def _font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size)


def _sha256(png: bytes) -> str:
    return hashlib.sha256(png).hexdigest()


@dataclass
class FixtureState:
    """Rendered application state; one instance per trial."""

    active_record: Optional[str] = None
    focused: Optional[str] = None
    note_text: str = ""
    saved: bool = False
    row_reordered: bool = False
    duplicate_save_control: bool = False
    save_control_hidden: bool = False
    status_line_override: Optional[str] = None
    focus_ring_target: Optional[str] = None


class GateOracle:
    """SQLite system of record plus an append-only guarded-input ledger.

    ``records`` is the qualified business surface. ``input_events`` is the
    independent persisted ledger of every delivered guarded input edge; the
    campaign binds each pre-write action to an exact one-write effect contract
    on that surface, so a duplicate or phantom edge fails verification.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = root / "records.sqlite3"
        self.ledger_path = root / "input-ledger.jsonl"
        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS records ("
                "record_id TEXT PRIMARY KEY, note TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS input_events ("
                "event_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, "
                "kind TEXT NOT NULL, detail TEXT NOT NULL, "
                "run_key TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        self.write_count = 0
        self.event_count = 0

    def reset(self) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DELETE FROM records")
            connection.execute("DELETE FROM input_events")
            connection.commit()
        finally:
            connection.close()
        self.write_count = 0
        self.event_count = 0
        self.ledger_path.write_text("", encoding="utf-8")

    def write(self, record_id: str, note: str) -> None:
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO records (record_id, note) VALUES (?, ?)",
                (record_id, note),
            )
            connection.commit()
        finally:
            connection.close()
        self.write_count += 1

    def read_all(self) -> list[dict[str, str]]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT record_id, note FROM records ORDER BY record_id"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def record_event(self, kind: str, detail: str) -> None:
        """Persist one delivered guarded input edge on its oracle surface.

        ``run_key`` is the at-most-once key the qualified effect contract
        counts: a duplicate delivery of the same edge lands a second row
        bearing the same key, which the runtime refutes as a duplicate.
        """

        import uuid

        self.event_count += 1
        event_id = f"evt-{self.event_count:04d}-{uuid.uuid4().hex[:8]}"
        run_key = f"gate-{kind}-once"
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO input_events (event_id, seq, kind, detail, run_key) "
                "VALUES (?, ?, ?, ?, ?)",
                (event_id, self.event_count, kind, detail, run_key),
            )
            connection.commit()
        finally:
            connection.close()
        self.append_ledger({"action": kind, "detail": detail, "event_id": event_id})

    def read_events(self) -> list[dict[str, str]]:
        connection = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT event_id, seq, kind, detail, run_key FROM input_events "
                "ORDER BY seq"
            ).fetchall()
            return [dict(row) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            connection.close()

    def append_ledger(self, entry: dict[str, Any]) -> None:
        with self.ledger_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, sort_keys=True) + "\n")

    def read_ledger(self) -> list[dict[str, Any]]:
        lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line]


def render_frame(state: FixtureState, *, drift: Optional[str] = None) -> bytes:
    """Render one deterministic frame of the synthetic application."""

    image = Image.new("RGB", VIEWPORT, _BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text(
        (40, 32), "OpenAdapt qualification-gate clinic", font=_font(28), fill=_TEXT
    )

    row_order = [("REC-100", OTHER_NAME), ("REC-200", TARGET_NAME)]
    if state.row_reordered:
        row_order.reverse()
    row_bands = {"REC-100": (120, 176), "REC-200": (220, 276)}
    rendered_rows = list(zip(row_order, (row_bands["REC-100"], row_bands["REC-200"])))
    for index, ((record_id, name), (top, bottom)) in enumerate(rendered_rows):
        fill = _ROW_ALT if index % 2 == 0 else _BACKGROUND
        if state.active_record == record_id:
            fill = _SELECTED
        draw.rectangle((40, top, 620, bottom), fill=fill)
        draw.text((60, top + 14), record_id, font=_font(28), fill=_TEXT)
        draw.text((320, top + 14), name, font=_font(28), fill=_TEXT)

    draw.text((60, 334), "Triage note entry", font=_font(24), fill=(90, 94, 102))
    focused_note = state.focused == "note"
    draw.rectangle(
        (480, 320, 1040, 384),
        outline=(40, 90, 220) if focused_note else _BORDER,
        width=3 if focused_note else 1,
        fill=_BACKGROUND,
    )
    if state.note_text:
        draw.text((496, 338), state.note_text, font=_font(28), fill=_TEXT)
    else:
        draw.text((645, 338), "Enter triage note", font=_font(28), fill=(150, 154, 162))

    if state.saved:
        draw.rectangle((480, 430, 700, 482), fill=_BANNER_BG)
        draw.text((496, 442), "Saved", font=_font(28), fill=(20, 96, 42))

    if state.duplicate_save_control and not state.save_control_hidden:
        # A competing control appears in the same visual state: TWO compact
        # controls labeled "Save" replace the demonstrated one, and BOTH
        # label centers fall inside the recorded target region. The recorded
        # template no longer matches either control, and OCR-based
        # resolution sees two candidates that retained evidence cannot
        # separate, so the resolver must refuse the target as ambiguous
        # before any input.
        _draw_button(draw, 954, 628, 88, 56, "Save")
        _draw_button(draw, 1046, 628, 88, 56, "Save")
    elif not state.save_control_hidden:
        _draw_button(draw, *SAVE_BUTTON_REGION, "Save")

    if state.status_line_override is not None:
        status = state.status_line_override
    else:
        # The status band carries ONLY the selected record's human-readable
        # name: a discriminative, OCR-reliable identity carrier. Machine
        # record identifiers stay in the row label and the system of
        # record; putting their digits into this band would rest identity
        # on an OCR-glyph-confusable token, which the runtime correctly
        # refuses to act on (see openadapt_flow.runtime.identity).
        display_name = {
            "REC-100": OTHER_NAME,
            "REC-200": TARGET_NAME,
        }.get(state.active_record)
        if state.active_record:
            status = f"{display_name} selected for triage note"
        else:
            status = "No active record"
    draw.text((60, 640), status, font=_font(28), fill=_TEXT)

    ring = state.focus_ring_target
    if ring == "note":
        draw.rectangle((474, 314, 1046, 390), outline=(40, 90, 220), width=2)
    elif ring in {"REC-100", "REC-200"}:
        top, bottom = row_bands[ring]
        draw.rectangle((34, top - 6, 626, bottom + 6), outline=(40, 90, 220), width=2)

    if drift == "moderate_display_drift":
        # A bounded scale/compression/theme change that keeps text legible.
        width, height = image.size
        image = image.resize(
            (max(1, int(width * 0.92)), max(1, int(height * 0.92))),
            Image.Resampling.BILINEAR,
        ).resize((width, height), Image.Resampling.BILINEAR)
        image = ImageEnhance.Contrast(image).enhance(0.88)
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=72)
        image = Image.open(io.BytesIO(encoded.getvalue())).convert("RGB")
    elif drift == "severe_display_drift":
        # A heavy scale/theme/compression fault removes reliable target and
        # record detail: strong downscale, inverted luminance, posterized
        # tones, blur, and lossy recompression. The visual ladder must halt
        # before any effect.
        from PIL import ImageFilter

        width, height = image.size
        image = image.resize(
            (max(1, int(width * 0.3)), max(1, int(height * 0.3))),
            Image.Resampling.BILINEAR,
        ).resize((width, height), Image.Resampling.BILINEAR)
        image = ImageOps.invert(image)
        image = image.point(lambda p: (p >> 4) << 4)
        image = image.filter(ImageFilter.GaussianBlur(radius=2))
        encoded = io.BytesIO()
        image.save(encoded, format="JPEG", quality=4)
        image = Image.open(io.BytesIO(encoded.getvalue())).convert("RGB")
    return _png_bytes(image)


def _draw_button(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    w: int,
    h: int,
    label: str,
    *,
    fill: str | tuple[int, int, int] = (232, 236, 242),
    outline: str | tuple[int, int, int] = _BORDER,
    text_fill: str | tuple[int, int, int] = _TEXT,
) -> None:
    draw.rectangle((x, y, x + w, y + h), fill=fill, outline=outline)
    left, top, right, bottom = draw.textbbox((0, 0), label, font=_font(32))
    draw.text(
        (x + (w - (right - left)) // 2 - left, y + (h - (bottom - top)) // 2 - top),
        label,
        font=_font(32),
        fill=text_fill,
    )


def _png_bytes(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


UncertaintyVariant = Literal[
    "write_lost_reset",
    "write_kept_timeout",
    "oracle_unreachable",
]


GATE_ENVIRONMENT_DIGEST = hashlib.sha256(
    b"qualification-gate-environment-v1"
).hexdigest()
GATE_OBSERVER_ID = "fixture:benchmark.qualification_gate.renderer"


class GateEnvironmentObserver:
    """Stable environment-observer identity bound into the sealed project."""

    @property
    def observer_id(self) -> str:
        return GATE_OBSERVER_ID

    @property
    def contract_sha256(self) -> str:
        return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()

    def observe(self, backend: Any, target_kind: str):
        from openadapt_flow.qualification_environment import (
            QualificationEnvironmentObservation,
        )

        value = backend.qualification_environment_identity()
        if not isinstance(value, tuple) or len(value) != 4:
            raise ValueError("qualification gate observation is incomplete")
        return QualificationEnvironmentObservation(
            target_kind=target_kind,
            application_identity=value[0],
            application_version=value[1],
            session_identity_sha256=value[2],
            environment_digest=value[3],
        )


@dataclass
class ConditionPlan:
    """One campaign condition's fixture-side configuration."""

    duplicate_save_control: bool = False
    row_reordered: bool = False
    drift: Optional[str] = None
    pre_write_mutation: Optional[Literal["wrong_record", "stale_identity"]] = None
    hide_save_control: bool = False
    uncertainty: Optional[UncertaintyVariant] = None


class GateFixtureBackend:
    """In-process backend implementing the local guarded actuation surface."""

    viewport = VIEWPORT

    def __init__(
        self,
        oracle: GateOracle,
        *,
        plan: ConditionPlan,
        actions_before_mutation: int = 2,
    ) -> None:
        self.oracle = oracle
        self.plan = plan
        self.state = FixtureState(
            row_reordered=plan.row_reordered,
            duplicate_save_control=plan.duplicate_save_control,
            save_control_hidden=plan.hide_save_control,
        )
        self.drift = plan.drift
        self._actions_before_mutation = actions_before_mutation
        self._input_guard: Any = None
        self._guarded_point: Optional[tuple[int, int]] = None
        self._guarded_keyboard_point: Optional[tuple[int, int]] = None
        self.delivered_actions: list[tuple[str, int, int]] = []
        self.save_delivery_attempts = 0
        self.mutation_applied = False
        self.uncertainty_events: list[dict[str, Any]] = []
        self.degraded_oracle = False

    # -- observation ---------------------------------------------------

    def screenshot(self) -> bytes:
        return render_frame(self.state, drift=self.drift)

    def qualification_environment_identity(self) -> tuple[str, str, str, str]:
        session = hashlib.sha256(b"qualification-gate-fixture-session-v1").hexdigest()
        return (
            "qualification-gate-fixture",
            "v1",
            session,
            GATE_ENVIRONMENT_DIGEST,
        )

    # -- raw input (recording time) -------------------------------------

    def click(self, x: int, y: int, *, double: bool = False) -> None:
        self._apply_raw_click(x, y)

    def right_click(self, x: int, y: int) -> None:
        self.delivered_actions.append(("right_click", x, y))

    def drag(self, x: int, y: int, end_x: int, end_y: int) -> None:
        self.delivered_actions.append(("drag", x, y))

    def type_text(self, text: str) -> None:
        if self.state.focused == "note":
            self.state.note_text = text

    def press(self, key: str) -> None:
        self.delivered_actions.append(("press", 0, 0))

    def scroll(self, dx: int, dy: int) -> None:
        self.delivered_actions.append(("scroll", dx, dy))

    def close(self) -> None:
        return None

    # -- qualification input guard --------------------------------------

    def set_qualification_input_guard(self, guard: Any) -> None:
        self._input_guard = guard

    def _guard(self) -> None:
        if self._input_guard is not None:
            self._input_guard()

    # -- guarded coordinate surface ---------------------------------------

    def arm_guarded_coordinate(self, x: int, y: int) -> None:
        self._guarded_point = (int(x), int(y))

    def cancel_guarded_coordinate(self) -> None:
        self._guarded_point = None

    def act_guarded_coordinate(
        self,
        x: int,
        y: int,
        *,
        expected_frame_sha256: str,
        double: bool = False,
        button: str = "left",
    ) -> ActionDeliveryReceipt:
        point = self._guarded_point
        self._guarded_point = None
        if point != (int(x), int(y)):
            raise RuntimeError("guarded coordinate target was not pre-armed")
        frame = render_frame(self.state, drift=self.drift)
        if _sha256(frame) != expected_frame_sha256:
            raise RuntimeError("guarded coordinate frame changed")
        fingerprint = hashlib.sha256(
            f"{expected_frame_sha256}\0{int(x)}\0{int(y)}".encode("utf-8")
        ).hexdigest()
        if button == "right":
            self.delivered_actions.append(("right_click", x, y))
            return ActionDeliveryReceipt(
                receipt_id=f"gate-{len(self.delivered_actions)}",
                operation="guarded_coordinate_click",
                native=False,
                target_fingerprint=fingerprint,
                delivered_at="2026-08-21T00:00:00+00:00",
            )
        if self._is_save_region(x, y):
            self.save_delivery_attempts += 1
            self._guard()
            self._deliver_save(x, y, fingerprint=fingerprint)
            if self.plan.uncertainty is not None:
                self._raise_uncertain(x, y, fingerprint)
            return ActionDeliveryReceipt(
                receipt_id=f"gate-{len(self.delivered_actions)}",
                operation="guarded_coordinate_click",
                native=False,
                target_fingerprint=fingerprint,
                delivered_at="2026-08-21T00:00:00+00:00",
            )
        self._guard()
        self._apply_raw_click(x, y)
        self.delivered_actions.append(("click", x, y))
        self.oracle.append_ledger(
            {
                "action": "click",
                "x": x,
                "y": y,
                "active_record": self.state.active_record,
            }
        )
        return ActionDeliveryReceipt(
            receipt_id=f"gate-{len(self.delivered_actions)}",
            operation="guarded_coordinate_click",
            native=False,
            target_fingerprint=fingerprint,
            delivered_at="2026-08-21T00:00:00+00:00",
        )

    # -- guarded keyboard surface -----------------------------------------

    def guarded_keyboard_frame(self) -> bytes:
        self._apply_pre_write_mutation_if_due()
        return render_frame(self.state, drift=self.drift)

    def arm_guarded_keyboard(self, x: int, y: int) -> None:
        self._guarded_keyboard_point = (int(x), int(y))

    def cancel_guarded_keyboard(self) -> None:
        self._guarded_keyboard_point = None

    def type_text_guarded(
        self, text: str, *, expected_frame_sha256: str
    ) -> ActionDeliveryReceipt:
        point = self._guarded_keyboard_point
        self._guarded_keyboard_point = None
        if point is None:
            raise RuntimeError("guarded keyboard target was not pre-armed")
        frame = render_frame(self.state, drift=self.drift)
        if _sha256(frame) != expected_frame_sha256:
            raise RuntimeError("guarded keyboard frame changed")
        self._guard()
        if self.state.focused != "note":
            raise RuntimeError("note field lost focus before typed delivery")
        self.state.note_text = text
        self.delivered_actions.append(("type", int(point[0]), int(point[1])))
        self.oracle.record_event("type_note", text)
        return ActionDeliveryReceipt(
            receipt_id=f"gate-type-{len(self.delivered_actions)}",
            operation="physical_type_text",
            native=False,
            delivered_at="2026-08-21T00:00:00+00:00",
        )

    def press_guarded(
        self, key: str, *, expected_frame_sha256: str
    ) -> ActionDeliveryReceipt:
        point = self._guarded_keyboard_point
        self._guarded_keyboard_point = None
        if point is None:
            raise RuntimeError("guarded keyboard target was not pre-armed")
        frame = render_frame(self.state, drift=self.drift)
        if _sha256(frame) != expected_frame_sha256:
            raise RuntimeError("guarded keyboard frame changed")
        self._guard()
        self.delivered_actions.append(("press", int(point[0]), int(point[1])))
        return ActionDeliveryReceipt(
            receipt_id=f"gate-key-{len(self.delivered_actions)}",
            operation="physical_press",
            native=False,
            delivered_at="2026-08-21T00:00:00+00:00",
        )

    # -- fault plumbing -----------------------------------------------------

    def _is_save_region(self, x: int, y: int) -> bool:
        if self.state.duplicate_save_control and not self.state.save_control_hidden:
            return 954 <= x < 1042 and 628 <= y < 684
        left, top, width, height = SAVE_BUTTON_REGION
        return left <= x < left + width and top <= y < top + height

    def _apply_raw_click(self, x: int, y: int) -> None:
        row_order = [("REC-100", OTHER_NAME), ("REC-200", TARGET_NAME)]
        if self.state.row_reordered:
            row_order.reverse()
        bands = ((120, 176), (220, 276))
        for (record_id, _name), (top, bottom) in zip(row_order, bands):
            if 40 <= x <= 620 and top <= y <= bottom:
                self.state.active_record = record_id
                self.state.focus_ring_target = record_id
                self.oracle.record_event("select_record", record_id)
                return
        if 480 <= x <= 1040 and 320 <= y <= 384:
            self.state.focused = "note"
            self.state.focus_ring_target = "note"
            self.oracle.record_event("focus_note", "")
            return
        if self._is_save_region(x, y):
            if self.state.save_control_hidden:
                return
            if self.state.active_record and self.state.note_text:
                self.oracle.write(self.state.active_record, self.state.note_text)
                self.oracle.record_event("save_note", self.state.active_record)
                self.state.saved = True

    def _apply_pre_write_mutation_if_due(self) -> None:
        if self.mutation_applied or self.plan.pre_write_mutation is None:
            return
        reversible_done = sum(
            1
            for action, *_rest in self.delivered_actions
            if action in {"click", "type"}
        )
        if reversible_done < self._actions_before_mutation:
            return
        self.mutation_applied = True
        if self.plan.pre_write_mutation == "wrong_record":
            self.state.active_record = OTHER_RECORD
            self.state.focus_ring_target = OTHER_RECORD
        elif self.plan.pre_write_mutation == "stale_identity":
            self.state.status_line_override = "Loading"

    def _deliver_save(self, x: int, y: int, *, fingerprint: str) -> None:
        self._apply_raw_click(x, y)
        self.delivered_actions.append(("save_click", x, y))

    def _raise_uncertain(self, x: int, y: int, fingerprint: str) -> None:
        variant = self.plan.uncertainty
        assert variant is not None
        persisted = variant in {"write_kept_timeout", "oracle_unreachable"}
        if persisted and not self.oracle.read_all():
            self.oracle.write(TARGET_RECORD, self.state.note_text or NOTE_VALUE)
            self.oracle.record_event("save_note", TARGET_RECORD)
        if variant == "write_lost_reset":
            # The transport lost certainty AND the application rolled its
            # commit back: the intended effect is absent from the system of
            # record, so the runtime must reconcile instead of confirming.
            connection = sqlite3.connect(self.oracle.path)
            try:
                connection.execute("DELETE FROM records")
                connection.execute("DELETE FROM input_events WHERE kind = 'save_note'")
                connection.commit()
            finally:
                connection.close()
            self.oracle.write_count = 0
        cause = (
            "TimeoutError"
            if variant == "write_kept_timeout"
            else "ConnectionResetError"
        )
        self.uncertainty_events.append(
            {
                "variant": variant,
                "cause_type": cause,
                "persisted": self.oracle.write_count > 0,
                "target_fingerprint": fingerprint,
            }
        )
        if variant == "oracle_unreachable":
            self.degraded_oracle = True
        raise ActionDeliveryUncertain(
            operation="guarded_coordinate_click",
            native=False,
            target_fingerprint=fingerprint,
            cause_type=cause,
        )

    def degrade_oracle_reads(self) -> None:
        """Mark the oracle unreachable after an uncertainty event."""

        self.degraded_oracle = True
