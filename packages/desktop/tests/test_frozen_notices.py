from __future__ import annotations

import hashlib
import json
from email.message import Message
from importlib.metadata import distribution
from pathlib import Path, PurePosixPath

import pytest

from scripts import frozen_notices as notices


class FakeDistribution:
    def __init__(
        self,
        root: Path,
        name: str,
        *,
        version: str = "1.0.0",
        requires: list[str] | None = None,
        license_expression: str | None = "MIT",
        license: str | None = None,
        classifiers: list[str] | None = None,
        notice_files: dict[str, str] | None = None,
    ) -> None:
        self.root = root
        self.version = version
        self.requires = requires or []
        self.metadata = Message()
        self.metadata["Name"] = name
        if license_expression is not None:
            self.metadata["License-Expression"] = license_expression
        if license is not None:
            self.metadata["License"] = license
        for classifier in classifiers or []:
            self.metadata["Classifier"] = classifier
        self.files = []
        for member, text in (notice_files or {}).items():
            path = root / member
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            self.files.append(PurePosixPath(member))

    def locate_file(self, member: PurePosixPath) -> Path:
        return self.root / member


def test_dependency_closure_resolves_only_selected_extra(tmp_path: Path) -> None:
    distributions = {
        "desktop": FakeDistribution(
            tmp_path / "desktop",
            "desktop",
            requires=[
                "runtime",
                "builder; extra == 'build'",
                "developer; extra == 'dev'",
            ],
        ),
        "runtime": FakeDistribution(tmp_path / "runtime", "runtime"),
        "builder": FakeDistribution(tmp_path / "builder", "builder", requires=["runtime"]),
        "developer": FakeDistribution(tmp_path / "developer", "developer"),
    }

    closure = notices.dependency_closure(
        root_name="desktop",
        root_extras={"build"},
        distribution_getter=distributions.__getitem__,
    )

    assert set(closure) == {"desktop", "runtime", "builder"}


def test_frozen_runtime_closure_excludes_build_tools(tmp_path: Path) -> None:
    distributions = {
        "openadapt-desktop": FakeDistribution(
            tmp_path / "desktop",
            "openadapt-desktop",
            requires=[
                "desktop-runtime",
                "pyinstaller; extra == 'build'",
                "openadapt-flow; extra == 'build'",
            ],
        ),
        "desktop-runtime": FakeDistribution(
            tmp_path / "desktop-runtime",
            "desktop-runtime",
        ),
        "openadapt-flow": FakeDistribution(
            tmp_path / "flow",
            "openadapt-flow",
            requires=["flow-runtime"],
        ),
        "flow-runtime": FakeDistribution(tmp_path / "flow-runtime", "flow-runtime"),
        "pyinstaller": FakeDistribution(tmp_path / "pyinstaller", "pyinstaller"),
    }

    closure = notices.frozen_runtime_closure(
        distribution_getter=distributions.__getitem__,
    )

    assert set(closure) == {
        "openadapt-desktop",
        "desktop-runtime",
        "openadapt-flow",
        "flow-runtime",
    }
    assert "pyinstaller" not in closure


def test_frozen_runtime_closure_excludes_managed_vision_wheels(
    tmp_path: Path,
) -> None:
    distributions = {
        "openadapt-desktop": FakeDistribution(
            tmp_path / "desktop",
            "openadapt-desktop",
            requires=["openadapt-flow"],
        ),
        "openadapt-flow": FakeDistribution(
            tmp_path / "flow",
            "openadapt-flow",
            requires=["numpy", "opencv-python-headless", "rapidocr-onnxruntime"],
        ),
        "numpy": FakeDistribution(tmp_path / "numpy", "numpy"),
        "opencv-python-headless": FakeDistribution(
            tmp_path / "opencv-headless",
            "opencv-python-headless",
        ),
        "rapidocr-onnxruntime": FakeDistribution(
            tmp_path / "rapidocr",
            "rapidocr-onnxruntime",
            requires=["opencv-python", "onnxruntime"],
        ),
        "opencv-python": FakeDistribution(tmp_path / "opencv", "opencv-python"),
        "onnxruntime": FakeDistribution(tmp_path / "onnxruntime", "onnxruntime"),
    }

    closure = notices.frozen_runtime_closure(
        distribution_getter=distributions.__getitem__,
    )

    assert set(closure) == {
        "onnxruntime",
        "openadapt-desktop",
        "openadapt-flow",
    }


