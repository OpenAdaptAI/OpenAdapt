"""One finite context model; no store effect or caller authority is inferred."""

from __future__ import annotations

import asyncio
import hashlib
import threading
from dataclasses import FrozenInstanceError

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from openadapt_flow.request_binding import (
    MCP_META_KEY,
    BindingError,
    RequestContext,
    context_from_mcp,
    current_request_context,
    request_scope,
)


def reservation(index: int = 0) -> dict:
    return {
        "mode": "skill-cache",
        "state": "running",
        "run_id": f"run-{index}",
        "request_id": f"request-{index}",
        "workflow": "wrap:" + hashlib.sha256(b"synthetic-task").hexdigest(),
        "fingerprint": hashlib.sha256(str(index).encode()).hexdigest(),
    }


@given(index=st.integers(min_value=0, max_value=100000))
def test_context_is_immutable_closed_and_roundtrips(index):
    context = RequestContext.from_record(reservation(index))
    assert RequestContext.parse(context.to_dict()) == context
    assert context_from_mcp({MCP_META_KEY: context.to_dict()}) == context
    assert set(context.to_dict()) == {
        "schema",
        "run_id",
        "request_ref",
        "workflow",
        "fingerprint",
    }
    with pytest.raises(FrozenInstanceError):
        context.run_id = "changed"
    for key in context.to_dict():
        changed = context.to_dict()
        changed[key] = None
        with pytest.raises(BindingError):
            RequestContext.parse(changed)
    with pytest.raises(BindingError):
        RequestContext.parse({**context.to_dict(), "unexpected": "field"})


@pytest.mark.parametrize("value", [None, {}, {"unrelated/protocol": "metadata"}])
def test_absent_mcp_context_is_unbound(value):
    assert context_from_mcp(value) is None


@pytest.mark.parametrize(
    "value",
    [None, {}, [], "context", True, 1],
    ids=("null", "empty-object", "array", "text", "boolean", "integer"),
)
def test_present_invalid_mcp_context_refuses(value):
    with pytest.raises(BindingError):
        context_from_mcp({MCP_META_KEY: value})


def test_scope_preserves_primary_and_resets_nested_context():
    context = RequestContext.from_record(reservation())
    primary = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with request_scope(context):
            assert current_request_context() == context
            with request_scope(context):
                assert current_request_context() == context
            with pytest.raises(BindingError):
                with request_scope(RequestContext.from_record(reservation(1))):
                    raise AssertionError("conflicting scope entered")
            with pytest.raises(BindingError):
                with request_scope(None):
                    raise AssertionError("scope downgraded")
            raise primary
    assert caught.value is primary
    assert current_request_context() is None


def test_threads_do_not_inherit_or_share_another_request():
    barrier = threading.Barrier(2)
    observed = {}
    errors = []

    def worker(index):
        try:
            assert current_request_context() is None
            context = RequestContext.from_record(reservation(index))
            with request_scope(context):
                barrier.wait(timeout=5)
                observed[index] = current_request_context()
            assert current_request_context() is None
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=worker, args=(i,)) for i in (1, 2)]
    with request_scope(RequestContext.from_record(reservation(99))):
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=6)
    assert not errors and not any(thread.is_alive() for thread in threads)
    assert observed == {i: RequestContext.from_record(reservation(i)) for i in (1, 2)}
    assert current_request_context() is None


def test_async_requests_have_independent_scopes_and_reset_after_failure():
    async def worker(index):
        context = RequestContext.from_record(reservation(index))
        with request_scope(context):
            await asyncio.sleep(0)
            assert current_request_context() == context
        assert current_request_context() is None
        return context

    async def run():
        return await asyncio.gather(worker(1), worker(2))

    assert asyncio.run(run()) == [
        RequestContext.from_record(reservation(i)) for i in (1, 2)
    ]
    assert current_request_context() is None

    primary = RuntimeError("synthetic interruption")

    async def interrupted():
        try:
            with request_scope(RequestContext.from_record(reservation(3))):
                await asyncio.sleep(0)
                raise primary
        finally:
            assert current_request_context() is None

    with pytest.raises(RuntimeError) as caught:
        asyncio.run(interrupted())
    assert caught.value is primary and current_request_context() is None


