"""Fail-closed authoring observe projector and window_tree contracts."""

from __future__ import annotations

import ast
import json
import queue
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from openadapt_capture.authoring_project import (
    AUTHORING_OBSERVE_SCHEMA_VERSION,
    MAX_AUTHORING_NODES,
    MAX_AUTHORING_WIRE_BYTES,
    AuthoringObserve,
    AuthoringRawNode,
    AuthoringRawWindow,
    AuthoringWindow,
    AuthoringWireNode,
    mint_node_id,
    project_authoring_observe,
    project_text,
)
from openadapt_capture.input_observer.windows import (
    LLMHF_INJECTED,
    MSLLHOOKSTRUCT,
    POINT,
    WM_LBUTTONDOWN,
    _mouse_event,
)
from openadapt_capture.recorder import on_click, on_move, on_scroll
from openadapt_capture.structural import (
    StructuralBounds,
    StructuralElement,
    StructuralObservation,
    StructuralProcessIdentity,
    StructuralTreeNode,
    StructuralWindowIdentity,
)

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "openadapt_capture"
# Pinned from OpenAdaptAI/openadapt-types#35 @ 3abf298b.
SCHEMA_PATH = ROOT / "tests" / "fixtures" / "authoring-observe-v1.json"
TEST_HMAC_KEY = bytes.fromhex("11" * 32)
TEST_LEASE_NONCE = b"test-lease-nonce"
FORBIDDEN_WIRE_KEYS = {"value", "title", "screenshot", "text", "url"}


def _all_keys(obj: object) -> set[str]:
    keys: set[str] = set()
    if isinstance(obj, dict):
        keys.update(obj)
        for value in obj.values():
            keys.update(_all_keys(value))
    elif isinstance(obj, list):
        for item in obj:
            keys.update(_all_keys(item))
    return keys


def _viewport() -> StructuralBounds:
    return StructuralBounds(left=0, top=0, right=1000, bottom=1000)


def _raw_node(**kwargs) -> AuthoringRawNode:
    bounds = kwargs.pop("bounds", StructuralBounds(left=720, top=880, right=860, bottom=930))
    kwargs.setdefault("enabled", True)
    kwargs.setdefault("focused", False)
    return AuthoringRawNode(bounds=bounds, **kwargs)


def _project(raw_tree: list[AuthoringRawNode], *, backend: str = "web", **kwargs):
    return project_authoring_observe(
        backend=backend,
        provider=kwargs.pop("provider", "playwright_ax"),
        hmac_key=TEST_HMAC_KEY,
        lease_nonce=TEST_LEASE_NONCE,
        raw_tree=raw_tree,
        raw_window=kwargs.pop(
            "raw_window",
            AuthoringRawWindow(
                process_name="Chromium",
                role="window",
                title="Patient chart — do not leak",
                bounds=_viewport(),
            ),
        ),
        observed_at_ms=1_785_500_000_123,
        **kwargs,
    )


def test_schema_fixture_forbids_value_title_screenshot_and_extra_keys() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["title"] == "AuthoringObserveV1"
    assert schema["additionalProperties"] is False
    node = schema["$defs"]["AuthoringNodeV1"]
    window = schema["$defs"]["AuthoringWindowV1"]
    bounds = schema["$defs"]["AuthoringNormalizedBoundsV1"]
    assert node["additionalProperties"] is False
    assert window["additionalProperties"] is False
    assert bounds["additionalProperties"] is False
    for forbidden in ("value", "title", "screenshot"):
        assert forbidden not in schema["properties"]
        assert forbidden not in window["properties"]
        assert forbidden not in node["properties"]
    assert set(node["required"]) == {"node_id", "role", "enabled", "focused", "bounds"}
    assert schema["$defs"]["AuthoringProviderV1"]["enum"] == [
        "playwright_ax",
        "macos_ax",
        "windows_uia",
        "linux_atspi",
        "none",
    ]


