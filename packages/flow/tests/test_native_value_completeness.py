"""Independent extent/full reads must qualify every complete native value."""

from __future__ import annotations

import ctypes
from dataclasses import replace
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from openadapt_flow.backends.win_agent.native_value import (
    PROOF_SCHEMA,
    NativeOwner,
    NativeRead,
    ValueUnavailable,
    Win32TextReader,
    observe_complete_value,
    require_non_sensitive_value,
    utf16_bytes,
    valid_value_proof,
)

OWNER = NativeOwner(123, 456, 789)


def control(value=""):
    pattern = SimpleNamespace(Value=value, IsReadOnly=False)
    target = SimpleNamespace(
        NativeWindowHandle=OWNER.hwnd,
        ProcessId=OWNER.pid,
        GetRuntimeId=lambda: [1, 2],
        IsPassword=False,
        ControlTypeName="EditControl",
        GetValuePattern=lambda: pattern,
        Name="never use Name",
    )

    def property_ex(property_id, ignore_default):
        assert property_id == 30019 and ignore_default == 1
        return target.IsPassword

    target.GetPropertyValueEx = property_ex
    return target, pattern


class Reader:
    def __init__(self, value=""):
        self.value = value
        self.reads = 0
        self.owner_value = OWNER

    def owner(self, hwnd):
        assert hwnd == OWNER.hwnd
        return self.owner_value

    def read(self, owner, *, guard):
        guard()
        self.reads += 1
        units = len(utf16_bytes(self.value)) // 2
        return NativeRead(owner, units, units, self.value)


def observe(target, reader, **kwargs):
    return observe_complete_value(
        target, identity=lambda: "bound:1", native_reader=reader, **kwargs
    )


@given(st.text(max_size=9000))
def test_exact_complete_values_require_both_sources_and_utf16_proof(value):
    target, _ = control(value)
    reader = Reader(value)
    result = observe(target, reader)
    assert result.complete and result.value == value and result.reason is None
    assert reader.reads == 2
    assert result.proof is not None
    proof = result.proof.payload()
    assert valid_value_proof(value, proof)
    assert proof["utf16_units"] == len(utf16_bytes(value)) // 2
    assert proof["hwnd"] == OWNER.hwnd and proof["pid"] == OWNER.pid


@pytest.mark.parametrize(
    "stored,provider",
    [
        ("x" * 8192 + "-END", "x" * 4096),
        ("x" * 4096 + "D", "x" * 4096),
        ("hidden suffix", ""),
        ("", "Name"),
    ],
)
def test_truncated_or_disagreeing_provider_never_becomes_a_complete_baseline_or_mask(
    stored, provider
):
    target, _ = control(provider)
    result = observe(target, Reader(stored))
    assert not result.complete and result.value is None and result.proof is None
    assert result.reason == "provider_native_value_disagreement"


@pytest.mark.parametrize(
    "fault",
    [
        "password",
        "password_unknown",
        "readonly_unknown",
        "pattern",
        "nonstring",
        "missing_hwnd",
        "missing_pid",
        "missing_runtime",
        "wrong_owner",
        "timeout",
        "oversize",
        "unstable_native",
        "unstable_provider",
        "replaced_identity",
        "scope_changed",
        "deadline",
        "bad_copy",
        "owner_changed",
    ],
)
def test_missing_or_unstable_completeness_evidence_is_explicitly_unavailable(fault):
    target, pattern = control("exact")
    reader = Reader("exact")
    kwargs = {}
    if fault == "password":
        target.IsPassword = True
    elif fault == "password_unknown":
        target.IsPassword = None
    elif fault == "readonly_unknown":
        pattern.IsReadOnly = None
    elif fault == "pattern":
        target.GetValuePattern = lambda: None
    elif fault == "nonstring":
        pattern.Value = None
    elif fault == "missing_hwnd":
        target.NativeWindowHandle = 0
    elif fault == "missing_pid":
        target.ProcessId = 0
    elif fault == "missing_runtime":
        target.GetRuntimeId = lambda: []
    elif fault == "wrong_owner":
        reader.owner_value = replace(OWNER, pid=999)
    elif fault == "timeout":

        def timeout(owner, *, guard):
            raise ValueUnavailable("native_text_timeout_or_failure")

        reader.read = timeout
    elif fault == "oversize":
        reader.value = "x" * 65537
    elif fault == "unstable_native":
        original = reader.read

        def changing(owner, *, guard):
            reader.value += "x"
            return original(owner, guard=guard)

        reader.read = changing
    elif fault == "unstable_provider":

        class Pattern:
            IsReadOnly = False
            reads = 0

            @property
            def Value(self):
                self.reads += 1
                return "exact" if self.reads == 1 else "changed"

        target.GetValuePattern = Pattern
        # Reuse one actual provider, not one object per read.
        instance = Pattern()
        target.GetValuePattern = lambda: instance
    elif fault == "replaced_identity":
        original = reader.read

        def replacing(owner, *, guard):
            target.GetRuntimeId = lambda: [9, 9]
            return original(owner, guard=guard)

        reader.read = replacing
    elif fault == "scope_changed":
        bindings = iter(["first", None])
        kwargs["identity"] = lambda: next(bindings, None)
    elif fault == "deadline":
        times = iter([0.0, 6.0])
        kwargs["clock"] = lambda: next(times, 6.0)
    elif fault == "bad_copy":
        reader.read = lambda owner, *, guard: NativeRead(owner, 5, 4, "exact")
    elif fault == "owner_changed":
        original = reader.read

        def replace_owner(owner, *, guard):
            reader.owner_value = replace(OWNER, thread=999)
            return original(owner, guard=guard)

        reader.read = replace_owner
    result = observe_complete_value(
        target,
        native_reader=reader,
        identity=kwargs.pop("identity", lambda: "bound"),
        **kwargs,
    )
    assert not result.complete and result.value is None and result.proof is None
    assert result.reason
    if fault.startswith("password"):
        assert reader.reads == 0


