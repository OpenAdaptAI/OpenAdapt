"""Tests for TaskVerifierRegistry and related components."""

from __future__ import annotations

import pytest

from openadapt_evals.evaluation.verifier_registry import (
    TaskVerifierRegistry,
    VerificationResult,
)


class TestVerificationResult:
    """Tests for VerificationResult dataclass."""

    def test_basic_creation(self):
        result = VerificationResult(success=True, score=1.0)
        assert result.success is True
        assert result.score == 1.0
        assert result.details == {}

    def test_with_details(self):
        details = {"cache_count": 0, "path": "/tmp/cache"}
        result = VerificationResult(
            success=False, score=0.5, details=details
        )
        assert result.success is False
        assert result.score == 0.5
        assert result.details == details

    def test_score_validation_too_high(self):
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            VerificationResult(success=True, score=1.5)

    def test_score_validation_too_low(self):
        with pytest.raises(ValueError, match="between 0.0 and 1.0"):
            VerificationResult(success=False, score=-0.1)

    def test_score_boundary_zero(self):
        result = VerificationResult(success=False, score=0.0)
        assert result.score == 0.0

    def test_score_boundary_one(self):
        result = VerificationResult(success=True, score=1.0)
        assert result.score == 1.0


class TestTaskVerifierRegistry:
    """Tests for TaskVerifierRegistry."""

    @pytest.fixture()
    def fresh_registry(self):
        """Create a fresh registry for each test."""
        return TaskVerifierRegistry()

    def test_register_decorator(self, fresh_registry):
        @fresh_registry.register("my_task")
        def verify_my_task(adapter):
            return VerificationResult(success=True, score=1.0)

        assert fresh_registry.has_verifier("my_task")
        assert "my_task" in fresh_registry.list_verifiers()

    def test_register_decorator_returns_original_function(self, fresh_registry):
        """The decorator should return the original function unchanged."""

        @fresh_registry.register("my_task")
        def verify_my_task(adapter):
            return VerificationResult(success=True, score=1.0)

        # The function should still be callable directly
        result = verify_my_task("dummy_adapter")
        assert result.success is True

    def test_register_function_programmatic(self, fresh_registry):
        def verify_fn(adapter):
            return VerificationResult(success=True, score=1.0)

        fresh_registry.register_function("prog_task", verify_fn)
        assert fresh_registry.has_verifier("prog_task")

    def test_duplicate_registration_raises(self, fresh_registry):
        @fresh_registry.register("dup_task")
        def verify_first(adapter):
            return VerificationResult(success=True, score=1.0)

        with pytest.raises(ValueError, match="already registered"):

            @fresh_registry.register("dup_task")
            def verify_second(adapter):
                return VerificationResult(success=False, score=0.0)

    def test_duplicate_programmatic_raises(self, fresh_registry):
        def fn1(adapter):
            return VerificationResult(success=True, score=1.0)

        def fn2(adapter):
            return VerificationResult(success=False, score=0.0)

        fresh_registry.register_function("dup", fn1)
        with pytest.raises(ValueError, match="already registered"):
            fresh_registry.register_function("dup", fn2)

    def test_duplicate_programmatic_force(self, fresh_registry):
        def fn1(adapter):
            return VerificationResult(success=True, score=1.0)

        def fn2(adapter):
            return VerificationResult(success=False, score=0.0)

        fresh_registry.register_function("dup", fn1)
        fresh_registry.register_function("dup", fn2, force=True)

        # fn2 should be the registered verifier now
        result = fresh_registry.verify("dup", None)
        assert result.success is False

    def test_verify_with_mock_adapter(self, fresh_registry):
        """Test verify() calls the registered function with the adapter."""

        class MockAdapter:
            def run_powershell(self, script):
                return "0\n"

        @fresh_registry.register("test_task")
        def verify_test(adapter):
            output = adapter.run_powershell("Get-ChildItem | Measure")
            count = int(output.strip())
            return VerificationResult(
                success=count == 0,
                score=1.0 if count == 0 else 0.0,
                details={"count": count},
            )

        result = fresh_registry.verify("test_task", MockAdapter())
        assert result.success is True
        assert result.score == 1.0
        assert result.details["count"] == 0

    def test_verify_failure_case(self, fresh_registry):
        """Test verify() with a verifier that returns failure."""

        class MockAdapter:
            def run_powershell(self, script):
                return "42\n"

        @fresh_registry.register("check_files")
        def verify_files(adapter):
            count = int(adapter.run_powershell("count").strip())
            return VerificationResult(
                success=count == 0,
                score=0.0,
                details={"file_count": count},
            )

        result = fresh_registry.verify("check_files", MockAdapter())
        assert result.success is False
        assert result.score == 0.0
        assert result.details["file_count"] == 42

    def test_missing_verifier_raises_keyerror(self, fresh_registry):
        with pytest.raises(KeyError, match="No verifier registered"):
            fresh_registry.verify("nonexistent_task", None)

    def test_list_verifiers_empty(self, fresh_registry):
        assert fresh_registry.list_verifiers() == []

    def test_list_verifiers_sorted(self, fresh_registry):
        for name in ["zz_task", "aa_task", "mm_task"]:
            fresh_registry.register_function(
                name,
                lambda adapter: VerificationResult(success=True, score=1.0),
            )

        assert fresh_registry.list_verifiers() == [
            "aa_task",
            "mm_task",
            "zz_task",
        ]

    def test_has_verifier_false(self, fresh_registry):
        assert fresh_registry.has_verifier("nope") is False

    def test_has_verifier_true(self, fresh_registry):
        fresh_registry.register_function(
            "exists",
            lambda a: VerificationResult(success=True, score=1.0),
        )
        assert fresh_registry.has_verifier("exists") is True

    def test_get_verifier(self, fresh_registry):
        def my_fn(adapter):
            return VerificationResult(success=True, score=1.0)

        fresh_registry.register_function("lookup", my_fn)
        assert fresh_registry.get_verifier("lookup") is my_fn

    def test_get_verifier_missing(self, fresh_registry):
        with pytest.raises(KeyError, match="No verifier registered"):
            fresh_registry.get_verifier("missing")

    def test_clear(self, fresh_registry):
        fresh_registry.register_function(
            "a",
            lambda a: VerificationResult(success=True, score=1.0),
        )
        fresh_registry.register_function(
            "b",
            lambda a: VerificationResult(success=True, score=1.0),
        )
        assert len(fresh_registry) == 2

        fresh_registry.clear()
        assert len(fresh_registry) == 0
        assert fresh_registry.list_verifiers() == []

    def test_len(self, fresh_registry):
        assert len(fresh_registry) == 0

        fresh_registry.register_function(
            "t1",
            lambda a: VerificationResult(success=True, score=1.0),
        )
        assert len(fresh_registry) == 1

    def test_repr(self, fresh_registry):
        r = repr(fresh_registry)
        assert "TaskVerifierRegistry" in r
        assert "0 verifier(s)" in r

    def test_verify_wrong_return_type(self, fresh_registry):
        """Verifier returning non-VerificationResult should raise TypeError."""

        @fresh_registry.register("bad_return")
        def verify_bad(adapter):
            return {"success": True}  # Wrong type

        with pytest.raises(TypeError, match="expected VerificationResult"):
            fresh_registry.verify("bad_return", None)

    def test_verifier_exception_propagates(self, fresh_registry):
        """Exceptions from verifiers should propagate to caller."""

        @fresh_registry.register("raises")
        def verify_raises(adapter):
            raise ConnectionError("VM unreachable")

        with pytest.raises(ConnectionError, match="VM unreachable"):
            fresh_registry.verify("raises", None)


