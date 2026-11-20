"""Synthetic positive/negative observations; no host volume or process query."""

import importlib.util
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "diagnose_dmg_mounts", Path(__file__).resolve().parents[1] / "scripts/diagnose_dmg_mounts.py"
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def image(
    root, *, name="rw.41.Desktop_0.30.6_x64.dmg", device="/dev/disk7s1", mount="/Volumes/dmg.TEST"
):
    return {
        "image-path": str(root / name),
        "system-entities": [{"dev-entry": device, "mount-point": mount}],
    }


def response(stdout=b"p123\ncDiskImagesHelper\nu501\n", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout)


class Probes:
    def __init__(self, images, *, after=None, code=0, stdout=None):
        self.images, self.after, self.code, self.stdout = images, after, code, stdout
        self.calls = []
        self.info_calls = 0

    def __call__(self, argv, *, timeout):
        self.calls.append((argv, timeout))
        assert 0 < timeout <= 3
        if argv == diag.INFO:
            self.info_calls += 1
            value = self.after if self.after is not None and self.info_calls > 1 else self.images
            return response(plistlib.dumps({"images": value}))
        assert argv[:3] == diag.LSOF
        return response(
            self.stdout if self.stdout is not None else b"p123\ncDiskImagesHelper\nu501\n",
            self.code,
        )


def test_scoped_stable_handles_with_no_cleanup_or_argument_collection(tmp_path):
    probes = Probes(
        [image(tmp_path), image(tmp_path.parent / "unrelated")],
        stdout=b"p12\ncFinder\nu501\nnprivate-name-and-secret-argument\n",
    )
    report = diag.collect(tmp_path, run=probes)
    assert report["outcome"] == "observed"
    assert report["cleanup_authorized"] is False
    assert len(report["images"]) == 1
    assert report["images"][0]["filename_pid_hint"] == 41
    assert report["images"][0]["handles"][0]["processes"] == [
        {"pid": 12, "command": "Finder", "uid": 501}
    ]
    assert "private-name" not in json.dumps(report)
    assert [call[0] for call in probes.calls] == [
        diag.INFO,
        [*diag.LSOF, "--", str(tmp_path / "rw.41.Desktop_0.30.6_x64.dmg")],
        [*diag.LSOF, "+f", "--", "/Volumes/dmg.TEST"],
        diag.INFO,
    ]


@pytest.mark.parametrize(
    "name", ["Desktop.dmg", "rw.abc.Desktop.dmg", "rw.1.x.dmg/../other", "rw.1.Desktop.iso"]
)
def test_names_outside_the_temporary_image_model_never_trigger_handle_reads(tmp_path, name):
    probes = Probes([image(tmp_path, name=name)])
    report = diag.collect(tmp_path, run=probes)
    assert report["images"] == []
    assert all(call[0] == diag.INFO for call in probes.calls)


def test_symlink_escape_does_not_query_other_resources(tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    outside = tmp_path / "rw.1.Other.dmg"
    outside.touch()
    (root / "rw.1.Desktop.dmg").symlink_to(outside)
    probes = Probes([image(root, name="rw.1.Desktop.dmg")])
    assert diag.collect(root, run=probes)["images"] == []
    assert all(call[0] == diag.INFO for call in probes.calls)


@pytest.mark.parametrize(
    "device,mount",
    [
        ("/dev/disk7;kill", "/Volumes/dmg.A"),
        ("/dev/disk7", "/Volumes/Other"),
        ("/dev/disk7", "/Volumes/dmg.A/../Other"),
    ],
)
def test_malformed_scope_fails_before_handle_reads(tmp_path, device, mount):
    probes = Probes([image(tmp_path, device=device, mount=mount)])
    report = diag.collect(tmp_path, run=probes)
    assert report["outcome"] == "incomplete"
    assert report["images"] == []
    assert len(probes.calls) == 1


def test_identity_change_discards_tentative_process_attribution(tmp_path):
    probes = Probes([image(tmp_path)], after=[image(tmp_path, device="/dev/disk8s1")])
    report = diag.collect(tmp_path, run=probes)
    assert report["reason"] == "resource_identity_changed"
    assert report["images"] == []
    assert "DiskImagesHelper" not in json.dumps(report)


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not plist",
        b"<plist/>\n",
        plistlib.dumps([]),
        plistlib.dumps({"images": "invalid"}),
        b"x" * (diag.MAX_BYTES + 1),
    ],
)
def test_bad_or_oversized_metadata_is_not_partial_success(tmp_path, raw):
    report = diag.collect(tmp_path, run=lambda *a, **k: response(raw))
    assert report["outcome"] == "incomplete" and report["images"] == []


