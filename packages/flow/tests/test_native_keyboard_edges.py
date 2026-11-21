"""One lease guard model for physical key edges and uncertain cleanup."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from openadapt_flow.backends.win_agent import server
from openadapt_flow.ir import ActionDeliveryReceipt


class _Keys:
    def __init__(self):
        self.held: set[str] = set()
        self.edges: list[tuple[str, str]] = []
        self.fail_edge: tuple[str, str] | None = None

    def _edge(self, kind, key):
        self.edges.append((kind, key))
        if kind == "down":
            self.held.add(key)
        else:
            self.held.discard(key)
        if self.fail_edge == (kind, key):
            self.fail_edge = None  # a throwing call might already have sent it
            raise OSError("synthetic native edge acknowledgement loss")

    def keyDown(self, key, *, _pause):
        assert _pause is False
        self._edge("down", key)

    def keyUp(self, key, *, _pause):
        assert _pause is False
        self._edge("up", key)


@pytest.mark.parametrize(
    "keys", [["enter"], ["ctrl", "x"], ["ctrl", "shift", "alt", "s"]]
)
def test_focus_loss_at_every_guard_boundary_stops_down_edges_and_releases(keys):
    for fail_at in range(4 * len(keys)):
        gui = _Keys()
        observations = 0
        edges_at_loss = None

        def guard():
            nonlocal observations, edges_at_loss
            current = observations
            observations += 1
            if current == fail_at:
                edges_at_loss = list(gui.edges)
                raise RuntimeError("synthetic target changed")

        with pytest.raises(RuntimeError, match="target changed"):
            server._guarded_key_chord(gui, keys, guard)
        assert observations == fail_at + 1
        assert not gui.held
        assert edges_at_loss is not None
        # Cleanup can only release keys; it cannot emit any new down edge.
        assert all(kind == "up" for kind, _ in gui.edges[len(edges_at_loss) :])


@given(
    st.lists(
        st.sampled_from(["ctrl", "shift", "alt", "x", "enter"]),
        min_size=1,
        max_size=4,
        unique=True,
    )
)
@settings(max_examples=35, deadline=None)
def test_successful_chord_guards_every_edge_and_releases_in_reverse_order(keys):
    gui = _Keys()
    snapshots = []
    server._guarded_key_chord(gui, keys, lambda: snapshots.append(tuple(gui.edges)))
    expected = [("down", key) for key in keys] + [("up", key) for key in reversed(keys)]
    assert gui.edges == expected and not gui.held
    assert len(snapshots) == 2 * len(expected)
    for index, edge in enumerate(expected):
        assert snapshots[2 * index] == tuple(expected[:index])
        assert snapshots[2 * index + 1] == tuple(expected[: index + 1])


@pytest.mark.parametrize(
    "failing_edge", [("down", "ctrl"), ("down", "x"), ("up", "x"), ("up", "ctrl")]
)
def test_native_edge_exception_is_uncertain_and_cleanup_releases_held_modifiers(
    failing_edge,
):
    gui = _Keys()
    gui.fail_edge = failing_edge
    with pytest.raises(OSError, match="acknowledgement loss"):
        server._guarded_key_chord(gui, ["ctrl", "x"], lambda: None)
    assert not gui.held
    failed_index = gui.edges.index(failing_edge)
    assert all(kind == "up" for kind, _ in gui.edges[failed_index + 1 :])


PAYLOADS = [
    ({"action": "press", "keys": ["enter"]}, ["enter"], "physical_press"),
    ({"action": "press", "keys": ["ctrl", "x"]}, ["ctrl", "x"], "physical_press"),
    (
        {"action": "type_text", "text": "Ab", "interval_s": 0},
        ["A", "b"],
        "physical_type_text",
    ),
    ({"action": "type_text", "text": "é"}, ["ctrl", "v"], "physical_type_text"),
]


def _install_gui(monkeypatch, gui):
    def forbidden(*args, **kwargs):
        raise AssertionError("leased input bypassed the guarded key-edge model")

    monkeypatch.setitem(
        sys.modules,
        "pyautogui",
        SimpleNamespace(
            keyDown=gui.keyDown,
            keyUp=gui.keyUp,
            write=forbidden,
            press=forbidden,
            hotkey=forbidden,
        ),
    )
    monkeypatch.setattr(server, "_foreground_window_identity", lambda: (1, 2))


@pytest.mark.parametrize("payload,keys,operation", PAYLOADS)
def test_every_leased_keyboard_entry_point_uses_real_receipt_after_guarded_edges(
    monkeypatch, payload, keys, operation
):
    gui = _Keys()
    _install_gui(monkeypatch, gui)
    observations = []
    raw = server._perform_input(
        payload,
        keyboard_guard=lambda: observations.append(tuple(gui.edges)),
        clipboard_prepared=True,
    )
    receipt = ActionDeliveryReceipt.model_validate(raw)
    assert receipt.operation == operation and receipt.native is False
    assert receipt.outcome_verified is False
    assert not gui.held and observations[-1] == tuple(gui.edges)
    assert [key for kind, key in gui.edges if kind == "down"] == keys


@pytest.mark.parametrize("payload,keys,operation", PAYLOADS)
def test_focus_change_inside_any_keyboard_entry_point_emits_no_receipt_or_later_down_edge(
    monkeypatch, payload, keys, operation
):
    gui = _Keys()
    _install_gui(monkeypatch, gui)

    def guard():
        if gui.edges:
            raise RuntimeError("synthetic focus moved inside gesture")

    with pytest.raises(RuntimeError, match="inside gesture"):
        server._perform_input(payload, keyboard_guard=guard, clipboard_prepared=True)
    assert gui.edges == [("down", keys[0]), ("up", keys[0])]
    assert not gui.held
