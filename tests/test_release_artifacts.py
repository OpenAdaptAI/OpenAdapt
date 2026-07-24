from __future__ import annotations

import io
import runpy
import tarfile
import zipfile
from importlib.metadata import metadata as distribution_metadata
from importlib.metadata import requires as distribution_requires
from pathlib import Path

import pytest
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
verify_release_artifacts = runpy.run_path(
    str(ROOT / "scripts/verify_release_artifacts.py")
)["verify_release_artifacts"]


def _active_distribution_requirements(
    distribution: str,
    *,
    extra: str,
    sys_platform: str,
) -> list[Requirement]:
    """Read the generated PEP 566 metadata for one extra and target OS."""
    raw_requirements = distribution_requires(distribution)
    assert raw_requirements is not None
    platform_system = {
        "darwin": "Darwin",
        "linux": "Linux",
        "win32": "Windows",
    }[sys_platform]
    environment = default_environment()
    environment.update(
        {
            "extra": extra,
            "sys_platform": sys_platform,
            "platform_system": platform_system,
        }
    )
    return [
        requirement
        for raw in raw_requirements
        if (requirement := Requirement(raw)).marker is None
        or requirement.marker.evaluate(environment)
    ]


def _flow_extras_selected_by_launcher_all(sys_platform: str) -> set[str]:
    """Expand launcher self-extras exactly as an installer resolves `[all]`."""
    pending = {"all"}
    expanded: set[str] = set()
    flow_extras: set[str] = set()

    while pending:
        extra = pending.pop()
        if extra in expanded:
            continue
        expanded.add(extra)
        for requirement in _active_distribution_requirements(
            "openadapt",
            extra=extra,
            sys_platform=sys_platform,
        ):
            name = canonicalize_name(requirement.name)
            if name == "openadapt":
                pending.update(requirement.extras)
            elif name == "openadapt-flow":
                flow_extras.update(requirement.extras)

    return flow_extras


def _flow_packages_for_extras(
    extras: set[str],
    *,
    sys_platform: str,
) -> set[str]:
    packages: set[str] = set()
    for extra in extras:
        packages.update(
            canonicalize_name(requirement.name)
            for requirement in _active_distribution_requirements(
                "openadapt-flow",
                extra=extra,
                sys_platform=sys_platform,
            )
        )
    return packages


def _metadata(version: str, requires_python: str = ">=3.10,<3.13") -> bytes:
    return (
        "Metadata-Version: 2.4\n"
        "Name: openadapt\n"
        f"Version: {version}\n"
        "Summary: Beta launcher\n"
        f"Requires-Python: {requires_python}\n"
        "Classifier: Development Status :: 4 - Beta\n\n"
    ).encode()


def _release_tree(
    tmp_path: Path,
    artifact_version: str = "2.0.0",
    artifact_requires_python: str = ">=3.10,<3.13",
) -> tuple[Path, Path]:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "openadapt"\n'
        'version = "2.0.0"\n'
        'requires-python = ">=3.10,<3.13"\n',
        encoding="utf-8",
    )
    dist = tmp_path / "dist"
    dist.mkdir()

    wheel = dist / "openadapt-2.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, mode="w") as archive:
        archive.writestr(
            "openadapt-2.0.0.dist-info/METADATA",
            _metadata(artifact_version, artifact_requires_python),
        )

    sdist = dist / "openadapt-2.0.0.tar.gz"
    raw = _metadata(artifact_version, artifact_requires_python)
    info = tarfile.TarInfo("openadapt-2.0.0/PKG-INFO")
    info.size = len(raw)
    with tarfile.open(sdist, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(raw))
    return dist, wheel


def test_release_artifacts_accept_exact_matching_pair(tmp_path: Path):
    dist, wheel = _release_tree(tmp_path)

    actual_wheel, actual_sdist = verify_release_artifacts(dist, root=tmp_path)

    assert actual_wheel == wheel
    assert actual_sdist == dist / "openadapt-2.0.0.tar.gz"


def test_release_artifacts_accept_uv_ignore_marker(tmp_path: Path):
    dist, _ = _release_tree(tmp_path)
    (dist / ".gitignore").write_text("*", encoding="utf-8")

    verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_unexpected_files(tmp_path: Path):
    dist, _ = _release_tree(tmp_path)
    (dist / "stale.whl").write_bytes(b"stale")

    with pytest.raises(ValueError, match="unexpected=\\[stale.whl\\]"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_metadata_version_drift(tmp_path: Path):
    dist, _ = _release_tree(tmp_path, artifact_version="1.9.9")

    with pytest.raises(ValueError, match="version does not match 2.0.0"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_python_range_drift(tmp_path: Path):
    dist, _ = _release_tree(tmp_path, artifact_requires_python=">=3.10")

    with pytest.raises(ValueError, match="Requires-Python does not match"):
        verify_release_artifacts(dist, root=tmp_path)


@pytest.mark.parametrize(
    ("sys_platform", "platform_extra", "excluded_extra"),
    [
        ("win32", None, {"macos", "linux"}),
        ("darwin", "macos", {"linux"}),
        ("linux", "linux", {"macos"}),
    ],
)
def test_all_artifact_metadata_selects_platform_substrate_dependencies(
    sys_platform: str,
    platform_extra: str | None,
    excluded_extra: set[str],
):
    """`[all]` reaches Flow's substrate packages on every target platform.

    This reads the PEP 566 metadata generated for the editable launcher and its
    pinned Flow distribution—the same Requires-Dist contract emitted into the
    wheel/sdist—then evaluates markers for each supported OS.
    """
    provided_extras = set(
        distribution_metadata("openadapt").get_all("Provides-Extra", [])
    )
    assert {"all", "capture", "windows", "macos", "linux", "rdp"} <= provided_extras

    flow_extras = _flow_extras_selected_by_launcher_all(sys_platform)
    assert {"hosted", "capture", "privacy", "windows", "rdp"} <= flow_extras
    if platform_extra is not None:
        assert platform_extra in flow_extras
    assert flow_extras.isdisjoint(excluded_extra)

    flow_packages = _flow_packages_for_extras(
        flow_extras,
        sys_platform=sys_platform,
    )
    # Flow's cross-platform Windows HTTP client and real RDP transport are both
    # selected everywhere by `[all]`.
    assert {"requests", "aardwolf"} <= flow_packages
    if sys_platform == "linux":
        assert "pygobject" in flow_packages
    else:
        assert "pygobject" not in flow_packages
    if sys_platform == "darwin":
        assert {
            "pyobjc-framework-applicationservices",
            "pyobjc-framework-cocoa",
            "pyobjc-framework-quartz",
        } <= flow_packages
    else:
        assert not any(name.startswith("pyobjc-") for name in flow_packages)