class ContextLifetime(RuleBasedStateMachine):
    """Independent finite owner oracle: a completed scope leaves no owner."""

    @rule(index=st.integers(min_value=0, max_value=20), interrupted=st.booleans())
    def request(self, index, interrupted):
        context = RequestContext.from_record(reservation(index))
        primary = SystemExit(3)
        try:
            with request_scope(context):
                assert current_request_context() == context
                if interrupted:
                    raise primary
        except SystemExit as caught:
            assert interrupted and caught is primary
        assert current_request_context() is None

    @invariant()
    def no_ambient_request_after_transition(self):
        assert current_request_context() is None


TestContextLifetime = ContextLifetime.TestCase


def test_pydantic_mcp_metadata_and_request_snapshot_do_not_alias_mutable_inputs():
    from pydantic import BaseModel, ConfigDict

    class Meta(BaseModel):
        model_config = ConfigDict(extra="allow")

    record = reservation(5)
    context = RequestContext.from_record(record)
    values = context.to_dict()
    meta = Meta(**{MCP_META_KEY: values})
    parsed = context_from_mcp(meta)
    values["run_id"] = "changed"
    record["run_id"] = "changed-again"
    assert parsed == context
    assert context.run_id == "run-5"


def test_emitted_context_symbols_never_add_or_replace_business_parameters():
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from tests.test_mcp_sdk_compatibility import _emit, _load

    @given(data=st.data())
    @settings(deadline=None)
    def check(data):
        names = data.draw(
            st.lists(
                st.sampled_from(
                    (
                        "MCPContext",
                        "request_context",
                        "parse_request_context",
                        "request_scope",
                        "request_meta",
                    )
                ),
                min_size=1,
                max_size=5,
                unique=True,
            )
        )
        with TemporaryDirectory() as directory:
            path = _emit(Path(directory), "compiled", names)
            _namespace, tool = _load(path)
            properties = tool.parameters["properties"]
            assert set(properties) == set(names)
            assert set(tool.parameters["required"]) == set(names)
            assert all(schema["type"] == "string" for schema in properties.values())

    check()


_JSON_LEAF = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(2**128), max_value=2**128),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=32),
)
_JSON_TREE = st.recursive(
    _JSON_LEAF,
    lambda child: st.one_of(
        st.lists(child, max_size=5),
        st.dictionaries(
            st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=16),
            child,
            max_size=5,
        ),
    ),
    max_leaves=30,
)


@given(value=_JSON_TREE)
def test_exact_json_preserves_roundtrip_types_and_mapping_order(value):
    import json

    from openadapt_flow.request_binding import _canonical

    encoded = _canonical(value)
    decoded = json.loads(encoded)
    assert _canonical(decoded) == encoded
    if type(value) is dict:
        assert _canonical(dict(reversed(list(value.items())))) == encoded
    assert len({_canonical(item) for item in (None, False, 0, 0.0, "0", [], {})}) == 7


@given(
    value=st.lists(_JSON_LEAF, max_size=8),
    key=st.integers(min_value=-100, max_value=100),
)
def test_json_python_coercions_never_certify_a_terminal(value, key):
    from openadapt_flow.request_binding import _canonical

    assert _canonical(value)
    assert _canonical({str(key): value})
    for malformed in (tuple(value), {key: value}, {"nested": tuple(value)}):
        with pytest.raises(BindingError):
            _canonical(malformed)


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), -float("inf"), b"bytes", {"set"}, object(), "\ud800"],
    ids=(
        "nan",
        "infinity",
        "negative-infinity",
        "bytes",
        "set",
        "opaque-object",
        "invalid-unicode",
    ),
)
def test_json_unsupported_or_nonfinite_values_refuse(value):
    from openadapt_flow.request_binding import _canonical

    with pytest.raises(BindingError):
        _canonical(value)


def test_json_exact_types_and_each_bound_refuse_without_a_coercion_callback():
    import openadapt_flow.request_binding as model

    class Text(str):
        pass

    class Number(int):
        pass

    class Array(list):
        pass

    class Mapping(dict):
        pass

    recursive = []
    recursive.append(recursive)
    deep = None
    for _ in range(model.MAX_JSON_DEPTH + 1):
        deep = [deep]
    values = [
        Text("text"),
        Number(1),
        Array([]),
        Mapping({}),
        recursive,
        deep,
        1 << model.MAX_JSON_INTEGER_BITS,
        [None] * model.MAX_JSON_NODES,
        "x" * (model.MAX_RECEIPT_BYTES + 1),
    ]
    for value in values:
        with pytest.raises(BindingError):
            model._canonical(value)
    assert model._canonical((1 << model.MAX_JSON_INTEGER_BITS) - 1)
    assert model._canonical([None] * (model.MAX_JSON_NODES - 1))