def test_wire_models_reject_value_title_screenshot_and_extra_keys() -> None:
    with pytest.raises(ValidationError):
        AuthoringWireNode.model_validate(
            {"node_id": "n_abcdef01", "role": "button", "value": "secret"}
        )
    with pytest.raises(ValidationError):
        AuthoringWindow.model_validate({"title": "Patient chart"})
    with pytest.raises(ValidationError):
        AuthoringObserve.model_validate(
            {
                "schema_version": AUTHORING_OBSERVE_SCHEMA_VERSION,
                "backend": "web",
                "provider": "playwright_ax",
                "mode": "authoring",
                "agent_drive": True,
                "coach_only": False,
                "recording": False,
                "window": {},
                "tree": [],
                "truncated": False,
                "node_count": 0,
                "screenshot": "iVBOR",
            }
        )


def test_six_digit_phone_ssn_email_at_and_url_names_are_dropped() -> None:
    assert project_text("Invoice 123456") is None
    assert project_text("Call 555-123-4567") is None
    assert project_text("123-45-6789") is None
    assert project_text("user@example.com") is None
    assert project_text("ping @operator") is None
    assert project_text("https://example.invalid/path") is None
    assert project_text("  Save  now  ") == "Save now"
    assert project_text("x" * 81) is None
    assert project_text("btnContinue") == "btnContinue"

    projection = _project(
        [
            _raw_node(
                provider_runtime_id="invoice",
                role="button",
                name="Invoice 123456",
                automation_id="https://example.invalid/id",
            ),
            _raw_node(
                provider_runtime_id="ssn",
                role="text_input",
                name="123-45-6789",
                automation_id="member@clinic.invalid",
            ),
            _raw_node(
                provider_runtime_id="ok",
                role="button",
                name="Continue",
                automation_id="btnContinue",
            ),
        ]
    )
    wire = projection.wire_dict()
    names = [node.get("name") for node in wire["tree"]]
    automation_ids = [node.get("automation_id") for node in wire["tree"]]
    assert "Invoice 123456" not in names
    assert "123-45-6789" not in names
    assert "Continue" in names
    assert "btnContinue" in automation_ids
    assert all(item is None or "://" not in item for item in automation_ids)
    assert all(item is None or "@" not in item for item in automation_ids)


def test_value_title_and_screenshot_never_appear_on_the_wire() -> None:
    observation = StructuralObservation(
        provider="macos_ax",
        event_timestamp=101.0,
        observed_at=101.25,
        query_kind="window_tree",
        element=StructuralElement(role="AXWindow", name="Chart"),
        process=StructuralProcessIdentity(process_id=7, process_name="Chromium"),
        window=StructuralWindowIdentity(
            title="Patient SSN 123-45-6789",
            bounds=_viewport(),
        ),
        tree=[
            StructuralTreeNode(
                provider_runtime_id="ax-1",
                role="text_input",
                name="Note",
                value="typed secret",
                enabled=True,
                focused=False,
                bounds=StructuralBounds(left=10, top=10, right=110, bottom=40),
            )
        ],
    )
    raw = observation.model_dump(mode="json", exclude_none=True)
    assert raw["window"]["title"] == "Patient SSN 123-45-6789"
    assert raw["tree"][0]["value"] == "typed secret"

    projection = project_authoring_observe(
        observation,
        backend="macos",
        hmac_key=TEST_HMAC_KEY,
        lease_nonce=TEST_LEASE_NONCE,
    )
    wire = projection.wire_dict()
    keys = _all_keys(wire)
    assert FORBIDDEN_WIRE_KEYS.isdisjoint(keys)
    assert "title" not in wire["window"]
    assert all("value" not in node for node in wire["tree"])
    dumped = json.dumps(wire)
    assert "typed secret" not in dumped
    assert "Patient SSN" not in dumped


def test_rdp_and_citrix_return_empty_coach_only_trees() -> None:
    secret = [
        _raw_node(
            provider_runtime_id="remote",
            role="button",
            name="Continue",
            value="should not leak",
        )
    ]
    for backend in ("rdp", "citrix"):
        projection = _project(secret, backend=backend, provider="windows_uia")
        wire = projection.wire_dict()
        assert wire["backend"] == backend
        assert wire["coach_only"] is True
        assert wire["agent_drive"] is False
        assert wire["tree"] == []
        assert wire["node_count"] == 0
        assert projection.node_table == ()
        assert "value" not in _all_keys(wire)


def test_windows_native_is_coach_only_but_may_keep_a_projected_tree() -> None:
    projection = _project(
        [_raw_node(provider_runtime_id="ok", role="button", automation_id="btnContinue")],
        backend="windows",
        provider="windows_uia",
    )
    wire = projection.wire_dict()
    assert wire["coach_only"] is True
    assert wire["agent_drive"] is False
    assert wire["tree"]
    assert wire["tree"][0]["automation_id"] == "btnContinue"


