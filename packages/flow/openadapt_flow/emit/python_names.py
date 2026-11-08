"""Allocate injective Python symbols separately from workflow data names."""

from __future__ import annotations

import ast
import builtins
import keyword
import re
from collections.abc import Iterable

from pydantic import BaseModel


class PythonNames:
    """Allocate against Python, MCP field-model and template namespaces."""

    def __init__(self, occupied: Iterable[str] = ()) -> None:
        self.occupied = set(occupied) | set(dir(builtins)) | set(dir(BaseModel))

    @classmethod
    def from_source(cls, source: str) -> PythonNames:
        names: set[str] = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.arg):
                names.add(node.arg)
            elif isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                names.add(node.name)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[0])
        return cls(names)

    def allocate(self, suggested: str) -> str:
        stem = re.sub(r"[^a-zA-Z0-9_]", "_", suggested) or "value"
        if not stem[0].isalpha():
            stem = "value_" + stem
        name = stem
        suffix = 2
        while name in self.occupied or keyword.iskeyword(name):
            name = f"{stem}_{suffix}"
            suffix += 1
        self.occupied.add(name)
        return name