class ExactJSONLifetime(RuleBasedStateMachine):
    """Independent JSON operation model: no mutable identity memoization."""

    def __init__(self):
        super().__init__()
        self.value = {"items": []}
        self.items = []

    @rule(value=_JSON_LEAF)
    def append(self, value):
        self.value["items"].append(value)
        self.items.append(value)

    @rule()
    def temporarily_replace_with_non_json(self):
        from openadapt_flow.request_binding import _canonical

        original = self.value["items"]
        self.value["items"] = tuple(original)
        with pytest.raises(BindingError):
            _canonical(self.value)
        self.value["items"] = original

    @invariant()
    def exact_mutable_contents_are_rechecked(self):
        import json

        from openadapt_flow.request_binding import _canonical

        assert json.loads(_canonical(self.value)) == {"items": self.items}


TestExactJSONLifetime = ExactJSONLifetime.TestCase


def receipt_file(root, raw=b'{"receipt":{},"evidence":{}}'):
    path = root / "receipts" / "operation" / "effect-receipt.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    return path


@pytest.mark.parametrize(
    "raw",
    [
        b'{"receipt":{},"evidence":{}}',
        b'{"receipt":{"x":[null,true,1,1.0]},"evidence":{}}',
    ],
    ids=("empty-document", "typed-values"),
)
def test_anchored_reader_retains_exact_bytes_without_claiming_receipt_validity(
    tmp_path, raw
):
    from openadapt_flow.request_binding import _read_receipt

    path = receipt_file(tmp_path / "owner", raw)
    actual, document = _read_receipt(str(path), tmp_path / "owner")
    assert actual == raw and set(document) == {"receipt", "evidence"}


@pytest.mark.parametrize(
    "raw",
    [
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":1,"x":2}',
        b'{"x":"\\ud800"}',
        b"[]",
        b"not-json",
    ],
    ids=(
        "nan",
        "infinity",
        "duplicate-key",
        "invalid-unicode",
        "array",
        "invalid-json",
    ),
)
def test_receipt_json_boundary_refuses_non_json_and_ambiguous_documents(tmp_path, raw):
    from openadapt_flow.request_binding import _read_receipt

    path = receipt_file(tmp_path / "owner", raw)
    with pytest.raises(BindingError):
        _read_receipt(str(path), tmp_path / "owner")


@pytest.mark.parametrize("component", ["owner", "receipts", "operation", "leaf"])
def test_no_follow_refuses_symlinks_at_every_owned_path_component(tmp_path, component):
    from openadapt_flow.request_binding import _read_receipt

    root = tmp_path / "owner"
    path = receipt_file(root)
    source = {
        "owner": root,
        "receipts": root / "receipts",
        "operation": path.parent,
        "leaf": path,
    }[component]
    moved = source.with_name(source.name + "-actual")
    source.rename(moved)
    source.symlink_to(moved, target_is_directory=component != "leaf")
    with pytest.raises(BindingError):
        _read_receipt(str(path), root)


@pytest.mark.parametrize(
    "change", ["directory-symlink", "directory-rename", "leaf-replace", "leaf-grow"]
)
def test_anchored_read_detects_changed_namespace_or_leaf_before_publication(
    tmp_path, monkeypatch, change
):
    import openadapt_flow.request_binding as model

    root = tmp_path / "owner"
    path = receipt_file(root)
    real_read = model.os.read
    changed = False

    def read_then_change(fd, count):
        nonlocal changed
        raw = real_read(fd, count)
        if not changed:
            changed = True
            if change in {"directory-symlink", "directory-rename"}:
                moved = tmp_path / "moved-operation"
                path.parent.rename(moved)
                if change == "directory-symlink":
                    path.parent.symlink_to(moved, target_is_directory=True)
            elif change == "leaf-replace":
                replacement = path.with_name("replacement")
                replacement.write_bytes(raw)
                replacement.replace(path)
            else:
                with path.open("ab") as stream:
                    stream.write(b" ")
        return raw

    with monkeypatch.context() as patch:
        patch.setattr(model.os, "read", read_then_change)
        with pytest.raises(BindingError):
            model._read_receipt(str(path), root)
    assert changed


