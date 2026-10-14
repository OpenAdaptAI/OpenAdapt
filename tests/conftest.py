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
