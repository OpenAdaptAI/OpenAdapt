"""Compatibility shim: `openadapt_agent` is now `openadapt_flow.agent`.

The openadapt-agent code moved into openadapt-flow. This package keeps the old PyPI
name, import name and commands working. `import openadapt_agent` returns the
`openadapt_flow.agent` module itself, and every `openadapt_agent.<sub>` import resolves to the
same module object as `openadapt_flow.agent.<sub>`, so classes, isinstance checks,
pickles and monkeypatches behave exactly as before. New code should import
`openadapt_flow.agent` directly.
"""

from __future__ import annotations

import importlib
import importlib.abc
import importlib.util
import sys

_OLD = "openadapt_agent"
_NEW = "openadapt_flow.agent"


class _AliasLoader(importlib.abc.Loader):
    def __init__(self, target: str) -> None:
        self._target = target
        self._spec = None

    def create_module(self, spec):  # noqa: ANN001 - importlib protocol
        module = importlib.import_module(self._target)
        self._spec = module.__spec__
        return module

    def exec_module(self, module) -> None:  # noqa: ANN001 - importlib protocol
        # The import system stamped the alias spec onto the real module; put
        # the real one back so importlib.resources and reload keep working.
        module.__spec__ = self._spec

    def get_code(self, fullname):  # noqa: ANN001 - used by `python -m old.module`
        spec = importlib.util.find_spec(self._target)
        return spec.loader.get_code(self._target)  # type: ignore[union-attr]


class _AliasFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):  # noqa: ANN001
        if not fullname.startswith(_OLD + ".") or fullname.endswith(".__main__"):
            # A package's __main__ is found on the real package's __path__, so
            # `python -m` runs the real file.
            return None
        new = _NEW + fullname[len(_OLD) :]
        target = sys.modules.get(new)
        target_spec = target.__spec__ if target is not None else importlib.util.find_spec(new)
        if target_spec is None:
            return None
        spec = importlib.util.spec_from_loader(
            fullname,
            _AliasLoader(new),
            is_package=target_spec.submodule_search_locations is not None,
        )
        if target_spec.submodule_search_locations is not None:
            spec.submodule_search_locations = list(target_spec.submodule_search_locations)
        return spec


if not any(isinstance(f, _AliasFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _AliasFinder())

sys.modules[_OLD] = importlib.import_module(_NEW)
