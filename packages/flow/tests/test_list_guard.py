"""CopyGuard wired to a backend and taught interstitials."""

from __future__ import annotations

from typing import Optional

import pytest

from openadapt_flow.ir import Anchor, Interstitial, Predicate, PredicateKind
from openadapt_flow.runtime.list_guard import close_locator, guard_for_backend
from openadapt_flow.runtime.list_source import ListHalt

CLOSE_POINT = (612, 140)


class FakeBackend:
    """Frames are the text on screen, encoded as bytes."""

    def __init__(self, app: Optional[str] = "Ledger", modal: Optional[str] = None):
        self.app = app
        self.modal = modal
        self.escape_clears = True
        self.keys: list[str] = []
        self.clicks: list[tuple[int, int]] = []
        self.close_clears = True

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

    def click(self, x: int, y: int) -> None:
        self.clicks.append((x, y))
        if (x, y) == CLOSE_POINT and self.close_clears:
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


CLOSE_ANCHOR = Anchor(
    template="templates/notice_close.png",
    region=(600, 128, 24, 24),
    click_point=CLOSE_POINT,
)
UPDATE = Interstitial(
    name="update available",
    detect=present("Update available"),
    dismiss_anchor=CLOSE_ANCHOR,
    risk="reversible",
    consequential=False,
    clearance=present("Accounts worktable"),
)


def find_close(item: Interstitial, frame: bytes) -> Optional[tuple[int, int]]:
    return CLOSE_POINT if item.name in ("update available",) else None


def test_known_dialog_is_closed_with_its_close_button():
    be = FakeBackend(modal="Update available")
    g = guard_for_backend(
        be, "Ledger", interstitials=[UPDATE], holds=holds, locate_close=find_close
    )
    g.check()
    assert be.clicks == [CLOSE_POINT] and be.keys == []
    assert g.dismissed == ["update available"]


def test_close_button_that_does_not_clear_halts_after_one_click():
    be = FakeBackend(modal="Update available")
    be.close_clears = False
    g = guard_for_backend(
        be, "Ledger", interstitials=[UPDATE], holds=holds, locate_close=find_close
    )
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_not_cleared" and be.clicks == [CLOSE_POINT]


def test_close_button_not_found_halts_without_clicking():
    be = FakeBackend(modal="Update available")
    g = guard_for_backend(
        be,
        "Ledger",
        interstitials=[UPDATE],
        holds=holds,
        locate_close=lambda _item, _frame: None,
    )
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_present" and be.clicks == []


def test_close_button_dialog_halts_without_a_locator():
    be = FakeBackend(modal="Update available")
    g = guard_for_backend(be, "Ledger", interstitials=[UPDATE], holds=holds)
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_present" and be.clicks == []


def test_undeclared_risk_is_not_clicked():
    be = FakeBackend(modal="Update available")
    risky = UPDATE.model_copy(update={"consequential": None})
    g = guard_for_backend(
        be, "Ledger", interstitials=[risky], holds=holds, locate_close=find_close
    )
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_present" and be.clicks == []


def test_unknown_dialog_still_halts():
    be = FakeBackend(modal="Overwrite record?")
    g = guard_for_backend(
        be,
        "Ledger",
        interstitials=[UPDATE],
        holds=holds,
        locate_close=find_close,
        modal_open=lambda: "Overwrite record?" if be.modal else None,
    )
    with pytest.raises(ListHalt) as exc:
        g.check()
    assert exc.value.reason == "modal_present" and "Overwrite" in exc.value.detail
    assert be.clicks == [] and be.keys == []


def test_close_locator_uses_the_template_without_a_model():
    import io
    from types import SimpleNamespace

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (800, 600), "white").save(buf, format="PNG")
    frame = buf.getvalue()
    seen: list[str] = []

    class Vision:
        def find_template(self, screen, tmpl, *, search_region=None, **_kw):
            seen.append("template")
            return SimpleNamespace(
                point=CLOSE_POINT,
                region=(600, 128, 24, 24),
                confidence=0.99,
            )

        def find_text(self, screen, text, **_kw):
            return None

    loads: list[str] = []

    def loader(rel: str) -> Optional[bytes]:
        loads.append(rel)
        return frame

    point = close_locator(Vision(), loader)(UPDATE, frame)
    assert loads == ["templates/notice_close.png"]
    assert seen and point == CLOSE_POINT