def test_dependency_closure_terminates_on_cycles(tmp_path: Path) -> None:
    distributions = {
        "desktop": FakeDistribution(
            tmp_path / "desktop",
            "desktop",
            requires=["runtime"],
        ),
        "runtime": FakeDistribution(
            tmp_path / "runtime",
            "runtime",
            requires=["desktop"],
        ),
    }

    closure = notices.dependency_closure(
        root_name="desktop",
        root_extras=set(),
        distribution_getter=distributions.__getitem__,
    )

    assert set(closure) == {"desktop", "runtime"}


def _bootloader_kwargs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_closure: dict[str, FakeDistribution],
) -> dict[str, object]:
    notice_text = "\n".join(notices.PYINSTALLER_EXCEPTION_MARKERS) + "\n"
    pyinstaller = FakeDistribution(
        tmp_path / "pyinstaller",
        "pyinstaller",
        version=notices.PYINSTALLER_VERSION,
        license_expression="GPL-2.0-or-later WITH Bootloader-exception",
        notice_files={
            (
                f"pyinstaller-{notices.PYINSTALLER_VERSION}.dist-info/licenses/COPYING.txt"
            ): notice_text,
            "PyInstaller/__init__.py": "",
        },
    )
    monkeypatch.setattr(
        notices,
        "PYINSTALLER_NOTICE_SHA256",
        hashlib.sha256(notice_text.encode()).hexdigest(),
    )
    return {
        "build_closure": {**runtime_closure, "pyinstaller": pyinstaller},
        "pyinstaller_dist": pyinstaller,
    }


def test_notice_bundle_copies_files_and_hashes_inventory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = FakeDistribution(
        tmp_path / "desktop",
        "openadapt-desktop",
        notice_files={"dist-info/LICENSE": "desktop MIT\n"},
    )
    capture = FakeDistribution(
        tmp_path / "capture",
        "openadapt-capture",
        notice_files={"dist-info/LICENSE": "capture MIT\n"},
    )
    closure = {
        "openadapt-capture": capture,
        "openadapt-desktop": desktop,
    }
    output = notices.prepare_notice_bundle(
        tmp_path / "bundle",
        root_license=tmp_path / "unused",
        closure=closure,
        required_notice_tokens={
            "openadapt-desktop": ("license",),
            "openadapt-capture": ("license",),
        },
        **_bootloader_kwargs(tmp_path, monkeypatch, closure),
    )

    inventory = json.loads((output / notices.NOTICE_INVENTORY_NAME).read_text())
    assert inventory["schema_version"] == 2
    assert [package["name"] for package in inventory["packages"]] == [
        "openadapt-capture",
        "openadapt-desktop",
    ]
    for package in inventory["packages"]:
        assert package["notices"]
        member = package["notices"][0]["bundled_member"]
        relative = member.removeprefix(f"{notices.NOTICE_BUNDLE_MEMBER}/")
        assert (output / relative).is_file()
    assert inventory["embedded_build_components"][0]["name"] == "pyinstaller-bootloader"
    assert inventory["build_only_packages"] == [
        {
            "archive_import_roots": ["PyInstaller"],
            "name": "pyinstaller",
            "version": notices.PYINSTALLER_VERSION,
        }
    ]
    assert (output / "build-components" / "pyinstaller" / "COPYING.txt").is_file()