@pytest.mark.parametrize(
    "code,raw",
    [
        (1, b""),
        (2, b""),
        (0, b"p1\ncFinder\n"),
        (0, b"p1\ncA\ncB\nu501\n"),
        (0, b"p1\ncA\nu501\np1\ncB\nu501\n"),
        (0, b"p1\ncprivate/secret\nu501\n"),
    ],
)
def test_unavailable_or_invalid_handles_do_not_mean_no_holder(tmp_path, code, raw):
    report = diag.collect(tmp_path, run=Probes([image(tmp_path)], code=code, stdout=raw))
    assert report["outcome"] == "incomplete" and report["images"] == []
    assert "private/secret" not in json.dumps(report)


def test_duplicate_images_are_ambiguous_before_handle_reads(tmp_path):
    probes = Probes([image(tmp_path), image(tmp_path)])
    assert diag.collect(tmp_path, run=probes)["outcome"] == "incomplete"
    assert len(probes.calls) == 1


def test_total_budget_does_not_sleep_retry_or_claim_partial_success(tmp_path):
    calls = []
    now = [0]

    def run(argv, *, timeout):
        calls.append(argv)
        now[0] += 9
        return response(plistlib.dumps({"images": [image(tmp_path)]}))

    report = diag.collect(tmp_path, run=run, clock=lambda: now[0])
    assert report["reason"] == "diagnostic_budget_exhausted"
    assert len(calls) == 1 and report["images"] == []


@pytest.mark.parametrize("error", [OSError("secret"), subprocess.TimeoutExpired("private-argv", 3)])
def test_probe_errors_are_redacted(tmp_path, error):
    def fail(*args, **kwargs):
        raise error

    report = diag.collect(tmp_path, run=fail)
    assert report["outcome"] == "incomplete" and "secret" not in json.dumps(report)
    assert "private-argv" not in json.dumps(report)


def test_workflow_keeps_build_failure_and_observes_only_failed_macos_bundles():
    import yaml

    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / ".github/workflows/desktop-build.yml").read_text())
    steps = workflow["jobs"]["native-installers"]["steps"]
    build = next(step for step in steps if step.get("id") == "native_bundle")
    diagnostic = next(
        step
        for step in steps
        if step.get("name") == "Read scoped DMG diagnostics after bundle failure"
    )
    assert "continue-on-error" not in build and "continue-on-error" not in diagnostic
    expected_command = " ".join(
        [
            "npm run tauri build --",
            "--bundles ${{ matrix.bundles }}",
            "--target ${{ matrix.target }}",
            "--config src-tauri/tauri.adhoc.conf.json",
        ]
    )
    assert build["run"] == expected_command
    assert (
        diagnostic["if"]
        == "${{ failure() && runner.os == 'macOS' && steps.native_bundle.outcome == 'failure' }}"
    )
    assert diagnostic["timeout-minutes"] == 1
    assert set(build["env"]) == {"TAURI_CLI_VERBOSITY"}
    assert build["env"]["TAURI_CLI_VERBOSITY"] == "${{ runner.os == 'macOS' && '1' || '0' }}"
    assert steps.index(diagnostic) == steps.index(build) + 1


@pytest.fixture
def owned_query_children(monkeypatch):
    """Track/reap only harmless children this fixture starts; never query the host."""
    real_popen = diag.subprocess.Popen
    records = []

    def start(*args, **kwargs):
        child = real_popen(*args, **kwargs)
        record = {"child": child, "stdout_fd": child.stdout.fileno(), "kill_calls": 0}
        real_kill = child.kill

        def kill():
            record["kill_calls"] += 1
            return real_kill()

        monkeypatch.setattr(child, "kill", kill)
        records.append(record)
        return child

    monkeypatch.setattr(diag.subprocess, "Popen", start)
    yield records
    # Failure cleanup for the test harness also uses only its retained handles.
    for record in records:
        child = record["child"]
        if child.poll() is None:
            child.kill()
            child.wait(timeout=1)