class TestGlobalRegistry:
    """Tests for the global registry and convenience aliases."""

    def test_global_registry_exists(self):
        from openadapt_evals.evaluation.verifier_registry import registry

        assert isinstance(registry, TaskVerifierRegistry)

    def test_register_alias_works(self):
        from openadapt_evals.evaluation.verifier_registry import (
            register as reg,
        )
        from openadapt_evals.evaluation.verifier_registry import (
            registry,
        )

        # Use a unique name to avoid conflicts with other tests
        task_name = "_test_global_alias_task"

        @reg(task_name)
        def verify_global_test(adapter):
            return VerificationResult(success=True, score=1.0)

        assert registry.has_verifier(task_name)

        # Clean up
        registry._verifiers.pop(task_name, None)

    def test_imports_from_evaluation_package(self):
        """Verify exports from openadapt_evals.evaluation."""
        from openadapt_evals.evaluation import (
            TaskVerifierRegistry,
            VerificationResult,
            register,
            registry,
        )

        assert TaskVerifierRegistry is not None
        assert VerificationResult is not None
        assert register is not None
        assert registry is not None


class TestBuiltinVerifiers:
    """Tests for built-in verifier registrations."""

    def test_clear_browsing_data_registered(self):
        """The clear_browsing_data verifier should be available after import."""
        # Import built-in verifiers to trigger registration
        import openadapt_evals.evaluation.builtin_verifiers  # noqa: F401
        from openadapt_evals.evaluation.verifier_registry import registry

        assert registry.has_verifier("clear_browsing_data")

    def test_clear_browsing_data_success(self):
        """Test the verifier with a mock adapter that reports empty cache."""
        import openadapt_evals.evaluation.builtin_verifiers  # noqa: F401
        from openadapt_evals.evaluation.verifier_registry import registry

        class MockAdapter:
            def run_powershell(self, script):
                return "0\n"

        result = registry.verify("clear_browsing_data", MockAdapter())
        assert result.success is True
        assert result.score == 1.0
        assert result.details["cache_file_count"] == 0

    def test_clear_browsing_data_failure(self):
        """Test the verifier with a mock adapter that reports non-empty cache."""
        import openadapt_evals.evaluation.builtin_verifiers  # noqa: F401
        from openadapt_evals.evaluation.verifier_registry import registry

        class MockAdapter:
            def run_powershell(self, script):
                return "157\n"

        result = registry.verify("clear_browsing_data", MockAdapter())
        assert result.success is False
        assert result.score == 0.0
        assert result.details["cache_file_count"] == 157

    def test_clear_browsing_data_error(self):
        """Test the verifier handles adapter errors gracefully."""
        import openadapt_evals.evaluation.builtin_verifiers  # noqa: F401
        from openadapt_evals.evaluation.verifier_registry import registry

        class BrokenAdapter:
            def run_powershell(self, script):
                raise ConnectionError("VM is down")

        result = registry.verify("clear_browsing_data", BrokenAdapter())
        assert result.success is False
        assert result.score == 0.0
        assert "error" in result.details
