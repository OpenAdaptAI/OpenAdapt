"""The operator-authored runner trust manifest (``runner.toml``).

Nothing writes this file programmatically. It names the deployment profiles a
dispatch may reference and the exact sealed bundles (by content digest) this
machine is willing to execute — a digest absent from this file is refused.
That is the no-remote-code-delivery hard line: the future runner daemon only
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.hosted import HostedError

_HEX64_RE = re.compile(r"^[a-f0-9]{64}$")


def _load_manifest_toml(path: Path) -> dict[str, Any]:
    """Full-TOML parse (the manifest uses ``[[bundles]]`` array tables, which
    ``hosted._load_toml``'s 3.10 minimal fallback cannot represent). Uses
    stdlib ``tomllib`` on 3.11+ and the declared ``tomli`` dependency on 3.10.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
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


@dataclass(frozen=True)
class RunnerConfig:
    """Parsed trust manifest."""

    name: str
    host: Optional[str] = None
    profiles: dict[str, Path] = field(default_factory=dict)
    bundles: dict[str, TrustedBundle] = field(default_factory=dict)
    #: Capability advertisement (deployment.yaml backend kinds this machine
    #: can drive) for the future register/poll payloads. Advisory only.
    backends: tuple[str, ...] = ("web",)


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


def load_runner_config(path: Optional[Path] = None) -> RunnerConfig:
    """Load and validate ``runner.toml``. Fail loudly on anything malformed."""
    cfg_path = path or runner_config_path()
    if not cfg_path.is_file():
        raise RunnerConfigError(
            f"No runner trust manifest at {cfg_path}. Create it first: it must "
            "list the deployment profiles and the exact sealed bundles (by "
            "content digest) this machine may execute."
        )
    data = _load_manifest_toml(cfg_path)

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
        )

    return RunnerConfig(
        name=name,
        host=host,
        profiles=profiles,
        bundles=bundles,
        backends=tuple(str(b).strip() for b in backends_raw),
    )
