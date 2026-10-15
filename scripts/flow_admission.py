#!/usr/bin/env python3
"""Verify the founder-signed admission for the exact Flow release a launcher ships.

A launcher release ships one exact openadapt-flow version, selected by
platform-manifest.json with its wheel and sdist SHA-256 digests. Before any tag
or publish, the release workflow runs ``verify``:

1. it downloads both Flow files from files.pythonhosted.org and checks their
   bytes against the manifest digests;
2. it loads ``release-admission/flow-<version>.json`` from the launcher
   candidate itself and requires its payload to equal, field for field, the
   payload built from the manifest (repository, version, tag, source commit,
   file names, digests);
3. it checks the Ed25519 signature over the canonical payload against a public
   key listed in ``release-admission/trusted-keys.json``.

Any missing file, mismatch, unknown key or bad signature fails the release.

The private key never enters CI or the repository. The founder signs on his own
machine with ``sign`` (see docs/release-admission.md). Revocation is a reviewed
pull request that removes a key from trusted-keys.json or an admission file.
Signature checks use the OpenSSL 3 command line so this file stays
standard-library only.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from release_platform_versions import load_release_component_versions  # noqa: E402

ADMISSION_SCHEMA = "openadapt.launcher-flow-admission/v1"
TRUSTED_KEYS_SCHEMA = "openadapt.launcher-admission-keys/v1"
ADMISSION_DIR = Path("release-admission")
TRUSTED_KEYS_FILE = ADMISSION_DIR / "trusted-keys.json"
# DER SubjectPublicKeyInfo prefix for a raw 32-byte Ed25519 public key.
ED25519_SPKI_PREFIX = bytes.fromhex("302a300506032b6570032100")
FLOW_REPOSITORY = "OpenAdaptAI/openadapt-flow"
FLOW_REPOSITORY_ID = "1291376938"
FLOW_CLAIM_SCOPE = "production_flow"
HEX40 = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
STABLE_VERSION = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
MAX_ARTIFACT_BYTES = 128 * 1024 * 1024
ARTIFACT_PROFILES = {
    "sdist": ("python-sdist", "application/gzip", ".tar.gz"),
    "bdist_wheel": ("python-wheel", "application/zip", ".whl"),
}


@dataclass(frozen=True)
class FlowArtifact:
    name: str
    kind: str
    url: str
    sha256: str
    media_type: str


@dataclass(frozen=True)
class FlowCandidate:
    version: str
    tag: str
    source_commit: str
    artifacts: tuple[FlowArtifact, ...]


def _object(value: Any, context: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def _load_manifest(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("platform manifest path is missing or invalid")
    try:
        return _object(
            json.loads(path.read_text(encoding="utf-8")), "platform manifest"
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the platform manifest: {exc}") from exc


def _artifact_url(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Flow artifact URL is invalid: {name}")
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "files.pythonhosted.org"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or Path(urllib.parse.unquote(parsed.path)).name != name
    ):
        raise ValueError(
            f"Flow artifact URL is outside the exact PyPI file boundary: {name}"
        )
    return value


def load_flow_candidate(manifest_path: Path, launcher_version: str) -> FlowCandidate:
    """Return the exact published Flow candidate selected by the manifest."""

    document = _load_manifest(manifest_path)
    versions = load_release_component_versions(manifest_path, launcher_version)
    flow = _object(
        _object(document.get("components"), "platform components").get("flow"),
        "Flow component",
    )
    version = versions["flow"]
    if STABLE_VERSION.fullmatch(version) is None or flow.get("version") != version:
        raise ValueError("Flow version is not one exact stable selected version")
    if flow.get("package") != "openadapt-flow" or flow.get("source") != "pypi":
        raise ValueError("Flow component is not the canonical published package")

    provenance = _object(flow.get("provenance"), "Flow provenance")
    tag = f"v{version}"
    source_commit = provenance.get("commit")
    if (
        provenance.get("repository") != FLOW_REPOSITORY
        or provenance.get("release_ref") != tag
        or not isinstance(source_commit, str)
        or HEX40.fullmatch(source_commit) is None
    ):
        raise ValueError(
            "Flow provenance does not bind the exact repository, tag, and commit"
        )

    artifact_values = flow.get("artifacts")
    if not isinstance(artifact_values, list) or len(artifact_values) != 2:
        raise ValueError("Flow must have exactly one wheel and one source distribution")
    artifacts: list[FlowArtifact] = []
    observed_types: set[str] = set()
    for index, raw in enumerate(artifact_values):
        artifact = _object(raw, f"Flow artifact {index}")
        if set(artifact) != {"type", "filename", "url", "sha256"}:
            raise ValueError(
                f"Flow artifact {index} fields differ from the published manifest contract"
            )
        artifact_type = artifact.get("type")
        if artifact_type not in ARTIFACT_PROFILES or artifact_type in observed_types:
            raise ValueError(
                "Flow artifact types must be one wheel and one source distribution"
            )
        observed_types.add(artifact_type)
        kind, media_type, suffix = ARTIFACT_PROFILES[artifact_type]
        name = artifact.get("filename")
        digest = artifact.get("sha256")
        expected_name = (
            f"openadapt_flow-{version}.tar.gz"
            if artifact_type == "sdist"
            else f"openadapt_flow-{version}-py3-none-any.whl"
        )
        if (
            not isinstance(name, str)
            or name != Path(name).name
            or name != expected_name
            or not name.endswith(suffix)
        ):
            raise ValueError(f"Flow artifact filename is invalid: {name}")
        if not isinstance(digest, str) or SHA256.fullmatch(digest) is None:
            raise ValueError(f"Flow artifact digest is invalid: {name}")
        artifacts.append(
            FlowArtifact(
                name=name,
                kind=kind,
                url=_artifact_url(artifact.get("url"), name),
                sha256=digest,
                media_type=media_type,
            )
        )
    if observed_types != set(ARTIFACT_PROFILES):
        raise ValueError(
            "Flow artifact types must be one wheel and one source distribution"
        )
    artifacts.sort(key=lambda item: (item.kind, item.name, item.sha256))
    return FlowCandidate(
        version=version,
        tag=tag,
        source_commit=source_commit,
        artifacts=tuple(artifacts),
    )


def _download(url: str, token: str | None = None) -> bytes:
    headers = {"User-Agent": "OpenAdapt-launcher-release/1"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        final = urllib.parse.urlsplit(response.geturl())
        requested = urllib.parse.urlsplit(url)
        if final.scheme != "https" or final.hostname != requested.hostname:
            raise ValueError("download redirected outside its exact host")
        raw = response.read(MAX_ARTIFACT_BYTES + 1)
    if not raw or len(raw) > MAX_ARTIFACT_BYTES:
        raise ValueError("download is empty or exceeds the artifact size limit")
    return raw


def stage_artifacts(
    candidate: FlowCandidate,
    artifact_root: Path,
    *,
    fetch_bytes: Callable[[str], bytes] = _download,
) -> dict[str, Any]:
    """Write exact candidate bytes and return the central artifact inventory."""

    if artifact_root.is_symlink():
        raise ValueError("artifact root cannot be a symlink")
    artifact_root.mkdir(parents=True, exist_ok=True)
    if not artifact_root.is_dir() or any(artifact_root.iterdir()):
        raise ValueError("artifact root must be an empty directory")

    inventory_artifacts: list[dict[str, Any]] = []
    for artifact in candidate.artifacts:
        raw = fetch_bytes(artifact.url)
        actual = hashlib.sha256(raw).hexdigest()
        if actual != artifact.sha256:
            raise ValueError(
                f"Flow artifact bytes differ from the manifest: {artifact.name}"
            )
        target = artifact_root / artifact.name
        with target.open("xb") as handle:
            handle.write(raw)
        inventory_artifacts.append(
            {
                "name": artifact.name,
                "kind": artifact.kind,
                "sha256": f"sha256:{actual}",
                "size_bytes": len(raw),
                "media_type": artifact.media_type,
                "publish_destinations": ["github-release", "pypi"],
            }
        )
    return {
        "schema_version": "openadapt.production-release-artifact-inventory/v1",
        "target": "flow",
        "claim_scope": FLOW_CLAIM_SCOPE,
        "artifacts": inventory_artifacts,
    }


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def admission_payload(candidate: FlowCandidate) -> dict[str, Any]:
    """Return the exact statement the founder signs for one Flow release."""

    return {
        "target": "flow",
        "package": "openadapt-flow",
        "repository": FLOW_REPOSITORY,
        "repository_id": FLOW_REPOSITORY_ID,
        "version": candidate.version,
        "tag": candidate.tag,
        "source_commit": candidate.source_commit,
        "artifacts": [
            {"filename": item.name, "sha256": item.sha256}
            for item in sorted(candidate.artifacts, key=lambda a: a.name)
        ],
    }


def admission_path(root: Path, version: str) -> Path:
    return root / ADMISSION_DIR / f"flow-{version}.json"


def key_id(public_key: bytes) -> str:
    return "ed25519:" + hashlib.sha256(public_key).hexdigest()[:32]


def _openssl() -> str:
    binary = os.environ.get("OPENSSL") or shutil.which("openssl")
    if not binary:
        raise ValueError("OpenSSL 3 is required and was not found")
    version = subprocess.run(
        [binary, "version"], capture_output=True, text=True, check=False
    ).stdout
    if not version.startswith("OpenSSL 3"):
        raise ValueError(f"OpenSSL 3 is required, found: {version.strip()!r}")
    return binary


def _decode_public_key(value: Any) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("trusted public key is not base64") from exc
    if len(raw) != 32:
        raise ValueError("trusted public key is not a raw 32-byte Ed25519 key")
    return raw


def load_trusted_keys(root: Path) -> dict[str, bytes]:
    path = root / TRUSTED_KEYS_FILE
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{TRUSTED_KEYS_FILE} is missing")
    document = _object(json.loads(path.read_text(encoding="utf-8")), "trusted keys")
    if document.get("schema_version") != TRUSTED_KEYS_SCHEMA:
        raise ValueError("trusted keys schema differs")
    keys: dict[str, bytes] = {}
    for raw in document.get("keys") or []:
        entry = _object(raw, "trusted key")
        public = _decode_public_key(entry.get("public_key"))
        if entry.get("key_id") != key_id(public):
            raise ValueError("trusted key id does not match its public key")
        keys[key_id(public)] = public
    if not keys:
        raise ValueError(
            "no trusted admission key is committed; the founder must add one"
        )
    return keys


def verify_signature(public_key: bytes, message: bytes, signature: bytes) -> bool:
    openssl = _openssl()
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        pem = work / "key.pem"
        pem.write_text(
            "-----BEGIN PUBLIC KEY-----\n"
            + base64.b64encode(ED25519_SPKI_PREFIX + public_key).decode()
            + "\n-----END PUBLIC KEY-----\n"
        )
        (work / "msg").write_bytes(message)
        (work / "sig").write_bytes(signature)
        result = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-verify",
                "-pubin",
                "-inkey",
                str(pem),
                "-rawin",
                "-in",
                str(work / "msg"),
                "-sigfile",
                str(work / "sig"),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    return result.returncode == 0 and "Signature Verified Successfully" in result.stdout


def verify_admission(root: Path, candidate: FlowCandidate) -> str:
    """Fail unless a trusted key signed exactly this Flow candidate. Returns the key id."""

    path = admission_path(root, candidate.version)
    if path.is_symlink() or not path.is_file():
        raise ValueError(
            f"no signed admission for Flow {candidate.version}: {path.relative_to(root)}"
        )
    try:
        document = _object(json.loads(path.read_text(encoding="utf-8")), "admission")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read the admission: {exc}") from exc
    if set(document) != {"schema_version", "payload", "signature"}:
        raise ValueError("admission fields differ")
    if document["schema_version"] != ADMISSION_SCHEMA:
        raise ValueError("admission schema differs")
    if document["payload"] != admission_payload(candidate):
        raise ValueError(
            "admission payload does not match the exact Flow release in the manifest"
        )
    signature = _object(document["signature"], "admission signature")
    if (
        set(signature) != {"algorithm", "key_id", "value"}
        or signature["algorithm"] != "ed25519"
    ):
        raise ValueError("admission signature fields differ")
    trusted = load_trusted_keys(root)
    public = trusted.get(signature["key_id"])
    if public is None:
        raise ValueError("admission is signed by a key that is not trusted")
    try:
        raw_signature = base64.b64decode(signature["value"], validate=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("admission signature is not base64") from exc
    if len(raw_signature) != 64 or not verify_signature(
        public, canonical_json(document["payload"]), raw_signature
    ):
        raise ValueError("admission signature does not verify")
    return signature["key_id"]


def _private_key_public_bytes(openssl: str, key: Path) -> bytes:
    der = subprocess.run(
        [openssl, "pkey", "-in", str(key), "-pubout", "-outform", "DER"],
        capture_output=True,
        check=True,
    ).stdout
    if len(der) != 44 or not der.startswith(ED25519_SPKI_PREFIX):
        raise ValueError(f"{key} is not an Ed25519 private key")
    return der[len(ED25519_SPKI_PREFIX) :]


def _pypi_digests(version: str) -> dict[str, str]:
    raw = _download(f"https://pypi.org/pypi/openadapt-flow/{version}/json")
    files = _object(json.loads(raw), "PyPI response").get("urls") or []
    return {item["filename"]: item["digests"]["sha256"] for item in files}


def sign_admission(
    root: Path,
    candidate: FlowCandidate,
    key: Path,
    *,
    pypi_digests: Callable[[str], dict[str, str]] = _pypi_digests,
) -> Path:
    """Sign the exact Flow candidate with a local Ed25519 key (founder's machine).

    Creates the key (mode 0600) if it does not exist and adds its public half to
    trusted-keys.json. Refuses to sign if PyPI's digests differ from the manifest.
    """

    openssl = _openssl()
    published = pypi_digests(candidate.version)
    for item in candidate.artifacts:
        if published.get(item.name) != item.sha256:
            raise ValueError(f"PyPI digest differs from the manifest: {item.name}")
    key = key.expanduser()
    if not key.exists():
        old = os.umask(0o077)
        try:
            subprocess.run(
                [openssl, "genpkey", "-algorithm", "ed25519", "-out", str(key)],
                check=True,
            )
        finally:
            os.umask(old)
        print(f"Created a new Ed25519 admission key at {key} (keep it offline).")
    public = _private_key_public_bytes(openssl, key)

    keys_path = root / TRUSTED_KEYS_FILE
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    document = (
        json.loads(keys_path.read_text(encoding="utf-8"))
        if keys_path.is_file()
        else {"schema_version": TRUSTED_KEYS_SCHEMA, "keys": []}
    )
    if key_id(public) not in {entry.get("key_id") for entry in document["keys"]}:
        document["keys"].append(
            {
                "key_id": key_id(public),
                "public_key": base64.b64encode(public).decode(),
                "owner": "Richard Abrich (founder, offline key)",
            }
        )
        keys_path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        print(f"Added public key {key_id(public)} to {TRUSTED_KEYS_FILE}.")

    payload = admission_payload(candidate)
    with tempfile.TemporaryDirectory() as tmp:
        message = Path(tmp) / "msg"
        message.write_bytes(canonical_json(payload))
        signature = subprocess.run(
            [
                openssl,
                "pkeyutl",
                "-sign",
                "-inkey",
                str(key),
                "-rawin",
                "-in",
                str(message),
            ],
            capture_output=True,
            check=True,
        ).stdout
    target = admission_path(root, candidate.version)
    target.write_text(
        json.dumps(
            {
                "schema_version": ADMISSION_SCHEMA,
                "payload": payload,
                "signature": {
                    "algorithm": "ed25519",
                    "key_id": key_id(public),
                    "value": base64.b64encode(signature).decode(),
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    verify_admission(root, candidate)
    return target


def _project_version(root: Path) -> str:
    import tomllib

    with (root / "pyproject.toml").open("rb") as stream:
        return tomllib.load(stream)["project"]["version"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="CI: verify bytes and signed admission")
    verify.add_argument("--artifact-root", type=Path, required=True)
    sign = sub.add_parser("sign", help="founder: sign the manifest's Flow release")
    sign.add_argument("--key", type=Path, required=True)
    for command in (verify, sign):
        command.add_argument("--root", type=Path, default=Path("."))
        command.add_argument("--manifest", type=Path, default=None)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    manifest = args.manifest or root / "platform-manifest.json"
    try:
        candidate = load_flow_candidate(manifest, _project_version(root))
        if args.command == "verify":
            stage_artifacts(candidate, args.artifact_root)
            signer = verify_admission(root, candidate)
            print(
                f"OK: Flow {candidate.tag} bytes match the manifest and its "
                f"admission is signed by trusted key {signer}."
            )
        else:
            target = sign_admission(root, candidate, args.key)
            print(f"OK: signed {target.relative_to(root)} for Flow {candidate.tag}.")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
