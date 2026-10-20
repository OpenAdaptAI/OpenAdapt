"""Shared test setup."""

from __future__ import annotations

import importlib.util

import pytest


@pytest.fixture(autouse=True, scope="session")
def _import_torch_before_find_spec_is_stubbed():
    """Import PyTorch once with the real module finder, if it's installed.

    Several doctor tests replace ``importlib.util.find_spec`` with a stub that
    reports every module as installed. Doctor then treats the research extras
    as present and runs its GPU check, which imports torch. Under that stub,
    torch's import fails partway (it looks for optional modules that the stub
    claims exist), doctor reports "PyTorch not installed", and the interpreter
    later crashes at exit with a segmentation fault (exit 139) after every
    test has passed. CI installs the research extras, so it hit this. With
    torch already imported, doctor's import finds it in ``sys.modules``.
    """
    if importlib.util.find_spec("torch") is not None:
        import torch  # noqa: F401
    yield


@pytest.fixture(autouse=True)
def _no_real_quickstart_runs(request, monkeypatch):
    """Keep unit tests from starting a real browser run by accident.

    The three-act quickstart drives a browser for about 30 seconds. Tests that
    mean to run it are marked ``real_quickstart``; everything else gets a
    clear failure instead of a silent half-minute run.
    """
    if request.node.get_closest_marker("real_quickstart"):
        yield
        return
    try:
        from openadapt.quickstart import engine
    except ImportError:
        yield
        return

    def refuse(self):
        raise AssertionError(
            "this test started a real quickstart run; stub "
            "openadapt.quickstart.run_quickstart or mark it real_quickstart"
        )

    monkeypatch.setattr(engine.FlowEngine, "__enter__", refuse)
    yield


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "real_quickstart: runs the real quickstart in a browser"
    )
