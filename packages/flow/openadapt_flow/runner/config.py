"""The operator-authored runner trust manifest (``runner.toml``).

Nothing writes this file programmatically. It names the deployment profiles a
dispatch may reference and the exact sealed bundles (by content digest) this
machine is willing to execute — a digest absent from this file is refused.
That is the no-remote-code-delivery hard line: the hosted adapter only
ever executes bundles the operator ALREADY installed and listed here; the
dispatch's ``bundle.url`` is never fetched.

Per-bundle knobs implement the local-policy-final posture the design review
requires of any L1 client:

* ``policy`` pins the admitted policy name the authorization must carry;
* ``params_ref_required`` refuses inline ``params.values`` dispatches for
  this bundle (the regulated posture — dispatch params ARE the PHI for the
  wedge ICP, review finding PHI-3);
* ``param_patterns`` pins a full-match regex per runtime param, enforced
  locally before start (review finding S2: local policy must be able to
  distinguish good params from bad ones);
* ``allow_unverified_writes`` / ``allow_unencrypted`` mirror the governed
  ``run`` CLI escape hatches and default OFF.

Example::

    [runner]
    name = "front-desk-1"
    backends = ["web"]

    [profiles]
    default = "/opt/openadapt/deployment.yaml"

    [[bundles]]
    content_digest = "<64-hex sealed bundle digest>"
    path = "/opt/openadapt/bundles/claims-entry"
    policy = "clinical-write"
    params_ref_required = false
    [bundles.param_patterns]
    visit_date = "^\\d{4}-\\d{2}-\\d{2}$"
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

from openadapt_flow.hosted import HostedError
from openadapt_flow.private_file import (
    PrivateFileAclError,
    windows_descriptor_has_private_acl,
)

_HEX64_RE = re.compile(r"^[a-f0-9]{64}$")
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+\-]{0,199}$")

LocalReleaseTarget = Literal["flow", "desktop", "capture"]


def _load_manifest_toml(path: Path, *, protected: bool = False) -> dict[str, Any]:
    """Full-TOML parse (the manifest uses ``[[bundles]]`` array tables, which
    ``hosted._load_toml``'s 3.10 minimal fallback cannot represent). Uses
    stdlib ``tomllib`` on 3.11+ and the declared ``tomli`` dependency on 3.10.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    if protected:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            path_before = path.lstat()
            if not stat.S_ISREG(path_before.st_mode) or stat.S_ISLNK(
                path_before.st_mode
            ):
                raise RunnerConfigError(
                    "hosted runner manifest is not a private regular file"
                )
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise RunnerConfigError(
                "hosted runner manifest could not be opened safely"
            ) from exc
        try:
            before = os.fstat(descriptor)
            try:
                private = (
                    windows_descriptor_has_private_acl(descriptor)
                    if os.name == "nt"
                    else (
                        before.st_uid == os.geteuid()
                        and stat.S_IMODE(before.st_mode) == 0o600
                    )
                )
            except PrivateFileAclError as exc:
                raise RunnerConfigError(
                    "hosted runner manifest ACL could not be verified"
                ) from exc
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_size > 1024 * 1024
                or not private
            ):
                raise RunnerConfigError(
                    "hosted runner manifest is not a private regular file"
                )
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(descriptor, min(remaining, 64 * 1024))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            after = os.fstat(descriptor)
            try:
                path_after = path.lstat()
            except OSError as exc:
                raise RunnerConfigError(
                    "hosted runner manifest changed during its protected read"
                ) from exc
            if (
                len(raw) != before.st_size
                or stat.S_ISLNK(path_after.st_mode)
                or (path_before.st_dev, path_before.st_ino)
                != (before.st_dev, before.st_ino)
                or (path_after.st_dev, path_after.st_ino)
                != (before.st_dev, before.st_ino)
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            ):
                raise RunnerConfigError(
                    "hosted runner manifest changed during its protected read"
                )
        finally:
            os.close(descriptor)
        try:
            return tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise RunnerConfigError("hosted runner manifest is not valid TOML") from exc
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RunnerConfigError(
            f"runner manifest {path} is not valid TOML: {exc}"
        ) from exc


class RunnerConfigError(HostedError):
    """The runner trust manifest is missing or malformed."""