def test_node_id_is_hmac_prefix_and_missing_runtime_id_mints_anon() -> None:
    expected = mint_node_id(
        hmac_key=TEST_HMAC_KEY,
        lease_nonce=TEST_LEASE_NONCE,
        provider_runtime_id="ax-elem-1",
    )
    assert re.fullmatch(r"n_[0-9a-f]{8}", expected)
    projection = _project(
        [
            _raw_node(provider_runtime_id="ax-elem-1", role="button"),
            _raw_node(role="button", name="Save"),
        ]
    )
    assert projection.observe.tree[0].node_id == expected
    anon = mint_node_id(
        hmac_key=TEST_HMAC_KEY,
        lease_nonce=TEST_LEASE_NONCE,
        provider_runtime_id="anon:1",
    )
    assert projection.observe.tree[1].node_id == anon
    assert projection.node_table[0].provider_runtime_id == "ax-elem-1"
    assert projection.node_table[1].provider_runtime_id is None


def test_caps_at_200_nodes_and_32kib(monkeypatch: pytest.MonkeyPatch) -> None:
    many = [
        _raw_node(provider_runtime_id=f"n{i}", role="button", name=f"Action{i}")
        for i in range(MAX_AUTHORING_NODES + 25)
    ]
    projection = _project(many)
    assert projection.observe.truncated is True
    assert projection.observe.node_count == MAX_AUTHORING_NODES
    assert len(projection.observe.tree) == MAX_AUTHORING_NODES

    monkeypatch.setattr(
        "openadapt_capture.authoring_project.MAX_AUTHORING_WIRE_BYTES",
        900,
    )
    bulky = [
        _raw_node(
            provider_runtime_id=f"b{i}",
            role="button",
            name="ContinueActionLabel",
            automation_id="btnContinueAction",
            class_name="ChromeButtonClass",
        )
        for i in range(40)
    ]
    bulky_projection = _project(bulky)
    wire = bulky_projection.wire_dict()
    assert bulky_projection.observe.truncated is True
    encoded = json.dumps(wire, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 900


def test_empty_projection_never_falls_back_to_raw() -> None:
    projection = _project(
        [
            _raw_node(
                provider_runtime_id="secret",
                name="123-45-6789",
                value="typed secret",
                title="Patient",
                bounds=None,
            )
        ]
    )
    wire = projection.wire_dict()
    assert wire["tree"] == []
    assert wire["reason"] == "empty_projection"
    assert "typed secret" not in json.dumps(wire)
    assert FORBIDDEN_WIRE_KEYS.isdisjoint(_all_keys(wire))


def test_invalid_process_name_is_dropped() -> None:
    projection = _project(
        [_raw_node(provider_runtime_id="ok", role="button")],
        raw_window=AuthoringRawWindow(
            process_name="C:\\secret.exe",
            role="window",
            title="do not leak",
            bounds=_viewport(),
        ),
    )
    assert projection.observe.window is None
    assert "window" not in projection.wire_dict()


def test_normalized_bounds_use_the_top_level_viewport() -> None:
    projection = _project(
        [
            _raw_node(
                provider_runtime_id="ok",
                role="button",
                bounds=StructuralBounds(left=720, top=880, right=860, bottom=930),
            )
        ]
    )
    bounds = projection.observe.tree[0].bounds
    assert bounds is not None
    assert bounds.x == pytest.approx(0.72)
    assert bounds.y == pytest.approx(0.88)
    assert bounds.w == pytest.approx(0.14)
    assert bounds.h == pytest.approx(0.05)
    pixels = projection.node_table[0].backend_pixels
    assert pixels is not None
    assert (pixels.x, pixels.y, pixels.w, pixels.h) == (720, 880, 140, 50)


def test_package_has_no_record_injected_api() -> None:
    for path in PACKAGE.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                assert node.name != "record_injected"
            if isinstance(node, ast.arg):
                assert node.arg != "record_injected"


def test_injected_clicks_still_do_not_persist() -> None:
    events: queue.Queue = queue.Queue()
    on_click(
        events,
        None,
        25,
        35,
        "left",
        True,
        injected=True,
        timestamp=101.0,
    )
    on_move(events, None, 26, 36, injected=True, timestamp=101.1)
    on_scroll(
        events,
        None,
        26,
        36,
        0,
        1,
        injected=True,
        timestamp=101.2,
    )
    assert events.empty()


def test_windows_llmhf_injected_still_returns_none() -> None:
    payload = MSLLHOOKSTRUCT(pt=POINT(12, 34), flags=LLMHF_INJECTED)
    assert (
        _mouse_event(
            WM_LBUTTONDOWN,
            payload,
            capture_mouse_moves=True,
        )
        is None
    )


def test_playwright_shaped_tree_projects_without_native_capture() -> None:
    projection = _project(
        [
            AuthoringRawNode(
                provider_runtime_id="btnContinue",
                role="button",
                control_type="button",
                automation_id="btnContinue",
                enabled=True,
                focused=False,
                bounds=StructuralBounds(left=720, top=880, right=860, bottom=930),
            )
        ]
    )
    wire = projection.wire_dict()
    assert wire["schema_version"] == AUTHORING_OBSERVE_SCHEMA_VERSION
    assert wire["backend"] == "web"
    assert wire["provider"] == "playwright_ax"
    assert wire["agent_drive"] is True
    assert wire["coach_only"] is False
    assert wire["tree"][0]["automation_id"] == "btnContinue"
    assert wire["window"]["process_name"] == "Chromium"
    assert MAX_AUTHORING_WIRE_BYTES == 32 * 1024


def test_native_roles_map_onto_element_role() -> None:
    macos = _project(
        [_raw_node(provider_runtime_id="ax", role="AXButton", name="Save")],
        backend="macos",
        provider="macos_ax",
    )
    assert macos.observe.tree[0].role == "button"
    assert macos.observe.tree[0].name == "Save"

    windows = _project(
        [
            _raw_node(
                provider_runtime_id="edit",
                role="Edit",
                control_type="Edit",
                name="Note",
            )
        ],
        backend="windows",
        provider="windows_uia",
    )
    assert windows.observe.tree[0].role == "text_input"

    linux = _project(
        [_raw_node(provider_runtime_id="btn", role="push button", name="Save")],
        backend="linux",
        provider="linux_atspi",
    )
    assert linux.observe.tree[0].role == "button"

    web = _project(
        [_raw_node(provider_runtime_id="box", role="textbox", name="Note")],
        backend="web",
        provider="playwright_ax",
    )
    assert web.observe.tree[0].role == "text_input"


def test_unmapped_native_role_fail_closes_to_empty_projection() -> None:
    projection = _project(
        [_raw_node(provider_runtime_id="ax", role="AXPrivateMysteryRole", name="Save")]
    )
    wire = projection.wire_dict()
    assert wire["tree"] == []
    assert wire["reason"] == "empty_projection"
    assert FORBIDDEN_WIRE_KEYS.isdisjoint(_all_keys(wire))


def test_credentials_never_appear_as_values_on_the_wire() -> None:
    projection = _project(
        [
            _raw_node(
                provider_runtime_id="password",
                role="AXSecureTextField",
                name="Password",
                value="typed-secret",
            )
        ],
        backend="macos",
        provider="macos_ax",
    )
    wire = projection.wire_dict()
    assert wire["tree"][0]["role"] == "text_input"
    assert "value" not in wire["tree"][0]
    dumped = json.dumps(wire)
    assert "typed-secret" not in dumped
    assert FORBIDDEN_WIRE_KEYS.isdisjoint(_all_keys(wire))


def test_projected_wire_keys_match_pinned_types_schema() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    projection = _project(
        [
            _raw_node(
                provider_runtime_id="btnContinue",
                role="button",
                control_type="button",
                automation_id="btnContinue",
            )
        ]
    )
    wire = projection.wire_dict()
    assert set(wire) <= set(schema["properties"])
    node_schema = schema["$defs"]["AuthoringNodeV1"]
    window_schema = schema["$defs"]["AuthoringWindowV1"]
    assert set(wire["window"]) <= set(window_schema["properties"])
    for required in window_schema["required"]:
        assert required in wire["window"]
    for node in wire["tree"]:
        assert set(node) <= set(node_schema["properties"])
        for required in node_schema["required"]:
            assert required in node
    AuthoringObserve.model_validate(wire)