def test_bootloader_notice_rejects_unreviewed_bytes(tmp_path: Path) -> None:
    pyinstaller = FakeDistribution(
        tmp_path / "pyinstaller",
        "pyinstaller",
        version=notices.PYINSTALLER_VERSION,
        license_expression="GPL-2.0-or-later WITH Bootloader-exception",
        notice_files={
            (
                f"pyinstaller-{notices.PYINSTALLER_VERSION}.dist-info/licenses/COPYING.txt"
            ): "\n".join(notices.PYINSTALLER_EXCEPTION_MARKERS) + "\nchanged\n",
        },
    )

    with pytest.raises(RuntimeError, match="notice bytes require review"):
        notices._stage_pyinstaller_bootloader_notice(  # noqa: SLF001
            tmp_path / "bundle",
            pyinstaller_dist=pyinstaller,
        )


def test_bootloader_notice_stages_canonical_lf_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notice_text = "\n".join(notices.PYINSTALLER_EXCEPTION_MARKERS) + "\n"
    pyinstaller = FakeDistribution(
        tmp_path / "pyinstaller",
        "pyinstaller",
        version=notices.PYINSTALLER_VERSION,
        license_expression="GPL-2.0-or-later WITH Bootloader-exception",
        notice_files={
            (
                f"pyinstaller-{notices.PYINSTALLER_VERSION}.dist-info/licenses/COPYING.txt"
            ): notice_text,
        },
    )
    source = pyinstaller.locate_file(pyinstaller.files[0])
    source.write_bytes(notice_text.replace("\n", "\r\n").encode())
    monkeypatch.setattr(
        notices,
        "PYINSTALLER_NOTICE_SHA256",
        hashlib.sha256(notice_text.encode()).hexdigest(),
    )

    record = notices._stage_pyinstaller_bootloader_notice(  # noqa: SLF001
        tmp_path / "bundle",
        pyinstaller_dist=pyinstaller,
    )

    staged = tmp_path / "bundle" / "build-components" / "pyinstaller" / "COPYING.txt"
    assert staged.read_bytes() == notice_text.encode()
    assert record["sha256"] == hashlib.sha256(notice_text.encode()).hexdigest()


def test_bootloader_notice_rejects_unreviewed_version(tmp_path: Path) -> None:
    pyinstaller = FakeDistribution(
        tmp_path / "pyinstaller",
        "pyinstaller",
        version="6.22.0",
        license_expression="GPL-2.0-or-later WITH Bootloader-exception",
    )

    with pytest.raises(RuntimeError, match="requires a new bootloader license review"):
        notices._stage_pyinstaller_bootloader_notice(  # noqa: SLF001
            tmp_path / "bundle",
            pyinstaller_dist=pyinstaller,
        )


def test_notice_bundle_rejects_copyleft_before_staging(tmp_path: Path) -> None:
    forbidden = FakeDistribution(
        tmp_path / "forbidden",
        "oa-atomacos",
        license_expression="GPL-2.0",
        notice_files={"dist-info/LICENSE": "GPL\n"},
    )

    with pytest.raises(RuntimeError, match="copyleft distribution"):
        notices.prepare_notice_bundle(
            tmp_path / "bundle",
            root_license=tmp_path / "LICENSE",
            closure={"oa-atomacos": forbidden},
            required_notice_tokens={},
        )


@pytest.mark.parametrize("name", ["av", "scipy"])
def test_known_binary_runtime_boundary_rejects_permissive_wrapper_metadata(
    tmp_path: Path,
    name: str,
) -> None:
    forbidden = FakeDistribution(
        tmp_path / name,
        name,
        license_expression="BSD-3-Clause",
        notice_files={"dist-info/LICENSE": "permissive wrapper terms\n"},
    )

    with pytest.raises(RuntimeError, match="copyleft distribution"):
        notices.prepare_notice_bundle(
            tmp_path / "bundle",
            root_license=tmp_path / "LICENSE",
            closure={name: forbidden},
            required_notice_tokens={},
        )