def _home() -> Path:
    root = os.environ.get("OPENADAPT_HOME")
    return Path(root) if root else Path.home() / ".openadapt"


def runner_config_path() -> Path:
    """The operator-authored trust manifest: ``~/.openadapt/runner.toml``."""
    return _home() / "runner.toml"


@dataclass(frozen=True)
class TrustedBundle:
    """One sealed bundle this machine already holds and is willing to run."""

    content_digest: str
    path: Path
    #: Optional pin: the dispatch's ``admitted_policy_name`` must equal this.
    policy: Optional[str] = None
    #: Refuse inline ``params.values`` dispatches for this bundle (regulated
    #: posture: runtime params ride a local reference, never the wire).
    params_ref_required: bool = False
    #: Full-match regex per runtime param, enforced locally before start.
    #: When non-empty, EVERY supplied param must have a matching pattern —
    #: an unlisted param is refused (fail closed).
    param_patterns: dict[str, str] = field(default_factory=dict)
    #: Local opt-in to pass ``--approve-unverified-writes`` when (and only
    #: when) the authorization carries explicit write approvals.
    allow_unverified_writes: bool = False
    #: Local escape hatch mirroring ``run --allow-unencrypted``.
    allow_unencrypted: bool = False
    #: Exact archive/object digest named by the workflow admission. Hosted
    #: execution requires this local pin; ordinary local execution does not.
    artifact_sha256: Optional[str] = None


@dataclass(frozen=True)
class BusinessDecisionServiceConfig:
    """Local-only secret reference for the typed-decision relay service."""

    key_file: Path


@dataclass(frozen=True)
class LocalRuntimeRelease:
    """One independently installed target release used during enrollment."""

    target: LocalReleaseTarget
    admission_id: str
    admission_sha256: str
    release_version: str
    release_artifact_sha256: str


@dataclass(frozen=True)
class AdmissionTrustFiles:
    """Local signer and revocation state used to verify hosted admissions."""

    signer_registry: Path
    state: Path


@dataclass(frozen=True)
class WorkflowAdmissionTrustFiles(AdmissionTrustFiles):
    """Local v2 expectation that is independent from the leased artifact."""

    expected_bindings: Path


@dataclass(frozen=True)
class RunnerConfig:
    """Parsed trust manifest."""

    name: str
    host: Optional[str] = None
    profiles: dict[str, Path] = field(default_factory=dict)
    bundles: dict[str, TrustedBundle] = field(default_factory=dict)
    #: Capability advertisement (deployment.yaml backend kinds this machine
    #: can drive) for hosted registration. Advisory only.
    backends: tuple[str, ...] = ("web",)
    business_decisions: Optional[BusinessDecisionServiceConfig] = None
    local_runtime_release: tuple[LocalRuntimeRelease, ...] = ()
    product_release_admission: Optional[AdmissionTrustFiles] = None
    workflow_admission: Optional[WorkflowAdmissionTrustFiles] = None
    params_ref_root: Optional[Path] = None
    evidence_runner_private_key: Optional[Path] = None


def _parse_param_patterns(raw: object, index: int) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise RunnerConfigError(
            f"[[bundles]] entry {index} param_patterns must be a table"
        )
    patterns: dict[str, str] = {}
    for key, value in raw.items():
        pattern = str(value)
        try:
            re.compile(pattern)
        except re.error as exc:
            raise RunnerConfigError(
                f"[[bundles]] entry {index} param_patterns[{key!r}] is not a "
                f"valid regex: {exc}"
            ) from exc
        patterns[str(key)] = pattern
    return patterns


