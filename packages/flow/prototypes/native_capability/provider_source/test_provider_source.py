"""Pure planted record faults, never a native/provider/SDK test."""

import copy
import hashlib
import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "own_provider_audit", ROOT / "provider_audit.py"
)
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)


def records(case=0, pattern=0):
    rows = []

    def add(event, argument=0, phase=5, kind=0, boolean=0, hr=0):
        rows.append(
            dict(
                role="trace",
                ordinal=len(rows),
                event=event,
                argument=argument,
                hr=hr,
                phase=phase,
                thread=71,
                generation=1,
                kind=kind,
                boolean_observed=kind == 11 and hr == 0,
                boolean=boolean,
            )
        )

    def private(phase, kind=11, boolean=0, hr=0):
        add(4, 30019, phase)
        add(16, 30019, phase, kind, boolean, hr)
        add(15, 100, phase, kind, boolean, hr)

    def guard(stage, sensitive=False, owner=False, expired=False):
        add(11, 0, stage)
        if owner:
            add(1, phase=6)
            add(11, 303, stage)
            return
        if expired:
            add(11, 301, stage)
            return
        if sensitive:
            private(stage, boolean=-1)
            add(11, 307, stage)
            return
        private(stage)
        private(stage)
        add(11, 200, stage)

    for getter in range(5):
        add(18, getter, hr=-2147418113)  # E_UNEXPECTED own getter oracle.
    add(8, 4)
    add(0)
    scope = dict(
        hwnd=23,
        pid=41,
        tid=71,
        session=1,
        creation_low=7,
        creation_high=2,
        generation=1,
        runtime_id=[],
    )
    result = dict(
        role="result",
        case=case,
        pattern=pattern,
        bound=True,
        guard_reason=0,
        capability=1,
        capability_reason=6,
        acquire_attempted=True,
        acquire_hr=0,
        acquire_nonnull=True,
        query_attempted=True,
        query_hr=0,
        query_nonnull=True,
        forbidden=0,
        unexplained=0,
        trace_overflow=False,
        live_objects=0,
        trace_clean=True,
        expected_outcome=True,
        observed_scope={**scope, "runtime_id": [2, -7, 23]},
        oracle_scope=scope,
        runtime_adoption=False,
    )
    no_bind = case in (4, 5, 6, 7, 9, 10)
    if no_bind:
        result.update(
            bound=False,
            capability=0,
            capability_reason=0,
            acquire_attempted=False,
            acquire_nonnull=False,
            query_attempted=False,
            query_nonnull=False,
            observed_scope=None,
        )
        reason = {4: 7, 5: 8, 6: 8, 7: 8, 9: 3, 10: 6}[case]
        result["guard_reason"] = reason
        if case == 4:
            private(5, boolean=-1)
        if case == 5:
            private(5, kind=13)
        if case == 6:
            private(5, kind=3)
        if case == 7:
            private(5, hr=-2147467259, kind=0)
        add(11, 300 + reason)
    else:
        private(5)
        private(5)
        guard(0, sensitive=case == 12, expired=case == 11, owner=case == 15)
        if case in (11, 12, 15):
            result.update(
                guard_reason={11: 1, 12: 7, 15: 3}[case],
                capability=0,
                capability_reason=1,
                acquire_attempted=False,
                acquire_nonnull=False,
                query_attempted=False,
                query_nonnull=False,
            )
        else:
            pattern_id = (10015, 10010, 10002)[pattern]
            add(5, pattern_id, 0)
            add(
                19,
                -pattern_id if case in (1, 2, 3) else pattern_id,
                0,
                hr=AUDIT.NOT_SUPPORTED
                if case == 2
                else -2147467259
                if case == 3
                else 0,
            )
            if case in (1, 2, 3):
                result.update(
                    capability=2 if case == 2 else 0,
                    capability_reason={1: 4, 2: 7, 3: 2}[case],
                    acquire_hr=AUDIT.NOT_SUPPORTED
                    if case == 2
                    else -2147467259
                    if case == 3
                    else 0,
                    acquire_nonnull=False,
                    query_attempted=False,
                    query_nonnull=False,
                )
                if case == 2:
                    guard(4)
            else:
                guard(1, sensitive=case == 13)
                if case == 13:
                    result.update(
                        guard_reason=7,
                        capability=0,
                        capability_reason=1,
                        query_attempted=False,
                        query_nonnull=False,
                    )
                else:
                    add(6, pattern + 1, 1)
                    guard(2, sensitive=case == 14)
                    if case == 14:
                        result.update(guard_reason=7, capability=0, capability_reason=1)
                    else:
                        add(8, phase=2)
                        add(8, phase=2)
                        guard(3)
        if case == 8:
            result["expected_outcome"] = False
    if case != 15:
        add(1, phase=6)
    add(8, phase=6)
    add(17, 1, phase=6)
    rows.append(result)
    return rows


