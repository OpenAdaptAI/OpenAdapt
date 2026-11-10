"""Finite loader semantics, with synthetic dependencies and no native I/O."""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import sys
import types
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

EXPORTS = (
    ("openadapt_flow.runtime", "AnthropicGrounder", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "FallbackGrounder", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "Grounder", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "GrounderMatch", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "NullGrounder", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "OCRAnchorGrounder", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "build_grounder", "openadapt_flow.runtime.grounder"),
    ("openadapt_flow.runtime", "apply_heal", "openadapt_flow.runtime.heal"),
    ("openadapt_flow.runtime", "build_heal_event", "openadapt_flow.runtime.heal"),
    ("openadapt_flow.runtime", "persist_heal", "openadapt_flow.runtime.heal"),
    ("openadapt_flow.runtime", "write_healed_bundle", "openadapt_flow.runtime.heal"),
    ("openadapt_flow.runtime", "Replayer", "openadapt_flow.runtime.replayer"),
    ("openadapt_flow.runtime", "RUNG_ORDER", "openadapt_flow.runtime.resolver"),
    ("openadapt_flow.runtime", "is_below_ocr", "openadapt_flow.runtime.resolver"),
    ("openadapt_flow.runtime", "resolve", "openadapt_flow.runtime.resolver"),
    (
        "openadapt_flow.runtime.durable",
        "ApprovalRecord",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "ApprovalRequired",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BundleMismatch",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "PauseExpired",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "ResumeRefused",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "StateDiverged",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "approval_pause_digest",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "enforce_resume_authorization",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "issue_resume_approval",
        "openadapt_flow.runtime.durable.approval",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedActionRefused",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedActionRequest",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedActionStore",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedDecision",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedExecutionResult",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedPauseCapability",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedRelayBinding",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BoundAttendedExecutor",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "SignedTransitionBaseline",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "TransitionObservation",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "execute_attended_action",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "issue_attended_capability",
        "openadapt_flow.runtime.durable.attended",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedActionService",
        "openadapt_flow.runtime.durable.attended_service",
    ),
    (
        "openadapt_flow.runtime.durable",
        "AttendedExecutorTimeout",
        "openadapt_flow.runtime.durable.attended_service",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BusinessDecisionPrincipal",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BusinessDecisionReceipt",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BusinessDecisionRefused",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BusinessDecisionRequest",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BusinessDecisionStore",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "BusinessDecisionSubmission",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "submit_business_decision",
        "openadapt_flow.runtime.durable.business_decision",
    ),
    (
        "openadapt_flow.runtime.durable",
        "CheckpointStore",
        "openadapt_flow.runtime.durable.checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "PendingEscalation",
        "openadapt_flow.runtime.durable.checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "RunCheckpoint",
        "openadapt_flow.runtime.durable.checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "RunManifest",
        "openadapt_flow.runtime.durable.checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "DurableRun",
        "openadapt_flow.runtime.durable.controller",
    ),
    (
        "openadapt_flow.runtime.durable",
        "classify_halt",
        "openadapt_flow.runtime.durable.controller",
    ),
    (
        "openadapt_flow.runtime.durable",
        "resumed_step_results",
        "openadapt_flow.runtime.durable.controller",
    ),
    (
        "openadapt_flow.runtime.durable",
        "TOP_GRAPH_ID",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "GraphFrame",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "LoopCursor",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "ProgramCheckpoint",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "ProgramTransitionReceipt",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "bundle_version",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "control_frames_hash",
        "openadapt_flow.runtime.durable.program_checkpoint",
    ),
    (
        "openadapt_flow.runtime.durable",
        "resume",
        "openadapt_flow.runtime.durable.resume",
    ),
    (
        "openadapt_flow.runtime.durable",
        "resume_point",
        "openadapt_flow.runtime.durable.resume",
    ),
)
PACKAGES = ("openadapt_flow.runtime", "openadapt_flow.runtime.durable")
FRAMEWORKS = ("Quartz", "ApplicationServices", "AppKit")


def _fresh_package(package, patch):
    installed = importlib.import_module(package)
    source = Path(installed.__file__).resolve()
    alias = "_finite_loader_probe_" + package.replace(".", "_")
    spec = importlib.util.spec_from_file_location(
        alias, source, submodule_search_locations=[str(source.parent)]
    )
    assert spec is not None and spec.loader is not None
    probe = importlib.util.module_from_spec(spec)
    patch.setitem(sys.modules, alias, probe)
    spec.loader.exec_module(probe)
    assert Path(probe.__file__).resolve() == source
    assert probe._LAZY_EXPORTS == {
        name: module for owner, name, module in EXPORTS if owner == package
    }
    return probe


def _modules(patch):
    modules = {}
    for _, name, target in EXPORTS:
        stub = modules.setdefault(target, types.ModuleType(target))
        setattr(stub, name, object())
    for target, stub in modules.items():
        patch.setitem(sys.modules, target, stub)
    return modules


@pytest.mark.parametrize("package,name,target", EXPORTS)
def test_every_lazy_export_is_deferred_exact_and_memoized(package, name, target):
    with pytest.MonkeyPatch.context() as patch:
        modules = _modules(patch)
        calls = []
        original = importlib.import_module

        def observed(module, package=None):
            if module in modules:
                calls.append(module)
            return original(module, package)

        patch.setattr(importlib, "import_module", observed)
        probe = _fresh_package(package, patch)
        assert name not in probe.__dict__
        assert calls == []
        expected = getattr(modules[target], name)
        assert getattr(probe, name) is expected
        assert probe.__dict__[name] is expected
        assert getattr(probe, name) is expected
        assert calls == [target]


