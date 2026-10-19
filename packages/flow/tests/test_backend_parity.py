"""No command may assume a browser where a deployment file can name the app.

Every command that takes an app URL flag must also take ``--backend`` or
``--config``, or be listed in docs/BACKEND_PARITY.md (the "URL flags" table)
with the reason it can't. A new ``--...-url`` flag counts as an app URL unless
it is a known service endpoint below. See docs/BACKEND_PARITY.md.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Callable, Iterator

import pytest

DOC = Path(__file__).resolve().parents[1] / "docs" / "BACKEND_PARITY.md"

#: URL flags that name a service, not the app being automated.
SERVICE_URL_FLAGS = frozenset(
    {
        "--agent-url",  # the in-guest Windows agent: a backend target itself
        "--windows-agent-url",
        "--allow-url-override",
        "--api-base-url",
        "--cloud-url",
        "--control-plane-url",
        "--effects-base-url",
        "--execute-url",
        "--tsa-url",
    }
)
#: Flags that let a command name any surface instead of a URL.
NEUTRAL_FLAGS = frozenset({"--backend", "--config"})
_URL_FLAG = re.compile(r"^--(?:[a-z0-9]+-)*url$")


def _flow_parser() -> argparse.ArgumentParser:
    from openadapt_flow.__main__ import build_parser

    return build_parser()


def _agent_parser() -> argparse.ArgumentParser:
    from openadapt_flow.agent.cli import build_parser

    return build_parser()


PARSERS: dict[str, Callable[[], argparse.ArgumentParser]] = {
    "openadapt-flow": _flow_parser,
    "openadapt-agent": _agent_parser,
}


def _commands(
    parser: argparse.ArgumentParser, prefix: str
) -> Iterator[tuple[str, set[str]]]:
    options = {
        option
        for action in parser._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    yield prefix, options
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for name, sub in action.choices.items():
                yield from _commands(sub, f"{prefix} {name}")


def url_flag_refusals(text: str) -> dict[str, set[str]]:
    """``{command: {flag, ...}}`` from the doc's URL flags table."""

    marker = "<!-- url-flag-refusals"
    assert marker in text, f"{DOC.name} lost its url-flag-refusals table"
    rows: dict[str, set[str]] = {}
    for line in text.split(marker, 1)[1].splitlines()[1:]:
        if not line.startswith("|"):
            if rows:
                break
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or cells[0] in ("Command", "") or set(cells[0]) <= {"-"}:
            continue
        command, flag, reason = cells[0].strip("`"), cells[1].strip("`"), cells[2]
        assert reason, f"{command} {flag}: a recorded refusal needs a reason"
        rows.setdefault(command, set()).add(flag)
    return rows


def app_url_gaps() -> list[str]:
    refusals = url_flag_refusals(DOC.read_text())
    gaps = []
    for program, build in PARSERS.items():
        for command, options in _commands(build(), program):
            app_urls = {
                o for o in options if _URL_FLAG.match(o) and o not in SERVICE_URL_FLAGS
            }
            if not app_urls or options & NEUTRAL_FLAGS:
                continue
            missing = app_urls - refusals.get(command, set())
            if missing:
                gaps.append(f"{command}: {', '.join(sorted(missing))}")
    return gaps


def test_every_app_url_flag_has_a_backend_neutral_alternative() -> None:
    gaps = app_url_gaps()
    assert not gaps, (
        "These commands take an app URL with no --backend or --config, and "
        "docs/BACKEND_PARITY.md records no reason:\n  "
        + "\n  ".join(gaps)
        + "\nAdd the backend flags (see __main__._add_backend_flags), or add a "
        "row to the URL flags table with the reason."
    )


def test_every_recorded_refusal_is_still_a_real_url_flag() -> None:
    """A stale row would hide a future regression on the same command."""

    refusals = url_flag_refusals(DOC.read_text())
    commands = {
        command: options
        for program, build in PARSERS.items()
        for command, options in _commands(build(), program)
    }
    for command, flags in refusals.items():
        assert command in commands, f"{command} is no longer a command"
        assert flags <= commands[command], f"{command} no longer takes {flags}"
        assert not commands[command] & NEUTRAL_FLAGS, (
            f"{command} now takes --backend/--config; drop its refusal row"
        )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({"--url"}, True),
        ({"--target-url"}, True),
        ({"--fault-url"}, True),
        ({"--url", "--backend"}, False),
        ({"--url", "--config"}, False),
        ({"--tsa-url"}, False),
        ({"--curl"}, False),
    ],
)
def test_the_rule(options: set[str], expected: bool, monkeypatch) -> None:
    def parser() -> argparse.ArgumentParser:
        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        verb = sub.add_parser("new-verb")
        for option in options:
            verb.add_argument(option)
        return p

    monkeypatch.setattr("tests.test_backend_parity.PARSERS", {"openadapt-flow": parser})
    assert bool(app_url_gaps()) is expected