def test_anchored_open_refuses_parent_swap_between_header_and_open(
    tmp_path, monkeypatch
):
    import openadapt_flow.request_binding as model

    root = tmp_path / "owner"
    path = receipt_file(root)
    real_open = model.os.open
    swapped = False

    def open_after_swap(name, flags, *, dir_fd=None):
        nonlocal swapped
        if name == "operation" and dir_fd is not None and not swapped:
            swapped = True
            moved = tmp_path / "outside-operation"
            path.parent.rename(moved)
            path.parent.symlink_to(moved, target_is_directory=True)
        if dir_fd is None:
            return real_open(name, flags)
        return real_open(name, flags, dir_fd=dir_fd)

    with monkeypatch.context() as patch:
        patch.setattr(
            model.os, "supports_dir_fd", model.os.supports_dir_fd | {open_after_swap}
        )
        patch.setattr(model.os, "open", open_after_swap)
        with pytest.raises(BindingError):
            model._read_receipt(str(path), root)
    assert swapped


@pytest.mark.parametrize(
    "mode", ["unsupported", "empty", "oversize", "outside", "dotdot"]
)
def test_reader_refuses_unsupported_headers_and_ownership_without_reading(
    tmp_path, monkeypatch, mode
):
    import openadapt_flow.request_binding as model

    root = tmp_path / "owner"
    path = receipt_file(root)
    if mode == "empty":
        path.write_bytes(b"")
    elif mode == "oversize":
        with path.open("wb") as stream:
            stream.truncate(model.MAX_RECEIPT_BYTES + 1)
    elif mode == "outside":
        path = receipt_file(tmp_path / "outside")
    elif mode == "dotdot":
        path = path.parent / ".." / "operation" / path.name

    def unexpected_read(_fd, _count):
        raise AssertionError("refused header reached body read")

    with monkeypatch.context() as patch:
        patch.setattr(model.os, "read", unexpected_read)
        if mode == "unsupported":
            patch.setattr(model.os, "supports_dir_fd", set())
        with pytest.raises(BindingError):
            model._read_receipt(str(path), root)


def test_all_opened_descriptors_close_on_primary_failure_or_close_failure(
    tmp_path, monkeypatch
):
    import openadapt_flow.request_binding as model

    path = receipt_file(tmp_path / "owner")
    real_open, real_close = model.os.open, model.os.close
    opened, closed = [], []
    primary = KeyboardInterrupt()

    def tracked_open(name, flags, *, dir_fd=None):
        fd = (
            real_open(name, flags)
            if dir_fd is None
            else real_open(name, flags, dir_fd=dir_fd)
        )
        opened.append(fd)
        return fd

    def failed_read(_fd, _count):
        raise primary

    def close_then_fail(fd):
        real_close(fd)
        closed.append(fd)
        if len(closed) == 1:
            raise OSError("secondary descriptor cleanup")

    with monkeypatch.context() as patch:
        patch.setattr(
            model.os, "supports_dir_fd", model.os.supports_dir_fd | {tracked_open}
        )
        patch.setattr(model.os, "open", tracked_open)
        patch.setattr(model.os, "read", failed_read)
        patch.setattr(model.os, "close", close_then_fail)
        with pytest.raises(KeyboardInterrupt) as caught:
            model._read_receipt(str(path), tmp_path / "owner")
    assert caught.value is primary
    assert len(opened) > 2 and closed == list(reversed(opened))
    for fd in opened:
        with pytest.raises(OSError):
            model.os.fstat(fd)


