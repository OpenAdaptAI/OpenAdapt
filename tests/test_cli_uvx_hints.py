"""The quickstart's next steps reach the same version under ``uvx``."""

from __future__ import annotations

import sys

from openadapt import cli


def test_hint_repeats_uvx_form_in_a_uvx_environment(monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/Users/x/.cache/uv/archive-v0/kj_30_Pw78")
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    assert cli._hint("flow demo") == f"uvx --python {py} openadapt flow demo"


def test_hint_keeps_plain_form_when_installed(monkeypatch) -> None:
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/openadapt")
    assert cli._hint("flow lint bundle") == "openadapt flow lint bundle"


# ---------------------------------------------------------------------------
# Under uvx the quickstart says how to keep this version, and warns when the
# shell's own `openadapt` is another one.
# ---------------------------------------------------------------------------

import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from openadapt.quickstart import next_steps  # noqa: E402
from openadapt.quickstart.story import Outcome  # noqa: E402
from openadapt.version import __version__  # noqa: E402

UVX_PREFIX = "/Users/x/.cache/uv/archive-v0/kj_30_Pw78"


def _version_run(output: str):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    return run


def test_keep_it_appears_only_under_uvx(monkeypatch) -> None:
    py = f"{sys.version_info[0]}.{sys.version_info[1]}"
    monkeypatch.setattr(sys, "prefix", UVX_PREFIX)
    assert cli._keep_command() == f"uv tool install --python {py} openadapt"
    monkeypatch.setattr(sys, "prefix", "/Users/x/.local/share/uv/tools/openadapt")
    assert cli._keep_command() is None


def test_an_older_openadapt_on_path_gets_one_warning() -> None:
    found = "/opt/homebrew/anaconda3/bin/openadapt"
    warning = cli._shadowing_openadapt(
        which=lambda name: found, run=_version_run("openadapt, version 1.5.1\n")
    )
    assert warning == (
        f"Your shell's openadapt is 1.5.1 at {found}; the commands above use uvx "
        "so they run this version."
    )


@pytest.mark.parametrize(
    ("which", "output"),
    [
        (lambda name: None, ""),
        (
            lambda name: "/Users/x/.local/bin/openadapt",
            f"openadapt, version {__version__}",
        ),
    ],
)
def test_no_warning_without_a_shadow(which, output) -> None:
    assert cli._shadowing_openadapt(which=which, run=_version_run(output)) is None


def test_an_openadapt_that_does_not_answer_is_still_named() -> None:
    def broken(argv, **kwargs):
        raise OSError("exec format error")

    warning = cli._shadowing_openadapt(
        which=lambda name: "/usr/bin/openadapt", run=broken
    )
    assert warning is not None and "unknown version at /usr/bin/openadapt" in warning


def test_the_finale_prints_keep_it_and_the_shadow_warning(
    tmp_path, monkeypatch
) -> None:
    import io

    from openadapt.quickstart.view import PlainView

    monkeypatch.setattr(sys, "prefix", UVX_PREFIX)
    steps = next_steps(
        cli._hint,
        cli._shell_quote,
        True,
        keep=cli._keep_command(),
        shadow=lambda: cli._shadowing_openadapt(
            which=lambda name: "/opt/conda/bin/openadapt",
            run=_version_run("openadapt, version 1.5.1"),
        ),
    )(Outcome(root=tmp_path / "qs"))
    stream = io.StringIO()
    PlainView(stream).finale(Outcome(root=Path(tmp_path / "qs")), steps)
    lines = stream.getvalue().splitlines()
    assert lines[-2].startswith("Keep it: uv tool install --python ")
    assert lines[-1].startswith(
        "Your shell's openadapt is 1.5.1 at /opt/conda/bin/openadapt"
    )
    finale = lines[lines.index("Receipt") :]
    assert len(finale) <= 12


def test_version_check_never_recurses(monkeypatch) -> None:
    """The child ``openadapt --version`` gets a guard, and the guard stops the check."""
    seen = {}

    def fake_run(args, **kwargs):
        seen["env"] = kwargs.get("env") or {}

        class R:
            stdout = "openadapt, version 0.0.1"
            stderr = ""

        return R()

    assert cli._launcher_version("/x/openadapt", fake_run) == "0.0.1"
    assert seen["env"].get(cli._NO_LAUNCHER_CHECK) == "1"

    called = []
    monkeypatch.setenv(cli._NO_LAUNCHER_CHECK, "1")
    monkeypatch.setattr(cli, "_launcher_conflicts", lambda: called.append(1) or [])
    cli._warn_about_launcher_conflicts()
    assert called == []
