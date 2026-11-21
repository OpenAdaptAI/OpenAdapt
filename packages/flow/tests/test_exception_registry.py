"""The exception registry: cards, signed answers, expiry, and what never proceeds.

Synthetic data only. The approver roster is a fresh one in a temporary
folder, with a root key made here; nothing reads the real ~/.openadapt.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_types.exception_card import (
    EXCEPTION_KINDS,
    ExceptionAnswerV1,
    ExceptionCardV1,
    answer_message,
)

from openadapt_flow import privacy
from openadapt_flow.approval_mode.approver_roster import (
    ROOT_ENV,
    file_key_signer,
    init_root,
)
from openadapt_flow.exceptions import cli as exceptions_cli
from openadapt_flow.exceptions.registry import (
    CHOICES,
    GENERIC_SUMMARIES,
    ExceptionRegistry,
    ExceptionRegistryError,
    iso,
    roster_verifier,
)
from openadapt_flow.execute.keys import sign_bytes
from openadapt_flow.jit.authority import public_hex

#: Made-up identifiers planted in what an agent says; none may reach a card.
PLANTED = ("Maribel Quintanilla", "1961-04-09", "MRN 88812345", "maribel@example.test")
SUMMARY = (
    "Code prompt for Maribel Quintanilla, born 1961-04-09, MRN 88812345, "
    "mail maribel@example.test"
)

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
    b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


class FakeScrubber:
    """Replaces the planted name; the registry masks digits and dates itself."""

    def scrub_text(self, text: str) -> str:
        return text.replace("Maribel Quintanilla", "<PERSON>")

    def scrub_image(self, image, fill_color=None):  # noqa: ARG002
        return image


@pytest.fixture(autouse=True)
def _scrubber():
    privacy.set_text_scrubber(FakeScrubber())
    yield
    privacy.reset_scrubbers()


@pytest.fixture
def registry(tmp_path: Path) -> ExceptionRegistry:
    return ExceptionRegistry(tmp_path / "exceptions")


@pytest.fixture
def roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    key_dir = tmp_path / "keys"
    key = Ed25519PrivateKey.generate()
    init_root(key_dir, principal="front-desk", public_key=public_hex(key), sign=file_key_signer(key))
    monkeypatch.setenv(ROOT_ENV, public_hex(key))
    return key_dir, key


def _signed(registry, card, answer: str, key, *, now=None) -> dict:
    unsigned = registry.unsigned_answer(card, answer, route="cli", now=now)
    return {
        **unsigned,
        "public_key": public_hex(key),
        "signature": sign_bytes(key, answer_message(unsigned)),
    }


def _verify(roster):
    key_dir, key = roster
    return roster_verifier(key_dir, public_hex(key))


def _no_sleep(_s: float) -> None:
    return None


# -- cards -----------------------------------------------------------------------


def test_a_card_is_sealed_private_and_names_closed_choices(registry):
    card = registry.open("run-1", "second_factor", "A code prompt appeared")

    path = registry.root / "run-1" / f"card-{card.card_id[7:]}.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    on_disk = ExceptionCardV1.model_validate(json.loads(path.read_text()))
    assert on_disk == card
    assert [c.id for c in card.choices] == ["done_by_person", "skip", "abort"]
    assert card.kind == "second_factor"
    assert registry.open_cards() == [card]


def test_every_kind_has_choices_and_no_card_offers_a_write_on_its_own():
    assert set(CHOICES) == set(EXCEPTION_KINDS)
    for kind, choices in CHOICES.items():
        assert choices, kind
        assert len(choices) <= 5


@pytest.mark.parametrize("kind", ["unknown", "SECOND_FACTOR", ""])
def test_an_unknown_kind_is_refused(registry, kind):
    with pytest.raises(ExceptionRegistryError) as err:
        registry.open("run-1", kind, "x")
    assert err.value.code == "kind_unknown"


@pytest.mark.parametrize("run_ref", ["../escape", "a/b", "", ".hidden", "x" * 200])
def test_a_run_reference_never_becomes_a_path_outside_the_registry(registry, run_ref):
    with pytest.raises(ExceptionRegistryError) as err:
        registry.open(run_ref, "cannot_complete", "x")
    assert err.value.code == "run_ref_invalid"


def test_a_tampered_card_is_not_listed(registry):
    card = registry.open("run-1", "cannot_complete", "Stuck")
    path = registry.root / "run-1" / f"card-{card.card_id[7:]}.json"
    data = json.loads(path.read_text())
    data["summary"] = "Something else"
    os.chmod(path, 0o600)
    path.unlink()
    path.write_text(json.dumps(data))
    assert registry.open_cards() == []


# -- scrubbing --------------------------------------------------------------------


def _bytes_under(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


def test_the_summary_is_scrubbed_and_cut(registry):
    card = registry.open("run-1", "second_factor", SUMMARY + " " + "x" * 400, task_id="T-12345678")

    assert len(card.summary) <= 200
    blob = _bytes_under(registry.root)
    for planted in PLANTED:
        assert planted.encode() not in blob
    assert b"<PERSON>" in blob
    assert card.task_id == "T-[number]"


def test_without_a_scrubber_the_card_says_only_the_kinds_own_words(registry):
    privacy.set_text_scrubber(None)
    card = registry.open("run-1", "session_expired", SUMMARY)

    assert card.summary == GENERIC_SUMMARIES["session_expired"]
    blob = _bytes_under(registry.root)
    for planted in PLANTED:
        assert planted.encode() not in blob


def test_a_broken_scrubber_is_no_scrubber(registry):
    class Broken:
        def scrub_text(self, text):
            raise RuntimeError("model missing")

    privacy.set_text_scrubber(Broken())
    card = registry.open("run-1", "cannot_complete", SUMMARY)
    assert card.summary == GENERIC_SUMMARIES["cannot_complete"]


def test_a_screenshot_is_kept_only_as_a_scrubbed_private_copy(registry, tmp_path):
    shot = tmp_path / "screen.png"
    shot.write_bytes(PNG)
    card = registry.open("run-1", "unexpected_dialog", "A dialog", screenshot_path=str(shot))

    assert card.screenshot_ref is not None
    copy = registry.root / "run-1" / card.screenshot_ref
    assert copy.read_bytes().startswith(b"\x89PNG")
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600


def test_no_screenshot_without_a_scrubber_a_png_or_a_plain_file(registry, tmp_path):
    shot = tmp_path / "screen.png"
    shot.write_bytes(PNG)
    link = tmp_path / "link.png"
    link.symlink_to(shot)
    text = tmp_path / "notes.png"
    text.write_text("not an image")

    assert registry.open("r", "cannot_complete", "x", screenshot_path=str(link)).screenshot_ref is None
    assert registry.open("r", "cannot_complete", "y", screenshot_path=str(text)).screenshot_ref is None
    assert registry.open("r", "cannot_complete", "z", screenshot_path=str(tmp_path)).screenshot_ref is None
    privacy.set_text_scrubber(None)
    assert registry.open("r", "cannot_complete", "w", screenshot_path=str(shot)).screenshot_ref is None


# -- answers -----------------------------------------------------------------------


def test_a_signed_done_by_person_lets_the_run_go_on(registry, roster):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt")
    registry.write_answer(card, _signed(registry, card, "done_by_person", key))

    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)

    assert outcome.proceed is True
    assert outcome.answer == "done_by_person"
    assert outcome.opened_at == card.opened_at
    assert outcome.decided_at is not None
    assert (registry.root / "run-1" / f"consumed-{card.card_id[7:]}.json").exists()
    assert registry.open_cards() == []


@pytest.mark.parametrize("answer", ["skip", "abort"])
def test_a_signed_stop_answer_stops_the_run(registry, roster, answer):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt")
    registry.write_answer(card, _signed(registry, card, answer, key))

    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False
    assert outcome.answer == answer


def test_an_answer_from_a_key_off_the_roster_never_proceeds(registry, roster):
    card = registry.open("run-1", "second_factor", "A code prompt")
    stranger = Ed25519PrivateKey.generate()
    registry.write_answer(card, _signed(registry, card, "done_by_person", stranger))

    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False
    assert outcome.reason == "human_approver_unregistered"


def test_a_forged_signature_never_proceeds(registry, roster):
    _key_dir, key = roster
    card = registry.open("run-1", "timeout_or_stall", "Nothing moves")
    answer = _signed(registry, card, "skip", key)
    answer["answer"] = "continue"  # the signature covered "skip"
    registry.write_answer(card, answer)

    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False
    assert outcome.reason == "human_answer_signature_invalid"


def test_without_a_pinned_root_no_answer_counts(registry, roster):
    key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt")
    registry.write_answer(card, _signed(registry, card, "done_by_person", key))

    outcome = registry.wait(card, verify=roster_verifier(key_dir, None), sleep=_no_sleep)
    assert outcome.proceed is False


def test_a_choice_the_card_doesnt_offer_never_proceeds(registry, roster):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt")
    with pytest.raises(ExceptionRegistryError):
        registry.unsigned_answer(card, "continue", route="cli")
    unsigned = {
        "schema": "openadapt.exception-answer/v1",
        "card_id": card.card_id,
        "kind": card.kind,
        "opened_at": card.opened_at,
        "decided_at": iso(datetime.now(timezone.utc)),
        "answer": "continue",
        "approver_kind": "person",
        "route": "cli",
        "reason": None,
    }
    registry.write_answer(
        card,
        {**unsigned, "public_key": public_hex(key), "signature": sign_bytes(key, answer_message(unsigned))},
    )
    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False
    assert outcome.reason == "choice_not_offered"


def test_an_answer_decided_after_the_deadline_never_proceeds(registry, roster):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt", deadline_s=5)
    late = datetime.now(timezone.utc) + timedelta(seconds=60)
    registry.write_answer(card, _signed(registry, card, "done_by_person", key, now=late))

    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False
    assert outcome.reason == "answer_late"


def test_an_answer_is_used_once(registry, roster):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt")
    registry.write_answer(card, _signed(registry, card, "done_by_person", key))
    assert registry.wait(card, verify=_verify(roster), sleep=_no_sleep).proceed is True

    again = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert again.proceed is False
    assert again.reason == "answer_already_used"


def test_a_card_has_one_answer(registry, roster):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt")
    registry.write_answer(card, _signed(registry, card, "abort", key))
    with pytest.raises(ExceptionRegistryError) as err:
        registry.write_answer(card, _signed(registry, card, "done_by_person", key))
    assert err.value.code == "already_answered"


def test_an_unreadable_answer_stops_the_run(registry, roster):
    card = registry.open("run-1", "second_factor", "A code prompt")
    (registry.root / "run-1" / f"answer-{card.card_id[7:]}.json").write_text("{not json")

    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False
    assert outcome.reason == "answer_unreadable"


def test_an_unsigned_answer_that_claims_to_be_a_persons_is_unreadable(registry, roster):
    card = registry.open("run-1", "second_factor", "A code prompt")
    answer = {
        "schema": "openadapt.exception-answer/v1",
        "card_id": card.card_id,
        "kind": card.kind,
        "opened_at": card.opened_at,
        "decided_at": iso(datetime.now(timezone.utc)),
        "answer": "done_by_person",
        "approver_kind": "person",
        "route": "cli",
    }
    (registry.root / "run-1" / f"answer-{card.card_id[7:]}.json").write_text(json.dumps(answer))
    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is False


# -- expiry ------------------------------------------------------------------------


def test_expiry_writes_a_decline_with_no_decision_time(registry, roster):
    card = registry.open("run-1", "second_factor", "A code prompt", deadline_s=60)
    after = datetime.now(timezone.utc) + timedelta(seconds=61)
    beats: list[float] = []

    outcome = registry.wait(
        card,
        verify=_verify(roster),
        clock=lambda: after,
        sleep=_no_sleep,
        heartbeat=lambda _id, left: beats.append(left),
    )

    assert outcome.proceed is False
    assert outcome.answer == "decline"
    assert outcome.reason == "expired"
    assert outcome.decided_at is None
    record = ExceptionAnswerV1.model_validate(
        json.loads((registry.root / "run-1" / f"answer-{card.card_id[7:]}.json").read_text())
    )
    assert record.approver_kind == "expiry"
    assert record.decided_at is None
    assert record.reason == "expired"
    assert record.opened_at == card.opened_at


def test_the_wait_beats_and_then_expires(registry, roster):
    card = registry.open("run-1", "timeout_or_stall", "Stalled", deadline_s=60)
    times = iter(
        [datetime.now(timezone.utc)] * 3 + [datetime.now(timezone.utc) + timedelta(seconds=120)] * 5
    )
    beats: list[float] = []
    outcome = registry.wait(
        card,
        verify=_verify(roster),
        clock=lambda: next(times),
        sleep=_no_sleep,
        heartbeat_s=0,
        heartbeat=lambda _id, left: beats.append(left),
    )
    assert outcome.reason == "expired"
    assert beats and all(b > 0 for b in beats)


def test_a_person_who_answers_at_the_deadline_wins_the_race(registry, roster, monkeypatch):
    _key_dir, key = roster
    card = registry.open("run-1", "second_factor", "A code prompt", deadline_s=60)
    answer = _signed(registry, card, "done_by_person", key)
    original = registry._expire

    def expire_after_the_person(c):
        registry.write_answer(c, answer)  # lands first
        original(c)  # then the registry's expiry link fails

    monkeypatch.setattr(registry, "_expire", expire_after_the_person)
    after = datetime.now(timezone.utc) + timedelta(seconds=61)
    outcome = registry.wait(card, verify=_verify(roster), clock=lambda: after, sleep=_no_sleep)
    assert outcome.proceed is True


def test_the_heartbeat_goes_to_stderr_never_stdout(registry, roster, capsys):
    card = registry.open("run-1", "timeout_or_stall", "Stalled", deadline_s=60)
    times = iter([datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(seconds=99)] * 3)
    registry.wait(card, verify=_verify(roster), clock=lambda: next(times), sleep=_no_sleep)
    out, err = capsys.readouterr()
    assert out == ""
    assert "waiting for a person" in err


# -- CLI ---------------------------------------------------------------------------


def _encrypted(tmp_path: Path, key: Ed25519PrivateKey, passphrase: str) -> Path:
    path = tmp_path / "approver.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase.encode()),
        )
    )
    path.chmod(0o600)
    return path


def _parse(argv: list[str]):
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    exceptions_cli.register(sub)
    return parser.parse_args(argv)


def test_the_cli_answers_with_a_key_on_the_roster(registry, roster, tmp_path, monkeypatch, capsys):
    key_dir, key = roster
    monkeypatch.setenv("OPENADAPT_JIT_KEY_PASSPHRASE", "synthetic-passphrase")
    pem = _encrypted(tmp_path, key, "synthetic-passphrase")
    card = registry.open("run-1", "second_factor", "A code prompt")

    args = _parse(["exceptions", "list", "--registry", str(registry.root), "--json"])
    assert args.func(args) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [c["card_id"] for c in listed["cards"]] == [card.card_id]

    args = _parse(
        [
            "exceptions", "answer", card.card_id, "done_by_person",
            "--registry", str(registry.root), "--key", str(pem), "--key-dir", str(key_dir),
        ]
    )
    assert args.func(args) == 0
    outcome = registry.wait(card, verify=_verify(roster), sleep=_no_sleep)
    assert outcome.proceed is True


def test_the_cli_refuses_a_key_off_the_roster(registry, roster, tmp_path, monkeypatch, capsys):
    key_dir, _key = roster
    monkeypatch.setenv("OPENADAPT_JIT_KEY_PASSPHRASE", "synthetic-passphrase")
    pem = _encrypted(tmp_path, Ed25519PrivateKey.generate(), "synthetic-passphrase")
    card = registry.open("run-1", "second_factor", "A code prompt")

    args = _parse(
        [
            "exceptions", "answer", card.card_id, "done_by_person",
            "--registry", str(registry.root), "--key", str(pem), "--key-dir", str(key_dir),
        ]
    )
    assert args.func(args) == 2
    assert "human_approver_unregistered" in capsys.readouterr().err
    assert not (registry.root / "run-1" / f"answer-{card.card_id[7:]}.json").exists()


def test_the_cli_refuses_a_choice_the_card_doesnt_offer(registry, roster, tmp_path, monkeypatch):
    key_dir, key = roster
    monkeypatch.setenv("OPENADAPT_JIT_KEY_PASSPHRASE", "synthetic-passphrase")
    pem = _encrypted(tmp_path, key, "synthetic-passphrase")
    card = registry.open("run-1", "second_factor", "A code prompt")
    args = _parse(
        [
            "exceptions", "answer", card.card_id, "continue",
            "--registry", str(registry.root), "--key", str(pem), "--key-dir", str(key_dir),
        ]
    )
    assert args.func(args) == 2


# -- the file Desktop reads ---------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "exception_card_second_factor.json"


def test_the_desktop_fixture_is_what_flow_writes(registry):
    """Desktop's suite reads this file; Flow must still write exactly it."""

    opened = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    card = registry.open(
        "fixture-run",
        "second_factor",
        "The sign-in page asks for a six-digit code",
        task_id="intake",
        now=opened,
    )
    written = json.loads(
        (registry.root / "fixture-run" / f"card-{card.card_id[7:]}.json").read_text()
    )
    if os.environ.get("OPENADAPT_REGENERATE_FIXTURES") == "1":
        FIXTURE.write_text(json.dumps(written, sort_keys=True, indent=2) + "\n")
    assert written == json.loads(FIXTURE.read_text())
