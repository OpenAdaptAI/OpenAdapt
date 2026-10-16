"""CLI smoke and seam-contract tests.

Issue #999: `openadapt serve` and `openadapt train start` were broken
for months while CI stayed green, because cli.py's imports of
openadapt-ml only execute inside command bodies and the broad
`except ImportError` handlers reported every failure as
"openadapt-ml not installed".

Three layers of defense here:

1. test_every_command_help — walks the whole Click tree and renders
   --help for every command, so any module-level wiring error fails CI.
2. Contract tests — monkeypatch the openadapt-ml entry points and
   assert cli.py calls them the way they're actually shaped today.
3. test_cmd_serve_reads_only_provided_args — parses the installed
   openadapt-ml's cmd_serve and asserts every `args.<attr>` it reads is
   provided by cli.py's Namespace, so the seam can't drift silently in
   either direction.

The openadapt-ml-dependent tests skip when it isn't installed; CI
installs it so they always run there.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from openadapt.cli import _correct_released_flow_x11_sonames, _supported_flow_version
from openadapt.cli import main as cli_main

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 CI
    import tomli as tomllib

# Namespace attributes cli.py's serve command provides to cmd_serve.
# Keep in sync with openadapt/cli.py::serve.
SERVE_NAMESPACE_ATTRS = {
    "port",
    "benchmark",
    "no_regenerate",
    "start_page",
    "quiet",
    "open",
}


def _iter_commands(group, prefix=()):
    yield prefix, group
    if isinstance(group, click.Group):
        for name, cmd in group.commands.items():
            yield from _iter_commands(cmd, prefix + (name,))


def test_every_command_help():
    """Render --help for every command in the tree."""
    runner = CliRunner()
    failures = []
    for path, _cmd in _iter_commands(cli_main):
        args = list(path) + ["--help"]
        result = runner.invoke(cli_main, args)
        if result.exit_code != 0:
            failures.append(f"{' '.join(args)!r} exited {result.exit_code}")
    assert not failures, "Commands whose --help failed:\n  " + "\n  ".join(failures)


def test_version_command():
    runner = CliRunner()
    result = runner.invoke(cli_main, ["version"])
    assert result.exit_code == 0


def test_quickstart_runs_one_local_lifecycle_without_overwriting(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "first-run"])
        assert result.exit_code == 0, result.output

    assert len(calls) == 1
    assert calls[0][0] == "tutorial"
    assert calls[0][1] == "--out"
    assert calls[0][3:] == ["--name", "local-quickstart"]
    assert "openadapt quickstart --break-it" in result.output


def test_quickstart_forwards_the_headed_tutorial_option(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda argv: calls.append(list(argv)) or 0,
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main,
            ["quickstart", "--headed", "--out", "headed-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == "tutorial"
    assert calls[0][-1] == "--headed"


def _engine_writing_broken_report(calls, report_text):
    """Fake engine that records argv and writes the halted rerun's report."""

    def invoke(argv):
        calls.append(list(argv))
        if report_text is not None:
            broken = Path(argv[argv.index("--out") + 1]) / "run-broken"
            broken.mkdir(parents=True)
            (broken / "report.json").write_text(report_text, encoding="utf-8")
        return 0

    return invoke


def test_quickstart_forwards_the_break_it_option_and_names_the_evidence(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        _engine_writing_broken_report(
            calls,
            '{"execution_outcome": "HALTED", '
            '"transaction_outcome": "RECONCILIATION_REQUIRED"}',
        ),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main,
            ["quickstart", "--break-it", "--out", "break-run"],
        )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][0] == "tutorial"
    assert calls[0][-1] == "--break-it"
    assert "run-broken" in result.output
    assert "HALTED" in result.output
    # The halted rerun ends RECONCILIATION_REQUIRED. Nothing may claim the
    # store is unchanged or that a retry is safe.
    assert "unchanged" not in result.output
    # vocab-allow: no-change-claim -- asserts that the claim is absent
    assert "nothing was written" not in result.output
    assert (
        "Result: check the record (transaction outcome RECONCILIATION_REQUIRED)"
        in result.output
    )
    assert "nothing is retried until a person checks the record" in result.output


@pytest.mark.parametrize(
    ("report_text", "expected"),
    [
        (
            '{"transaction_outcome": "HALTED_BEFORE_EFFECT"}',
            "Result: stopped before saving (transaction outcome "
            "HALTED_BEFORE_EFFECT). The engine proved nothing was written.",
        ),
        (
            '{"transaction_outcome": "RECONCILIATION_REQUIRED"}',
            "Result: check the record (transaction outcome RECONCILIATION_REQUIRED)",
        ),
        # A value this launcher doesn't know, a coarse HALTED with no
        # transaction outcome, an unreadable report, or no report at all
        # can't prove that nothing was written.
        (
            '{"transaction_outcome": "SOMETHING_NEW"}',
            "Result: check the record (transaction outcome SOMETHING_NEW)",
        ),
        (
            '{"execution_outcome": "HALTED"}',
            "Result: check the record (transaction outcome unknown)",
        ),
        ("not json", "Result: check the record (transaction outcome unknown)"),
        ("[]", "Result: check the record (transaction outcome unknown)"),
        (None, "Result: check the record (transaction outcome unknown)"),
    ],
)
def test_quickstart_derives_the_halted_rerun_result_from_its_report(
    monkeypatch, report_text, expected
):
    calls = []
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        _engine_writing_broken_report(calls, report_text),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(
            cli_main, ["quickstart", "--break-it", "--out", "break-run"]
        )

    assert result.exit_code == 0, result.output
    assert expected in result.output
    if "HALTED_BEFORE_EFFECT" not in expected:
        assert "nothing was written" not in result.output
        assert "A save may have gone through" in result.output