def test_readonly_is_allowed_for_approval_observation_but_not_edit_baseline():
    target, pattern = control("")
    pattern.IsReadOnly = True
    assert observe(target, Reader()).complete
    result = observe(target, Reader(), require_editable=True)
    assert not result.complete and result.value is None


@pytest.mark.parametrize(
    "key,value",
    [
        ("native_reads", True),
        ("uia_reads", 1),
        ("utf16_units", True),
        ("sha256_utf16le", "0" * 64),
        ("hwnd", 0),
        ("method", "uia-only"),
        ("schema", "legacy"),
        ("extra", "x"),
    ],
)
def test_client_proof_rejects_missing_fake_or_legacy_completeness(key, value):
    target, _ = control("é𝄞 ")
    result = observe(target, Reader("é𝄞 "))
    proof = result.proof.payload()
    proof[key] = value
    assert not valid_value_proof("é𝄞 ", proof)


class User32:
    def __init__(self, value):
        self.raw = utf16_bytes(value)
        self.calls = []
        self.pid = OWNER.pid
        self.timeout = False
        self.unicode = True
        self.extent_delta = 0
        self.copy_delta = 0
        self.replace_after_extent = False

    def IsWindow(self, hwnd):
        return hwnd == OWNER.hwnd

    def IsWindowUnicode(self, hwnd):
        return self.unicode

    def GetWindowThreadProcessId(self, hwnd, pointer):
        from ctypes import wintypes

        ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))[0] = self.pid
        return OWNER.thread

    def SendMessageTimeoutW(
        self, hwnd, message, count, pointer, flags, timeout, result
    ):
        self.calls.append((message, count, flags, timeout))
        assert hwnd == OWNER.hwnd and flags == 0x23 and timeout == 1000
        if self.timeout:
            return 0
        if message == 0xE:
            assert count == pointer == 0
            returned = len(self.raw) // 2 + self.extent_delta
            if self.replace_after_extent:
                self.pid += 1
        elif message == 0xD:
            assert count == len(self.raw) // 2 + self.extent_delta + 1
            ctypes.memmove(pointer, self.raw + b"\0\0", len(self.raw) + 2)
            returned = len(self.raw) // 2 + self.copy_delta
        else:
            raise AssertionError("unexpected native message")
        ctypes.cast(result, ctypes.POINTER(ctypes.c_size_t))[0] = returned
        return 1


def protocol_reader(value):
    reader = Win32TextReader.__new__(Win32TextReader)
    user = User32(value)
    reader._user = user
    return reader, user


@pytest.mark.parametrize("value", ["", "é𝄞  \n", "x" * 8192 + "-END"])
def test_actual_native_message_protocol_uses_exact_utf16_buffer_and_counts(value):
    reader, user = protocol_reader(value)
    result = reader.read(OWNER, guard=lambda: None)
    assert result.value == value and result.extent == len(utf16_bytes(value)) // 2
    assert [entry[0] for entry in user.calls] == [0xE, 0xD]