@pytest.mark.skipif(os.name != "posix", reason="macOS/Linux bounded pipe reader")
@pytest.mark.parametrize(
    "cap,size", [(1024, 0), (1024, 1), (1024, 1023), (1024, 1024), (70000, 70000)]
)
def test_query_child_at_or_below_cap_keeps_exact_bytes_without_termination(
    cap, size, monkeypatch, owned_query_children
):
    monkeypatch.setattr(diag, "MAX_BYTES", cap)
    result = diag.probe([sys.executable, "-c", f"import os; os.write(1, b'x' * {size})"], timeout=2)
    assert result.returncode == 0 and result.stdout == b"x" * size
    assert owned_query_children[0]["kill_calls"] == 0
    assert owned_query_children[0]["child"].poll() == 0


@pytest.mark.skipif(os.name != "posix", reason="macOS/Linux bounded pipe reader")
@pytest.mark.parametrize("cap", [1024, 70000])
def test_overflow_is_detected_during_read_and_reaps_only_its_live_query_child(
    cap, monkeypatch, owned_query_children
):
    monkeypatch.setattr(diag, "MAX_BYTES", cap)
    real_read = diag.os.read
    reads = []

    def read(fd, requested):
        value = real_read(fd, requested)
        if any(record["stdout_fd"] == fd for record in owned_query_children):
            reads.append((requested, len(value)))
        return value

    monkeypatch.setattr(diag.os, "read", read)
    with pytest.raises(diag.Incomplete, match="query_output_limit"):
        diag.probe(
            [
                sys.executable,
                "-c",
                f"import os,time; os.write(1,b'x'*{max(65536, cap * 2)}); time.sleep(5)",
            ],
            timeout=0.8,
        )
    assert reads and sum(length for _, length in reads) <= cap + 1
    assert max(requested for requested, _ in reads) <= min(65536, cap + 1)
    assert len(owned_query_children) == 1
    assert owned_query_children[0]["kill_calls"] == 1
    assert owned_query_children[0]["child"].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="macOS/Linux bounded pipe reader")
@pytest.mark.parametrize("before_wait", ["", "os.write(1,b'small');", "os.close(1);"])
def test_silent_partial_and_eof_stalls_preserve_deadline_and_reap_owned_child(
    before_wait, owned_query_children
):
    with pytest.raises(diag.Incomplete, match="query_deadline_exceeded"):
        diag.probe(
            [sys.executable, "-c", f"import os,time; {before_wait} time.sleep(5)"], timeout=0.8
        )
    assert owned_query_children[0]["kill_calls"] == 1
    assert owned_query_children[0]["child"].poll() is not None


@pytest.mark.skipif(os.name != "posix", reason="macOS/Linux bounded pipe reader")
def test_query_failure_exit_is_not_relabelled_success(owned_query_children):
    result = diag.probe([sys.executable, "-c", "raise SystemExit(7)"], timeout=2)
    assert result.returncode == 7
    assert owned_query_children[0]["kill_calls"] == 0


def test_insufficient_query_budget_starts_no_child(owned_query_children):
    with pytest.raises(diag.Incomplete, match="query_budget_or_platform_unavailable"):
        diag.probe([sys.executable, "-c", "pass"], timeout=0.01)
    assert owned_query_children == []


def test_final_parse_cannot_turn_an_exhausted_total_budget_into_observed(tmp_path):
    now = [0]
    count = [0]

    def run(argv, *, timeout):
        count[0] += 1
        if count[0] == 2:
            now[0] = 9
        return response(plistlib.dumps({"images": []}))

    report = diag.collect(tmp_path, run=run, clock=lambda: now[0])
    assert report["reason"] == "diagnostic_budget_exhausted"
    assert report["images"] == []
