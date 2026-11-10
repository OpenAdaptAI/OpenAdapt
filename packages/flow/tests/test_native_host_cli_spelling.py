"""Native-host CLIs accept full flags and refuse every undeclared prefix."""

from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import pytest

HOSTS = Path(__file__).resolve().parents[1] / "chrome_extension" / "native_host"
ENTRIES = [
    ("install_host", "parse_args"),
    ("openadapt_extension_host", "parse_known_args"),
]


class _ParserReady(Exception):
    def __init__(self, parser):
        self.parser = parser


def _real_parser(monkeypatch, name, parse_method):
    spec = importlib.util.spec_from_file_location(name, HOSTS / f"{name}.py")
    assert spec and spec.loader
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    monkeypatch.setenv("OPENADAPT_EXTENSION_RECORDING", "")

    def stop_before_io(parser, *_args, **_kwargs):
        raise _ParserReady(parser)

    # Keep the genuine constructor/registration. Stop at its parse boundary,
    # before installer writes or the host reads stdin/opens a recording.
    with monkeypatch.context() as patch:
        patch.setattr(argparse.ArgumentParser, parse_method, stop_before_io)
        with pytest.raises(_ParserReady) as ready:
            entry.main([])
    return ready.value.parser


def _resolutions(parser, spelling):
    result = parser._parse_optional(spelling)
    rows = [] if result is None else result if isinstance(result, list) else [result]
    assert all(isinstance(row, tuple) and len(row) in (3, 4) for row in rows)
    return rows


@pytest.mark.parametrize(("name", "parse_method"), ENTRIES)
def test_native_host_cli_refuses_every_undeclared_long_prefix(
    monkeypatch, name, parse_method
):
    parser = _real_parser(monkeypatch, name, parse_method)
    assert parser.allow_abbrev is False
    declared = parser._option_string_actions
    options = prefixes = 0
    for option, action in declared.items():
        if not option.startswith("--"):
            continue
        matches = _resolutions(parser, option)
        assert len(matches) == 1 and matches[0][0] is action
        options += 1
        for length in range(2, len(option)):
            prefix = option[:length]
            if prefix in declared:
                continue
            for spelling in (prefix, prefix + "=synthetic"):
                assert all(row[0] is None for row in _resolutions(parser, spelling)), (
                    spelling
                )
                prefixes += 1
    assert options >= 2 and prefixes > options


@pytest.mark.parametrize(("name", "parse_method"), ENTRIES)
def test_native_host_full_flags_preserve_declared_and_chrome_arguments(
    monkeypatch, tmp_path, name, parse_method
):
    parser = _real_parser(monkeypatch, name, parse_method)
    recording = str(tmp_path / "recording")
    if name == "install_host":
        profile = str(tmp_path / "profile")
        args = parser.parse_args(
            [
                "--user-data-dir",
                profile,
                "--extension-id",
                "a" * 32,
                "--recording",
                recording,
            ]
        )
        assert args.user_data_dir == profile and args.extension_id == "a" * 32
        assert args.recording == recording and args.uninstall is False
    else:
        chrome = ["chrome-extension://" + "a" * 32 + "/", "123"]
        args, unknown = parser.parse_known_args(["--recording", recording, *chrome])
        assert args.recording == recording and unknown == chrome
    assert list(tmp_path.iterdir()) == []
