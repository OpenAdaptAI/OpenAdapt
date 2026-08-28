"""Verify that ``dist/`` contains exactly the release wheel and source archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import tarfile
import zipfile
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY_SCHEMA_VERSION = 1
MAX_MEMBER_BYTES = 16 * 1024 * 1024


def _quoted_table_value(text: str, table: str, key: str) -> str:
    current_table = ""
    assignment = re.compile(rf'^{re.escape(key)}\s*=\s*"([^"]+)"\s*$')
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            current_table = line
            continue
        if current_table != f"[{table}]":
            continue
        match = assignment.match(line)
        if match:
            return match.group(1)
    raise ValueError(f"missing quoted {key!r} in [{table}]")


def _project_identity(root: Path) -> tuple[str, str, str]:
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return (
        _quoted_table_value(project_text, "project", "name"),
        _quoted_table_value(project_text, "project", "version"),
        _quoted_table_value(project_text, "project", "requires-python"),
    )


def _distribution_metadata(raw: bytes, source: str) -> Message:
    metadata = BytesParser(policy=policy.default).parsebytes(raw)
    for field in ("Name", "Version", "Summary", "Requires-Python"):
        if not metadata.get(field):
            raise ValueError(f"{source} metadata is missing {field}")
    return metadata


def _wheel_metadata(path: Path) -> Message:
    with zipfile.ZipFile(path) as archive:
        members = [
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        ]
        if len(members) != 1:
            raise ValueError(f"{path.name} must contain exactly one METADATA file")
        return _distribution_metadata(archive.read(members[0]), path.name)


def _sdist_metadata(path: Path) -> Message:
    with tarfile.open(path, mode="r:gz") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.name.endswith("/PKG-INFO")
        ]
        if len(members) != 1 or not members[0].isfile():
            raise ValueError(f"{path.name} must contain exactly one PKG-INFO file")
        stream = archive.extractfile(members[0])
        if stream is None:
            raise ValueError(f"cannot read PKG-INFO from {path.name}")
        return _distribution_metadata(stream.read(), path.name)


def _lifecycle(metadata: Message) -> list[str]:
    return [
        value
        for value in metadata.get_all("Classifier", [])
        if value.startswith("Development Status :: ")
    ]


def _specifier_terms(value: str) -> set[str]:
    return {term.strip() for term in value.split(",") if term.strip()}


def _release_policy(root: Path) -> tuple[tuple[str, ...], tuple[bytes, ...]]:
    policy_path = root / "source-policy.public.json"
    try:
        document = json.loads(policy_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the rendered source policy: {exc}") from exc
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != POLICY_SCHEMA_VERSION
    ):
        raise ValueError("rendered source policy schema is invalid")
    enforcement = document.get("enforcement")
    built = (
        enforcement.get("built_artifacts") if isinstance(enforcement, dict) else None
    )
    prefixes = built.get("path_prefixes") if isinstance(built, dict) else None
    if (
        not isinstance(prefixes, list)
        or not prefixes
        or not all(isinstance(item, str) and item for item in prefixes)
    ):
        raise ValueError("built_artifacts.path_prefixes policy is invalid")
    signature_parts = (
        enforcement.get("content_signature_parts")
        if isinstance(enforcement, dict)
        else None
    )
    if not isinstance(signature_parts, list) or not signature_parts:
        raise ValueError("content_signature_parts policy is invalid")
    signatures: list[bytes] = []
    for parts in signature_parts:
        if (
            not isinstance(parts, list)
            or not parts
            or not all(isinstance(part, str) for part in parts)
        ):
            raise ValueError("content_signature_parts policy is invalid")
        signature = "".join(parts).encode()
        if not signature:
            raise ValueError("content signature is empty")
        signatures.append(signature)
    return tuple(item.strip("/").lower() for item in prefixes), tuple(signatures)


def _safe_member_path(name: str, *, root_prefix: str | None = None) -> str:
    if not name or "\0" in name or name.startswith("/") or "\\" in name:
        raise ValueError(f"release archive member path is invalid: {name!r}")
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"release archive member path is invalid: {name!r}")
    if root_prefix is not None:
        if not parts or parts[0] != root_prefix:
            raise ValueError(f"sdist member escapes its exact root: {name!r}")
        parts = parts[1:]
    return "/".join(parts)


def _check_member(
    relative_path: str,
    body: bytes,
    *,
    denied_prefixes: tuple[str, ...],
    signatures: tuple[bytes, ...],
) -> None:
    lower = relative_path.lower()
    if any(
        lower == prefix or lower.startswith(prefix + "/") for prefix in denied_prefixes
    ):
        raise ValueError(
            f"release archive contains denied built artifact path: {relative_path}"
        )
    if any(signature in body for signature in signatures):
        raise ValueError(
            f"release archive contains a private content signature: {relative_path}"
        )


def _verify_archive_boundary(wheel: Path, sdist: Path, root: Path) -> None:
    denied_prefixes, signatures = _release_policy(root)
    license_files = [root / "LICENSE"]
    license_files.extend(
        sorted(
            path
            for path in root.iterdir()
            if path.is_file()
            and (path.name.startswith("NOTICE") or "THIRD_PARTY_NOTICES" in path.name)
        )
    )
    if not license_files[0].is_file() or license_files[0].is_symlink():
        raise ValueError("release source LICENSE is missing or invalid")
    required_notices = {path.name: path.read_bytes() for path in license_files}

    wheel_notices: dict[str, bytes] = {}
    with zipfile.ZipFile(wheel) as archive:
        names: set[str] = set()
        for member in archive.infolist():
            relative = _safe_member_path(member.filename)
            if relative in names:
                raise ValueError(f"wheel member is duplicated: {relative}")
            names.add(relative)
            mode = member.external_attr >> 16
            if member.is_dir():
                continue
            if stat.S_ISLNK(mode) or member.file_size > MAX_MEMBER_BYTES:
                raise ValueError(f"wheel member is invalid: {relative}")
            body = archive.read(member)
            _check_member(
                relative,
                body,
                denied_prefixes=denied_prefixes,
                signatures=signatures,
            )
            marker = ".dist-info/licenses/"
            if marker in relative:
                wheel_notices[relative.rsplit("/", 1)[-1]] = body

    sdist_notices: dict[str, bytes] = {}
    expected_root = sdist.name.removesuffix(".tar.gz")
    with tarfile.open(sdist, mode="r:gz") as archive:
        names: set[str] = set()
        for member in archive.getmembers():
            relative = _safe_member_path(member.name, root_prefix=expected_root)
            if relative in names:
                raise ValueError(f"sdist member is duplicated: {relative}")
            names.add(relative)
            if member.isdir():
                continue
            if not member.isfile() or member.size > MAX_MEMBER_BYTES:
                raise ValueError(f"sdist member is invalid: {relative}")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"cannot read sdist member: {relative}")
            body = stream.read()
            _check_member(
                relative,
                body,
                denied_prefixes=denied_prefixes,
                signatures=signatures,
            )
            if "/" not in relative and relative in required_notices:
                sdist_notices[relative] = body

    for name, expected_body in required_notices.items():
        if wheel_notices.get(name) != expected_body:
            raise ValueError(f"wheel does not contain the exact required {name}")
        if sdist_notices.get(name) != expected_body:
            raise ValueError(f"sdist does not contain the exact required {name}")


def verify_release_artifacts(
    dist_dir: Path,
    root: Path = ROOT,
) -> tuple[Path, Path]:
    """Return the verified ``(wheel, sdist)`` paths or raise ``ValueError``."""
    if dist_dir.is_symlink() or not dist_dir.is_dir():
        raise ValueError(
            f"release artifact directory is missing or invalid: {dist_dir}"
        )
    package_name, project_version, project_requires_python = _project_identity(root)
    wheel_name = re.sub(r"[-_.]+", "_", package_name)
    sdist_name = re.sub(r"[-_.]+", "-", package_name)

    wheels = sorted(dist_dir.glob(f"{wheel_name}-{project_version}-*.whl"))
    sdist = dist_dir / f"{sdist_name}-{project_version}.tar.gz"
    if (
        len(wheels) != 1
        or wheels[0].is_symlink()
        or not sdist.is_file()
        or sdist.is_symlink()
    ):
        raise ValueError(
            f"expected one {wheel_name}-{project_version}-*.whl and {sdist.name}"
        )

    expected = {wheels[0], sdist}
    marker = dist_dir / ".gitignore"
    allowed = set(expected)
    if marker.is_symlink():
        raise ValueError("dist/.gitignore must not be a symlink")
    if marker.is_file():
        if marker.read_bytes() not in {b"", b"\n", b"*"}:
            raise ValueError("dist/.gitignore contains unexpected data")
        allowed.add(marker)
    entries = set(dist_dir.iterdir())
    invalid = sorted(
        path.name for path in entries if path.is_symlink() or not path.is_file()
    )
    if invalid:
        raise ValueError(
            f"release artifact directory contains invalid entries: {', '.join(invalid)}"
        )
    actual = entries
    if actual != allowed:
        unexpected = ", ".join(sorted(path.name for path in actual - allowed))
        missing = ", ".join(sorted(path.name for path in allowed - actual))
        raise ValueError(
            f"release artifact set mismatch; unexpected=[{unexpected}], missing=[{missing}]"
        )

    wheel_metadata = _wheel_metadata(wheels[0])
    sdist_metadata = _sdist_metadata(sdist)
    for source, metadata in (
        (wheels[0].name, wheel_metadata),
        (sdist.name, sdist_metadata),
    ):
        if metadata["Name"].lower() != package_name.lower():
            raise ValueError(f"{source} package name does not match {package_name}")
        if metadata["Version"] != project_version:
            raise ValueError(f"{source} version does not match {project_version}")
        if _specifier_terms(metadata["Requires-Python"]) != _specifier_terms(
            project_requires_python
        ):
            raise ValueError(
                f"{source} Requires-Python does not match {project_requires_python}"
            )
        if _lifecycle(metadata):
            raise ValueError(
                f"{source} publishes a static lifecycle classifier; "
                "use the signed public Production record"
            )

    comparable_fields = ("Name", "Version", "Summary", "Requires-Python")
    if any(
        wheel_metadata[field] != sdist_metadata[field] for field in comparable_fields
    ):
        raise ValueError("wheel and source distribution metadata disagree")

    _verify_archive_boundary(wheels[0], sdist, root)

    return wheels[0], sdist


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dist_dir", nargs="?", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    try:
        wheel, sdist = verify_release_artifacts(args.dist_dir)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
        parser.exit(1, f"{exc}\n")

    for artifact in (wheel, sdist):
        print(f"{_sha256(artifact)}  {artifact.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
