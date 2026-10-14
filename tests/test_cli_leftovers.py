"""Launcher leftovers from the research CLI: version, capture stop, usage lines.

- `openadapt version` printed four research packages as "not installed" on
  every base install, which looks like a broken install, and never listed
  openadapt-types, which the base install brings in.
- `openadapt capture stop` can't stop anything, yet the module usage and the
  `capture` help advertised it next to the working commands.
- The module usage said `openadapt train --capture ...` and
  `openadapt eval --checkpoint ...`; the real commands are `train start` and
  `eval run`.
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from openadapt import cli
from openadapt.cli import main as cli_main

PRODUCT = {
    "openadapt": "1.99.0",
    "openadapt-flow": "1.35.1",
    "openadapt-agent": "2.0.1",
    "openadapt-types": "0.17.1",
}
RESEARCH = (
    "openadapt-ml",
    "openadapt-evals",
    "openadapt-viewer",
    "openadapt-grounding",
    "openadapt-retrieval",
)


def _installed(monkeypatch, versions: dict[str, str]) -> None:
    from importlib.metadata import PackageNotFoundError

    def version(name):
        if name not in versions:
            raise PackageNotFoundError(name)
        return versions[name]

    monkeypatch.setattr("importlib.metadata.version", version)


def test_version_lists_product_components_and_hides_absent_research(monkeypatch):
    _installed(monkeypatch, PRODUCT)

    result = CliRunner().invoke(cli_main, ["version"])

    assert result.exit_code == 0, result.output
    for name, number in PRODUCT.items():
        assert f"{name}: {number}" in result.output
    assert "openadapt-capture: not installed" in result.output
    assert "Desktop" in result.output
    assert "Research" not in result.output
    for name in RESEARCH:
        assert name not in result.output


def test_version_lists_installed_research_packages_under_research(monkeypatch):
    _installed(monkeypatch, {**PRODUCT, "openadapt-ml": "0.2.0"})

    result = CliRunner().invoke(cli_main, ["version"])

    assert result.exit_code == 0, result.output
    heading = result.output.index("Research (optional):")
    assert result.output.index("openadapt-ml: 0.2.0") > heading
    for name in RESEARCH[1:]:
        assert name not in result.output


def test_capture_help_does_not_advertise_stop():
    result = CliRunner().invoke(cli_main, ["capture", "--help"])

    assert result.exit_code == 0, result.output
    assert "stop" not in result.output


def test_capture_stop_still_explains_and_does_not_claim_success():
    result = CliRunner().invoke(cli_main, ["capture", "stop"])

    assert result.exit_code != 0
    assert "Ctrl+C" in result.output


def _usage_lines() -> list[list[str]]:
    import shlex

    lines = [
        line.strip()
        for line in (cli.__doc__ or "").splitlines()
        if line.strip().startswith("openadapt ")
    ]
    assert lines
    return [shlex.split(line.split("  #", 1)[0]) for line in lines]


@pytest.mark.parametrize("argv", _usage_lines(), ids=" ".join)
def test_module_usage_lines_parse_as_real_commands(argv):
    ctx = click.Context(cli_main, info_name="openadapt")
    command: click.Command = cli_main
    args = argv[1:]
    while isinstance(command, click.Group):
        assert args, argv
        sub = command.get_command(ctx, args[0])
        assert sub is not None, f"{argv}: no command {args[0]!r}"
        assert not sub.hidden, f"{argv}: {args[0]!r} is hidden"
        ctx = click.Context(sub, info_name=args[0], parent=ctx)
        command, args = sub, args[1:]
    # Parse the options without running the command.
    command.make_context(ctx.info_name, list(args), parent=ctx.parent)
