"""Check built facade code, metadata, entry point and license against source."""

from __future__ import annotations

import argparse
import ast
import configparser
import hashlib
import json
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path

try:
    import tomllib
except ImportError:
    import tomli as tomllib

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet


def check(dist: Path, root: Path) -> dict:
    config = tomllib.loads((root / "pyproject.toml").read_text())
    version = config["project"]["version"]
    module = ast.parse((root / "src/openadapt_verify/__init__.py").read_text())
    declared = [
        ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__version__" for t in node.targets)
    ]
    assert declared == [version], "module version must match package metadata"
    lock = tomllib.loads((root / "uv.lock").read_text())
    assert [
        p["version"] for p in lock["package"] if p["name"] == config["project"]["name"]
    ] == [version], "lock version must match package metadata"
    expected = {
        p.relative_to(root / "src").as_posix(): p.read_bytes()
        for p in sorted((root / "src").rglob("*.py"))
    }
    assert "openadapt_verify/__init__.py" in expected
    assert "openadapt_verify/cli.py" in expected
    wheels = list(dist.glob("*.whl"))
    sdists = list(dist.glob("*.tar.gz"))
    assert len(wheels) == len(sdists) == 1, "exactly one wheel and sdist required"
    result = []
    with zipfile.ZipFile(wheels[0]) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "duplicate wheel member"
        code = {name for name in names if ".dist-info/" not in name}
        assert code == set(expected), (
            "unexpected or missing wheel code",
            code ^ set(expected),
        )
        for name, data in expected.items():
            assert archive.read(name) == data, ("wheel code differs", name)
        metadata_name = next(
            name for name in names if name.endswith(".dist-info/METADATA")
        )
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        assert metadata["Name"] == config["project"]["name"]
        assert metadata["Version"] == config["project"]["version"]
        assert SpecifierSet(metadata["Requires-Python"]) == SpecifierSet(
            config["project"]["requires-python"]
        )
        assert metadata["License-Expression"] == "MIT"
        required = [
            Requirement(value) for value in metadata.get_all("Requires-Dist", [])
        ]
        base = {
            requirement.name: requirement
            for requirement in required
            if requirement.marker is None
        }
        assert base["openadapt-flow"].extras == {"receipts"}
        assert base["openadapt-flow"].specifier == SpecifierSet(">=3.0.0,<4")
        assert base["cryptography"].specifier == SpecifierSet(">=50.0.0")
        # Flow 3's base install has no vision stack; nothing here adds one.
        assert set(base) == {"openadapt-flow", "cryptography"}, set(base)
        extras = {
            requirement.name: requirement
            for requirement in required
            if requirement.marker is not None
        }
        assert "vision" not in {e for r in required for e in r.extras}
        assert {"pymysql", "openadapt-flow"} <= {n.lower() for n in extras}
        entries = configparser.ConfigParser()
        entry_name = next(
            name for name in names if name.endswith(".dist-info/entry_points.txt")
        )
        entries.read_string(archive.read(entry_name).decode())
        assert (
            entries["console_scripts"]["openadapt-verify"]
            == "openadapt_verify.cli:main"
        )
        license_name = next(
            name for name in names if name.endswith(".dist-info/licenses/LICENSE")
        )
        assert archive.read(license_name) == (root / "LICENSE").read_bytes()
        result.append({"file": wheels[0].name, "code_files": len(code)})

    expected_source = {
        p.relative_to(root).as_posix(): p.read_bytes()
        for folder in ("src", "tests", "scripts")
        for p in sorted((root / folder).rglob("*.py"))
    }
    for name in ("README.md", "LICENSE", "pyproject.toml", "uv.lock", ".gitignore"):
        expected_source[name] = (root / name).read_bytes()
    with tarfile.open(sdists[0], "r:gz") as archive:
        files = [member for member in archive if member.isfile()]
        top = {Path(member.name).parts[0] for member in files}
        assert len(top) == 1
        found = {
            Path(member.name).relative_to(next(iter(top))).as_posix(): member
            for member in files
        }
        assert len(found) == len(files), "duplicate sdist member"
        assert set(found) == set(expected_source) | {"PKG-INFO"}, (
            "unexpected or missing sdist source",
            set(found) ^ (set(expected_source) | {"PKG-INFO"}),
        )
        for name, data in expected_source.items():
            assert archive.extractfile(found[name]).read() == data, (
                "sdist differs",
                name,
            )
        result.append({"file": sdists[0].name, "source_files": len(expected_source)})
    for item in result:
        data = (dist / item["file"]).read_bytes()
        item.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    return {"result": "PASS", "artifacts": result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.dist, Path(__file__).resolve().parents[1]), indent=2))


if __name__ == "__main__":
    main()