class ProviderSourceTests(unittest.TestCase):
    def test_closed_case_family(self):
        for case in range(16):
            for pattern in range(3):
                with self.subTest(case=case, pattern=pattern):
                    self.assertEqual(
                        AUDIT.audit(records(case, pattern), case, pattern), case != 8
                    )

    def test_raw_privacy_types_and_hresult(self):
        for status in (0, 1, -1, True, 0.0):
            for kind in (0, 3, 11, 13, 11.0, True, 0x400B):
                for value in (0, -1, 1, True, False, None, 0.0):
                    wanted = (
                        ("non_sensitive" if value == 0 else "sensitive")
                        if (
                            type(status) is int
                            and status == 0
                            and type(kind) is int
                            and kind == 11
                            and type(value) is int
                            and value in (0, -1)
                        )
                        else "unknown"
                    )
                    self.assertEqual(AUDIT.privacy(status, kind, value), wanted)

    def test_full_observed_scope_no_substitution(self):
        for key in AUDIT.SCOPE_KEYS - {"runtime_id"}:
            rows = records()
            rows[-1]["observed_scope"][key] += 1
            self.assertFalse(AUDIT.audit(rows, 0, 0), key)
        for runtime in ([], None, [True], [2**31], list(range(65))):
            rows = records()
            rows[-1]["observed_scope"]["runtime_id"] = runtime
            self.assertFalse(AUDIT.audit(rows, 0, 0))
        rows = records()
        rows[-1]["observed_scope"]["runtime_id"] = [-(2**31), 0, 2**31 - 1]
        self.assertTrue(AUDIT.audit(rows, 0, 0))  # Opaque, no prefix/token decoding.

    def test_no_caption_or_semantic_error_downgrade(self):
        for event in (9, 10, 12, 13, 14):
            rows = records()
            rows[7]["event"] = event
            self.assertFalse(AUDIT.audit(rows, 0, 0))
        rows = records()
        rows[7]["argument"] = 30005
        self.assertFalse(AUDIT.audit(rows, 0, 0))

    def test_entire_bootstrap_is_counted(self):
        rows = records()
        rows[0]["event"] = 12
        self.assertFalse(AUDIT.audit(rows, 0, 0))
        rows = records()[6:]
        for ordinal, row in enumerate(rows[:-1]):
            row["ordinal"] = ordinal
        self.assertFalse(AUDIT.audit(rows, 0, 0))

    def test_oracle_must_be_live_and_complete(self):
        for getter in range(5):
            rows = records()
            rows[getter]["argument"] = 9
            self.assertFalse(AUDIT.audit(rows, 0, 0))

    def test_normal_null_and_error_cannot_promote(self):
        for case in (1, 2, 3):
            rows = records(case)
            rows[-1].update(capability=1, capability_reason=6)
            self.assertFalse(AUDIT.audit(rows, case, 0))

    def test_unknown_privacy_short_circuits(self):
        rows = records(5)
        refused = next(
            i
            for i, row in enumerate(rows[:-1])
            if row["event"] == 11 and row["argument"] == 308
        )
        planted = copy.deepcopy(rows[7])
        planted.update(event=4, argument=30019)
        rows.insert(refused + 1, planted)
        for ordinal, row in enumerate(rows[:-1]):
            row["ordinal"] = ordinal
        self.assertFalse(AUDIT.audit(rows, 5, 0))

    def test_sensitive_cannot_resume_a_later_phase(self):
        for case in (12, 13, 14):
            rows = records(case)
            refused = next(
                i
                for i, row in enumerate(rows[:-1])
                if row["event"] == 11 and row["argument"] == 307
            )
            rows[refused + 1]["event"] = 6
            self.assertFalse(AUDIT.audit(rows, case, 0))

    def test_checkpoint_census_and_no_skipped_privacy(self):
        for field, value in (("phase", 4), ("argument", 0)):
            rows = records()
            index = next(
                i
                for i, row in enumerate(rows[:-1])
                if row["event"] == 11 and row["argument"] == 200
            )
            rows[index][field] = value
            self.assertFalse(AUDIT.audit(rows, 0, 0))
        rows = records()
        index = next(
            i
            for i, row in enumerate(rows[:-1])
            if row["event"] == 15 and row["phase"] == 2
        )
        rows.pop(index)
        for ordinal, row in enumerate(rows[:-1]):
            row["ordinal"] = ordinal
        self.assertFalse(AUDIT.audit(rows, 0, 0))

    def test_closed_shapes_and_strict_types(self):
        for field, value in (
            ("bound", 1),
            ("capability", True),
            ("trace_clean", 1),
            ("expected_outcome", True),
            ("unexplained", 1),
            ("trace_overflow", True),
            ("runtime_adoption", True),
        ):
            rows = records()
            rows[-1][field] = value
            self.assertEqual(AUDIT.audit(rows, 0, 0), field == "expected_outcome")
        rows = records()
        rows[-1]["provider_exception"] = "not permitted"
        self.assertFalse(AUDIT.audit(rows, 0, 0))
        self.assertFalse(AUDIT.audit(records(), True, 0))

    def test_duplicate_missing_extra_ordinal(self):
        for mutation in (
            lambda r: r.insert(5, copy.deepcopy(r[4])),
            lambda r: r.pop(5),
            lambda r: r.append(copy.deepcopy(r[-1])),
        ):
            rows = records()
            mutation(rows)
            self.assertFalse(AUDIT.audit(rows, 0, 0))

    def test_vt_empty_intent_never_claims_unknown(self):
        rows = records(8)
        for row in rows[:-1]:
            if row["event"] == 16 and row["argument"] == 30019:
                row.update(kind=0, boolean_observed=False)
        self.assertFalse(AUDIT.audit(rows, 8, 0))

    def test_request_identity_not_other_case(self):
        self.assertFalse(AUDIT.audit(records(), 1, 0))
        self.assertFalse(AUDIT.audit(records(), 0, 1))

    def test_source_boundary_and_frozen_harness(self):
        native = ROOT.parent
        expected = {
            "sdk_capability.cpp": "e6e38e71c14b5d73aac7c1f68dc9e366450aba71b292884395cb4d2ef58a3639",
            "sdk_capability.h": "35800bddab432003a5677ee07c25d0881ff1a653692cad4a37a75baa66c2eadb",
            "run_abi_fixture.py": "6ed593c74f06518d4e9c7fc73626a362b8da1b1ee30ec53b635ebcd93062ed39",
        }
        for name, sha in expected.items():
            self.assertEqual(
                hashlib.sha256((native / name).read_bytes()).hexdigest(), sha
            )
        project = native.parents[1]
        import tomllib

        package = tomllib.loads((project / "pyproject.toml").read_text())
        self.assertIn(
            "/prototypes",
            package["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"],
        )
        self.assertEqual(
            package["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"],
            ["openadapt_flow"],
        )


class HarnessSourceTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from types import SimpleNamespace

        self.temporary = tempfile.TemporaryDirectory(prefix="owned-provider-pure-")
        self.root = Path(self.temporary.name)
        self.out = self.root / "out"
        self.out.mkdir()
        self.runner = self.load(ROOT / "run_provider_source.py", "own_provider_runner")
        self.abi = self.load(ROOT.parent / "run_abi_fixture.py", "own_frozen_abi")
        self.options = SimpleNamespace(
            case=0, pattern=1, vcvars=Path("owned-vcvars"), sdk="10.0.22621.0"
        )
        self.receipt, self.calls = {}, []

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def load(path, name):
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        assert Path(module.__file__).resolve() == path.resolve()
        assert Path(module.__spec__.origin).resolve() == path.resolve()
        return module

    def fake(self, fault=None):
        from types import SimpleNamespace

        valid = dict(
            started=True,
            timeout=False,
            exit_code=0,
            owned_process_stopped=True,
            job_assigned=True,
            job_empty=True,
            job_closed=True,
        )

        def bounded(command, root, log, timeout, deadline, support, error_log=None):
            self.calls.append(command)
            if len(self.calls) == 1:
                log.write_bytes(
                    b"" if fault == "zero_capture" else self.abi.CAPTURE_STDOUT
                )
                error_log.write_bytes(self.abi.CAPTURE_STDERR)
                if fault == "job_unknown":
                    return {**valid, "job_empty": False}
            elif len(self.calls) == 2:
                log.write_bytes(
                    b"" if fault == "empty_compiler" else b"owned compiler category\n"
                )
                (self.out / "provider_fixture.exe").write_bytes(
                    b"owned fake binary, NOT native"
                )
            else:
                rows = records(0, 1)
                if fault == "bad_case":
                    rows[-1]["case"] = 3
                log.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
                error_log.write_bytes(b"unexpected" if fault == "stderr" else b"")
            return valid.copy()

        return SimpleNamespace(
            bounded=bounded,
            completed_ok=self.abi.completed_ok,
            capture_control_evidence=self.abi.capture_control_evidence,
        )

    def execute(self, fault=None):
        return self.runner.execute(
            self.root,
            self.out,
            None,
            self.fake(fault),
            AUDIT,
            self.options,
            Path("owned-compiler"),
            self.receipt,
        )

    def test_same_shared_capture_job_precedes_compile_and_one_case(self):
        self.assertEqual(self.execute(), 0)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(
            self.calls[-1], [str(self.out / "provider_fixture.exe"), "0", "1"]
        )
        self.assertTrue(self.receipt["category_audit"])

    def test_original_zero_streams_and_unknown_job_stop_before_compile(self):
        for fault in ("zero_capture", "job_unknown"):
            with self.subTest(fault=fault):
                self.calls.clear()
                self.assertEqual(self.execute(fault), 2)
                self.assertEqual(len(self.calls), 1)

    def test_empty_compiler_capture_stops_before_provider(self):
        self.assertEqual(self.execute("empty_compiler"), 3)
        self.assertEqual(len(self.calls), 2)

    def test_stderr_and_case_mismatch_never_qualify(self):
        self.assertEqual(self.execute("stderr"), 4)
        self.calls.clear()
        self.assertEqual(self.execute("bad_case"), 5)

    def test_exact_frozen_source_loader_and_origin_refusal(self):
        path = ROOT.parent / "run_abi_fixture.py"
        loaded = self.runner.load_source(
            path, "own_readonly_abi", self.runner.ABI_RUNNER_SHA256
        )
        self.assertEqual(Path(loaded.__file__).resolve(), path.resolve())
        with self.assertRaisesRegex(RuntimeError, "frozen_source_mismatch"):
            self.runner.load_source(path, "must_not_load", "0" * 64)


if __name__ == "__main__":
    origin = {
        "file": AUDIT.__file__,
        "spec": AUDIT.__spec__.origin,
        "sha256": hashlib.sha256(Path(AUDIT.__file__).read_bytes()).hexdigest(),
        "flow_types_loaded": [
            name
            for name in sys.modules
            if name.startswith(("openadapt_flow", "openadapt_types"))
        ],
    }
    assert Path(origin["file"]).resolve() == (ROOT / "provider_audit.py").resolve()
    assert origin["file"] == origin["spec"] and not origin["flow_types_loaded"]
    print(json.dumps(origin), flush=True)
    unittest.main(verbosity=2)