@pytest.mark.parametrize("package,name,target", EXPORTS)
def test_missing_export_remains_attributeerror_and_is_not_memoized(
    package, name, target
):
    with pytest.MonkeyPatch.context() as patch:
        probe = _fresh_package(package, patch)
        patch.setitem(sys.modules, target, types.ModuleType(target))
        with pytest.raises(AttributeError):
            getattr(probe, name)
        assert name not in probe.__dict__


@pytest.mark.parametrize("package,name,target", EXPORTS)
def test_missing_export_dependency_keeps_original_importerror(package, name, target):
    with pytest.MonkeyPatch.context() as patch:
        probe = _fresh_package(package, patch)
        original = importlib.import_module
        missing = ImportError("synthetic dependency missing")
        calls = []

        def refused(module, package=None):
            if module == target:
                calls.append(module)
                raise missing
            return original(module, package)

        patch.setattr(importlib, "import_module", refused)
        with pytest.raises(ImportError) as result:
            getattr(probe, name)
        assert result.value is missing
        assert calls == [target]
        assert name not in probe.__dict__


@pytest.mark.parametrize("package", PACKAGES)
@given(st.text().filter(lambda name: all(name != value[1] for value in EXPORTS)))
def test_unknown_lazy_name_refuses_without_target_import(package, name):
    with pytest.MonkeyPatch.context() as patch:
        probe = _fresh_package(package, patch)
        calls = []
        original = importlib.import_module

        def observed(module, package=None):
            calls.append(module)
            return original(module, package)

        patch.setattr(importlib, "import_module", observed)
        with pytest.raises(AttributeError) as result:
            probe.__getattr__(name)
        assert (
            str(result.value) == f"module {probe.__name__!r} has no attribute {name!r}"
        )
        assert calls == []


@given(st.lists(st.sampled_from(EXPORTS), min_size=1, max_size=30))
def test_lazy_sequences_share_one_deferred_identity_and_cache_model(sequence):
    with pytest.MonkeyPatch.context() as patch:
        probes = {owner: _fresh_package(owner, patch) for owner in PACKAGES}
        modules = _modules(patch)
        calls = []
        original = importlib.import_module

        def observed(module, package=None):
            calls.append(module)
            return original(module, package)

        patch.setattr(importlib, "import_module", observed)
        seen = set()
        expected_calls = []
        for owner, name, target in sequence:
            if (owner, name) not in seen:
                expected_calls.append(target)
                seen.add((owner, name))
            assert getattr(probes[owner], name) is getattr(modules[target], name)
            assert probes[owner].__dict__[name] is getattr(modules[target], name)
            assert calls == expected_calls


@pytest.mark.parametrize("name", FRAMEWORKS)
def test_every_native_framework_uses_its_exact_literal_module(name):
    from openadapt_flow.backends import remote_display as backend

    with pytest.MonkeyPatch.context() as patch:
        calls = []
        expected = types.ModuleType(name)
        original = builtins.__import__

        def controlled(module, globals=None, locals=None, fromlist=(), level=0):
            if module in FRAMEWORKS:
                calls.append(module)
                assert module == name
                return expected
            return original(module, globals, locals, fromlist, level)

        patch.setattr(builtins, "__import__", controlled)
        assert backend._load_pyobjc_module(name) is expected
        assert calls == [name]
        assert backend._pyobjc_module(name) is expected
        assert calls == [name, name]


@given(st.sets(st.sampled_from(FRAMEWORKS)))
def test_native_dependency_aggregation_preserves_all_missing_members(missing):
    from openadapt_flow.backends import remote_display as backend

    with pytest.MonkeyPatch.context() as patch:
        calls = []
        original = builtins.__import__

        def controlled(module, globals=None, locals=None, fromlist=(), level=0):
            if module in FRAMEWORKS:
                calls.append(module)
                if module in missing:
                    raise ImportError("synthetic framework missing")
                return types.ModuleType(module)
            return original(module, globals, locals, fromlist, level)

        patch.setattr(builtins, "__import__", controlled)
        if missing:
            with pytest.raises(backend.MacOSDependencyMissing) as result:
                backend.require_pyobjc()
            assert str(result.value) == backend._pyobjc_missing_message(
                [name for name in FRAMEWORKS if name in missing]
            )
        else:
            assert backend.require_pyobjc() is None
        assert calls == list(FRAMEWORKS)


@pytest.mark.parametrize("name", FRAMEWORKS)
def test_native_missing_member_keeps_dependency_error_and_cause(name):
    from openadapt_flow.backends import remote_display as backend

    with pytest.MonkeyPatch.context() as patch:
        missing = ImportError("synthetic framework missing")
        original = builtins.__import__
        calls = []

        def controlled(module, globals=None, locals=None, fromlist=(), level=0):
            if module in FRAMEWORKS:
                calls.append(module)
                raise missing
            return original(module, globals, locals, fromlist, level)

        patch.setattr(builtins, "__import__", controlled)
        with pytest.raises(backend.MacOSDependencyMissing) as result:
            backend._pyobjc_module(name)
        assert result.value.__cause__ is missing
        assert str(result.value) == backend._pyobjc_missing_message([name])
        assert calls == [name]


@given(st.text().filter(lambda name: name not in FRAMEWORKS))
def test_unknown_framework_refuses_without_any_framework_import(name):
    from openadapt_flow.backends import remote_display as backend

    with pytest.MonkeyPatch.context() as patch:
        calls = []
        original = builtins.__import__

        def controlled(module, globals=None, locals=None, fromlist=(), level=0):
            if module in FRAMEWORKS:
                calls.append(module)
            return original(module, globals, locals, fromlist, level)

        patch.setattr(builtins, "__import__", controlled)
        for loader in (backend._load_pyobjc_module, backend._pyobjc_module):
            with pytest.raises(ValueError, match="^unknown macOS framework module$"):
                loader(name)
        assert calls == []
