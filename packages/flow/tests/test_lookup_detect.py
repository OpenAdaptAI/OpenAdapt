"""The recording teaches the check: a look-up after the save becomes the
record check, with no marking and no configuration.

A synthetic recording of the RDP kiosk fixture: the person books an
appointment, presses Save appointment, then types the request ID into the
look-up box and presses Find. The detector finds the look-up and proposes a
separate-session ``desktop_readback``. That proposal, with only its address
filled in, confirms the booking through real OCR. Synthetic data only.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from openadapt_flow.backends.rdp_backend import FreeRDPBackend
from openadapt_flow.compiler.lookup_detect import (
    CHECK_PROPOSAL_FILE,
    READBACK_PROPOSAL_FILE,
    accept_check_proposal,
    detect_lookup,
    to_check_marks,
    to_desktop_readback,
    write_proposals,
)
from openadapt_flow.compiler.readback_authoring import CHECK_FILE, load_check
from openadapt_flow.runtime.effects import Effect, Verdict
from openadapt_flow.runtime.effects.desktop_readback import (
    DesktopReadbackSpec,
    DesktopSessionFacts,
)
from openadapt_flow.runtime.effects.desktop_readback_sessions import (
    BackendSessionDriver,
    build_desktop_readback_verifier,
)

from ._desktop_readback_support import KioskLookupTransport

pytest.importorskip("rapidocr_onnxruntime")

PARAMS = {
    "appointment_slot": "2026-11-02 09:30",
    "visit_type": "Follow-up",
    "request_id": "REQ-4815",
}
ENV = {"RB_USER": "reader", "RB_PASS": "synthetic-pass"}


def _png(transport: KioskLookupTransport) -> bytes:
    buffer = io.BytesIO()
    transport.render().save(buffer, format="PNG")
    return buffer.getvalue()


class _Recorder:
    """Writes events.jsonl, frames and meta.json as the recorder does."""

    def __init__(self, root: Path, kiosk: KioskLookupTransport) -> None:
        self.root = root
        self.kiosk = kiosk
        (root / "frames").mkdir(parents=True)
        self.events: list[dict[str, Any]] = []

    def _commit(self, event: dict[str, Any], act: Any) -> None:
        i = len(self.events)
        (self.root / "frames" / f"{i:04d}_before.png").write_bytes(_png(self.kiosk))
        act()
        (self.root / "frames" / f"{i:04d}_after.png").write_bytes(_png(self.kiosk))
        self.events.append({"i": i, **event})

    def click(self, x: int, y: int) -> None:
        self._commit(
            {"kind": "click", "x": x, "y": y},
            lambda: self.kiosk.pointer(x, y, "left", False),
        )

    def type(self, text: str, param: str | None = None) -> None:
        def act() -> None:
            for ch in text:
                self.kiosk.key(ch, True)
                self.kiosk.key(ch, False)

        event: dict[str, Any] = {"kind": "type", "text": text}
        if param:
            event["param"] = param
        self._commit(event, act)

    def save(self, act: Any) -> None:
        self._commit({"kind": "click", "x": 690, "y": 592}, act)

    def finish(self) -> Path:
        (self.root / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.events)
        )
        (self.root / "meta.json").write_text(
            json.dumps({"id": "rec", "params": PARAMS, "secret_params": []})
        )
        return self.root


def _record(
    tmp_path: Path, *, look_up: bool = True
) -> tuple[Path, KioskLookupTransport]:
    kiosk = KioskLookupTransport(tmp_path / "appointments.sqlite3")
    rec = _Recorder(tmp_path / "recording", kiosk)
    rec.click(265, 193)  # Ada Lovelace
    rec.click(775, 269)
    rec.type(PARAMS["appointment_slot"], "appointment_slot")
    rec.click(775, 389)
    rec.type(PARAMS["visit_type"], "visit_type")
    rec.click(775, 509)
    rec.type(PARAMS["request_id"], "request_id")

    def save() -> None:
        kiosk.pointer(690, 592, "left", False)
        kiosk.book(
            PARAMS["request_id"],
            "MRN A1001",
            "Ada Lovelace",
            PARAMS["appointment_slot"],
            PARAMS["visit_type"],
        )

    rec.save(save)
    if look_up:
        rec.click(90, 389)  # the look-up box
        rec.type(PARAMS["request_id"])
        rec.click(430, 386)  # Find
    return rec.finish(), kiosk


def test_a_look_up_after_the_save_is_found(tmp_path: Path) -> None:
    recording, _kiosk = _record(tmp_path)
    proposal = detect_lookup(recording)
    assert proposal is not None
    assert proposal.save_event == 7
    assert "save appointment" in proposal.save_label.casefold()
    assert proposal.start_event == 8  # the click into the look-up box
    assert proposal.query_param == "request_id"
    assert proposal.submit_label == "Find"
    found = {seen.param: seen.label for seen in proposal.fields}
    # Only values the look-up brought up; the booking form still shows the
    # same text but was on screen before the look-up started.
    assert found == {
        "request_id": "Request ID",
        "appointment_slot": "Slot",
        "visit_type": "Visit type",
    }
    for seen in proposal.fields:
        assert seen.region[0] < 400  # the look-up panel, not the booking form


def test_no_look_up_means_no_proposal(tmp_path: Path) -> None:
    recording, _kiosk = _record(tmp_path, look_up=False)
    assert detect_lookup(recording) is None


def test_the_proposed_check_confirms_the_booking(tmp_path: Path) -> None:
    recording, kiosk = _record(tmp_path)
    proposal = detect_lookup(recording)
    assert proposal is not None
    written = write_proposals(recording, proposal, session_kind="rdp")
    assert [p.name for p in written] == [CHECK_PROPOSAL_FILE, READBACK_PROPOSAL_FILE]
    document = yaml.safe_load((recording / READBACK_PROPOSAL_FILE).read_text())
    section = document["verifier"]["desktop_readback"]
    assert section["session"] == {"kind": "rdp", "rdp_host": "UNRESOLVED"}
    assert all(e["needs_operator_confirmation"] for e in document["effects"])

    # The operator fills in the address; nothing else.
    section["session"]["rdp_host"] = "kiosk.test"
    section["credentials"] = {"username_env": "RB_USER", "password_env": "RB_PASS"}
    section["settle_s"] = 0.0
    section["min_ocr_confidence"] = 0.6
    spec = DesktopReadbackSpec.model_validate(section)

    readers: list[KioskLookupTransport] = []

    def open_backend(user: str, password: str) -> tuple[Any, DesktopSessionFacts]:
        reader = KioskLookupTransport(kiosk.db_path)  # a fresh RDP logon
        readers.append(reader)
        return FreeRDPBackend(reader), DesktopSessionFacts(True, True, True, "ocr")

    verifier = build_desktop_readback_verifier(
        spec,
        PARAMS,
        timeout_s=0.0,
        poll_interval_s=0.0,
        driver=BackendSessionDriver(open_backend, settle_s=0.0),
        environ=ENV,
    )
    before = verifier.capture_pre_state()
    for raw in document["effects"]:
        raw = {k: v for k, v in raw.items() if k != "needs_operator_confirmation"}
        effect = Effect.model_validate({**raw, "timeout_s": 0.0}).resolve(PARAMS)
        verdict = verifier.verify(effect, before)
        assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert readers
    assert all(r.save_clicks == 0 and r.booking_typed == "" for r in readers)


def test_the_same_session_check_is_proposed_and_accepted(tmp_path: Path) -> None:
    recording, _kiosk = _record(tmp_path)
    proposal = detect_lookup(recording)
    assert proposal is not None
    marks = to_check_marks(proposal)
    assert marks.start_event == 8
    assert {f.param for f in marks.fields} == set(PARAMS)
    write_proposals(recording, proposal, session_kind="rdp")
    path = accept_check_proposal(recording)
    assert path.name == CHECK_FILE
    assert load_check(recording).start_event == 8
    with pytest.raises(FileExistsError):
        accept_check_proposal(recording)


def test_a_web_recording_gets_only_the_same_session_check(tmp_path: Path) -> None:
    recording, _kiosk = _record(tmp_path)
    proposal = detect_lookup(recording)
    assert proposal is not None
    assert to_desktop_readback(proposal, "web") is None
    written = write_proposals(recording, proposal, session_kind="web")
    assert [p.name for p in written] == [CHECK_PROPOSAL_FILE]


# -- the CLI ----------------------------------------------------------------------


def test_bare_record_verify_needs_no_file() -> None:
    from openadapt_flow.__main__ import build_parser

    args = build_parser().parse_args(["record", "--out", "rec", "--verify"])
    assert args.verify == ""
    args = build_parser().parse_args(["record", "--out", "rec", "--verify", "v.yaml"])
    assert args.verify == "v.yaml"


def test_record_end_proposes_the_look_up(tmp_path: Path, capsys: Any) -> None:
    import argparse

    from openadapt_flow.__main__ import _propose_lookup_check
    from openadapt_flow.record_sor import PROPOSED_CONTRACT_NAME

    recording, _kiosk = _record(tmp_path)
    args = argparse.Namespace(out=str(recording), backend="rdp")
    _propose_lookup_check(args, from_lookup=True)
    out = capsys.readouterr().out
    assert "Found a look-up after the save" in out
    assert "check accept" in out
    contract = yaml.safe_load((recording / PROPOSED_CONTRACT_NAME).read_text())
    assert contract["verifier"]["kind"] == "desktop_readback"
    assert (recording / PROPOSED_CONTRACT_NAME).stat().st_mode & 0o777 == 0o600


def test_record_end_asks_for_a_look_up_when_there_is_none(
    tmp_path: Path, capsys: Any
) -> None:
    import argparse

    from openadapt_flow.__main__ import _propose_lookup_check
    from openadapt_flow.compiler.lookup_detect import LOOKUP_PROMPT

    recording, _kiosk = _record(tmp_path, look_up=False)
    _propose_lookup_check(
        argparse.Namespace(out=str(recording), backend="rdp"), from_lookup=False
    )
    assert LOOKUP_PROMPT in capsys.readouterr().out
    assert not (recording / CHECK_PROPOSAL_FILE).exists()


def test_check_accept_cli(tmp_path: Path, capsys: Any) -> None:
    from openadapt_flow.__main__ import build_parser

    recording, _kiosk = _record(tmp_path)
    proposal = detect_lookup(recording)
    assert proposal is not None
    write_proposals(recording, proposal, session_kind="rdp")
    args = build_parser().parse_args(["check", "accept", str(recording)])
    assert args.func(args) == 0
    assert (recording / CHECK_FILE).is_file()
    assert "check admit" in capsys.readouterr().out
    assert args.func(args) == 2  # never overwrites a check.json