def test_quickstart_restores_the_operator_scrub_setting(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    monkeypatch.setattr("openadapt.cli._invoke_flow", lambda _argv: 0)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "first-run"])

    assert result.exit_code == 0, result.output
    assert os.environ["OPENADAPT_FLOW_SCRUB"] == "auto"


def test_quickstart_retains_artifacts_when_the_engine_halts(monkeypatch):
    def halt_after_writing(argv):
        root = Path(argv[argv.index("--out") + 1])
        root.mkdir(parents=True)
        (root / "halt-evidence.json").write_text("{}")
        return 2

    monkeypatch.setattr("openadapt.cli._invoke_flow", halt_after_writing)

    runner = CliRunner()
    with runner.isolated_filesystem():
        result = runner.invoke(cli_main, ["quickstart", "--out", "halted-run"])

        assert result.exit_code != 0
        assert Path("halted-run/halt-evidence.json").is_file()


def test_quickstart_refuses_an_existing_output(monkeypatch):
    monkeypatch.setattr(
        "openadapt.cli._invoke_flow",
        lambda _argv: pytest.fail("engine must not run before output refusal"),
    )

    runner = CliRunner()
    with runner.isolated_filesystem():
        Path("existing").mkdir()
        result = runner.invoke(cli_main, ["quickstart", "--out", "existing"])

    assert result.exit_code != 0
    assert "Output already exists" in result.output


def test_version_flag_matches_installed_metadata():
    """`openadapt --version` must print the real installed distribution
    version (importlib.metadata), never a hardcoded string that can drift
    from pyproject.toml. See openadapt/version.py."""
    from importlib.metadata import version as dist_version

    expected = dist_version("openadapt")

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--version"])
    assert result.exit_code == 0, result.output
    assert expected in result.output
    # And it must not be the old hardcoded value unless that is genuinely
    # the installed version.
    from openadapt.version import __version__

    assert __version__ == expected


def test_distribution_metadata_does_not_publish_a_static_lifecycle():
    """The signed public record, not immutable package metadata, owns maturity."""
    from importlib.metadata import metadata

    classifiers = metadata("openadapt").get_all("Classifier") or []
    lifecycle = [
        classifier
        for classifier in classifiers
        if classifier.startswith("Development Status :: ")
    ]
    assert lifecycle == []


def test_distribution_metadata_installs_the_launcher_on_newer_python():
    """An upper Requires-Python bound made pip on 3.13+ backtrack to 1.3.0.

    The launcher installs on any Python >=3.10; the engine dependencies carry
    the version marker instead (tests/test_python_support.py).
    """
    from importlib.metadata import metadata

    actual = metadata("openadapt")["Requires-Python"]
    assert {term.strip() for term in actual.split(",")} == {">=3.10"}


def test_doctor_lists_quickstart_dependencies_as_core_not_extras(monkeypatch):
    """`openadapt doctor` must treat Flow and Playwright as base dependencies.

    The other capabilities stay optional and must not cause a failure.
    """
    core = {"openadapt_flow", "openadapt_agent", "playwright"}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in core else None,
    )
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._missing_chromium_system_libs", lambda: []
    )
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._chromium_present", lambda: False
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["doctor"])
    assert result.exit_code == 0, result.output
    out = result.output

    core_idx = out.index("Core packages")
    optional_idx = out.index("Optional packages")
    assert core_idx < optional_idx

    core_section = out[core_idx:optional_idx]
    optional_section = out[optional_idx:]

    # Flow, the agent bridge, and the browser tutorial driver are core.
    assert "openadapt_flow" in core_section
    assert "openadapt_agent" in core_section
    assert "playwright" in core_section
    assert "playwright" not in optional_section
    # The excluded-by-default extras must appear only in the optional
    # section and must not be reported as [MISSING].
    for extra_pkg in (
        "openadapt_capture",
        "openadapt_ml",
        "openadapt_evals",
        "openadapt_viewer",
    ):
        assert extra_pkg in optional_section
        assert extra_pkg not in core_section
    assert "[MISSING]" not in optional_section
    # Optional section tells the user how to install extras.
    assert 'pip install "openadapt[' in optional_section