def test_reviewed_matplotlib_dual_license_uses_exact_ftl_evidence() -> None:
    matplotlib = distribution("matplotlib")
    evidence = notices.license_evidence(matplotlib)

    assert notices.COPYLEFT_LICENSE_RE.search("\n".join(evidence))
    assert not notices.has_unapproved_copyleft_evidence(
        "matplotlib",
        str(matplotlib.version),
        evidence,
    )


def test_reviewed_dual_license_rejects_any_metadata_drift() -> None:
    assert notices.has_unapproved_copyleft_evidence(
        "matplotlib",
        "3.11.1",
        ["License: FTL OR GPL-2.0-or-later\nchanged"],
    )


def _flatbuffers_distribution(tmp_path: Path) -> FakeDistribution:
    return FakeDistribution(
        tmp_path / "flatbuffers",
        "flatbuffers",
        version="25.12.19",
        license_expression=None,
        license="Apache 2.0",
        classifiers=["License :: OSI Approved :: Apache Software License"],
    )


def _rapidocr_distribution(tmp_path: Path) -> FakeDistribution:
    return FakeDistribution(
        tmp_path / "rapidocr",
        "rapidocr-onnxruntime",
        version="1.4.4",
        license_expression=None,
        license="Apache-2.0",
    )


def test_flatbuffers_uses_exact_reviewed_upstream_notice(tmp_path: Path) -> None:
    flatbuffers = _flatbuffers_distribution(tmp_path)

    sources = notices._reviewed_external_notice_sources(  # noqa: SLF001
        "flatbuffers",
        flatbuffers,
    )

    assert len(sources) == 1
    source_member, source = sources[0]
    assert (
        notices.REVIEWED_EXTERNAL_NOTICE_FILES[("flatbuffers", str(flatbuffers.version))][
            "source_commit"
        ]
        in source_member
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"
    )


def test_reviewed_notice_hash_is_stable_across_windows_line_endings(
    tmp_path: Path,
) -> None:
    flatbuffers = _flatbuffers_distribution(tmp_path)
    notice = tmp_path / "notices" / "flatbuffers" / "LICENSE"
    notice.parent.mkdir(parents=True)
    canonical = (notices.REVIEWED_NOTICE_ROOT / "flatbuffers" / "LICENSE").read_bytes()
    notice.write_bytes(canonical.replace(b"\n", b"\r\n"))

    sources = notices._reviewed_external_notice_sources(  # noqa: SLF001
        "flatbuffers",
        flatbuffers,
        notice_root=tmp_path / "notices",
    )

    assert sources == [
        (
            (
                "reviewed-upstream:"
                + notices.REVIEWED_EXTERNAL_NOTICE_FILES[
                    ("flatbuffers", str(flatbuffers.version))
                ]["source_url"]
                + "#commit="
                + notices.REVIEWED_EXTERNAL_NOTICE_FILES[
                    ("flatbuffers", str(flatbuffers.version))
                ]["source_commit"]
            ),
            notice,
        )
    ]


def test_reviewed_external_notice_rejects_byte_drift(tmp_path: Path) -> None:
    flatbuffers = _flatbuffers_distribution(tmp_path)
    notice = tmp_path / "notices" / "flatbuffers" / "LICENSE"
    notice.parent.mkdir(parents=True)
    notice.write_text("changed terms\n")

    with pytest.raises(RuntimeError, match="notice bytes require review"):
        notices._reviewed_external_notice_sources(  # noqa: SLF001
            "flatbuffers",
            flatbuffers,
            notice_root=tmp_path / "notices",
        )


def test_reviewed_external_notice_is_not_a_version_wildcard(tmp_path: Path) -> None:
    flatbuffers = FakeDistribution(
        tmp_path / "installed",
        "flatbuffers",
        version="25.12.20",
        license_expression="Apache 2.0",
    )

    assert (
        notices._reviewed_external_notice_sources(  # noqa: SLF001
            "flatbuffers",
            flatbuffers,
            notice_root=tmp_path / "notices",
        )
        == []
    )


