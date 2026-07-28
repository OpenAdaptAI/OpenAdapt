#!/usr/bin/env python3
"""Validate platform-manifest.json against the real published artifacts.

CI-runnable drift detector for the OpenAdapt platform release manifest
(see docs/platform-manifest.md). It fails loudly when the committed manifest
disagrees with reality instead of letting a stale or invented manifest ship.

What it validates TODAY:

* Structure: manifest kind, schema major version, required fields, and at
  least one artifact with a sha256 digest per component.
* Signature honesty: while the signature value is null the status must say
  "unsigned (signing infrastructure pending)". A non-null signature value
  FAILS validation, because no verification infrastructure exists yet and a
  claimed-but-unverifiable signature is worse than an honest null.
* Repo agreement (offline): the openadapt-* compatibility ranges in the
  manifest match this checkout's pyproject.toml, and the manifest's launcher
  version is not AHEAD of pyproject.toml's. A pyproject version ahead of the
  manifest is the normal in-flight-release state and only warns; the PyPI
  comparison below is what authoritatively detects staleness.
* Published-artifact agreement (network): for every component, the manifest
  version equals the latest version on PyPI, and every artifact filename,
  URL, and sha256 digest matches what PyPI reports. A manifest version that
  is BEHIND PyPI's latest is drift and fails; one that is AHEAD but already
  published is PyPI index propagation lag and warns (its digests are still
  verified); one PyPI never published fails. An unreachable PyPI warns rather
  than fails -- it is not evidence of drift, and a flaky guard gets ignored --
  unless --require-network is passed. Run with --offline to skip only this
  class of check (for example in an airgapped environment).
* Status-document agreement (network): version skew against
  https://openadapt.ai/status.json is reported as a WARNING by default,
  because status.json lives in another repository and this manifest is not its
  source of truth. It stays a warning so a release here cannot be blocked by an
  edit nobody in this repo can make. Pass --strict-status to escalate the skew
  to a failure.

  Do NOT read that warning as self-healing. status.json is HAND-MAINTAINED in
  openadapt-web; nothing generates it from PyPI or from this manifest. On
  2026-07-28 it advertised flow 1.24.0 and launcher 1.10.0 while PyPI had
  1.25.1 and 1.10.1. openadapt-web runs its own daily guard
  (scripts/check_published_version_claims.mjs) that fails on exactly this, but
  that is after-the-fact detection and still needs a person to open the
  corrective PR. Skew reported here means someone must go and edit that file.

What it does NOT validate yet (planned, see docs/platform-manifest.md):

* Cryptographic signature verification (sigstore for the manifest itself,
  Authenticode / Apple Developer ID for OS installers).
* Desktop OS installer artifacts (MSI/DMG); only PyPI artifacts exist today.

Usage:
    python scripts/validate_platform_manifest.py [--manifest PATH]
        [--offline] [--strict-status]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_KIND = "openadapt-platform-release-manifest"
EXPECTED_SCHEMA_MAJOR = 1
EXPECTED_UNSIGNED_STATUS = "unsigned (signing infrastructure pending)"
STATUS_URL = "https://openadapt.ai/status.json"
PYPI_URL_TEMPLATE = "https://pypi.org/pypi/{package}/json"
HTTP_TIMEOUT_SECONDS = 30

REQUIRED_TOP_LEVEL_FIELDS = (
    "manifest_kind",
    "schema_version",
    "generated_at",
    "release_channel",
    "components",
    "compatibility",
    "supported_os",
    "substrate_drivers",
    "qualification_evidence",
    "signature",
)


def _fetch_json(url: str) -> dict:
    request = urllib.request.Request(
        url, headers={"User-Agent": "openadapt-platform-manifest-validator"}
    )
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as resp:
        return json.load(resp)


class Report:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def error(self, message: str) -> None:
        self.errors.append(message)

    def warning(self, message: str) -> None:
        self.warnings.append(message)


def check_structure(manifest: dict, report: Report) -> None:
    for field in REQUIRED_TOP_LEVEL_FIELDS:
        if field not in manifest:
            report.error(f"missing required top-level field: {field}")
    if manifest.get("manifest_kind") != EXPECTED_KIND:
        report.error(
            f"manifest_kind is {manifest.get('manifest_kind')!r}, expected "
            f"{EXPECTED_KIND!r}"
        )
    schema_version = str(manifest.get("schema_version", ""))
    major = schema_version.split(".", 1)[0]
    if not major.isdigit() or int(major) != EXPECTED_SCHEMA_MAJOR:
        report.error(
            f"schema_version {schema_version!r} is not major version "
            f"{EXPECTED_SCHEMA_MAJOR}; this validator cannot interpret it"
        )
    for role, component in manifest.get("components", {}).items():
        artifacts = component.get("artifacts") or []
        if not artifacts:
            report.error(f"component {role} lists no artifacts")
        for artifact in artifacts:
            for key in ("type", "filename", "url", "sha256"):
                if not artifact.get(key):
                    report.error(
                        f"component {role} artifact "
                        f"{artifact.get('filename', '<unnamed>')} missing {key}"
                    )
    if not manifest.get("qualification_evidence"):
        report.error("qualification_evidence is empty")


def check_signature_honesty(manifest: dict, report: Report) -> None:
    signature = manifest.get("signature") or {}
    if signature.get("value") is None:
        if signature.get("status") != EXPECTED_UNSIGNED_STATUS:
            report.error(
                "signature.value is null but signature.status is "
                f"{signature.get('status')!r}; while unsigned it must be "
                f"{EXPECTED_UNSIGNED_STATUS!r}"
            )
    else:
        report.error(
            "signature.value is set, but no signature verification "
            "infrastructure exists yet. Refusing to accept an unverifiable "
            "signature claim. Implement verification before signing "
            "(docs/platform-manifest.md, Signing plan)."
        )


def compare_launcher_to_pyproject(
    manifest_version: str | None, pyproject_version: str | None, report: Report
) -> None:
    """Compare the manifest's launcher version to this checkout's pyproject.

    The manifest records the PUBLISHED launcher, while ``pyproject.toml``
    records the version this checkout would publish next. Those legitimately
    disagree for the ~90 seconds between python-semantic-release pushing its
    version-bump commit and the release workflow's reconcile job regenerating
    the manifest, so:

    * equal -> silent.
    * pyproject AHEAD of the manifest -> WARNING. A release is in flight or
      the bump has not been published yet. Treating this as an error made the
      guard fail on main after every single release, which is precisely how
      a real drift failure became invisible. Staleness against the real world
      is detected by ``compare_component_to_pypi``, which is authoritative.
    * pyproject BEHIND the manifest, or either version unparseable -> ERROR.
      The manifest claims a launcher this repository never produced.
    """
    if manifest_version == pyproject_version:
        return

    manifest_release = _release_tuple(str(manifest_version))
    pyproject_release = _release_tuple(str(pyproject_version))
    if (
        manifest_release is not None
        and pyproject_release is not None
        and pyproject_release > manifest_release
    ):
        report.warning(
            f"launcher: pyproject.toml has {pyproject_version!r} but the "
            f"manifest records the published {manifest_version!r}. A release "
            "is in flight or unpublished; the manifest deliberately records "
            "only published versions. PyPI comparison remains authoritative."
        )
        return

    report.error(
        f"launcher version drift: manifest has {manifest_version!r} "
        f"but pyproject.toml has {pyproject_version!r}. The manifest names a "
        "launcher this repository does not produce. Regenerate with "
        "scripts/generate_platform_manifest.py."
    )


def check_against_pyproject(manifest: dict, report: Report) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    project = pyproject["project"]

    launcher = manifest.get("components", {}).get("launcher", {})
    compare_launcher_to_pyproject(launcher.get("version"), project["version"], report)

    compatibility = manifest.get("compatibility", {})
    if compatibility.get("python") != project["requires-python"]:
        report.error(
            f"python compatibility drift: manifest has "
            f"{compatibility.get('python')!r} but pyproject.toml has "
            f"{project['requires-python']!r}"
        )

    # Recompute the openadapt-* pins exactly as the generator does.
    requirement_strings: list[str] = list(project.get("dependencies", []))
    for extra_requirements in project.get("optional-dependencies", {}).values():
        requirement_strings.extend(extra_requirements)
    pins: dict[str, set[str]] = {}
    for requirement in requirement_strings:
        bare = requirement.split(";")[0].strip()
        for index, char in enumerate(bare):
            if char in "><=!~ [":
                name = bare[:index]
                rest = bare[index:]
                break
        else:
            name, rest = bare, ""
        if not name.startswith("openadapt"):
            continue
        specifier = rest
        if specifier.startswith("["):
            specifier = specifier[specifier.index("]") + 1 :]
        specifier = specifier.strip()
        if specifier:
            pins.setdefault(name, set()).add(specifier)
    expected = {name: sorted(specs) for name, specs in sorted(pins.items())}
    actual = compatibility.get("launcher_requires")
    if actual != expected:
        report.error(
            "compatibility.launcher_requires drift: manifest has "
            f"{actual!r} but pyproject.toml implies {expected!r}"
        )


def _release_tuple(version: str) -> tuple[int, ...] | None:
    """Return the numeric release segment of a version, or None if unparseable.

    Only the release segment is compared. Any pre/post/dev suffix makes the
    version unparseable here, which callers treat conservatively (as drift)
    rather than guessing an ordering.
    """
    parts = version.split(".")
    if not parts or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


def _files_for_version(doc: dict, version: str) -> list[dict] | None:
    """Return PyPI's file records for ``version``, or None if it has none.

    ``info.version`` lags behind an upload on some PyPI CDN edges, so
    ``urls`` (which describes ``info.version``) is not always the right list.
    ``releases`` is keyed by version and is authoritative for a named one.
    """
    releases = doc.get("releases")
    if isinstance(releases, dict):
        files = releases.get(version)
        if files:
            return list(files)
        if version in releases:
            return []
        return None
    # Older/partial payloads without a `releases` map: fall back to `urls`,
    # which only ever describes `info.version`.
    if doc.get("info", {}).get("version") == version:
        return list(doc.get("urls", []))
    return None


def compare_component_to_pypi(
    role: str, component: dict, doc: dict, report: Report
) -> None:
    """Compare one manifest component against PyPI's published state.

    Pure (no I/O) so the drift guard can be tested against simulated future
    releases and tampered digests. Classification:

    * manifest version OLDER than PyPI's latest -> ERROR. This is staleness:
      the manifest advertises a superseded release and its digests, so anyone
      verifying today's artifact against it fails. This is the exact bug this
      guard exists to catch.
    * manifest version NEWER than PyPI's latest but already present in
      ``releases`` -> WARNING. PyPI's ``info.version`` lags an upload by up to
      a few minutes on some CDN edges, and a release-time false red trains
      people to ignore this check. Digests are still verified against the
      named version, so a wrong digest still fails.
    * manifest version absent from ``releases`` entirely -> ERROR. The
      manifest names a release that was never published.
    * any filename/URL/digest mismatch -> ERROR, always.
    """
    package = component.get("package")
    manifest_version = component.get("version")
    latest_version = doc.get("info", {}).get("version")

    files = _files_for_version(doc, manifest_version)
    if not files:
        report.error(
            f"{role} version drift: manifest has {manifest_version!r} but "
            f"PyPI publishes no files for {package}=={manifest_version} "
            f"(PyPI's latest is {latest_version!r}). The manifest names a "
            "release that does not exist. Regenerate the manifest."
        )
        return

    if manifest_version != latest_version:
        manifest_release = _release_tuple(str(manifest_version))
        latest_release = _release_tuple(str(latest_version))
        ahead = (
            manifest_release is not None
            and latest_release is not None
            and manifest_release > latest_release
        )
        if ahead:
            report.warning(
                f"{role}: manifest has {manifest_version!r} but PyPI's "
                f"latest {package} still reads {latest_version!r}. The "
                "manifest version is published, so this is PyPI index "
                "propagation lag, not drift; digests are verified against "
                f"{manifest_version!r}."
            )
        else:
            report.error(
                f"{role} version drift: manifest has {manifest_version!r} "
                f"but PyPI's latest {package} is {latest_version!r}. The "
                "manifest advertises a superseded release and its digests. "
                "Regenerate it with scripts/generate_platform_manifest.py."
            )
            return

    published = {
        entry["filename"]: (entry["url"], entry["digests"]["sha256"]) for entry in files
    }
    for artifact in component.get("artifacts", []):
        filename = artifact.get("filename")
        if filename not in published:
            report.error(
                f"{role} artifact {filename} is not among PyPI's files "
                f"for {package}=={manifest_version}"
            )
            continue
        url, sha256 = published[filename]
        if artifact.get("url") != url:
            report.error(
                f"{role} artifact {filename} URL drift: manifest has "
                f"{artifact.get('url')} but PyPI has {url}"
            )
        if artifact.get("sha256") != sha256:
            report.error(
                f"{role} artifact {filename} sha256 drift: manifest has "
                f"{artifact.get('sha256')} but PyPI has {sha256}. A consumer "
                "verifying the published artifact against this manifest "
                "would fail."
            )


def check_against_pypi(manifest: dict, report: Report, require_network: bool) -> None:
    for role, component in manifest.get("components", {}).items():
        package = component.get("package")
        try:
            doc = _fetch_json(PYPI_URL_TEMPLATE.format(package=package))
        except (urllib.error.URLError, TimeoutError) as exc:
            # An unreachable index is not evidence of drift. Failing here
            # makes the guard flaky, and a flaky guard gets ignored -- which
            # is how the stale manifest survived. Release gates pass
            # --require-network so the comparison cannot be silently skipped
            # where it is load-bearing.
            emit = report.error if require_network else report.warning
            emit(f"could not fetch PyPI metadata for {package}: {exc}")
            continue
        compare_component_to_pypi(role, component, doc, report)


def check_against_status(manifest: dict, report: Report, strict: bool) -> None:
    try:
        status = _fetch_json(STATUS_URL)
    except (urllib.error.URLError, TimeoutError) as exc:
        report.warning(f"could not fetch {STATUS_URL}: {exc}")
        return
    status_versions = status.get("versions", {})
    emit = report.error if strict else report.warning
    for role, component in manifest.get("components", {}).items():
        status_version = status_versions.get(role)
        if status_version is None:
            continue
        if status_version != component.get("version"):
            emit(
                f"{role} skew vs {STATUS_URL}: manifest has "
                f"{component.get('version')!r}, status.json has "
                f"{status_version!r} (status.json is hand-maintained in "
                "openadapt-web and will not correct itself -- edit "
                "public/status.json and data/published-version-claims.json "
                "there)"
            )
    lifecycle = (status.get("product_lifecycle") or "").lower()
    if lifecycle and lifecycle != manifest.get("release_channel"):
        emit(
            f"release_channel skew: manifest has "
            f"{manifest.get('release_channel')!r}, status.json lifecycle is "
            f"{lifecycle!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest", type=Path, default=ROOT / "platform-manifest.json"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip network checks against PyPI and status.json.",
    )
    parser.add_argument(
        "--strict-status",
        action="store_true",
        help="Treat status.json skew as a failure instead of a warning.",
    )
    parser.add_argument(
        "--require-network",
        action="store_true",
        help=(
            "Treat an unreachable PyPI as a failure instead of a warning. Use "
            "at release gates, where skipping the comparison is not "
            "acceptable."
        ),
    )
    args = parser.parse_args()

    if not args.manifest.exists():
        print(f"FATAL: {args.manifest} does not exist", file=sys.stderr)
        return 1
    try:
        manifest = json.loads(args.manifest.read_text())
    except json.JSONDecodeError as exc:
        print(f"FATAL: {args.manifest} is not valid JSON: {exc}", file=sys.stderr)
        return 1

    report = Report()
    check_structure(manifest, report)
    check_signature_honesty(manifest, report)
    check_against_pyproject(manifest, report)
    if not args.offline:
        check_against_pypi(manifest, report, require_network=args.require_network)
        check_against_status(manifest, report, strict=args.strict_status)

    for warning in report.warnings:
        print(f"WARNING: {warning}")
    for error in report.errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if report.errors:
        print(
            f"\nFAILED: {len(report.errors)} error(s), "
            f"{len(report.warnings)} warning(s).",
            file=sys.stderr,
        )
        return 1
    print(
        f"OK: platform manifest validated "
        f"({'offline checks only' if args.offline else 'including published artifacts'}); "
        f"{len(report.warnings)} warning(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
