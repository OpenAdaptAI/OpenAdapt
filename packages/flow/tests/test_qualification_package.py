"""``openadapt_flow.qualification`` keeps every name after its model moved out.

The qualification-project data model (constants, enums, Pydantic models and
their helpers) moved unchanged from ``qualification.py`` to
``qualification/models.py`` (openadapt-internal ``docs/design/monorepo-refactor-
2026-11/PLAN.md``, track F). The package re-exports each moved name, so every
``from openadapt_flow.qualification import X`` keeps working and gives the same
object.
"""

from __future__ import annotations

import ast
from pathlib import Path

import openadapt_flow.qualification as qualification
from openadapt_flow.qualification import models

MODELS = Path(models.__file__)


def _top_level_names() -> list[str]:
    names = []
    for node in ast.parse(MODELS.read_text(encoding="utf-8")).body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            names += [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.append(node.target.id)
    return names


def test_every_moved_name_is_the_same_object_through_the_old_path() -> None:
    names = _top_level_names()
    assert len(names) == 42
    missing = [
        n for n in names if getattr(qualification, n, None) is not getattr(models, n)
    ]
    assert missing == []


def test_the_old_imports_still_work() -> None:
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification import (
        QualificationError,
        QualificationProject,
        _qualification_actuation_path,
        build_outcome_envelope,
        evaluate_qualification,
    )

    # ir resolves Workflow.qualification to the same class.
    field = Workflow.model_fields["qualification"].annotation
    assert QualificationProject in getattr(field, "__args__", (field,))
    assert issubclass(QualificationError, ValueError)
    assert callable(evaluate_qualification) and callable(build_outcome_envelope)
    assert _qualification_actuation_path("api") == "api"


def test_models_import_nothing_that_imports_the_rest_of_the_package() -> None:
    tree = ast.parse(MODELS.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "openadapt_flow.qualification" not in imported