def test_launcher_flow_and_substrate_extras_metadata():
    """Install routes resolve the Flow release whose replay parser exposes
    every native/remote backend, without installing OS bindings elsewhere.

    Every engine requirement also carries the supported-Python marker, so pip
    on Python 3.13+ installs this launcher (which prints the remedy) instead
    of backtracking to an unrelated old release."""
    metadata = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text()
    )["project"]
    extras = metadata["optional-dependencies"]
    supported = "python_version < '3.13'"

    assert (
        metadata["dependencies"].count(
            f"openadapt-flow[browser,hosted]>=2.0.0,<3.0.0; {supported}"
        )
        == 1
    )
    assert (
        metadata["dependencies"].count(f"openadapt-agent>=2.1.0,<3; {supported}") == 1
    )
    assert extras["flow"] == [f"openadapt-flow>=2.0.0,<3.0.0; {supported}"]
    assert extras["agent"] == [f"openadapt-agent>=2.1.0,<3; {supported}"]
    assert extras["browser"] == [f"openadapt-flow[browser]>=2.0.0,<3.0.0; {supported}"]
    assert extras["privacy"] == [f"openadapt-flow[privacy]>=2.0.0,<3.0.0; {supported}"]
    assert extras["capture"] == [
        "openadapt-capture>=1.2.0,<2.0.0",
        f"openadapt-flow[capture]>=2.0.0,<3.0.0; {supported}",
    ]
    assert extras["windows"] == [f"openadapt-flow[windows]>=2.0.0,<3.0.0; {supported}"]
    assert extras["macos"] == [
        f"openadapt-flow[macos]>=2.0.0,<3.0.0; sys_platform == 'darwin' and {supported}"
    ]
    assert extras["linux"] == [
        f"openadapt-flow[linux]>=2.0.0,<3.0.0; sys_platform == 'linux' and {supported}"
    ]
    assert extras["rdp"] == [f"openadapt-flow[rdp]>=2.0.0,<3.0.0; {supported}"]
    assert extras["all"] == [
        "openadapt[browser,core,grounding,retrieval,privacy,flow,windows,rdp,agent]",
        "openadapt[macos]; sys_platform == 'darwin'",
        "openadapt[linux]; sys_platform == 'linux'",
    ]


def test_released_flow_x11_sonames_are_case_corrected(monkeypatch):
    import openadapt_flow._browser_setup as browser_setup

    monkeypatch.setattr(
        browser_setup,
        "_LINUX_CHROMIUM_SONAMES",
        ("nss3", "xcomposite", "xdamage", "xfixes", "xrandr", "gbm"),
        raising=False,
    )
    monkeypatch.setattr(sys, "platform", "linux")

    _correct_released_flow_x11_sonames()

    assert browser_setup._LINUX_CHROMIUM_SONAMES == (
        "nss3",
        "Xcomposite",
        "Xdamage",
        "Xfixes",
        "Xrandr",
        "gbm",
    )


@pytest.mark.parametrize("version", ["2.0.0", "2.0.1", "2.99.0"])
def test_launcher_accepts_flow_versions_in_the_supported_range(version):
    assert _supported_flow_version(version)


@pytest.mark.parametrize("version", ["1.28.9", "1.35.1", "3.0.0", "invalid"])
def test_launcher_rejects_flow_versions_outside_the_supported_range(version):
    assert not _supported_flow_version(version)


def test_doctor_does_not_require_browser_for_citrix(monkeypatch):
    core = {"openadapt_flow", "openadapt_agent", "playwright"}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in core else None,
    )
    # On Windows the Citrix window client needs no extra Python package.
    monkeypatch.setattr("openadapt.cli._host_platform", lambda: "win32", raising=False)
    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "citrix"])
    assert result.exit_code == 0, result.output
    assert "browser support is not required" in result.output
    assert "no Playwright or Chromium setup will run" in result.output


CORE_PACKAGES = {"openadapt_flow", "openadapt_agent", "playwright"}
MAC_MODULES = {"AppKit", "Quartz", "ApplicationServices"}

# backend, host, modules its surface imports there, install command
NATIVE_BACKEND_CASES = [
    ("macos", "darwin", MAC_MODULES, 'python -m pip install "openadapt[macos]"'),
    ("linux", "linux", {"gi"}, 'python -m pip install "openadapt[linux]"'),
    ("windows", "darwin", {"requests"}, 'python -m pip install "openadapt[windows]"'),
    ("windows", "linux", {"requests"}, 'python -m pip install "openadapt[windows]"'),
    ("citrix", "darwin", MAC_MODULES, 'python -m pip install "openadapt[macos]"'),
]


def _host(monkeypatch, host: str, installed: set[str]) -> None:
    monkeypatch.setattr("openadapt.cli._host_platform", lambda: host, raising=False)
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in installed else None,
    )


@pytest.mark.parametrize(
    ("backend", "host", "modules", "install"), NATIVE_BACKEND_CASES
)
def test_doctor_native_backend_fails_without_its_platform_extra(
    monkeypatch, backend, host, modules, install
):
    """A base install can't open these surfaces, so doctor must not say [OK]."""
    _host(monkeypatch, host, CORE_PACKAGES)

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", backend])

    assert result.exit_code != 0, result.output
    assert f"[MISSING] {backend}" in result.output
    assert install in result.output
    assert f"[OK] {backend}" not in result.output
    assert "System check failed" in result.output