@pytest.mark.parametrize(
    "fault,reason",
    [
        ("timeout", "native_text_timeout_or_failure"),
        ("extent", "native_extent_or_copy_incomplete"),
        ("copied", "native_extent_or_copy_incomplete"),
        ("oversize", "value_exceeds_limit"),
        ("replace", "native_owner_changed"),
        ("ansi", "native_window_unavailable_or_not_unicode"),
        ("null", "native_extent_or_copy_incomplete"),
    ],
)
def test_native_protocol_never_promotes_timeout_owner_change_or_incomplete_copy(
    fault, reason
):
    reader, user = protocol_reader("a\x00b" if fault == "null" else "exact")
    if fault == "timeout":
        user.timeout = True
    elif fault == "extent":
        user.extent_delta = 1
    elif fault == "copied":
        user.copy_delta = -1
    elif fault == "oversize":
        user.extent_delta = 65537
    elif fault == "replace":
        user.replace_after_extent = True
    elif fault == "ansi":
        user.unicode = False
    with pytest.raises(ValueUnavailable, match=reason):
        reader.read(OWNER, guard=lambda: None)
    if fault in {"timeout", "oversize", "replace", "ansi"}:
        assert 0xD not in [entry[0] for entry in user.calls]


def test_uia_only_without_native_api_never_claims_complete(monkeypatch):
    monkeypatch.delattr(ctypes, "WinDLL", raising=False)
    target, _ = control("a complete-looking provider value")
    result = observe_complete_value(target, identity=lambda: "bound")
    assert not result.complete and result.value is None and result.proof is None
    assert result.reason == "native_api_unavailable"


def test_provider_without_a_native_extent_or_with_a_mutable_scope_is_incomplete():
    target, _ = control("exact")
    for scope in (None, {}, [], ""):
        result = observe_complete_value(
            target, identity=lambda: scope, native_reader=Reader("exact")
        )
        assert not result.complete and result.value is None
    assert not valid_value_proof("exact", None)
    result = observe(target, Reader("exact"))
    proof = result.proof.payload()
    del proof["native_reads"]
    assert not valid_value_proof("exact", proof)


@pytest.mark.parametrize("status", [True, None, 0, 1, "false", "error"])
def test_shared_sensitive_guard_refuses_before_any_source_or_capability_getter(status):
    class Target:
        calls = []

        @property
        def IsPassword(self):
            if status == "error":
                raise RuntimeError("status unavailable")
            return status

        def GetPropertyValueEx(self, property_id, ignore_default):
            assert property_id == 30019 and ignore_default == 1
            return self.IsPassword

        def __getattr__(self, name):
            self.calls.append(name)
            raise AssertionError("no source or capability access is allowed")

    target = Target()
    with pytest.raises(
        ValueUnavailable, match="password_or_password_status_unavailable"
    ):
        require_non_sensitive_value(target)
    result = observe_complete_value(
        target, identity=lambda: "bound", native_reader=Reader()
    )
    assert (
        not result.complete
        and result.reason == "password_or_password_status_unavailable"
    )
    assert target.calls == []


@pytest.mark.parametrize("stage", ["pattern_lookup", "pattern_call", "readability"])
@pytest.mark.parametrize("status", [True, None])
def test_status_changes_during_capability_calls_never_reach_value_getter(stage, status):
    target, _ = control("exact")
    calls = []

    class Pattern:
        @property
        def IsReadOnly(self):
            calls.append("readability")
            if stage == "readability":
                target.IsPassword = status
            return False

        @property
        def Value(self):
            calls.append("value")
            raise AssertionError("sensitive getter must not be called")

    def factory():
        calls.append("pattern_call")
        if stage == "pattern_call":
            target.IsPassword = status
        return Pattern()

    class Target(type(target)):
        @property
        def GetValuePattern(self):
            calls.append("pattern_lookup")
            if stage == "pattern_lookup":
                self.IsPassword = status
            return factory

    target = Target(**{k: v for k, v in vars(target).items() if k != "GetValuePattern"})
    target.GetPropertyValueEx = lambda property_id, ignore_default: target.IsPassword
    result = observe(target, Reader("exact"))
    assert not result.complete and result.value is None
    assert result.reason == "password_or_password_status_unavailable"
    assert "value" not in calls
    if stage == "pattern_lookup":
        assert "pattern_call" not in calls


def test_native_extent_status_change_refuses_before_full_text_message():
    target, _ = control("exact")
    reader, user = protocol_reader("exact")
    original = user.SendMessageTimeoutW

    def message(*args):
        result = original(*args)
        if args[1] == 0xE:
            target.IsPassword = True
        return result

    user.SendMessageTimeoutW = message
    result = observe(target, reader)
    assert not result.complete and result.value is None
    assert result.reason == "password_or_password_status_unavailable"
    assert [call[0] for call in user.calls] == [0xE]


def test_previous_proof_version_is_not_compatible_with_pre_read_sensitive_guard():
    target, _ = control("exact")
    result = observe(target, Reader("exact"))
    assert result.complete and result.proof.schema == PROOF_SCHEMA
    proof = result.proof.payload()
    for previous in ("v1", "v2"):
        proof["schema"] = "openadapt.native-value-completeness/" + previous
        assert not valid_value_proof("exact", proof)


