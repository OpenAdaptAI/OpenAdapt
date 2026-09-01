"""Authoring MCP wire contracts refuse extra keys and omitted PHI fields."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest
from pydantic import ValidationError

from openadapt_types import (
    AUTHORING_BIND_SCHEMA,
    AUTHORING_COMMAND_SCHEMA,
    AUTHORING_MAX_COMMAND_BYTES,
    AUTHORING_MAX_OBSERVE_BYTES,
    AUTHORING_OBSERVE_SCHEMA,
    OBSERVE_SCHEMA_VERSION,
    AuthoringAllowStateV1,
    AuthoringBindClaimV1,
    AuthoringBindMintV1,
    AuthoringBindPackResultV1,
    AuthoringBindV1,
    AuthoringCallbackV1,
    AuthoringClickArgsV1,
    AuthoringCommandLookupArgsV1,
    AuthoringCommandLookupV1,
    AuthoringCommandV1,
    AuthoringCompileResultV1,
    AuthoringEnqueueAcceptedV1,
    AuthoringErrorResultV1,
    AuthoringGetCoachResultV1,
    AuthoringHaltResultV1,
    AuthoringHostedClickArgsV1,
    AuthoringHostedPackArgsV1,
    AuthoringInFlightV1,
    AuthoringNormalizedBoundsV1,
    AuthoringNotBoundV1,
    AuthoringObserveV1,
    AuthoringPauseResultV1,
    AuthoringPollRequestV1,
    ComputerState,
    ElementRole,
    UINode,
    parse_authoring_bind_token,
    parse_authoring_lease_secret,
    parse_authoring_runner_uri,
)
from openadapt_types.authoring import AuthoringTokenError


VALID_BIND = "oab_" + "G" * 43
VALID_LEASE = "oals_" + "a" * 64
VALID_PACK = "p.abcdefghijkl"
VALID_COMMAND_ID = "cmd_01JABCDEFGHJKMNPQRSTVWXYZ0"
VALID_NODE = "n_9f2c3a10"
VALID_SUB = "b" * 64
VALID_CLIENT = "c" * 64
VALID_DEEP_LINK = (
    f"openadapt://runner?pack={VALID_PACK}&bind={VALID_BIND}"
    "&origin=https%3A%2F%2Fopenadapt.ai"
)
FORBIDDEN_FIELDS = {
    "value": "secret-ssn",
    "title": "Patient chart",
    "screenshot": "data:image/png;base64,secret",
    "text": "typed note",
    "backend_pixels": {"x": 920, "y": 640, "w": 180, "h": 36},
    "provider_runtime_id": "ax-elem-secret",
}


def _bounds() -> dict[str, float]:
    return {"x": 0.72, "y": 0.88, "w": 0.14, "h": 0.05}


def _observe_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": AUTHORING_OBSERVE_SCHEMA,
        "backend": "web",
        "provider": "playwright_ax",
        "mode": "authoring",
        "agent_drive": True,
        "coach_only": False,
        "recording": False,
        "window": {
            "process_name": "Chromium",
            "role": "window",
            "bounds": {"x": 0.0, "y": 0.0, "w": 1.0, "h": 1.0},
        },
        "tree": [
            {
                "node_id": VALID_NODE,
                "role": "button",
                "control_type": "button",
                "automation_id": "btnContinue",
                "enabled": True,
                "focused": False,
                "bounds": _bounds(),
            }
        ],
        "truncated": False,
        "node_count": 1,
    }
    payload.update(updates)
    return payload


def _command_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": AUTHORING_COMMAND_SCHEMA,
        "command_id": VALID_COMMAND_ID,
        "pack_id": VALID_PACK,
        "tool": "click",
        "args": {"node_id": VALID_NODE},
        "enqueued_at": "2026-08-31T12:00:00Z",
        "expires_at": "2026-08-31T12:15:00Z",
        "status": "pending",
        "result": None,
        "oauth_sub_sha256": VALID_SUB,
        "client_id_sha256": VALID_CLIENT,
    }
    payload.update(updates)
    return payload


def _unconstrained_string_paths(schema: object) -> list[str]:
    paths: list[str] = []

    def visit(node: object, path: str) -> None:
        if isinstance(node, dict):
            if node.get("type") == "string" and not (
                {"pattern", "const", "enum"} & set(node)
            ):
                paths.append(path)
            for key, value in node.items():
                visit(value, f"{path}/{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node):
                visit(value, f"{path}/{index}")

    visit(schema, "")
    return paths


def test_spec_observe_example_is_accepted() -> None:
    observe = AuthoringObserveV1.model_validate(_observe_payload())
    assert observe.schema_version == AUTHORING_OBSERVE_SCHEMA
    assert observe.tree[0].node_id == VALID_NODE
    assert observe.tree[0].role is ElementRole.BUTTON
    assert observe.window is not None
    assert observe.window.role == "window"


def test_observe_does_not_reuse_computer_state_or_ui_node() -> None:
    assert not issubclass(AuthoringObserveV1, ComputerState)
    assert not issubclass(AuthoringObserveV1, UINode)
    dumped = AuthoringObserveV1.model_validate(_observe_payload()).model_dump()
    assert "nodes" not in dumped
    assert "screenshot_png" not in dumped
    assert "active_window" not in dumped


@pytest.mark.parametrize("field,value", sorted(FORBIDDEN_FIELDS.items()))
def test_observe_refuses_value_title_screenshot_and_other_phi(field: str, value: object) -> None:
    payload = _observe_payload()
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringObserveV1.model_validate({**payload, field: value})

    window = dict(payload["window"])  # type: ignore[arg-type]
    window[field] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringObserveV1.model_validate({**payload, "window": window})

    node = dict(payload["tree"][0])  # type: ignore[index]
    node[field] = value
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringObserveV1.model_validate({**payload, "tree": [node], "node_count": 1})


def test_observe_refuses_unknown_keys() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringObserveV1.model_validate({**_observe_payload(), "url": "https://example"})


def test_observe_refuses_six_digit_and_at_sign_labels() -> None:
    payload = _observe_payload()
    node = dict(payload["tree"][0])  # type: ignore[index]
    for name in (
        "acct 009321",
        "user@example.com",
        "https://example",
        "123-45-6789",
        "555-123-4567",
    ):
        node["name"] = name
        with pytest.raises(ValidationError):
            AuthoringObserveV1.model_validate({**payload, "tree": [node]})


def test_citrix_observe_is_coach_only_with_empty_tree() -> None:
    observe = AuthoringObserveV1.model_validate(
        {
            "backend": "citrix",
            "provider": "none",
            "agent_drive": False,
            "coach_only": True,
            "recording": False,
            "tree": [],
            "truncated": False,
            "node_count": 0,
        }
    )
    assert observe.agent_drive is False
    assert observe.tree == ()
    with pytest.raises(ValidationError, match="coach_only"):
        AuthoringObserveV1.model_validate(
            {
                "backend": "windows",
                "provider": "windows_uia",
                "agent_drive": True,
                "coach_only": False,
                "recording": False,
                "window": {
                    "process_name": "App",
                    "role": "window",
                    "bounds": {"x": 0, "y": 0, "w": 1, "h": 1},
                },
                "tree": [],
                "truncated": False,
                "node_count": 0,
            }
        )


def test_normalized_bounds_are_not_pixels() -> None:
    with pytest.raises(ValidationError):
        AuthoringNormalizedBoundsV1.model_validate({"x": 920, "y": 640, "w": 180, "h": 36})
    with pytest.raises(ValidationError):
        AuthoringNormalizedBoundsV1.model_validate(
            {"x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0}
        )


def test_command_click_is_node_id_only() -> None:
    command = AuthoringCommandV1.model_validate(_command_payload())
    assert isinstance(command.args, AuthoringClickArgsV1)
    assert command.args.node_id == VALID_NODE
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCommandV1.model_validate(
            _command_payload(args={"node_id": VALID_NODE, "x": 12, "y": 40})
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCommandV1.model_validate(
            _command_payload(args={"node_id": VALID_NODE, "value": "typed"})
        )


@pytest.mark.parametrize("field,value", sorted(FORBIDDEN_FIELDS.items()))
def test_command_refuses_value_title_screenshot_and_extra_keys(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCommandV1.model_validate(_command_payload(**{field: value}))


def test_pause_result_has_param_name_and_no_value() -> None:
    command = AuthoringCommandV1.model_validate(
        _command_payload(
            tool="pause_for_input",
            args={"param": "note", "secret": True},
            status="done",
            result={"recorded": True, "param": "note"},
        )
    )
    assert isinstance(command.result, AuthoringPauseResultV1)
    assert command.result.param == "note"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCommandV1.model_validate(
            _command_payload(
                tool="pause_for_input",
                args={"param": "note", "secret": True, "value": "typed"},
                status="pending",
            )
        )


def test_compile_result_is_needs_human_admit_not_verified() -> None:
    command = AuthoringCommandV1.model_validate(
        _command_payload(
            tool="compile",
            args={},
            status="done",
            result={
                "status": "needs_human_admit",
                "workflow_id": "wf_recording01",
                "recording_retained": True,
            },
        )
    )
    assert isinstance(command.result, AuthoringCompileResultV1)
    with pytest.raises(ValidationError):
        AuthoringCommandV1.model_validate(
            _command_payload(
                tool="compile",
                args={},
                status="done",
                result={
                    "status": "VERIFIED",
                    "workflow_id": "wf_recording01",
                    "recording_retained": True,
                },
            )
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCompileResultV1.model_validate(
            {
                "status": "needs_human_admit",
                "workflow_id": "wf_recording01",
                "recording_retained": True,
                "success": True,
            }
        )


def test_pending_lookup_has_null_result() -> None:
    lookup = AuthoringCommandLookupV1.model_validate(
        {
            "command_id": VALID_COMMAND_ID,
            "status": "pending",
            "retry_after_ms": 1000,
            "result": None,
        }
    )
    assert lookup.result is None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCommandLookupV1.model_validate(
            {
                "command_id": VALID_COMMAND_ID,
                "status": "pending",
                "retry_after_ms": 1000,
                "result": None,
                "screenshot": "x",
            }
        )


def test_enqueue_ack_is_pending_command_id() -> None:
    ack = AuthoringEnqueueAcceptedV1.model_validate(
        {"status": "pending", "command_id": VALID_COMMAND_ID}
    )
    assert ack.command_id == VALID_COMMAND_ID
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringEnqueueAcceptedV1.model_validate(
            {
                "status": "pending",
                "command_id": VALID_COMMAND_ID,
                "title": "running",
            }
        )


def test_bind_status_has_no_secrets_or_tree() -> None:
    status = AuthoringBindV1.model_validate(
        {
            "pack_id": VALID_PACK,
            "bound": True,
            "allow": "granted",
            "client_display": "ChatGPT",
            "backend": "web",
            "coach_only": False,
            "halted": False,
        }
    )
    assert status.schema_version == AUTHORING_BIND_SCHEMA
    assert status.allow is AuthoringAllowStateV1.GRANTED
    dumped = status.model_dump()
    assert "bind" not in dumped
    assert "leaseSecret" not in dumped
    assert "tree" not in dumped
    assert "args" not in dumped
    pending = AuthoringBindV1.model_validate(
        {
            "pack_id": VALID_PACK,
            "bound": True,
            "allow": "pending",
            "backend": "web",
            "coach_only": False,
        }
    )
    assert pending.allow is AuthoringAllowStateV1.PENDING
    assert pending.client_display is None
    for field, value in FORBIDDEN_FIELDS.items():
        with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
            AuthoringBindV1.model_validate(
                {
                    "pack_id": VALID_PACK,
                    "bound": False,
                    "allow": "none",
                    "coach_only": True,
                    field: value,
                }
            )


def test_bind_token_parser_is_exact_prefix_alphabet_and_length() -> None:
    assert parse_authoring_bind_token(VALID_BIND) == VALID_BIND
    assert parse_authoring_lease_secret(VALID_LEASE) == VALID_LEASE
    rejected = (
        "oar_" + "a" * 64,
        "oap_" + "A" * 43,
        "oab_" + "a" * 64,
        "oals_" + "A" * 43,
        "oa",
        "oab",
        "oals",
        "oab_" + "A" * 42,
        "oals_" + "a" * 63,
        "oab_" + "A" * 43 + "!",
        True,
        1,
    )
    for value in rejected:
        with pytest.raises(AuthoringTokenError):
            parse_authoring_bind_token(value)
        with pytest.raises(AuthoringTokenError):
            parse_authoring_lease_secret(value)


def test_runner_uri_accepts_only_runner_fields() -> None:
    parsed = parse_authoring_runner_uri(VALID_DEEP_LINK)
    assert parsed.pack == VALID_PACK
    assert parsed.bind == VALID_BIND
    assert parsed.origin == "https://openadapt.ai"
    mint = AuthoringBindMintV1.model_validate(
        {"bind": VALID_BIND, "deep_link": VALID_DEEP_LINK}
    )
    assert mint.bind == VALID_BIND
    claim = AuthoringBindClaimV1.model_validate(
        {"leaseSecret": VALID_LEASE, "lease_s": 900}
    )
    assert claim.lease_s == 900
    with pytest.raises(AuthoringTokenError):
        parse_authoring_runner_uri(
            f"openadapt://connect?pack={VALID_PACK}&bind={VALID_BIND}"
            "&origin=https://openadapt.ai"
        )
    with pytest.raises(AuthoringTokenError):
        parse_authoring_runner_uri(VALID_DEEP_LINK + "&command=run")
    with pytest.raises(AuthoringTokenError):
        parse_authoring_runner_uri(
            f"openadapt://runner?pack={VALID_PACK}&bind={VALID_BIND}"
            "&origin=https://preview.openadapt.ai"
        )


def test_error_result_names_stale_node_without_pixels() -> None:
    command = AuthoringCommandV1.model_validate(
        _command_payload(
            status="error",
            result={"error": "stale_node"},
        )
    )
    assert isinstance(command.result, AuthoringErrorResultV1)
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringErrorResultV1.model_validate(
            {"error": "stale_node", "backend_pixels": {"x": 1}}
        )


@pytest.mark.parametrize(
    ("model", "filename"),
    [
        (AuthoringObserveV1, "authoring-observe-v1.json"),
        (AuthoringCommandV1, "authoring-command-v1.json"),
        (AuthoringBindV1, "authoring-bind-v1.json"),
    ],
)
def test_packaged_authoring_schemas_are_strict(
    model: type[AuthoringObserveV1 | AuthoringCommandV1 | AuthoringBindV1],
    filename: str,
) -> None:
    schema = model.model_json_schema()
    assert schema["additionalProperties"] is False
    encoded = json.dumps(schema)
    assert "openadapt.authoring" in encoded
    if filename == "authoring-observe-v1.json":
        assert schema["x-openadapt-max-bytes"] == AUTHORING_MAX_OBSERVE_BYTES
        assert schema["x-openadapt-max-nodes"] == 200
    if filename == "authoring-command-v1.json":
        assert schema["x-openadapt-max-enqueue-bytes"] == AUTHORING_MAX_COMMAND_BYTES
    properties = schema.get("properties", {})
    for forbidden in ("value", "title", "screenshot", "text", "backend_pixels"):
        assert forbidden not in properties
    assert _unconstrained_string_paths(schema) == []
    packaged = files("openadapt_types.schemas").joinpath(filename)
    assert json.loads(packaged.read_text(encoding="utf-8")) == schema


def test_schema_version_alias_matches_observe_contract() -> None:
    assert OBSERVE_SCHEMA_VERSION == AUTHORING_OBSERVE_SCHEMA
    assert OBSERVE_SCHEMA_VERSION == "openadapt.authoring.observe/v1"


def test_linux_unique_title_may_agent_drive() -> None:
    observe = AuthoringObserveV1.model_validate(
        _observe_payload(backend="linux", provider="linux_atspi")
    )
    assert observe.agent_drive is True
    assert observe.coach_only is False


def test_empty_projection_is_empty_tree_never_raw() -> None:
    observe = AuthoringObserveV1.model_validate(
        {
            "backend": "web",
            "provider": "playwright_ax",
            "agent_drive": False,
            "coach_only": True,
            "recording": False,
            "tree": [],
            "truncated": False,
            "node_count": 0,
            "reason": "empty_projection",
        }
    )
    dumped = observe.model_dump()
    assert dumped["tree"] == ()
    assert "raw" not in dumped
    with pytest.raises(ValidationError):
        AuthoringObserveV1.model_validate(
            {
                **_observe_payload(),
                "reason": "empty_projection",
            }
        )


def test_observe_refuses_payloads_over_32kib() -> None:
    payload = _observe_payload(value="x" * AUTHORING_MAX_OBSERVE_BYTES)
    with pytest.raises(ValidationError, match="32"):
        AuthoringObserveV1.model_validate(payload)


def test_get_coach_result_is_hint_only() -> None:
    command = AuthoringCommandV1.model_validate(
        _command_payload(
            tool="get_coach",
            args={},
            status="done",
            result={"hint": "Click Continue"},
        )
    )
    assert isinstance(command.result, AuthoringGetCoachResultV1)
    assert command.result.hint == "Click Continue"
    empty = AuthoringCommandV1.model_validate(
        _command_payload(
            tool="get_coach",
            args={},
            status="done",
            result={"hint": None},
        )
    )
    assert empty.result is not None and empty.result.hint is None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringGetCoachResultV1.model_validate({"hint": "Click Continue", "value": "ssn"})


def test_bind_pack_args_and_result_are_pack_and_allow_only() -> None:
    command = AuthoringCommandV1.model_validate(
        _command_payload(
            tool="bind_pack",
            args={"pack_id": VALID_PACK},
            status="done",
            result={"allowed": True, "client_display": "ChatGPT"},
        )
    )
    assert isinstance(command.result, AuthoringBindPackResultV1)
    assert command.result.allowed is True
    with pytest.raises(ValidationError):
        AuthoringCommandV1.model_validate(
            _command_payload(
                tool="bind_pack",
                args={"pack_id": "p.otherpackid1"},
                status="pending",
            )
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringBindPackResultV1.model_validate(
            {"allowed": True, "client_display": "ChatGPT", "sub": VALID_SUB}
        )


def test_halt_and_recording_acks_are_closed() -> None:
    halt = AuthoringCommandV1.model_validate(
        _command_payload(tool="halt", args={}, status="done", result={"halted": True})
    )
    assert isinstance(halt.result, AuthoringHaltResultV1)
    started = AuthoringCommandV1.model_validate(
        _command_payload(
            tool="start_record",
            args={},
            status="done",
            result={"recording": True},
        )
    )
    assert started.result is not None and started.result.recording is True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringHaltResultV1.model_validate({"halted": True, "screenshot": "x"})


def test_hosted_click_args_are_pack_and_node_id() -> None:
    args = AuthoringHostedClickArgsV1.model_validate(
        {"pack_id": VALID_PACK, "node_id": VALID_NODE}
    )
    assert args.node_id == VALID_NODE
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringHostedClickArgsV1.model_validate(
            {"pack_id": VALID_PACK, "node_id": VALID_NODE, "x": 12, "y": 40}
        )
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringHostedClickArgsV1.model_validate(
            {"pack_id": VALID_PACK, "node_id": VALID_NODE, "value": "typed"}
        )
    pack = AuthoringHostedPackArgsV1.model_validate({"pack_id": VALID_PACK})
    assert pack.pack_id == VALID_PACK
    lookup = AuthoringCommandLookupArgsV1.model_validate({"command_id": VALID_COMMAND_ID})
    assert lookup.command_id == VALID_COMMAND_ID


def test_enqueue_not_bound_and_in_flight_are_closed() -> None:
    unbound = AuthoringNotBoundV1.model_validate({"status": "not_bound"})
    assert unbound.status == "not_bound"
    busy = AuthoringInFlightV1.model_validate(
        {"error": "in_flight", "command_id": VALID_COMMAND_ID}
    )
    assert busy.command_id == VALID_COMMAND_ID
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringNotBoundV1.model_validate({"status": "not_bound", "tree": []})
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringInFlightV1.model_validate(
            {"error": "in_flight", "command_id": VALID_COMMAND_ID, "title": "x"}
        )


def test_poll_is_wait_zero_and_callback_is_phi_free() -> None:
    poll = AuthoringPollRequestV1.model_validate(
        {"wait_seconds": 0, "lease_seconds": 900}
    )
    assert poll.wait_seconds == 0
    with pytest.raises(ValidationError):
        AuthoringPollRequestV1.model_validate({"wait_seconds": 25, "lease_seconds": 900})
    callback = AuthoringCallbackV1.model_validate(
        {
            "command_id": VALID_COMMAND_ID,
            "status": "done",
            "result": {"recorded": True, "param": "note"},
        }
    )
    assert callback.result is not None
    halt = AuthoringCallbackV1.model_validate({"halted": True})
    assert halt.halted is True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        AuthoringCallbackV1.model_validate(
            {
                "command_id": VALID_COMMAND_ID,
                "status": "done",
                "result": {"recorded": True, "param": "note"},
                "screenshot": "x",
            }
        )


def test_command_enqueue_without_result_is_capped_at_8kib() -> None:
    payload = _command_payload()
    payload["value"] = "x" * AUTHORING_MAX_COMMAND_BYTES
    with pytest.raises(ValidationError, match="8"):
        AuthoringCommandV1.model_validate(payload)
