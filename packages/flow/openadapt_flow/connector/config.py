"""Connector settings + persisted enrollment (``~/.openadapt/connector.toml``).

``openadapt-flow connector enroll`` writes this file (mode 0600) after a
successful enrollment; ``openadapt-flow connector run`` reads it so a bare
``run`` works after enrolling once. Resolution order for every field is
explicit flag > env var > connector.toml > built-in default.

The file holds the per-connector TOKEN, so it is written 0600 and never logged.
It also names the local deployment PROFILE (a ``deployment.yaml`` the OPERATOR
authored) that the governed ``run`` uses as ``--config`` — the substrate, egress
posture, effect verifiers, and at-rest key all come from that operator-owned
profile, never from the control plane. That is the BYOC boundary: the customer
owns the data plane and its configuration.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from openadapt_flow.hosted import HostedError


class ConnectorConfigError(HostedError):
    """The connector settings are missing or malformed."""


def _home() -> Path:
    root = os.environ.get("OPENADAPT_HOME")
    return Path(root) if root else Path.home() / ".openadapt"


def connector_config_path() -> Path:
    """Persisted enrollment: ``~/.openadapt/connector.toml``."""
    return _home() / "connector.toml"


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]
    try:
        with path.open("rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConnectorConfigError(
            f"connector config {path} is not valid TOML: {exc}"
        ) from exc


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


@dataclass
class ConnectorSettings:
    """Resolved connector settings (never logged: token is a secret)."""

    control_plane_url: str = "https://app.openadapt.ai"
    token: Optional[str] = None
    org_id: Optional[str] = None
    name: str = "openadapt-connector"
    poll_wait_s: int = 25
    #: Path to the operator-authored deployment.yaml the governed run uses.
    profile: Optional[str] = None
    #: Optional pinned policy name (else the deployment config's policy).
    policy: Optional[str] = None
    #: Customer-owned storage (the byoc data boundary).
    storage_backend: str = "local"
    storage_root: Optional[str] = None
    storage_bucket: Optional[str] = None
    storage_prefix: str = ""
    #: Mirror the governed ``run`` escape hatch; default OFF (fail closed).
    allow_unencrypted: bool = False
    #: Local secret store: ``<secrets_dir>/<secrets_ref>.env`` (mode 0600).
    #: Only the ``secrets_ref`` name ever crosses the network.
    secrets_dir: Optional[str] = None
    #: Comma-separated Ed25519 public keys (64 hex) of the program admitters
    #: this runner trusts. A dispatch whose admission key is not pinned here is
    #: refused: the control plane cannot widen what this machine will run.
    trusted_admission_keys: str = ""
    #: Local, append-only JSONL log of every request body this connector sent.
    egress_audit_log: Optional[str] = None
    #: Local ceiling on parallel jobs; the server's cap applies under it.
    max_workers: int = 4
    #: Path to the qualification signer registry JSON (production admission).
    qualification_signers_file: Optional[str] = None

    def pinned_admission_keys(self) -> frozenset[str]:
        keys = {k.strip().lower() for k in self.trusted_admission_keys.split(",")}
        return frozenset(
            k for k in keys if len(k) == 64 and all(c in "0123456789abcdef" for c in k)
        )

    def redacted(self) -> dict[str, Any]:
        """A log-safe view (token elided)."""
        return {
            "control_plane_url": self.control_plane_url,
            "org_id": self.org_id,
            "name": self.name,
            "poll_wait_s": self.poll_wait_s,
            "profile": self.profile,
            "policy": self.policy,
            "storage_backend": self.storage_backend,
            "secrets_dir": self.secrets_dir,
            "trusted_admission_keys": sorted(self.pinned_admission_keys()),
            "max_workers": self.max_workers,
            "token": "<set>" if self.token else None,
        }


def _pick(
    flags: dict[str, Any], env_key: str, file_val: Any, default: Any, flag_key: str
) -> Any:
    val = flags.get(flag_key)
    if val is None:
        val = os.environ.get(env_key)
    if val is None:
        val = file_val
    return val if val is not None else default


def load_settings(
    flags: Optional[dict[str, Any]] = None, *, path: Optional[Path] = None
) -> ConnectorSettings:
    """Resolve settings: explicit flag > env > connector.toml > default."""
    flags = flags or {}
    cfg_path = path or connector_config_path()
    file_cfg = _load_toml(cfg_path).get("connector", {}) if cfg_path.is_file() else {}
    if not isinstance(file_cfg, dict):
        raise ConnectorConfigError("[connector] must be a table")

    url = str(
        _pick(
            flags,
            "CONTROL_PLANE_URL",
            file_cfg.get("control_plane_url"),
            "https://app.openadapt.ai",
            "control_plane_url",
        )
    ).rstrip("/")
    return ConnectorSettings(
        control_plane_url=url,
        token=_pick(
            flags, "BYOC_CONNECTOR_TOKEN", file_cfg.get("token"), None, "token"
        ),
        org_id=_pick(flags, "BYOC_ORG_ID", file_cfg.get("org_id"), None, "org_id"),
        name=str(
            _pick(
                flags,
                "BYOC_CONNECTOR_NAME",
                file_cfg.get("name"),
                "openadapt-connector",
                "name",
            )
        ),
        poll_wait_s=int(
            _pick(
                flags, "BYOC_POLL_WAIT_S", file_cfg.get("poll_wait_s"), 25, "poll_wait"
            )
        ),
        profile=_pick(flags, "BYOC_PROFILE", file_cfg.get("profile"), None, "profile"),
        policy=_pick(flags, "BYOC_POLICY", file_cfg.get("policy"), None, "policy"),
        storage_backend=str(
            _pick(
                flags,
                "BYOC_STORAGE_BACKEND",
                file_cfg.get("storage_backend"),
                "local",
                "storage_backend",
            )
        ),
        storage_root=_pick(
            flags,
            "BYOC_STORAGE_ROOT",
            file_cfg.get("storage_root"),
            None,
            "storage_root",
        ),
        storage_bucket=_pick(
            flags,
            "BYOC_STORAGE_BUCKET",
            file_cfg.get("storage_bucket"),
            None,
            "storage_bucket",
        ),
        storage_prefix=str(
            _pick(
                flags,
                "BYOC_STORAGE_PREFIX",
                file_cfg.get("storage_prefix"),
                "",
                "storage_prefix",
            )
            or ""
        ),
        allow_unencrypted=bool(file_cfg.get("allow_unencrypted", False)),
        secrets_dir=_pick(
            flags, "BYOC_SECRETS_DIR", file_cfg.get("secrets_dir"), None, "secrets_dir"
        ),
        trusted_admission_keys=str(
            _pick(
                flags,
                "BYOC_TRUSTED_ADMISSION_KEYS",
                file_cfg.get("trusted_admission_keys"),
                "",
                "trusted_admission_keys",
            )
            or ""
        ),
        egress_audit_log=_pick(
            flags,
            "BYOC_EGRESS_AUDIT_LOG",
            file_cfg.get("egress_audit_log"),
            None,
            "egress_audit_log",
        ),
        max_workers=max(
            1,
            min(
                16,
                int(
                    _pick(
                        flags,
                        "BYOC_MAX_WORKERS",
                        file_cfg.get("max_workers"),
                        4,
                        "max_workers",
                    )
                ),
            ),
        ),
        qualification_signers_file=_pick(
            flags,
            "BYOC_QUALIFICATION_SIGNERS_FILE",
            file_cfg.get("qualification_signers_file"),
            None,
            "qualification_signers_file",
        ),
    )


def save_enrollment(
    settings: ConnectorSettings, *, path: Optional[Path] = None
) -> Path:
    """Persist the enrollment to connector.toml (0600). Token INCLUDED."""
    cfg_path = path or connector_config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[connector]"]
    fields: list[tuple[str, Any]] = [
        ("control_plane_url", settings.control_plane_url),
        ("token", settings.token),
        ("org_id", settings.org_id),
        ("name", settings.name),
        ("poll_wait_s", settings.poll_wait_s),
        ("profile", settings.profile),
        ("policy", settings.policy),
        ("storage_backend", settings.storage_backend),
        ("storage_root", settings.storage_root),
        ("storage_bucket", settings.storage_bucket),
        ("storage_prefix", settings.storage_prefix),
        ("allow_unencrypted", settings.allow_unencrypted),
        ("secrets_dir", settings.secrets_dir),
        ("trusted_admission_keys", settings.trusted_admission_keys),
        ("egress_audit_log", settings.egress_audit_log),
        ("max_workers", settings.max_workers),
        ("qualification_signers_file", settings.qualification_signers_file),
    ]
    for key, value in fields:
        if value is None or value == "":
            continue
        lines.append(f"{key} = {_toml_scalar(value)}")
    # Atomic: a crash mid-write (for example during token rotation) leaves
    # either the old file or the new one, never a truncated token.
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, cfg_path)
    try:
        os.chmod(cfg_path, 0o600)  # holds the per-connector token
    except OSError:  # pragma: no cover - platform without chmod semantics
        pass
    return cfg_path