@pytest.mark.parametrize(
    ("backend", "host", "modules", "install"), NATIVE_BACKEND_CASES
)
def test_doctor_native_backend_ok_when_its_platform_extra_is_installed(
    monkeypatch, backend, host, modules, install
):
    _host(monkeypatch, host, CORE_PACKAGES | modules)

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", backend])

    assert result.exit_code == 0, result.output
    assert f"[OK] {backend}" in result.output
    assert "[MISSING]" not in result.output


@pytest.mark.parametrize(
    ("backend", "host"),
    [("macos", "linux"), ("macos", "win32"), ("linux", "darwin"), ("citrix", "linux")],
)
def test_doctor_native_backend_refuses_the_wrong_host(monkeypatch, backend, host):
    everything = CORE_PACKAGES | MAC_MODULES | {"gi", "requests"}
    _host(monkeypatch, host, everything)

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", backend])

    assert result.exit_code != 0, result.output
    assert f"[UNSUPPORTED] {backend}" in result.output
    assert f"[OK] {backend}" not in result.output


@pytest.mark.parametrize(
    ("backend", "host", "modules", "install"), NATIVE_BACKEND_CASES
)
def test_deploy_native_backend_fails_without_its_platform_extra(
    monkeypatch, backend, host, modules, install
):
    _host(monkeypatch, host, {"openadapt_flow"})
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", backend])

    assert result.exit_code != 0, result.output
    assert f"[MISSING] {backend}" in result.output
    assert install in result.output
    assert "Preflight passed" not in result.output


@pytest.mark.parametrize(
    ("backend", "host", "modules", "install"), NATIVE_BACKEND_CASES
)
def test_deploy_native_backend_passes_with_its_platform_extra(
    monkeypatch, backend, host, modules, install
):
    _host(monkeypatch, host, {"openadapt_flow"} | modules)
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", backend])

    assert result.exit_code == 0, result.output
    assert f"[OK] {backend}" in result.output
    # Flow checks the target itself only when it opens, so this stays a
    # [SETUP] item and the preflight can't call the host ready (B021).
    assert f"[SETUP] {backend} target readiness" in result.output
    assert "Preflight incomplete" in result.output
    assert "Preflight passed" not in result.output


def test_doctor_rdp_fails_without_transport_dependency(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "rdp"])

    assert result.exit_code != 0
    assert "[MISSING] rdp" in result.output
    assert 'python -m pip install "openadapt[rdp]"' in result.output
    assert "System check failed" in result.output


def test_doctor_rdp_reports_transport_ready(monkeypatch):
    installed = {"aardwolf", "openadapt_flow", "openadapt_agent", "playwright"}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in installed else None,
    )

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "rdp"])

    assert result.exit_code == 0, result.output
    assert "RDP transport dependency is installed" in result.output


@pytest.mark.parametrize("backend_args", [[], ["--backend", "web"]])
def test_doctor_fails_for_missing_chromium_system_libraries(monkeypatch, backend_args):
    """Default and web checks must stop before a Chromium download can fail."""
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._missing_chromium_system_libs",
        lambda: ["Xcomposite", "Xdamage", "Xfixes", "Xrandr"],
    )
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._chromium_present",
        lambda: pytest.fail("Chromium must not be inspected before its libraries"),
    )

    result = CliRunner().invoke(cli_main, ["doctor", *backend_args])

    assert result.exit_code != 0
    assert "Xcomposite, Xdamage, Xfixes, Xrandr" in result.output
    assert "-m playwright install-deps chromium" in result.output
    assert "System check failed" in result.output


def test_doctor_reports_lazy_browser_download_after_host_is_ready(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._missing_chromium_system_libs", lambda: []
    )
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._chromium_present", lambda: False
    )

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "web"])

    assert result.exit_code == 0, result.output
    assert "system libraries are ready" in result.output
    assert "will try to download" in result.output


def test_doctor_fails_when_chromium_cannot_be_inspected(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr(
        "openadapt_flow._browser_setup._missing_chromium_system_libs", lambda: []
    )

    def fail_inspection():
        raise RuntimeError("driver failed")

    monkeypatch.setattr(
        "openadapt_flow._browser_setup._chromium_present", fail_inspection
    )

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "web"])

    assert result.exit_code != 0
    assert "could not inspect the installed Chromium build" in result.output
    assert "System check failed" in result.output


def test_doctor_fails_without_playwright_for_web(monkeypatch):
    installed = {"openadapt_flow", "openadapt_agent"}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in installed else None,
    )

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "web"])

    assert result.exit_code != 0
    assert "base install does not contain Playwright" in result.output
    assert "System check failed" in result.output


@pytest.mark.parametrize("missing", ["openadapt_flow", "openadapt_agent", "playwright"])
def test_doctor_fails_when_a_core_package_is_missing(monkeypatch, missing):
    core = {"openadapt_flow", "openadapt_agent", "playwright"}
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in core - {missing} else None,
    )

    result = CliRunner().invoke(cli_main, ["doctor", "--backend", "citrix"])

    assert result.exit_code != 0
    assert f"[MISSING] {missing} (core dependency" in result.output
    assert "System check failed" in result.output