def test_loguru_uses_exact_reviewed_upstream_notice() -> None:
    loguru = distribution("loguru")

    sources = notices._reviewed_external_notice_sources(  # noqa: SLF001
        "loguru",
        loguru,
    )

    assert len(sources) == 1
    source_member, source = sources[0]
    assert (
        notices.REVIEWED_EXTERNAL_NOTICE_FILES[("loguru", str(loguru.version))]["source_commit"]
        in source_member
    )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == (
        "b35d026cc7aca9d5859a02eb87ddf7a386a24c986838651bd1f283f94e003327"
    )


def test_rapidocr_uses_exact_reviewed_upstream_notice(tmp_path: Path) -> None:
    rapidocr = _rapidocr_distribution(tmp_path)

    sources = notices._reviewed_external_notice_sources(  # noqa: SLF001
        "rapidocr-onnxruntime",
        rapidocr,
    )

    assert len(sources) == 1
    assert hashlib.sha256(sources[0][1].read_bytes()).hexdigest() == (
        "3e0af25fdd06aa9586ae97adb00ea927ebe5a3805ac77d2d3a81ce5f55693333"
    )


def test_notice_discovery_ignores_binary_symbol_named_copying(tmp_path: Path) -> None:
    pyobjc = FakeDistribution(
        tmp_path / "pyobjc",
        "pyobjc-core",
        notice_files={
            (
                "PyObjCTest/copying.cpython-312-darwin.so.dSYM/"
                "Contents/Resources/DWARF/copying.cpython-312-darwin.so"
            ): "binary-like symbol payload",
        },
    )

    assert notices._notice_sources(pyobjc) == []  # noqa: SLF001


def test_unreviewed_dual_license_is_not_a_blanket_exception() -> None:
    assert notices.has_unapproved_copyleft_evidence(
        "another-package",
        "1.0.0",
        ["License-Expression: MIT OR GPL-2.0-only"],
    )


def test_first_party_mit_fallback_is_explicit_and_concrete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    privacy = FakeDistribution(tmp_path / "privacy", "openadapt-privacy")
    root_license = tmp_path / "LICENSE"
    root_license.write_text("OpenAdapt MIT terms\n")

    closure = {"openadapt-privacy": privacy}
    output = notices.prepare_notice_bundle(
        tmp_path / "bundle",
        root_license=root_license,
        closure=closure,
        required_notice_tokens={"openadapt-privacy": ("license",)},
        **_bootloader_kwargs(tmp_path, monkeypatch, closure),
    )

    inventory = json.loads((output / notices.NOTICE_INVENTORY_NAME).read_text())
    notice = inventory["packages"][0]["notices"][0]
    assert notice["source_member"] == "workspace:LICENSE"
    relative = notice["bundled_member"].removeprefix(f"{notices.NOTICE_BUNDLE_MEMBER}/")
    assert (output / relative).read_text() == "OpenAdapt MIT terms\n"


def test_notice_bundle_rejects_third_party_without_concrete_notice(
    tmp_path: Path,
) -> None:
    unknown = FakeDistribution(
        tmp_path / "unknown",
        "unknown-package",
        license_expression="",
    )

    with pytest.raises(RuntimeError, match="no concrete license/NOTICE"):
        notices.prepare_notice_bundle(
            tmp_path / "bundle",
            root_license=tmp_path / "LICENSE",
            closure={"unknown-package": unknown},
            required_notice_tokens={},
        )


def test_metadata_license_does_not_replace_concrete_notice(
    tmp_path: Path,
) -> None:
    dependency = FakeDistribution(
        tmp_path / "dependency",
        "metadata-only",
        license_expression="MIT",
    )

    with pytest.raises(RuntimeError, match="no concrete license/NOTICE"):
        notices.prepare_notice_bundle(
            tmp_path / "bundle",
            root_license=tmp_path / "LICENSE",
            closure={"metadata-only": dependency},
            required_notice_tokens={},
        )