def load_runner_config(
    path: Optional[Path] = None, *, protected: bool = False
) -> RunnerConfig:
    """Load and validate ``runner.toml``. Fail loudly on anything malformed."""
    cfg_path = path or runner_config_path()
    if not cfg_path.is_file():
        raise RunnerConfigError(
            f"No runner trust manifest at {cfg_path}. Create it first: it must "
            "list the deployment profiles and the exact sealed bundles (by "
            "content digest) this machine may execute."
        )
    data = _load_manifest_toml(cfg_path, protected=protected)

    runner_tbl = data.get("runner") or {}
    if not isinstance(runner_tbl, dict):
        raise RunnerConfigError("[runner] must be a table")
    name = str(runner_tbl.get("name") or "").strip()
    if not name:
        raise RunnerConfigError("[runner] name is required")
    host = runner_tbl.get("host")
    host = str(host).strip() if host else None
    backends_raw = runner_tbl.get("backends") or ["web"]
    if not isinstance(backends_raw, list) or not all(
        isinstance(b, str) and b.strip() for b in backends_raw
    ):
        raise RunnerConfigError("[runner] backends must be a list of strings")

    profiles_tbl = data.get("profiles") or {}
    if not isinstance(profiles_tbl, dict):
        raise RunnerConfigError("[profiles] must be a table of name = path")
    profiles: dict[str, Path] = {}
    for prof_name, prof_path in profiles_tbl.items():
        p = Path(str(prof_path)).expanduser()
        if not p.is_file():
            raise RunnerConfigError(
                f"[profiles] {prof_name} points at a missing deployment config: {p}"
            )
        profiles[str(prof_name)] = p

    bundles_raw = data.get("bundles") or []
    if not isinstance(bundles_raw, list):
        raise RunnerConfigError("[[bundles]] must be an array of tables")
    bundles: dict[str, TrustedBundle] = {}
    for i, entry in enumerate(bundles_raw):
        if not isinstance(entry, dict):
            raise RunnerConfigError(f"[[bundles]] entry {i} must be a table")
        digest = str(entry.get("content_digest") or "").strip().lower()
        if not _HEX64_RE.fullmatch(digest):
            raise RunnerConfigError(
                f"[[bundles]] entry {i} content_digest must be 64 lowercase hex"
            )
        bundle_path = Path(str(entry.get("path") or "")).expanduser()
        if not bundle_path.is_dir():
            raise RunnerConfigError(
                f"[[bundles]] entry {i} path is not a bundle directory: {bundle_path}"
            )
        if digest in bundles:
            raise RunnerConfigError(
                f"[[bundles]] duplicate content_digest {digest[:16]}..."
            )
        policy = entry.get("policy")
        bundles[digest] = TrustedBundle(
            content_digest=digest,
            path=bundle_path,
            policy=str(policy).strip() if policy else None,
            params_ref_required=bool(entry.get("params_ref_required", False)),
            param_patterns=_parse_param_patterns(entry.get("param_patterns"), i),
            allow_unverified_writes=bool(entry.get("allow_unverified_writes", False)),
            allow_unencrypted=bool(entry.get("allow_unencrypted", False)),
            artifact_sha256=(
                str(entry["artifact_sha256"])
                if entry.get("artifact_sha256") is not None
                else None
            ),
        )
        if bundles[digest].artifact_sha256 is not None and not _HEX64_RE.fullmatch(
            bundles[digest].artifact_sha256 or ""
        ):
            raise RunnerConfigError(
                f"[[bundles]] entry {i} artifact_sha256 must be 64 lowercase hex"
            )

    decision_tbl = data.get("business_decisions")
    business_decisions = None
    if decision_tbl is not None:
        if not isinstance(decision_tbl, dict):
            raise RunnerConfigError("[business_decisions] must be a table")
        unknown = set(decision_tbl) - {"key_file"}
        if unknown:
            raise RunnerConfigError(
                "[business_decisions] has unknown keys: "
                + ", ".join(sorted(str(key) for key in unknown))
            )
        raw_key_file = str(decision_tbl.get("key_file") or "").strip()
        if not raw_key_file:
            raise RunnerConfigError("business_decisions.key_file is required")
        key_file = Path(raw_key_file).expanduser()
        if not key_file.is_file():
            raise RunnerConfigError(
                "business_decisions.key_file is not an existing file"
            )
        business_decisions = BusinessDecisionServiceConfig(key_file=key_file)

    local_release_tbl = data.get("local_runtime_release") or {}
    if not isinstance(local_release_tbl, dict):
        raise RunnerConfigError("[local_runtime_release] must be a table")
    local_runtime_release: list[LocalRuntimeRelease] = []
    expected_release_targets: tuple[LocalReleaseTarget, ...] = (
        "flow",
        "desktop",
        "capture",
    )
    for target in expected_release_targets:
        entry = local_release_tbl.get(target)
        if entry is None:
            continue
        if not isinstance(entry, dict) or set(entry) != {
            "admission_id",
            "admission_sha256",
            "release_version",
            "release_artifact_sha256",
        }:
            raise RunnerConfigError(
                f"[local_runtime_release.{target}] has an invalid exact shape"
            )
        admission_sha256 = str(entry["admission_sha256"])
        artifact_sha256 = str(entry["release_artifact_sha256"])
        admission_id = str(entry["admission_id"])
        release_version = str(entry["release_version"])
        if not _HEX64_RE.fullmatch(admission_sha256) or not _HEX64_RE.fullmatch(
            artifact_sha256
        ):
            raise RunnerConfigError(
                f"[local_runtime_release.{target}] contains an invalid digest"
            )
        if not _UUID_RE.fullmatch(admission_id) or not _SAFE_ID_RE.fullmatch(
            release_version
        ):
            raise RunnerConfigError(
                f"[local_runtime_release.{target}] contains an invalid identity"
            )
        local_runtime_release.append(
            LocalRuntimeRelease(
                target=target,
                admission_id=admission_id,
                admission_sha256=admission_sha256,
                release_version=release_version,
                release_artifact_sha256=artifact_sha256,
            )
        )
    unknown_release_targets = sorted(
        set(local_release_tbl).difference(expected_release_targets)
    )
    if unknown_release_targets:
        raise RunnerConfigError(
            "[local_runtime_release] contains unknown target(s): "
            + ", ".join(unknown_release_targets)
        )

    def admission_trust_files(table_name: str) -> Optional[AdmissionTrustFiles]:
        table = data.get(table_name)
        if table is None:
            return None
        if not isinstance(table, dict) or set(table) != {"signer_registry", "state"}:
            raise RunnerConfigError(f"[{table_name}] has an invalid exact shape")
        registry = Path(str(table["signer_registry"])).expanduser()
        state = Path(str(table["state"])).expanduser()
        if not registry.is_file() or not state.is_file():
            raise RunnerConfigError(
                f"[{table_name}] trust files must be existing regular files"
            )
        return AdmissionTrustFiles(signer_registry=registry, state=state)

    product_release_admission = admission_trust_files("product_release_admission")
    workflow_table = data.get("workflow_admission")
    workflow_admission = None
    if workflow_table is not None:
        if not isinstance(workflow_table, dict) or set(workflow_table) != {
            "signer_registry",
            "state",
            "expected_bindings",
        }:
            raise RunnerConfigError("[workflow_admission] has an invalid exact shape")
        workflow_paths = {
            key: Path(str(workflow_table[key])).expanduser()
            for key in ("signer_registry", "state", "expected_bindings")
        }
        if any(not path.is_file() for path in workflow_paths.values()):
            raise RunnerConfigError(
                "[workflow_admission] trust files must be existing regular files"
            )
        workflow_admission = WorkflowAdmissionTrustFiles(
            signer_registry=workflow_paths["signer_registry"],
            state=workflow_paths["state"],
            expected_bindings=workflow_paths["expected_bindings"],
        )

    params_tbl = data.get("params")
    params_ref_root = None
    if params_tbl is not None:
        if not isinstance(params_tbl, dict) or set(params_tbl) != {"protected_root"}:
            raise RunnerConfigError("[params] has an invalid exact shape")
        params_ref_root = Path(str(params_tbl["protected_root"])).expanduser()
        if not params_ref_root.is_dir():
            raise RunnerConfigError(
                "params.protected_root must be an existing directory"
            )

    evidence_tbl = data.get("evidence_runner")
    evidence_runner_private_key = None
    if evidence_tbl is not None:
        if not isinstance(evidence_tbl, dict) or set(evidence_tbl) != {
            "private_key_file"
        }:
            raise RunnerConfigError("[evidence_runner] has an invalid exact shape")
        evidence_runner_private_key = Path(
            str(evidence_tbl["private_key_file"])
        ).expanduser()
        if not evidence_runner_private_key.is_file():
            raise RunnerConfigError(
                "evidence_runner.private_key_file must be an existing file"
            )

    return RunnerConfig(
        name=name,
        host=host,
        profiles=profiles,
        bundles=bundles,
        backends=tuple(str(b).strip() for b in backends_raw),
        business_decisions=business_decisions,
        local_runtime_release=tuple(local_runtime_release),
        product_release_admission=product_release_admission,
        workflow_admission=workflow_admission,
        params_ref_root=params_ref_root,
        evidence_runner_private_key=evidence_runner_private_key,
    )
