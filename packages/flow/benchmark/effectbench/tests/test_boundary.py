"""Source-availability boundary self-check (AGENTS.md 4.2).

EffectBench ships the MECHANISM + the SWER metric + a synthetic sample + the
reference scorer. It must NOT carry any crown-jewel artifact: the grown
hardening corpus, tuned adversary params, deployment-derived thresholds,
per-system-of-record oracle recipes, or real-EMR datasets. This test guards the
standalone package so a third party (and CI) can prove the artifact stays clean.
"""

from __future__ import annotations

from pathlib import Path

import effectbench

PACKAGE_DIR = Path(effectbench.__file__).resolve().parent

# Crown-jewel path/content tokens that must never appear in the standalone
# artifact (mirrors source-policy.yaml crown_jewel categories + the container
# real-system-of-record packs that stay outside the synthetic sample).
FORBIDDEN_TOKENS = (
    "openadapt_flow",
    "adversary_corpus",
    "grown_corpus",
    "tuned_adversary",
    "deployment_thresholds",
    "oracle_recipe",
    "real_emr",
    "held_out_corpus",
    "pixel_verify_cert",
    "identity_roc",
    "openemr",
    "frappe",
    "openimis",
)


def _package_py_files() -> list[Path]:
    return sorted(PACKAGE_DIR.rglob("*.py"))


def test_no_openadapt_flow_dependency() -> None:
    # The whole point of the standalone artifact: it imports nothing from the
    # OpenAdapt codebase.
    for path in _package_py_files():
        text = path.read_text(encoding="utf-8")
        assert "import openadapt_flow" not in text, path
        assert "from openadapt_flow" not in text, path


def test_version_is_not_the_missing_file_fallback() -> None:
    expected = (Path(__file__).resolve().parent.parent / "VERSION").read_text().strip()
    assert effectbench.__version__ == expected


def test_no_crown_jewel_tokens_in_the_package() -> None:
    for path in _package_py_files():
        lowered = path.read_text(encoding="utf-8").lower()
        for token in FORBIDDEN_TOKENS:
            assert token not in lowered, f"{token!r} leaked into {path}"


def test_only_synthetic_fixtures_ship() -> None:
    # The MockMed fixture must declare itself synthetic and target no real system.
    from effectbench.fixtures import mockmed

    src = Path(mockmed.__file__).read_text(encoding="utf-8").lower()
    assert "synthetic" in src
    assert "fake" in src


def test_runtime_dependency_is_only_pydantic() -> None:
    # A quick import-graph smoke: the package imports only stdlib + pydantic +
    # itself. (A heavier third party can also verify the installed wheel's
    # METADATA declares just pydantic.)
    import ast

    allowed_top = {"effectbench", "pydantic"}
    for path in _package_py_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top in allowed_top or _is_stdlib(top), (path, alias.name)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                assert top in allowed_top or _is_stdlib(top), (path, node.module)


_STDLIB = {
    "__future__", "argparse", "ast", "collections", "dataclasses", "datetime",
    "enum", "hashlib", "importlib", "json", "math", "pathlib", "platform",
    "random", "sys", "time", "typing", "collections.abc",
}


def _is_stdlib(name: str) -> bool:
    return name in _STDLIB
