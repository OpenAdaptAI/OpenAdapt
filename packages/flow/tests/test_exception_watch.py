"""The exception watch raises cards and holds saves without the agent.

No browser: page snapshots are the dicts :data:`SNAPSHOT_JS` returns, and
requests are :class:`HeldRequest` as the guard's CDP layer builds them.
Synthetic screens and keys only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.exception_card import answer_message

from openadapt_flow.agent_guard.requests import HeldRequest, NotASave
from openadapt_flow.approval_mode.approver_roster import (
    ROOT_ENV,
    file_key_signer,
    init_root,
)
from openadapt_flow.exceptions.registry import ExceptionRegistry, roster_verifier
from openadapt_flow.exceptions.watch import WATCH_EVENTS, ExceptionWatch
from openadapt_flow.execute.keys import sign_bytes
from openadapt_flow.jit.authority import public_hex

APP = "http://127.0.0.1:8123"
CODE_SCREEN = {
    "text": "Check your phone. Verification code",
    "elements": [
        {
            "tag": "input",
            "type": "text",
            "id": "otc",
            "label": "Verification code",
            "autocomplete": "one-time-code",
            "inputmode": "numeric",
            "maxlength": "6",
            "visible": True,
            "bbox": [10, 10, 100, 20],
        }
    ],
}
FORM_SCREEN = {
    "text": "New note",
    "elements": [
        {
            "tag": "input",
            "type": "text",
            "id": "note",
            "label": "Note",
            "visible": True,
        },
        {"tag": "button", "type": "", "id": "save", "visible": True},
    ],
}
SIGN_IN_SCREEN = {
    "text": "Sign in",
    "elements": [{"tag": "input", "type": "password", "id": "pw", "visible": True}],
}


@pytest.fixture
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key_dir = tmp_path / "keys"
    key = Ed25519PrivateKey.generate()
    init_root(
        key_dir,
        principal="front-desk",
        public_key=public_hex(key),
        sign=file_key_signer(key),
    )
    monkeypatch.setenv(ROOT_ENV, public_hex(key))
    return key_dir, key


class _Clock:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _watch(tmp_path: Path, roster, **kw) -> ExceptionWatch:
    key_dir, key = roster
    return ExceptionWatch(
        ExceptionRegistry(tmp_path / "registry"),
        run_ref="watch-run",
        verify=roster_verifier(key_dir, public_hex(key)),
        app_origin=APP,
        events_path=tmp_path / "run" / WATCH_EVENTS,
        **kw,
    )


def _save(path: str = "/api/encounter", method: str = "POST") -> HeldRequest:
    return HeldRequest(
        request_id="r1",
        method=method,
        url=APP + path,
        resource_type="Fetch",
        post_data=json.dumps({"note": "synthetic"}),
    )


def _answer(watch: ExceptionWatch, key, choice: str) -> None:
    card = watch.card
    unsigned = watch.registry.unsigned_answer(card, choice, route="cli")
    watch.registry.write_answer(
        card,
        {
            **unsigned,
            "public_key": public_hex(key),
            "signature": sign_bytes(key, answer_message(unsigned)),
        },
    )


def _events(tmp_path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (tmp_path / "run" / WATCH_EVENTS).read_text().splitlines()
    ]


def test_a_code_screen_opens_a_card_and_blocks_saves_until_a_person_answers(
    tmp_path: Path, roster
) -> None:
    watch = _watch(tmp_path, roster)
    assert watch.on_request(_save()) is None  # nothing open: the save goes on
    assert watch.observe(CODE_SCREEN) == "second_factor"
    assert watch.card is not None and watch.card.kind == "second_factor"
    # The agent never asked; it saves anyway. Blocked, whatever it does.
    assert watch.on_request(_save()) == "exception_open"
    # Reads still go through, and so does a write the guard config lists
    # as not a save (the code form a person submits in the live browser).
    assert watch.on_request(_save(method="GET")) is None
    assert watch.poll() is None  # nobody has answered
    _answer(watch, roster[1], "done_by_person")
    outcome = watch.poll()
    assert outcome is not None and outcome.proceed is True
    assert watch.card is None and not watch.holding
    assert watch.on_request(_save()) is None
    # The page still shows the code screen: not asked again until it goes.
    assert watch.observe(CODE_SCREEN) is None
    assert watch.observe(FORM_SCREEN) is None
    assert watch.observe(CODE_SCREEN) == "second_factor"
    assert watch.stop_code() == 0
    events = [e["event"] for e in _events(tmp_path)]
    assert events[:5] == [
        "watch_started",
        "card_opened",
        "request_blocked",
        "answer",
        "card_opened",
    ]


def test_a_not_a_save_write_goes_through_while_a_card_is_open(
    tmp_path: Path, roster
) -> None:
    watch = _watch(tmp_path, roster, not_saves=(NotASave("POST", "/verify"),))
    watch.observe(CODE_SCREEN)
    assert watch.on_request(_save("/verify")) is None
    assert watch.on_request(_save("/api/encounter")) == "exception_open"


@pytest.mark.parametrize("choice", ["abort", "skip"])
def test_a_stop_answer_blocks_every_later_save(
    tmp_path: Path, roster, choice: str
) -> None:
    watch = _watch(tmp_path, roster)
    watch.observe(CODE_SCREEN)
    _answer(watch, roster[1], choice)
    outcome = watch.poll()
    assert outcome is not None and outcome.proceed is False
    assert watch.stopped == choice
    assert watch.on_request(_save()) == "exception_stopped"
    # No new card after a stop: the run is over.
    assert watch.observe(SIGN_IN_SCREEN) is None
    assert watch.stop_code() == 3


def test_an_answer_off_the_roster_keeps_the_run_stopped(tmp_path: Path, roster) -> None:
    watch = _watch(tmp_path, roster)
    watch.observe(CODE_SCREEN)
    _answer(watch, Ed25519PrivateKey.generate(), "done_by_person")
    outcome = watch.poll()
    assert outcome is not None and outcome.proceed is False
    assert outcome.reason == "human_approver_unregistered"
    assert watch.on_request(_save()) == "exception_stopped"


def test_an_expired_card_stops_the_run(tmp_path: Path, roster) -> None:
    watch = _watch(tmp_path, roster, deadline_s=1)
    watch.observe(CODE_SCREEN)
    card = watch.card
    from datetime import timedelta

    from openadapt_flow.exceptions.registry import parse_time

    later = parse_time(card.deadline_at) + timedelta(seconds=1)
    outcome = watch.registry.poll(card, verify=watch.verify, now=later)
    assert outcome is not None and outcome.reason == "expired"
    # The watch reads the same record on its next look.
    assert watch.poll() is not None
    assert watch.on_request(_save()) == "exception_stopped"


def test_a_sign_in_counts_only_after_a_save(tmp_path: Path, roster) -> None:
    watch = _watch(tmp_path, roster)
    assert watch.observe(SIGN_IN_SCREEN) is None  # the ordinary start of a task
    watch.on_request(_save())
    assert watch.observe(SIGN_IN_SCREEN) == "session_expired"


def test_a_stall_is_raised_only_when_asked_for(tmp_path: Path, roster) -> None:
    clock = _Clock()
    off = _watch(tmp_path / "off", roster, clock=clock)
    on = _watch(tmp_path / "on", roster, clock=clock, stall_s=30)
    for watch in (off, on):
        watch.observe(FORM_SCREEN)
    clock.t += 31
    assert off.observe(FORM_SCREEN) is None
    assert on.observe(FORM_SCREEN) == "timeout_or_stall"


def test_the_card_carries_nothing_from_the_page(tmp_path: Path, roster) -> None:
    planted = dict(
        CODE_SCREEN, text="Verification code for Maribel Quintanilla 88812345"
    )
    watch = _watch(tmp_path, roster)
    watch.observe(planted)
    blob = b"".join(
        p.read_bytes() for p in (tmp_path / "registry").rglob("*") if p.is_file()
    )
    blob += (tmp_path / "run" / WATCH_EVENTS).read_bytes()
    assert b"Maribel" not in blob and b"88812345" not in blob


def test_a_bad_run_ref_is_refused_before_anything_runs(tmp_path: Path, roster) -> None:
    from openadapt_flow.exceptions.registry import ExceptionRegistryError

    key_dir, key = roster
    with pytest.raises(ExceptionRegistryError):
        ExceptionWatch(
            ExceptionRegistry(tmp_path / "registry"),
            run_ref="../elsewhere",
            verify=roster_verifier(key_dir, public_hex(key)),
            app_origin=APP,
        )