def test_deploy_preflight_composes_existing_flow_interfaces_without_secrets(
    monkeypatch,
):
    """A clean host gets diagnostics plus commands for the existing engine.

    The deployment guide must remain a launcher seam: it does not start a
    second engine, accept a secret value, or replace Flow's rollback path.
    """
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (
            object()
            if name
            in {"openadapt_flow", "aardwolf", "fastapi", "uvicorn", "openadapt_types"}
            else None
        ),
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")
    monkeypatch.setenv("BYOC_CONNECTOR_TOKEN", "connector-token-value")
    result = CliRunner().invoke(
        cli_main,
        ["deploy", "--backend", "rdp", "--secret-ref", "env:BYOC_CONNECTOR_TOKEN"],
    )

    assert result.exit_code == 0, result.output
    assert "Environment fingerprint" in result.output
    assert "[OK] env:BYOC_CONNECTOR_TOKEN is set" in result.output
    assert "connector-token-value" not in result.output
    assert "dashboard/settings/connectors" in result.output
    assert "connector enroll" not in result.output
    assert "connector run" in result.output
    assert "flow console" in result.output
    assert "flow repair rollback" in result.output
    assert "No service was started" in result.output


def test_deploy_base_hosted_install_gives_conditional_console_setup(monkeypatch):
    """Base Flow hosted installs must not receive an unusable console command."""
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name in {"openadapt_flow", "aardwolf"} else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code == 0, result.output
    assert "optional local operator console is not installed" in result.output
    assert 'python -m pip install "openadapt-flow[console]==2.0.0"' in result.output
    assert "openadapt flow console --bundles" not in result.output
    assert "Re-run this preflight" in result.output


def test_deploy_console_requires_openadapt_types(monkeypatch):
    """The supported Flow console imports openadapt-types when it starts."""
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: (
            object()
            if name in {"openadapt_flow", "aardwolf", "fastapi", "uvicorn"}
            else None
        ),
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code == 0, result.output
    assert "optional local operator console is not installed" in result.output
    assert 'python -m pip install "openadapt-flow[console]==2.0.0"' in result.output
    assert "openadapt flow console --bundles" not in result.output


def test_deploy_preflight_refuses_secret_values_and_missing_engine(monkeypatch):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)

    runner = CliRunner()
    secret_result = runner.invoke(cli_main, ["deploy", "--secret-ref", "actual-secret"])
    assert secret_result.exit_code != 0
    assert "do not pass secret values" in secret_result.output
    assert "actual-secret" not in secret_result.output
    assert "value hidden" in secret_result.output

    missing_result = runner.invoke(cli_main, ["deploy", "--backend", "windows"])
    assert missing_result.exit_code != 0
    assert "[MISSING]" in missing_result.output
    assert "Preflight failed" in missing_result.output


def test_deploy_preflight_fails_without_web_runtime(monkeypatch):
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "openadapt_flow" else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "web"])

    assert result.exit_code != 0
    assert (
        "[MISSING] the base OpenAdapt install does not contain Playwright"
        in result.output
    )
    assert "Preflight failed" in result.output
    assert "Preflight passed" not in result.output


def test_deploy_preflight_fails_without_rdp_transport(monkeypatch):
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name: object() if name == "openadapt_flow" else None,
    )
    monkeypatch.setattr("importlib.metadata.version", lambda _name: "2.0.0")

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code != 0
    assert "[MISSING] RDP transport dependency" in result.output
    assert 'python -m pip install "openadapt[rdp]"' in result.output
    assert "Preflight failed" in result.output
    assert "Preflight passed" not in result.output


@pytest.mark.parametrize("flow_version", ["1.35.1", "3.0.0", "3.1.0", "invalid"])
def test_deploy_preflight_fails_for_unsupported_flow(monkeypatch, flow_version):
    monkeypatch.setattr("importlib.util.find_spec", lambda _name: object())
    monkeypatch.setattr("importlib.metadata.version", lambda _name: flow_version)

    result = CliRunner().invoke(cli_main, ["deploy", "--backend", "rdp"])

    assert result.exit_code != 0
    assert "[UNSUPPORTED]" in result.output
    assert ">=2.0.0,<3.0.0" in result.output
    assert "Preflight passed" not in result.output


def test_top_level_help_leads_with_flow():
    """`openadapt --help` must list flow before the other commands."""
    runner = CliRunner()
    result = runner.invoke(cli_main, ["--help"])
    assert result.exit_code == 0
    # Quick Start headline and Commands listing both lead with flow.
    assert "openadapt flow demo-record" in result.output
    assert "openadapt flow tutorial" in result.output
    assert "openadapt quickstart --break-it" in result.output
    # openadapt-agent 2.0.1 requires --bundles; the help names a command that
    # works with every released agent and every install method.
    assert "openadapt agent serve --bundles DIR --allow-run" in result.output
    assert "openadapt agent serve --allow-run" not in result.output
    assert "effect-verified first run" in result.output
    assert "Standalone local human GUI capture" in result.output
    assert "Research: evaluate" in result.output
    assert "Research: train" in result.output
    commands_idx = result.output.index("Commands:")
    flow_idx = result.output.index("flow", commands_idx)
    capture_idx = result.output.index("capture", commands_idx)
    assert flow_idx < capture_idx, "flow should be listed before capture"


