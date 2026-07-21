"""Security invariants for optional runtime dependency surfaces."""

from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib


ROOT = Path(__file__).resolve().parent.parent


def _lock_packages() -> list[dict]:
    return tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))["package"]


def _locked_version(name: str) -> tuple[int, ...]:
    matches = [package for package in _lock_packages() if package["name"] == name]
    assert len(matches) == 1, (
        f"expected one locked {name} package, found {len(matches)}"
    )
    return tuple(int(part) for part in matches[0]["version"].split("."))


def test_mlx_research_extra_keeps_transformers_in_patched_range() -> None:
    """Do not reintroduce the three model-loading/Trainer RCE advisories."""

    assert (5, 5) <= _locked_version("transformers") < (5, 13)
    assert (0, 6, 4) <= _locked_version("mlx-vlm") < (0, 7)


def test_transformers_is_confined_to_the_mlx_research_extra() -> None:
    roots = [
        package
        for package in _lock_packages()
        if package["name"] == "openadapt-flow"
        and package.get("source") == {"editable": "."}
    ]
    assert len(roots) == 1
    root = roots[0]

    core_names = {dependency["name"] for dependency in root["dependencies"]}
    mlx_names = {
        dependency["name"]
        for dependency in root["optional-dependencies"]["service-mlx"]
    }
    assert "transformers" not in core_names
    assert {"mlx-vlm", "transformers"} <= mlx_names


def test_privacy_extra_declares_spacy_runtime_click_dependency() -> None:
    """Do not rely on Typer to make spaCy's direct Click import available."""

    roots = [
        package
        for package in _lock_packages()
        if package["name"] == "openadapt-flow"
        and package.get("source") == {"editable": "."}
    ]
    assert len(roots) == 1
    privacy_names = {
        dependency["name"]
        for dependency in roots[0]["optional-dependencies"]["privacy"]
    }
    assert {"click", "openadapt-privacy"} <= privacy_names
