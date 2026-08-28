from __future__ import annotations

import io
import json
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


def _metadata(
    version: str,
    requires_python: str = ">=3.10,<3.13",
    lifecycle_classifier: str | None = None,
) -> bytes:
    classifier = f"Classifier: {lifecycle_classifier}\n" if lifecycle_classifier else ""
    return (
        "Metadata-Version: 2.4\n"
        "Name: openadapt\n"
        f"Version: {version}\n"
        "Summary: OpenAdapt launcher\n"
        f"Requires-Python: {requires_python}\n"
        f"{classifier}\n"
    ).encode()


def _release_tree(
    tmp_path: Path,
    artifact_version: str = "2.0.0",
    artifact_requires_python: str = ">=3.10,<3.13",
    lifecycle_classifier: str | None = None,
) -> tuple[Path, Path]:
    license_body = b"MIT test license\n"
    (tmp_path / "LICENSE").write_bytes(license_body)
    (tmp_path / "source-policy.public.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "enforcement": {
                    "built_artifacts": {"path_prefixes": ["private/release-corpus"]},
                    "content_signature_parts": [["PRIVATE-", "RELEASE-SIGNATURE"]],
                },
            }
        ),
        encoding="utf-8",
    )
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
            _metadata(
                artifact_version,
                artifact_requires_python,
                lifecycle_classifier,
            ),
        )
        archive.writestr("openadapt-2.0.0.dist-info/licenses/LICENSE", license_body)

    sdist = dist / "openadapt-2.0.0.tar.gz"
    raw = _metadata(
        artifact_version,
        artifact_requires_python,
        lifecycle_classifier,
    )
    info = tarfile.TarInfo("openadapt-2.0.0/PKG-INFO")
    info.size = len(raw)
    with tarfile.open(sdist, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(raw))
        license_info = tarfile.TarInfo("openadapt-2.0.0/LICENSE")
        license_info.size = len(license_body)
        archive.addfile(license_info, io.BytesIO(license_body))
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


def test_release_artifacts_reject_directory_and_symlink_entries(tmp_path: Path):
    dist, wheel = _release_tree(tmp_path)
    (dist / "nested").mkdir()

    with pytest.raises(ValueError, match="invalid entries: nested"):
        verify_release_artifacts(dist, root=tmp_path)

    (dist / "nested").rmdir()
    wheel.unlink()
    wheel.symlink_to(tmp_path / "pyproject.toml")
    with pytest.raises(ValueError, match="expected one"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_metadata_version_drift(tmp_path: Path):
    dist, _ = _release_tree(tmp_path, artifact_version="1.9.9")

    with pytest.raises(ValueError, match="version does not match 2.0.0"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_python_range_drift(tmp_path: Path):
    dist, _ = _release_tree(tmp_path, artifact_requires_python=">=3.10")

    with pytest.raises(ValueError, match="Requires-Python does not match"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_reject_static_lifecycle_metadata(tmp_path: Path):
    """Immutable package bytes must not override the signed lifecycle record."""

    dist, _ = _release_tree(
        tmp_path,
        lifecycle_classifier="Development Status :: 4 - Beta",
    )

    with pytest.raises(ValueError, match="static lifecycle classifier"):
        verify_release_artifacts(dist, root=tmp_path)


def test_release_artifacts_require_policy_and_exact_license(tmp_path: Path):
    policy_root = tmp_path / "policy"
    policy_root.mkdir()
    dist, _ = _release_tree(policy_root)
    (policy_root / "source-policy.public.json").unlink()
    with pytest.raises(ValueError, match="rendered source policy"):
        verify_release_artifacts(dist, root=policy_root)

    license_root = tmp_path / "license"
    license_root.mkdir()
    dist, _ = _release_tree(license_root)
    (license_root / "LICENSE").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="exact required LICENSE"):
        verify_release_artifacts(dist, root=license_root)


def test_release_artifacts_enforce_built_archive_policy(tmp_path: Path):
    dist, wheel = _release_tree(tmp_path)
    with zipfile.ZipFile(wheel, mode="a") as archive:
        archive.writestr("private/release-corpus/sample.json", b"{}")

    with pytest.raises(ValueError, match="denied built artifact path"):
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
    assert {"browser", "hosted", "capture", "privacy", "windows", "rdp"} <= flow_extras
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
