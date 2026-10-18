"""The install-weight budgets and the rules that enforce them.

The CI job (packages.yml, install-weight) runs the real installs. These tests
pin the rules: what counts as a violation, which stacks each leaf package
must never pull, and that a build never leaves its version label behind.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "install_weight" / "weigh.py"
SPEC = importlib.util.spec_from_file_location("weigh", SCRIPT)
assert SPEC and SPEC.loader
weigh = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(weigh)

CONFIG = json.loads((ROOT / "scripts" / "install_weight" / "budgets.json").read_text())
PACKAGES = CONFIG["packages"]
DEFAULTS = CONFIG["defaults"]


def _result(*dists: str, mb: float = 1.0, import_error: str | None = None) -> dict:
    return {
        "dists": [f"{name}==1.0" for name in dists],
        "dist_count": len(dists),
        "site_packages_bytes": int(mb * 1e6),
        "import_error": import_error,
    }


BUDGET = {"allow": ["pydantic", "pydantic-core", "pyobjc-*"], "max_mb": 10, "max_dists": 3}


def test_an_install_inside_its_budget_passes() -> None:
    assert weigh.violations(_result("pydantic", "pyobjc-core"), BUDGET, DEFAULTS) == []


def test_a_distribution_outside_the_allow_list_fails() -> None:
    problems = weigh.violations(_result("pydantic", "requests"), BUDGET, DEFAULTS)
    assert problems == ["pulls requests, which is not in its allow list"]


def test_a_forbidden_distribution_fails_even_when_allowed() -> None:
    budget = dict(BUDGET, allow=["*"], forbid=["opencv-*"])
    problems = weigh.violations(_result("opencv-python", "torch"), budget, DEFAULTS)
    assert problems == [
        "pulls forbidden distribution opencv-python",
        "pulls forbidden distribution torch",
    ]


def test_size_count_and_import_failures_fail() -> None:
    problems = weigh.violations(
        _result("pydantic", "pydantic-core", "pyobjc-core", "pyobjc-cocoa", mb=11, import_error="boom"),
        BUDGET,
        DEFAULTS,
    )
    assert problems == [
        "does not import alone: boom",
        "installs 11.0 MB, over its 10 MB budget",
        "installs 4 distributions, over its budget of 3",
    ]


def _forbids(label: str, name: str) -> bool:
    budget = PACKAGES[label]
    return bool(
        weigh.violations(_result(name), dict(budget, max_dists=99, max_mb=1e9), DEFAULTS)
    )


@pytest.mark.parametrize(
    "name",
    [
        "openadapt-flow",
        "openadapt-desktop",
        "opencv-python",
        "opencv-python-headless",
        "onnxruntime",
        "onnxruntime-gpu",
        "rapidocr-onnxruntime",
        "torch",
        "playwright",
    ],
)
@pytest.mark.parametrize("label", ["privacy", "types"])
def test_leaf_packages_never_pull_the_engine_desktop_or_vision_stacks(label: str, name: str) -> None:
    assert _forbids(label, name)


@pytest.mark.parametrize("name", ["matplotlib", "sounddevice", "soundfile", "pympler", "alembic"])
def test_capture_core_never_pulls_its_opt_in_stacks(name: str) -> None:
    assert _forbids("capture", name)


def test_types_allows_only_pydantic_and_itself() -> None:
    assert set(PACKAGES["types"]["allow"]) == {
        "annotated-types",
        "openadapt-types",
        "pydantic",
        "pydantic-core",
        "typing-extensions",
        "typing-inspection",
    }


def test_every_package_has_a_budget_and_no_allow_forbid_overlap() -> None:
    assert set(PACKAGES) == {"types", "privacy", "capture", "flow", "agent", "desktop", "launcher"}
    for label, budget in PACKAGES.items():
        assert {"path", "module", "max_mb", "max_dists", "allow"} <= set(budget), label
        overlap = [
            name
            for name in budget["allow"]
            if weigh.violations(_result(name), dict(budget, max_dists=99, max_mb=1e9), DEFAULTS)
        ]
        assert not overlap, (label, overlap)


def test_local_build_patches_a_copy_and_never_the_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "pkg"
    (package / "example").mkdir(parents=True)
    (package / "example" / "__init__.py").write_text("", encoding="utf-8")
    original = '[project]\nname = "example"\nversion = "1.2.3"\n\n[tool.x]\nversion = "9"\n'
    (package / "pyproject.toml").write_text(original, encoding="utf-8")
    seen = {}

    def fake_run(command, check=False, **kwargs):  # noqa: ANN001 - subprocess.run stand-in
        if command[0] == "git":
            return weigh.subprocess.CompletedProcess(command, 128, "", "not a git repository")
        built = Path(command[-1])
        seen["text"] = (built / "pyproject.toml").read_text(encoding="utf-8")
        seen["files"] = sorted(p.relative_to(built).as_posix() for p in built.rglob("*") if p.is_file())
        seen["dir"] = built
        return weigh.subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(weigh.subprocess, "run", fake_run)
    assert weigh.build_local_wheel(package, tmp_path / "dist") == ("example", "1.2.3+weight")
    assert 'version = "1.2.3+weight"' in seen["text"]
    assert 'version = "9"' in seen["text"]
    assert seen["files"] == ["example/__init__.py", "pyproject.toml"]
    assert seen["dir"] != package and not seen["dir"].exists()
    assert (package / "pyproject.toml").read_text(encoding="utf-8") == original