def test_capture_start_refuses_when_recorder_never_becomes_ready(monkeypatch):
    """The compatibility command must not label failed startup as saved."""
    from types import SimpleNamespace

    state = {}

    class NeverReadyRecorder:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            state["exited"] = True
            return False

        def wait_for_ready(self):
            return False

    monkeypatch.setitem(
        sys.modules,
        "openadapt_capture",
        SimpleNamespace(Recorder=NeverReadyRecorder),
    )

    result = CliRunner().invoke(
        cli_main,
        ["capture", "start", "--name", "failed-capture", "--no-video"],
    )

    assert result.exit_code != 0
    assert state == {"exited": True}
    assert "did not become ready" in result.output
    assert "Recording..." not in result.output
    assert "Capture saved" not in result.output


def test_capture_stop_fails_until_capture_has_a_control_channel():
    result = CliRunner().invoke(cli_main, ["capture", "stop"])

    assert result.exit_code != 0
    assert "Ctrl+C in the recorder terminal" in result.output
    assert "authenticated, owner-only local control channel" in result.output
    assert "Stopping active capture session" not in result.output


def test_flow_help_is_current_engine_help():
    """The launcher must not maintain a second, stale Flow command list."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    result = CliRunner().invoke(cli_main, ["flow", "--help"])

    assert result.exit_code == 0, result.output
    assert result.output == flow_main_mod.build_parser().format_help()


@pytest.mark.parametrize(
    ("verb", "expected_options"),
    [
        ("record", ("--backend", "--agent-url", "--task")),
        ("replay", ("--backend", "--config", "--rdp-readiness-text")),
        ("demo-record", ("--record-video", "--note-text", "--param-name")),
        ("compile", ("--accept-params", "--params-from", "--no-confirm-params")),
        ("certify", ("--config", "--policy")),
    ],
)
def test_flow_subcommand_help_is_engine_help(verb, expected_options):
    _require_openadapt_flow()
    result = CliRunner().invoke(cli_main, ["flow", verb, "--help"])
    assert result.exit_code == 0, result.output
    for option in expected_options:
        assert option in result.output, (
            f"{option} missing from `flow {verb} --help`; the launcher is "
            "hiding current engine options"
        )


@pytest.mark.parametrize(
    "argv",
    [
        ["demo-record", "--out", "rec", "--record-video"],
        [
            "compile",
            "rec",
            "--out",
            "bundle",
            "--name",
            "demo",
            "--params-from",
            "params.json",
            "--accept-params",
            "note",
            "--no-confirm-params",
        ],
        ["certify", "bundle", "--config", "deployment.yaml"],
    ],
)
def test_flow_forwards_current_engine_options_verbatim(monkeypatch, argv):
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda value: captured.update(argv=list(value))
    )

    result = CliRunner().invoke(cli_main, ["flow", *argv])

    assert result.exit_code == 0, result.output
    assert captured["argv"] == argv


def test_flow_record_forwards_backend_options(monkeypatch):
    """Regression (launcher <=1.7.0): the explicit record wrapper rejected
    `--backend windows` with "No such option". record must forward every
    engine option verbatim, like run/push."""
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda argv: captured.update(argv=list(argv))
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "flow",
            "record",
            "--backend",
            "windows",
            "--agent-url",
            "http://localhost:5001",
            "--out",
            "rec",
            "--task",
            "triage note",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "record",
        "--backend",
        "windows",
        "--agent-url",
        "http://localhost:5001",
        "--out",
        "rec",
        "--task",
        "triage note",
    ]


def test_flow_record_web_invocation_forwards_verbatim(monkeypatch):
    """The pre-existing web invocation shape keeps working unchanged."""
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda argv: captured.update(argv=list(argv))
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "flow",
            "record",
            "--url",
            "http://localhost:3000",
            "--out",
            "rec",
            "--secret",
            "password",
            "--param",
            "note",
            "--headless",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "record",
        "--url",
        "http://localhost:3000",
        "--out",
        "rec",
        "--secret",
        "password",
        "--param",
        "note",
        "--headless",
    ]


def test_flow_missing_shows_install_hint(monkeypatch):
    """When openadapt-flow isn't installed, `openadapt flow <verb>` exits
    nonzero with a pip install hint instead of a traceback."""
    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "openadapt_flow.__main__" or name.startswith("openadapt_flow"):
            raise ImportError("No module named 'openadapt_flow'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    monkeypatch.delitem(sys.modules, "openadapt_flow", raising=False)
    monkeypatch.delitem(sys.modules, "openadapt_flow.__main__", raising=False)

    runner = CliRunner()
    result = runner.invoke(
        cli_main, ["flow", "compile", "rec", "--out", "b", "--name", "x"]
    )
    assert result.exit_code != 0
    assert "pip install --upgrade openadapt" in result.output
    assert "pip install openadapt-flow" in result.output


def _require_openadapt_flow():
    return pytest.importorskip("openadapt_flow", reason="openadapt-flow not installed")


def test_flow_delegates_to_flow_main(monkeypatch):
    """`openadapt flow <verb>` must reconstruct argv and call
    openadapt_flow.__main__.main so behavior matches `openadapt-flow <verb>`."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    calls = []

    def fake_main(argv):
        calls.append(list(argv))
        return 0

    monkeypatch.setattr(flow_main_mod, "main", fake_main)

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "flow",
            "compile",
            "my-rec",
            "--out",
            "my-bundle",
            "--name",
            "my-flow",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [["compile", "my-rec", "--out", "my-bundle", "--name", "my-flow"]]


