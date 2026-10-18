"""CopyGuard wired to a backend and taught interstitials."""

from __future__ import annotations

from typing import Optional

import pytest

from openadapt_flow.ir import Interstitial, Predicate, PredicateKind
from openadapt_flow.runtime.list_guard import guard_for_backend
from openadapt_flow.runtime.list_source import ListHalt


class FakeBackend:
    """Frames are the text on screen, encoded as bytes."""

    def __init__(self, app: Optional[str] = "Ledger", modal: Optional[str] = None):
        self.app = app
        self.modal = modal
        self.escape_clears = True
        self.keys: list[str] = []

    def application_identity(self) -> Optional[str]:
        return self.app

    def screenshot(self) -> bytes:
        text = "Accounts worktable"
        if self.modal:
            text += " " + self.modal
        return text.encode()

    def press(self, key: str) -> None:
        self.keys.append(key)
        if key == "Escape" and self.escape_clears:
            self.modal = None


def holds(pred: Predicate, frame: bytes) -> bool:
    text = frame.decode()
    if pred.kind is PredicateKind.TEXT_PRESENT:
        return bool(pred.text) and pred.text in text
    if pred.kind is PredicateKind.TEXT_ABSENT:
        return bool(pred.text) and pred.text not in text
    return False


def present(t: str) -> Predicate:
    return Predicate(kind=PredicateKind.TEXT_PRESENT, text=t)


NOTICE = Interstitial(
    name="session notice",
    detect=present("Session notice"),
    dismiss_key="Escape",
    risk="reversible",
    consequential=False,
    clearance=present("Accounts worktable"),
)
LOCKED = Interstitial(name="record locked", detect=present("Record locked"))


def test_clean_screen_passes():
    guard_for_backend(
        FakeBackend(), "Ledger", interstitials=[NOTICE], holds=holds
    ).check()


def test_focus_lost_halts():
    with pytest.raises(ListHalt) as exc:
        guard_for_backend(FakeBackend(app="Mail"), "Ledger").check()
    assert exc.value.reason == "focus_lost"


def test_unknown_focus_halts():
    with pytest.raises(ListHalt) as exc:
        guard_for_backend(FakeBackend(app=None), "Ledger").check()
    assert exc.value.reason == "focus_lost"


def test_reversible_notice_is_dismissed_once():
    be = FakeBackend(modal="Session notice")
    g = guard_for_backend(be, "Ledger", interstitials=[NOTICE], holds=holds)
    g.check()
    assert be.keys == ["Escape"] and g.dismissed == ["session notice"]


def test_notice_that_does_not_clear_halts_after_one_key():
    be = FakeBackend(modal="Session notice")
    be.escape_clears = False
    g = guard_for_backend(be, "Ledger", interstitials=[NOTICE], holds=holds)
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_not_cleared" and be.keys == ["Escape"]


def test_blocking_interstitial_halts_by_name():
    be = FakeBackend(modal="Record locked")
    g = guard_for_backend(be, "Ledger", interstitials=[NOTICE, LOCKED], holds=holds)
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_present" and "record locked" in exc.value.detail
    assert be.keys == []


def test_interstitials_need_an_evaluator():
    with pytest.raises(ValueError):
        guard_for_backend(FakeBackend(), "Ledger", interstitials=[NOTICE])
