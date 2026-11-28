"""One-command redacted, deterministic, hash-pinned support bundle export.

``openadapt support-bundle <run_dir>`` scrubs PII/PHI using ``openadapt-privacy``
when installed (falling back to a built-in pattern and key scrubber), excludes
raw private sidecars, secret material, and unredacted screenshots/media, and
writes a deterministic ``.approved.zip`` archive with a SHA-256 ``manifest.json``.
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

SUPPORT_BUNDLE_SCHEMA = "openadapt.support-bundle/v1"
MANIFEST_FILENAME = "manifest.json"
_DETERMINISTIC_ZIP_DATE = (1980, 1, 1, 0, 0, 0)

_EXCLUDED_EXACT_NAMES = frozenset(
    {
        "receipt.private.json",
        "seal.private.json",
        "keys.json",
        ".env",
        "id_rsa",
        "id_ed25519",
    }
)
_EXCLUDED_SUFFIXES = frozenset(
    {
        ".key",
        ".pem",
        ".p12",
        ".pfx",
        ".dpapi",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".mp4",
        ".mov",
        ".mkv",
        ".avi",
        ".wav",
        ".mp3",
        ".pyc",
        ".sqlite",
        ".db",
    }
)

_SENSITIVE_JSON_KEYS = frozenset(
    {
        "patient",
        "patient_name",
        "full_name",
        "first_name",
        "last_name",
        "middle_name",
        "dob",
        "date_of_birth",
        "birth_date",
        "ssn",
        "social_security_number",
        "mrn",
        "medical_record_number",
        "member_id",
        "subscriber_id",
        "email",
        "email_address",
        "phone",
        "phone_number",
        "mobile",
        "address",
        "street_address",
        "home_address",
        "password",
        "passwd",
        "secret",
        "api_key",
        "access_token",
        "refresh_token",
        "authorization",
        "cookie",
        "private_key",
    }
)

_TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED:SSN]"),
    (
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
        "[REDACTED:EMAIL]",
    ),
    (
        re.compile(r"(?<!\d)(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]\d{3}[-.\s]\d{4}(?!\d)"),
        "[REDACTED:PHONE]",
    ),
    (
        re.compile(r"\b(?:MRN|mrn)[:#\s-]*[A-Za-z0-9-]{4,20}\b"),
        "[REDACTED:MRN]",
    ),
    (
        re.compile(r"\b(?:DOB|dob)[:\s-]*\d{2,4}[-/]\d{1,2}[-/]\d{1,4}\b"),
        "[REDACTED:DOB]",
    ),
    (
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{8,}"),
        "Bearer [REDACTED:TOKEN]",
    ),
    (
        re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
        "[REDACTED:API_KEY]",
    ),
)

TextScrubber = Callable[[str], str]


class SupportBundleError(ValueError):
    """Raised when support bundle creation or verification fails."""


@dataclass(frozen=True)
class SupportBundleResult:
    """Result of building a redacted ``.approved.zip`` support bundle."""

    output_path: str
    bundle_sha256: str
    redaction_engine: str
    included_files: tuple[str, ...]
    excluded_count: int
    manifest: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": SUPPORT_BUNDLE_SCHEMA,
            "output_path": self.output_path,
            "bundle_sha256": self.bundle_sha256,
            "redaction_engine": self.redaction_engine,
            "included_count": len(self.included_files),
            "excluded_count": self.excluded_count,
            "included_files": list(self.included_files),
        }


@dataclass(frozen=True)
class BundleVerifyResult:
    """Result of verifying a ``.approved.zip`` support bundle."""

    ok: bool
    reason: str
    verified_files: int
    bundle_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "verified_files": self.verified_files,
            "bundle_sha256": self.bundle_sha256,
        }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_excluded_path(rel_path: Path) -> bool:
    """Return True if ``rel_path`` must never be included in a support bundle."""
    parts = [p.lower() for p in rel_path.parts]
    if any(p in ("__pycache__", ".git", ".venv") for p in parts):
        return True
    name = rel_path.name.lower()
    if name in _EXCLUDED_EXACT_NAMES:
        return True
    if name.endswith(".private.json") or name.startswith(".env."):
        return True
    if name.startswith("secrets.") or name.startswith("credentials."):
        return True
    if rel_path.suffix.lower() in _EXCLUDED_SUFFIXES:
        return True
    return False


def scrub_text_builtin(text: str) -> str:
    """Scrub common PII/PHI and secret patterns from text."""
    out = text
    for pattern, replacement in _TEXT_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def _try_openadapt_privacy_scrubber() -> Optional[TextScrubber]:
    """Return an ``openadapt-privacy`` text scrubber if the package is installed."""
    try:
        import openadapt_privacy  # type: ignore[import-not-found]
    except ImportError:
        return None

    for attr in ("scrub_text", "redact_text", "scrub", "redact"):
        fn = getattr(openadapt_privacy, attr, None)
        if callable(fn):
            return lambda text, _fn=fn: str(_fn(text))
    return None


def _resolve_scrubber(
    scrubber: Optional[TextScrubber] = None,
) -> tuple[str, TextScrubber]:
    if scrubber is not None:
        return "custom", scrubber
    external = _try_openadapt_privacy_scrubber()
    if external is not None:
        return "openadapt-privacy", lambda text: scrub_text_builtin(external(text))
    return "builtin-pii-phi", scrub_text_builtin


def _scrub_json_value(value: Any, scrub: TextScrubber) -> Any:
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            key_lower = str(key).strip().lower()
            if key_lower in _SENSITIVE_JSON_KEYS:
                cleaned[str(key)] = "[REDACTED:FIELD]"
            else:
                cleaned[str(key)] = _scrub_json_value(item, scrub)
        return cleaned
    if isinstance(value, list):
        return [_scrub_json_value(elem, scrub) for elem in value]
    if isinstance(value, str):
        return scrub(value)
    return value


def _redact_file_bytes(rel_path: Path, raw: bytes, scrub: TextScrubber) -> bytes:
    suffix = rel_path.suffix.lower()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw

    if suffix == ".json":
        try:
            doc = json.loads(text)
            cleaned = _scrub_json_value(doc, scrub)
            return (json.dumps(cleaned, indent=2, sort_keys=True) + "\n").encode("utf-8")
        except ValueError:
            return scrub(text).encode("utf-8")

    if suffix == ".jsonl":
        out_lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
                out_lines.append(
                    json.dumps(_scrub_json_value(obj, scrub), sort_keys=True)
                )
            except ValueError:
                out_lines.append(scrub(line))
        return ("\n".join(out_lines) + ("\n" if out_lines else "")).encode("utf-8")

    return scrub(text).encode("utf-8")


def _write_deterministic_zip_entry(
    zf: zipfile.ZipFile, arcname: str, data: bytes
) -> None:
    info = zipfile.ZipInfo(filename=arcname, date_time=_DETERMINISTIC_ZIP_DATE)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    zf.writestr(info, data)


def default_output_path(run_dir: Path) -> Path:
    resolved = run_dir.expanduser().resolve(strict=False)
    return resolved.parent / f"{resolved.name}.approved.zip"


def create_support_bundle(
    run_dir: Path,
    *,
    out_path: Optional[Path] = None,
    scrubber: Optional[TextScrubber] = None,
) -> SupportBundleResult:
    """Create a redacted, deterministic, hash-pinned ``.approved.zip`` from ``run_dir``."""
    source_dir = run_dir.expanduser().resolve(strict=False)
    if not source_dir.is_dir():
        raise SupportBundleError(f"Run directory does not exist: {source_dir}")

    target_zip = (out_path or default_output_path(source_dir)).expanduser().resolve(
        strict=False
    )
    if not target_zip.name.endswith(".approved.zip"):
        target_zip = target_zip.with_name(f"{target_zip.stem}.approved.zip")

    engine_name, scrub_fn = _resolve_scrubber(scrubber)

    entries: list[tuple[str, bytes]] = []
    excluded_count = 0

    for file_path in sorted(source_dir.rglob("*")):
        if not file_path.is_file():
            continue
        if file_path.resolve(strict=False) == target_zip:
            continue
        rel = file_path.relative_to(source_dir)
        if is_excluded_path(rel) or rel.as_posix() == MANIFEST_FILENAME:
            excluded_count += 1
            continue
        redacted = _redact_file_bytes(rel, file_path.read_bytes(), scrub_fn)
        entries.append((rel.as_posix(), redacted))

    entries.sort(key=lambda item: item[0])

    files_manifest: dict[str, dict[str, Any]] = {
        arcname: {
            "sha256": _sha256_bytes(data),
            "size_bytes": len(data),
        }
        for arcname, data in entries
    }

    manifest: dict[str, Any] = {
        "schema": SUPPORT_BUNDLE_SCHEMA,
        "redaction_engine": engine_name,
        "included_count": len(entries),
        "excluded_count": excluded_count,
        "files": files_manifest,
    }
    manifest_bytes = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )

    target_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target_zip, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, data in entries:
            _write_deterministic_zip_entry(zf, arcname, data)
        _write_deterministic_zip_entry(zf, MANIFEST_FILENAME, manifest_bytes)

    bundle_sha = _sha256_bytes(target_zip.read_bytes())
    return SupportBundleResult(
        output_path=str(target_zip),
        bundle_sha256=bundle_sha,
        redaction_engine=engine_name,
        included_files=tuple(arcname for arcname, _ in entries),
        excluded_count=excluded_count,
        manifest=manifest,
    )


def verify_support_bundle(bundle_path: Path) -> BundleVerifyResult:
    """Verify the manifest hashes and exclusion policy of a ``.approved.zip``."""
    target = bundle_path.expanduser().resolve(strict=False)
    if not target.is_file():
        return BundleVerifyResult(False, f"Bundle not found: {target}", 0, "")

    bundle_sha = _sha256_bytes(target.read_bytes())
    try:
        zf = zipfile.ZipFile(target, mode="r")
    except zipfile.BadZipFile:
        return BundleVerifyResult(False, "Not a valid zip archive.", 0, bundle_sha)

    with zf:
        names = set(zf.namelist())
        if MANIFEST_FILENAME not in names:
            return BundleVerifyResult(
                False, f"Missing {MANIFEST_FILENAME} in archive.", 0, bundle_sha
            )
        try:
            manifest = json.loads(zf.read(MANIFEST_FILENAME).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            return BundleVerifyResult(
                False, f"Malformed {MANIFEST_FILENAME}: {exc}", 0, bundle_sha
            )

        if not isinstance(manifest, dict) or manifest.get("schema") != SUPPORT_BUNDLE_SCHEMA:
            return BundleVerifyResult(
                False,
                f"Unsupported manifest schema: {manifest.get('schema') if isinstance(manifest, dict) else None!r}.",
                0,
                bundle_sha,
            )

        files_map = manifest.get("files")
        if not isinstance(files_map, dict):
            return BundleVerifyResult(
                False, "Manifest 'files' section is missing or invalid.", 0, bundle_sha
            )

        archive_files = names - {MANIFEST_FILENAME}
        manifest_files = set(files_map.keys())
        if archive_files != manifest_files:
            extra = sorted(archive_files - manifest_files)
            missing = sorted(manifest_files - archive_files)
            return BundleVerifyResult(
                False,
                f"Archive file set mismatch (missing={missing}, extra={extra}).",
                0,
                bundle_sha,
            )

        verified = 0
        for arcname in sorted(manifest_files):
            if is_excluded_path(Path(arcname)):
                return BundleVerifyResult(
                    False,
                    f"Excluded file class found in archive: {arcname}.",
                    verified,
                    bundle_sha,
                )
            meta = files_map[arcname]
            if not isinstance(meta, dict) or not isinstance(meta.get("sha256"), str):
                return BundleVerifyResult(
                    False,
                    f"Malformed manifest entry for {arcname}.",
                    verified,
                    bundle_sha,
                )
            data = zf.read(arcname)
            actual_sha = _sha256_bytes(data)
            if actual_sha != meta["sha256"]:
                return BundleVerifyResult(
                    False,
                    f"SHA-256 mismatch for {arcname}: expected {meta['sha256']}, got {actual_sha}.",
                    verified,
                    bundle_sha,
                )
            if "size_bytes" in meta and len(data) != meta["size_bytes"]:
                return BundleVerifyResult(
                    False,
                    f"Size mismatch for {arcname}: expected {meta['size_bytes']}, got {len(data)}.",
                    verified,
                    bundle_sha,
                )
            verified += 1

    return BundleVerifyResult(
        True,
        f"Verified {verified} hash-pinned file(s).",
        verified,
        bundle_sha,
    )
