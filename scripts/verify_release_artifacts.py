"""Verify that ``dist/`` contains exactly the release wheel and source archive."""

from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
import zipfile
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BETA_CLASSIFIER = "Development Status :: 4 - Beta"


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


def _project_identity(root: Path) -> tuple[str, str]:
    project_text = (root / "pyproject.toml").read_text(encoding="utf-8")
    return (
        _quoted_table_value(project_text, "project", "name"),
        _quoted_table_value(project_text, "project", "version"),
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


def verify_release_artifacts(
    dist_dir: Path,
    root: Path = ROOT,
) -> tuple[Path, Path]:
    """Return the verified ``(wheel, sdist)`` paths or raise ``ValueError``."""
    package_name, project_version = _project_identity(root)
    wheel_name = re.sub(r"[-_.]+", "_", package_name)
    sdist_name = re.sub(r"[-_.]+", "-", package_name)

    wheels = sorted(dist_dir.glob(f"{wheel_name}-{project_version}-*.whl"))
    sdist = dist_dir / f"{sdist_name}-{project_version}.tar.gz"
    if len(wheels) != 1 or not sdist.is_file():
        raise ValueError(
            f"expected one {wheel_name}-{project_version}-*.whl and {sdist.name}"
        )

    expected = {wheels[0], sdist}
    marker = dist_dir / ".gitignore"
    allowed = set(expected)
    if marker.is_file():
        if marker.read_bytes() not in {b"", b"\n", b"*"}:
            raise ValueError("dist/.gitignore contains unexpected data")
        allowed.add(marker)
    actual = {path for path in dist_dir.iterdir() if path.is_file()}
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
        if _lifecycle(metadata) != [BETA_CLASSIFIER]:
            raise ValueError(f"{source} does not declare the Beta launcher lifecycle")

    comparable_fields = ("Name", "Version", "Summary", "Requires-Python")
    if any(
        wheel_metadata[field] != sdist_metadata[field] for field in comparable_fields
    ):
        raise ValueError("wheel and source distribution metadata disagree")

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
