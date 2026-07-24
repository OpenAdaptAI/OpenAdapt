#!/usr/bin/env python3
"""Verify that CI produced a usable platform-native desktop artifact."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

try:
    from scripts.frozen_notices import (
        EXTERNALLY_PROVISIONED_DISTRIBUTIONS,
        FORBIDDEN_FROZEN_DISTRIBUTIONS,
        FROZEN_RUNTIME_ROOTS,
        NOTICE_BUNDLE_MEMBER,
        NOTICE_INVENTORY_NAME,
        PYINSTALLER_DISTRIBUTION,
        PYINSTALLER_EXCEPTION_MARKERS,
        PYINSTALLER_NOTICE_MEMBER,
        PYINSTALLER_NOTICE_SHA256,
        PYINSTALLER_VERSION,
        REQUIRED_NOTICE_TOKENS,
        has_unapproved_copyleft_evidence,
    )
except ModuleNotFoundError:  # pragma: no cover - direct ``python scripts/...`` use
    from frozen_notices import (
        EXTERNALLY_PROVISIONED_DISTRIBUTIONS,
        FORBIDDEN_FROZEN_DISTRIBUTIONS,
        FROZEN_RUNTIME_ROOTS,
        NOTICE_BUNDLE_MEMBER,
        NOTICE_INVENTORY_NAME,
        PYINSTALLER_DISTRIBUTION,
        PYINSTALLER_EXCEPTION_MARKERS,
        PYINSTALLER_NOTICE_MEMBER,
        PYINSTALLER_NOTICE_SHA256,
        PYINSTALLER_VERSION,
        REQUIRED_NOTICE_TOKENS,
        has_unapproved_copyleft_evidence,
    )

ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_FROZEN_MEMBERS = re.compile(
    r"(?:openimis|adversary_corpus|identity_roc|reliability_corpus|"
    r"grown[_-]corpus|oracle[_-]recipes|oa[_.-]atomacos|pynput|"
    r"(?:^|[/.'\" _-])scipy(?:[/.'\" _-]|$)|"
    r"(?:^|[/.'\" ])av(?:[/.'\" ]|$)|"
    r"lib(?:x264|x265|quadmath|gfortran|gcc))",
    re.IGNORECASE,
)
FORBIDDEN_EMBEDDED_VISION_MEMBERS = re.compile(
    r"(?:^|[/.'\" _-])(?:numpy|cv2|opencv[_-]python(?:[_-]headless)?|"
    r"rapidocr[_-]onnxruntime)(?:[/.'\" _-]|$)",
    re.IGNORECASE,
)

REQUIRED_FROZEN_NOTICES = (
    "third_party/onnxruntime/LICENSE",
    "third_party/onnxruntime/ThirdPartyNotices.txt",
    "third_party/rapidocr/LICENSE",
    "third_party/rapidocr/NOTICE",
)
NOTICE_INVENTORY_MEMBER = f"{NOTICE_BUNDLE_MEMBER}/{NOTICE_INVENTORY_NAME}"


def bundled_flow_banner(root: Path = ROOT) -> str:
    """Return the CLI version banner for the exact configured Flow build pin."""

    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = pyproject["project"]["optional-dependencies"]["build"]
    pins = [
        dependency.removeprefix("openadapt-flow==")
        for dependency in dependencies
        if dependency.startswith("openadapt-flow==")
    ]
    if len(pins) != 1 or not re.fullmatch(r"\d+\.\d+\.\d+", pins[0]):
        raise ValueError(f"expected one exact openadapt-flow build pin, found: {pins}")
    return f"openadapt-flow {pins[0]}"


def normalized_inventory(value: str) -> str:
    """Use one member separator for PyInstaller inventories on every OS."""

    # archive_viewer prints member names with ``repr``. A Windows path therefore
    # contains two literal backslashes in stdout; collapse the whole separator
    # run rather than converting each one into a separate slash.
    return re.sub(r"[\\/]+", "/", value)


def frozen_member_keys(raw_members) -> dict[str, str]:
    """Map normalized archive members back to their exact platform keys."""

    member_keys: dict[str, str] = {}
    for raw_member in raw_members:
        member = normalized_inventory(raw_member)
        if member in member_keys and member_keys[member] != raw_member:
            raise ValueError(f"duplicate normalized frozen member: {member}")
        member_keys[member] = raw_member
    return member_keys


def validate_frozen_notice_inventory(
    inventory_payload: bytes,
    *,
    members: set[str],
    extract_member,
) -> tuple[str, ...]:
    """Validate the generated notice inventory against exact archive bytes."""

    try:
        inventory = json.loads(inventory_payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen notice inventory is not valid UTF-8 JSON") from exc
    if inventory.get("schema_version") != 2:
        raise ValueError("frozen notice inventory has an unsupported schema")
    if inventory.get("runtime_roots") != list(FROZEN_RUNTIME_ROOTS):
        raise ValueError("frozen notice inventory has unexpected runtime roots")
    packages = inventory.get("packages")
    if not isinstance(packages, list) or not packages:
        raise ValueError("frozen notice inventory has no packages")

    package_index: dict[str, dict] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise ValueError("frozen notice inventory has a malformed package")
        name = package["name"]
        if name in package_index:
            raise ValueError(f"duplicate frozen notice package: {name}")
        evidence = package.get("license_evidence")
        if not isinstance(evidence, list) or not all(isinstance(value, str) for value in evidence):
            raise ValueError(f"invalid license evidence for {name}")
        version = package.get("version")
        if not isinstance(version, str):
            raise ValueError(f"invalid frozen notice version for {name}")
        if name in EXTERNALLY_PROVISIONED_DISTRIBUTIONS:
            raise ValueError(f"separately provisioned package crossed frozen boundary: {name}")
        if name in FORBIDDEN_FROZEN_DISTRIBUTIONS or (
            has_unapproved_copyleft_evidence(name, version, evidence)
        ):
            raise ValueError(f"copyleft package crossed frozen boundary: {name}")
        notices = package.get("notices")
        if not isinstance(notices, list) or not notices:
            raise ValueError(f"missing concrete notice files for {name}")
        for notice in notices:
            if not isinstance(notice, dict):
                raise ValueError(f"invalid notice record for {name}")
            member = notice.get("bundled_member")
            expected_hash = notice.get("sha256")
            if not isinstance(member, str) or member not in members:
                raise ValueError(f"notice member is absent for {name}: {member}")
            if not isinstance(expected_hash, str) or not re.fullmatch(
                r"[0-9a-f]{64}", expected_hash
            ):
                raise ValueError(f"invalid notice hash for {name}: {expected_hash}")
            payload = extract_member(member)
            if hashlib.sha256(payload).hexdigest() != expected_hash:
                raise ValueError(f"notice hash mismatch for {name}: {member}")
        package_index[name] = package

    for required_name, required_tokens in REQUIRED_NOTICE_TOKENS.items():
        package = package_index.get(required_name)
        if package is None:
            raise ValueError(f"required frozen notice package is absent: {required_name}")
        notice_members = [str(notice["bundled_member"]).lower() for notice in package["notices"]]
        for token in required_tokens:
            if not any(token.lower() in member for member in notice_members):
                raise ValueError(f"{required_name} is missing required {token} notice")

    build_only_packages = inventory.get("build_only_packages")
    if not isinstance(build_only_packages, list) or not build_only_packages:
        raise ValueError("frozen notice inventory has no build-only package boundary")
    build_only_import_roots: set[str] = set()
    build_only_names: set[str] = set()
    for package in build_only_packages:
        if not isinstance(package, dict):
            raise ValueError("frozen notice inventory has a malformed build-only package")
        name = package.get("name")
        version = package.get("version")
        roots = package.get("archive_import_roots")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or not isinstance(roots, list)
            or not roots
            or not all(isinstance(root, str) and root.isidentifier() for root in roots)
        ):
            raise ValueError(f"invalid build-only package record: {package}")
        if name in package_index or name in build_only_names:
            raise ValueError(f"duplicate runtime/build-only package: {name}")
        build_only_names.add(name)
        build_only_import_roots.update(roots)

    pyinstaller_build_record = next(
        (package for package in build_only_packages if package["name"] == PYINSTALLER_DISTRIBUTION),
        None,
    )
    if (
        pyinstaller_build_record is None
        or pyinstaller_build_record["version"] != PYINSTALLER_VERSION
        or "PyInstaller" not in pyinstaller_build_record["archive_import_roots"]
    ):
        raise ValueError("exact PyInstaller build-only boundary is absent")

    components = inventory.get("embedded_build_components")
    if not isinstance(components, list) or len(components) != 1:
        raise ValueError("frozen notice inventory has an invalid build-component boundary")
    bootloader = components[0]
    if not isinstance(bootloader, dict):
        raise ValueError("PyInstaller bootloader notice record is malformed")
    expected_fields = {
        "name": "pyinstaller-bootloader",
        "source_distribution": PYINSTALLER_DISTRIBUTION,
        "source_version": PYINSTALLER_VERSION,
        "license_scope": "GPL-2.0-or-later WITH PyInstaller-Bootloader-exception",
        "source_member": (f"pyinstaller-{PYINSTALLER_VERSION}.dist-info/licenses/COPYING.txt"),
        "bundled_member": PYINSTALLER_NOTICE_MEMBER,
        "sha256": PYINSTALLER_NOTICE_SHA256,
        "required_markers": list(PYINSTALLER_EXCEPTION_MARKERS),
    }
    for key, expected in expected_fields.items():
        if bootloader.get(key) != expected:
            raise ValueError(f"PyInstaller bootloader {key} drifted")
    if PYINSTALLER_NOTICE_MEMBER not in members:
        raise ValueError("PyInstaller Bootloader Exception notice is absent")
    bootloader_payload = extract_member(PYINSTALLER_NOTICE_MEMBER)
    if hashlib.sha256(bootloader_payload).hexdigest() != PYINSTALLER_NOTICE_SHA256:
        raise ValueError("PyInstaller Bootloader Exception notice hash mismatch")
    if bootloader.get("bytes") != len(bootloader_payload):
        raise ValueError("PyInstaller Bootloader Exception notice size mismatch")
    try:
        bootloader_text = bootloader_payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("PyInstaller Bootloader Exception notice is not UTF-8") from exc
    missing_markers = [
        marker for marker in PYINSTALLER_EXCEPTION_MARKERS if marker not in bootloader_text
    ]
    if missing_markers:
        raise ValueError(
            "PyInstaller Bootloader Exception notice omitted reviewed terms: "
            + "; ".join(missing_markers)
        )
    return tuple(sorted(build_only_import_roots))


def _frozen_python_modules(reader) -> set[str]:
    """Return top-level and PYZ module names from a PyInstaller archive."""

    modules = {
        normalized_inventory(str(member))
        for member in reader.toc
        if not normalized_inventory(str(member)).startswith(f"{NOTICE_BUNDLE_MEMBER}/")
    }
    for member, entry in reader.toc.items():
        # ``z`` is PyInstaller's CArchive typecode for an embedded PYZ archive.
        if entry[-1] != "z":
            continue
        embedded = reader.open_embedded_archive(member)
        modules.update(str(module) for module in embedded.toc)
    return modules


def reject_frozen_build_only_imports(
    *,
    modules: set[str],
    import_roots: tuple[str, ...],
) -> None:
    """Prove build-tool Python packages did not cross the frozen boundary."""

    violations: list[str] = []
    for module in modules:
        normalized_module = normalized_inventory(module)
        for root in import_roots:
            if normalized_module == root or normalized_module.startswith((f"{root}.", f"{root}/")):
                violations.append(normalized_module)
                break
    if violations:
        raise ValueError(
            "build-only Python modules crossed frozen boundary: "
            + "; ".join(sorted(violations)[:20])
        )


def verify_frozen_notice_bundle(artifact: Path) -> None:
    """Read and validate license data from the actual PyInstaller archive."""

    try:
        from PyInstaller.archive.readers import CArchiveReader
    except ImportError as exc:  # pragma: no cover - build environment guard
        raise RuntimeError("PyInstaller is required to inspect frozen notices") from exc

    reader = CArchiveReader(str(artifact))
    # CArchiveReader exposes the exact top-level member keys and avoids parsing
    # archive_viewer's repr-formatted, platform-dependent diagnostic output.
    member_keys = frozen_member_keys(reader.toc)
    if NOTICE_INVENTORY_MEMBER not in member_keys:
        raise ValueError("frozen sidecar omitted the Python notice inventory")

    def extract_member(member: str) -> bytes:
        raw_member = member_keys.get(member)
        if raw_member is None:
            raise ValueError(f"frozen notice member is absent: {member}")
        payload = reader.extract(raw_member)
        if not isinstance(payload, bytes):
            raise ValueError(f"could not extract frozen notice member: {member}")
        return payload

    build_only_import_roots = validate_frozen_notice_inventory(
        extract_member(NOTICE_INVENTORY_MEMBER),
        members=set(member_keys),
        extract_member=extract_member,
    )
    reject_frozen_build_only_imports(
        modules=_frozen_python_modules(reader),
        import_roots=build_only_import_roots,
    )


def artifact_path(kind: str, root: Path = ROOT) -> Path:
    suffix = ".exe" if sys.platform == "win32" else ""
    if kind == "sidecar":
        return root / "dist" / f"openadapt-engine{suffix}"
    return root / "src-tauri" / "target" / "release" / f"openadapt-desktop{suffix}"


def verify_python_distributions(parser: argparse.ArgumentParser, root: Path = ROOT) -> None:
    """Inspect the archives users actually install, not only the source tree."""

    archives = sorted((root / "dist").glob("openadapt_desktop-*.whl"))
    archives += sorted((root / "dist").glob("openadapt_desktop-*.tar.gz"))
    if len(archives) != 2:
        parser.error(f"expected one wheel and one sdist, found: {archives}")

    for archive in archives:
        if archive.suffix == ".whl":
            with zipfile.ZipFile(archive) as package:
                members = package.namelist()
        else:
            with tarfile.open(archive, "r:gz") as package:
                members = package.getnames()
        forbidden = sorted(member for member in members if FORBIDDEN_FROZEN_MEMBERS.search(member))
        if forbidden:
            parser.error(
                f"{archive.name} crossed the AGPL/private-corpus boundary: "
                + "; ".join(forbidden[:20])
            )
        print(f"Verified Python distribution: {archive} ({archive.stat().st_size} bytes)")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("python-distribution", "sidecar", "tauri"))
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repository root containing the artifact (used by guarded recovery publication)",
    )
    args = parser.parse_args()

    if args.kind == "python-distribution":
        verify_python_distributions(parser, args.root)
        return 0

    artifact = artifact_path(args.kind, args.root)
    if not artifact.is_file() or artifact.stat().st_size == 0:
        parser.error(f"missing or empty {args.kind} artifact: {artifact}")

    if args.kind == "sidecar":
        result = subprocess.run(
            [str(artifact), "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0 or "OpenAdapt Desktop" not in output:
            parser.error(
                f"sidecar smoke test failed with exit {result.returncode}: {output[-1000:]}"
            )

        flow = subprocess.run(
            [str(artifact), "__openadapt_flow__", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        flow_output = flow.stdout + flow.stderr
        if flow.returncode != 0 or bundled_flow_banner(args.root) not in flow_output:
            parser.error(
                "bundled Flow runtime smoke test failed with exit "
                f"{flow.returncode}: {flow_output[-1000:]}"
            )

        playwright = subprocess.run(
            [str(artifact), "-m", "playwright", "--version"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if playwright.returncode != 0 or "Version 1.61.0" not in (
            playwright.stdout + playwright.stderr
        ):
            parser.error("bundled Playwright bootstrap is absent or version-drifted")

        inventory = subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller.utils.cliutils.archive_viewer",
                "-r",
                "-l",
                str(artifact),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if inventory.returncode != 0:
            parser.error(f"could not inventory frozen sidecar: {inventory.stderr[-1000:]}")
        inventory_text = normalized_inventory(inventory.stdout)
        forbidden = sorted(
            line.strip()
            for line in inventory_text.splitlines()
            if FORBIDDEN_FROZEN_MEMBERS.search(line)
            or (
                FORBIDDEN_EMBEDDED_VISION_MEMBERS.search(line)
                and not any(
                    notice in line
                    for notice in (
                        "third_party/rapidocr/LICENSE",
                        "third_party/rapidocr/NOTICE",
                    )
                )
            )
        )
        if forbidden:
            parser.error(
                "frozen sidecar crossed the AGPL/private-corpus boundary: "
                + "; ".join(forbidden[:20])
            )
        missing_notices = [
            member for member in REQUIRED_FROZEN_NOTICES if member not in inventory_text
        ]
        if missing_notices:
            parser.error(
                "frozen sidecar omitted required third-party notices: " + "; ".join(missing_notices)
            )
        try:
            verify_frozen_notice_bundle(artifact)
        except (RuntimeError, ValueError) as exc:
            parser.error(str(exc))

    print(f"Verified {args.kind} artifact: {artifact} ({artifact.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