def test_json_snapshot_does_not_retain_mutable_caller_containers(monkeypatch):
    import openadapt_flow.request_binding as model

    original = {"items": [{"value": [1]}]}
    detached = model._json_value(original)
    assert detached == original and detached is not original
    original["items"][0]["value"].append(2)
    assert detached == {"items": [{"value": [1]}]}
    real_dumps = model.json.dumps

    def mutate_after_snapshot(value, **kwargs):
        original["items"] = ("not-json",)
        return real_dumps(value, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(model.json, "dumps", mutate_after_snapshot)
        encoded = model._canonical(original)
    assert encoded == '{"items":[{"value":[1,2]}]}'
    with pytest.raises(BindingError):
        model._canonical(original)


def test_context_and_mcp_metadata_use_exact_json_keys_without_key_equality_callbacks():
    calls = []

    class Key:
        def __hash__(self):
            return hash("schema")

        def __eq__(self, other):
            calls.append(other)
            return other == "schema"

    context = RequestContext.from_record(reservation(9))
    malformed = context.to_dict()
    del malformed["schema"]
    malformed[Key()] = context.to_dict()["schema"]
    calls.clear()
    with pytest.raises(BindingError):
        RequestContext.parse(malformed)
    assert calls == []
    with pytest.raises(BindingError):
        context_from_mcp(malformed)
    assert calls == []
    with pytest.raises(BindingError):
        context_from_mcp({MCP_META_KEY: context.to_dict(), "other/protocol": (1, 2)})
    assert (
        context_from_mcp({MCP_META_KEY: context.to_dict(), "other/protocol": [1, 2]})
        == context
    )


def _native_mcp_metadata(value):
    from tests._mcp_tools import sdk_modules

    _server, _error, types = sdk_modules()

    request = types.CallToolRequestParams.model_validate(
        {"name": "synthetic_context_binding", "arguments": {}, "_meta": value}
    )
    assert request.meta is not None
    return request.meta


@pytest.mark.parametrize(
    "value",
    [None, {}, [], "context", True, 1],
    ids=("null", "empty-object", "array", "text", "boolean", "integer"),
)
def test_native_mcp_present_invalid_context_matches_exact_dict_refusal(value):
    metadata = {"other/protocol": None, MCP_META_KEY: value}
    for representation in (metadata, _native_mcp_metadata(metadata)):
        with pytest.raises(BindingError):
            context_from_mcp(representation)
    assert current_request_context() is None


@given(
    index=st.integers(min_value=0, max_value=100),
    unrelated=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(min_value=-5, max_value=5),
        st.text(alphabet="abc", max_size=5),
    ),
)
def test_native_mcp_absent_and_valid_context_parity_preserves_unrelated_nulls(
    index, unrelated
):
    context = RequestContext.from_record(reservation(index))
    for present in (False, True):
        metadata = {"other/protocol": unrelated}
        if present:
            metadata[MCP_META_KEY] = context.to_dict()
        native = _native_mcp_metadata(metadata)
        expected = context if present else None
        assert context_from_mcp(metadata) == expected
        assert context_from_mcp(native) == expected
    assert current_request_context() is None


def test_native_mcp_reserved_progress_null_is_distinct_from_binding_metadata():
    from pydantic import ValidationError

    from tests._mcp_tools import sdk_modules

    _server, _error, types = sdk_modules()
    context = RequestContext.from_record(reservation(10))
    metadata = {"progressToken": None, MCP_META_KEY: context.to_dict()}
    assert context_from_mcp(metadata) == context
    if types.__name__ == "mcp_types":
        with pytest.raises(ValidationError):
            _native_mcp_metadata(metadata)
    else:
        assert types.__name__ == "mcp.types"
        assert context_from_mcp(_native_mcp_metadata(metadata)) == context
    valid = {"progressToken": "synthetic-progress", MCP_META_KEY: context.to_dict()}
    assert context_from_mcp(_native_mcp_metadata(valid)) == context
    assert current_request_context() is None


@pytest.mark.parametrize(
    "value",
    [None, {}, [], "context", True, 1],
    ids=("null", "empty-object", "array", "text", "boolean", "integer"),
)
def test_mcp_scope_uses_the_same_strict_metadata_refusal(value):
    from types import SimpleNamespace

    from openadapt_flow.request_binding import mcp_request_scope

    holder = SimpleNamespace(
        request_context=SimpleNamespace(meta={MCP_META_KEY: value})
    )
    effects = []
    with pytest.raises(BindingError):
        with mcp_request_scope(holder):
            effects.append("dispatched")
    assert effects == []
    assert current_request_context() is None


def test_mcp_scope_preserves_primary_reset_and_owned_unbound_refusal():
    from types import SimpleNamespace

    from openadapt_flow.request_binding import mcp_request_scope

    context = RequestContext.from_record(reservation(11))
    holder = SimpleNamespace(
        request_context=SimpleNamespace(meta={MCP_META_KEY: context.to_dict()})
    )
    primary = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as caught:
        with mcp_request_scope(holder):
            assert current_request_context() == context
            raise primary
    assert caught.value is primary
    assert current_request_context() is None

    class DirectCall:
        @property
        def request_context(self):
            raise ValueError("no native request context")

    for absent in (None, DirectCall()):
        with mcp_request_scope(absent):
            assert current_request_context() is None
        with request_scope(context):
            with pytest.raises(BindingError):
                with mcp_request_scope(absent):
                    raise AssertionError("owned context downgraded")
            assert current_request_context() == context
    assert current_request_context() is None