def test_unwrapped_flow_command_delegates_to_engine(monkeypatch):
    captured = {}

    def fake_run(argv):
        captured["argv"] = argv

    monkeypatch.setattr("openadapt.cli._run_flow", fake_run)
    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "flow",
            "sanitize",
            "recording",
            "--kind",
            "recording",
            "--out",
            "sanitized",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "sanitize",
        "recording",
        "--kind",
        "recording",
        "--out",
        "sanitized",
    ]


def test_flow_help_lists_delegated_launch_commands():
    result = CliRunner().invoke(cli_main, ["flow", "--help"])

    assert result.exit_code == 0, result.output
    for command in (
        "run",
        "teach",
        "connect",
        "login",
        "sanitize",
        "review-sanitized",
        "approve-sanitized",
        "validate-hosted",
        "push",
        "report-break",
    ):
        assert command in result.output


def test_top_level_connect_is_one_command_and_delegates_narrow_pairing(monkeypatch):
    import openadapt_flow.hosted as hosted

    captured = {}
    monkeypatch.setattr(hosted, "connect", object(), raising=False)
    monkeypatch.setattr(
        "openadapt.cli._run_flow",
        lambda argv: captured.update(argv=list(argv)),
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "connect",
            "--pairing",
            "oap_" + "A" * 43,
            "--host",
            "https://app.openadapt.ai",
            "--device-name",
            "Reception PC",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "connect",
        "--pairing",
        "oap_" + "A" * 43,
        "--host",
        "https://app.openadapt.ai",
        "--device-name",
        "Reception PC",
    ]


def test_top_level_connect_preserves_exact_desktop_uri(monkeypatch):
    import openadapt_flow.hosted as hosted

    captured = {}
    uri = (
        "openadapt://connect?pairing=oap_"
        + "A" * 43
        + "&host=https%3A%2F%2Fapp.openadapt.ai"
        + "&destination_kind=openadapt-managed"
    )
    monkeypatch.setattr(hosted, "connect", object(), raising=False)
    monkeypatch.setattr(
        "openadapt.cli._run_flow",
        lambda argv: captured.update(argv=list(argv)),
    )

    result = CliRunner().invoke(
        cli_main,
        ["connect", "--uri", uri, "--device-name", "Reception PC"],
    )

    assert result.exit_code == 0, result.output
    assert captured["argv"] == [
        "connect",
        "--uri",
        uri,
        "--device-name",
        "Reception PC",
    ]


def test_top_level_connect_requires_exactly_one_fixed_pairing_source():
    runner = CliRunner()
    assert runner.invoke(cli_main, ["connect"]).exit_code != 0
    both = runner.invoke(
        cli_main,
        [
            "connect",
            "--pairing",
            "oap_" + "A" * 43,
            "--uri",
            "openadapt://connect?pairing=x&host=https://app.openadapt.ai",
        ],
    )
    assert both.exit_code != 0
    assert "exactly one" in both.output


def test_flow_replay_argv_reconstruction(monkeypatch):
    """Repeatable and flag options are forwarded verbatim to flow's main."""
    _require_openadapt_flow()
    import openadapt_flow.__main__ as flow_main_mod

    calls = []
    monkeypatch.setattr(
        flow_main_mod, "main", lambda argv: calls.append(list(argv)) or 0
    )

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "flow",
            "replay",
            "bundle",
            "--param",
            "note=hi",
            "--param",
            "id=7",
            "--headed",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        ["replay", "bundle", "--param", "note=hi", "--param", "id=7", "--headed"]
    ]


@pytest.mark.parametrize(
    ("backend", "target_args"),
    [
        ("windows", ["--agent-url", "http://localhost:5001"]),
        (
            "macos",
            ["--macos-app", "TextEdit", "--macos-window-title", "Notes"],
        ),
        (
            "linux",
            [
                "--linux-app",
                "gedit",
                "--linux-window-title",
                "notes.txt",
                "--linux-allow-physical-input",
            ],
        ),
        ("rdp", ["--rdp-host", "rdp.example.test"]),
        (
            "citrix",
            [
                "--rdp-window",
                "Citrix Viewer",
                "--rdp-window-title",
                "Ward A",
                "--rdp-readiness-text",
                "Appointments",
            ],
        ),
    ],
)
def test_flow_replay_forwards_backend_config_and_native_targets(
    monkeypatch, backend, target_args
):
    """Regression: replay must remain an argv-transparent engine command.

    The former Click wrapper rejected every option below before Flow could
    validate the selected deployment and target.
    """
    captured = {}
    monkeypatch.setattr(
        "openadapt.cli._run_flow", lambda argv: captured.update(argv=list(argv))
    )
    args = [
        "flow",
        "replay",
        "bundle",
        "--backend",
        backend,
        *target_args,
        "--config",
        "deployment.yaml",
        "--params-file",
        "params.json",
        "--allow-model-grounding",
    ]

    result = CliRunner().invoke(cli_main, args)

    assert result.exit_code == 0, result.output
    assert captured["argv"] == ["replay", *args[2:]]