@pytest.mark.parametrize("stage", ["extent", "full", "value"])
@pytest.mark.parametrize("status", [True, None])
def test_editability_metadata_cannot_invalidate_final_sensitive_guard(stage, status):
    class Target(SimpleNamespace):
        armed = False

        @property
        def ControlTypeName(self):
            if self.armed:
                self.IsPassword = status
            return "EditControl"

    base, _ = control("")
    target = Target(**{k: v for k, v in vars(base).items() if k != "ControlTypeName"})
    target.GetPropertyValueEx = lambda property_id, ignore_default: target.IsPassword
    accesses = []

    class StagedReader(Reader):
        def read(self, owner, *, guard):
            target.armed = stage == "extent"
            guard()
            accesses.append("extent")
            target.armed = stage == "full"
            guard()
            accesses.append("full")
            return NativeRead(owner, 0, 0, "")

    class Pattern:
        @property
        def IsReadOnly(self):
            target.armed = stage == "value"
            return False

        @property
        def Value(self):
            accesses.append("value")
            return ""

    target.GetValuePattern = Pattern
    result = observe(target, StagedReader(), require_editable=True)
    assert not result.complete and result.value is None
    assert result.reason == "password_or_password_status_unavailable"
    assert stage not in accesses


@pytest.mark.parametrize("raw", [None, object(), 0, 1, "false", True])
def test_defaulted_false_password_wrapper_is_not_known_nonsensitive_status(raw):
    target, _ = control("")
    # The friendly wrapper says False even when the raw ignored-default
    # property is unavailable or unsupported. Only the raw result qualifies.
    target.IsPassword = False
    queries = []

    def property_ex(property_id, ignore_default):
        queries.append((property_id, ignore_default))
        return raw

    target.GetPropertyValueEx = property_ex
    target.GetValuePattern = lambda: pytest.fail(
        "unknown raw status must not read value"
    )
    reader = Reader()
    result = observe(target, reader)
    assert (
        not result.complete
        and result.reason == "password_or_password_status_unavailable"
    )
    assert queries == [(30019, 1)] and reader.reads == 0


@pytest.mark.parametrize("fault", ["missing", "lookup", "call"])
def test_unavailable_raw_password_status_refuses_without_wrapper_or_text_access(fault):
    class Target:
        @property
        def GetPropertyValueEx(self):
            if fault == "missing":
                return None
            if fault == "lookup":
                raise RuntimeError("raw status lookup failed")
            return lambda *_: (_ for _ in ()).throw(RuntimeError("raw query failed"))

        @property
        def IsPassword(self):
            pytest.fail("coerced/defaulted wrapper is not an authority source")

        def GetValuePattern(self):
            pytest.fail("missing raw status must not read value")

    result = observe_complete_value(
        Target(), identity=lambda: "bound", native_reader=Reader()
    )
    assert (
        not result.complete
        and result.reason == "password_or_password_status_unavailable"
    )


def test_exact_helper_bytes_need_no_global_import_cache_registration() -> None:
    import inspect
    import sys
    from pathlib import Path
    from types import ModuleType

    from openadapt_flow.backends.win_agent import native_value

    source = Path(native_value.__file__).read_bytes()
    name = "_unregistered_native_value_test"
    assert name not in sys.modules
    module = ModuleType(name)
    # Compile inside this future-annotations test module: caller flags must
    # not turn the helper's concrete annotations back into deferred strings.
    exec(
        compile(source, str(native_value.__file__), "exec", dont_inherit=True),
        module.__dict__,
    )
    assert name not in sys.modules
    for value in vars(module).values():
        if (inspect.isfunction(value) or inspect.isclass(value)) and getattr(
            value, "__module__", None
        ) == name:
            assert all(
                type(annotation) is not str
                for annotation in getattr(value, "__annotations__", {}).values()
            )
            if inspect.isclass(value):
                for member in vars(value).values():
                    if inspect.isfunction(member):
                        assert all(
                            type(annotation) is not str
                            for annotation in member.__annotations__.values()
                        )
    owner = module.NativeOwner(1, 2, 3)
    assert module.NativeRead(owner, 0, 0, "").value == ""
    proof = module.ValueProof(
        module.PROOF_SCHEMA,
        "uia-value+wm-gettext",
        1,
        2,
        3,
        0,
        module.hashlib.sha256(b"").hexdigest(),
    )
    result = module.CompleteValue(True, "", None, proof)
    assert result.complete and module.valid_value_proof(
        result.value, result.proof.payload()
    )