# ---------------------------------------------------------------------------
# Seam contracts with openadapt-ml
# ---------------------------------------------------------------------------


def _require_openadapt_ml():
    return pytest.importorskip("openadapt_ml", reason="openadapt-ml not installed")


def test_train_start_calls_real_entry_point(monkeypatch, tmp_path):
    """`openadapt train start` must call scripts.train.main with kwargs
    that exist in its signature."""
    _require_openadapt_ml()
    import inspect

    from openadapt_ml.scripts import train as train_module

    real_params = set(inspect.signature(train_module.main).parameters)
    calls = []

    def fake_main(**kwargs):
        unknown = set(kwargs) - real_params
        assert not unknown, (
            f"cli.py passes kwargs {sorted(unknown)} that "
            f"openadapt_ml.scripts.train.main does not accept "
            f"(it takes {sorted(real_params)})"
        )
        calls.append(kwargs)

    monkeypatch.setattr(train_module, "main", fake_main)

    capture_dir = tmp_path / "my-capture"
    capture_dir.mkdir()
    config = tmp_path / "config.yaml"
    config.write_text("model:\n  name: test\n")

    runner = CliRunner()
    result = runner.invoke(
        cli_main,
        [
            "train",
            "start",
            "--capture",
            str(capture_dir),
            "--config",
            str(config),
            "--no-open",
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    kwargs = calls[0]
    assert kwargs["config_path"] == str(config)
    assert kwargs["capture_path"] == str(capture_dir)
    assert kwargs["open_dashboard"] is False


def test_serve_calls_cmd_serve_with_expected_namespace(monkeypatch):
    """`openadapt serve` must call cmd_serve with the agreed Namespace."""
    _require_openadapt_ml()
    from openadapt_ml.cloud import local as oa_local

    received = []

    def fake_cmd_serve(args):
        received.append(args)
        return 0

    monkeypatch.setattr(oa_local, "cmd_serve", fake_cmd_serve)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["serve", "--port", "8123", "--no-open"])
    assert result.exit_code == 0, result.output
    assert len(received) == 1
    ns = received[0]
    assert ns.port == 8123
    assert ns.open is False  # --no-open passes through to cmd_serve
    for attr in SERVE_NAMESPACE_ATTRS:
        assert hasattr(ns, attr), f"Namespace missing {attr}"


def test_serve_honors_output_directory(monkeypatch, tmp_path):
    """--output must repoint openadapt-ml's TRAINING_OUTPUT."""
    _require_openadapt_ml()
    from openadapt_ml.cloud import local as oa_local

    monkeypatch.setattr(oa_local, "cmd_serve", lambda args: 0)

    runner = CliRunner()
    out = tmp_path / "runs"
    result = runner.invoke(cli_main, ["serve", "--output", str(out), "--no-open"])
    assert result.exit_code == 0, result.output
    assert Path(oa_local.TRAINING_OUTPUT) == out


def test_cmd_serve_reads_only_provided_args():
    """Every `args.<attr>` cmd_serve reads must be in cli.py's Namespace.

    This is the direction the contract can silently drift: openadapt-ml
    adds a new required Namespace attribute and cli.py doesn't provide
    it. Parse the installed cmd_serve and check.
    """
    ml = _require_openadapt_ml()
    local_path = Path(next(iter(ml.__path__))) / "cloud" / "local.py"
    tree = ast.parse(local_path.read_text(encoding="utf-8"))
    cmd_serve = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "cmd_serve"
        ),
        None,
    )
    assert cmd_serve is not None, "cmd_serve not found in openadapt-ml"

    args_param = cmd_serve.args.args[0].arg
    read_attrs = {
        node.attr
        for node in ast.walk(cmd_serve)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == args_param
    }
    missing = read_attrs - SERVE_NAMESPACE_ATTRS
    assert not missing, (
        f"openadapt-ml's cmd_serve reads args attributes "
        f"{sorted(missing)} that openadapt's serve command does not "
        f"provide; update openadapt/cli.py (and SERVE_NAMESPACE_ATTRS)"
    )


def test_import_error_messages_not_masked(monkeypatch):
    """Internal ImportErrors must surface the real error, not claim
    openadapt-ml isn't installed."""
    _require_openadapt_ml()

    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "openadapt_ml.cloud" or name.startswith("openadapt_ml.cloud."):
            raise ImportError(
                "cannot import name 'definitely_phantom' from "
                "'openadapt_ml.cloud.local'"
            )
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    monkeypatch.delitem(sys.modules, "openadapt_ml.cloud.local", raising=False)
    monkeypatch.delitem(sys.modules, "openadapt_ml.cloud", raising=False)

    runner = CliRunner()
    result = runner.invoke(cli_main, ["serve", "--no-open"])
    assert result.exit_code != 0
    assert "definitely_phantom" in result.output, (
        "The underlying ImportError must appear in the CLI output; "
        f"got: {result.output}"
    )
