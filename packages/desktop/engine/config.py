"""Engine configuration using pydantic-settings.

Settings are loaded from environment variables (prefixed with OPENADAPT_)
and validated at startup. The build profile (enterprise, community, full)
determines which storage backends are available.

Configuration validation chain (from design doc Section 7.6):
    1. Build includes backend? -> Hard error if mismatch.
    2. Credentials valid? -> Verify S3 bucket access, HF token, etc.
    3. Log the active configuration to audit.jsonl.

See design doc Section 7.6 for the full .env specification.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource

# Non-secret on-disk config lives here (spec section 3e). Secrets (ingest token,
# Supabase refresh token) NEVER land in this file -- they go to the OS keychain
# via ``engine.auth.store``. Overridable for tests via OPENADAPT_CONFIG_TOML.
DEFAULT_CONFIG_TOML = Path.home() / ".openadapt" / "config.toml"

# Maps ``[hosted]`` TOML keys onto EngineConfig field names.
_HOSTED_KEY_MAP = {
    "host": "hosted_host",
    "deployment_lane": "deployment_lane",
    "phi_mode": "phi_mode",
    "poll_interval_s": "poll_interval_s",
}


def _config_toml_path() -> Path:
    override = os.environ.get("OPENADAPT_CONFIG_TOML", "").strip()
    return Path(override) if override else DEFAULT_CONFIG_TOML


class HostedTomlSource(PydanticBaseSettingsSource):
    """Loads non-secret hosted config from ``~/.openadapt/config.toml``.

    Reads the ``[hosted]`` table (plus any top-level keys matching fields) and
    contributes them at LOWER priority than env/init, so environment variables
    always win. Secrets are ignored here by construction -- they are not written
    to the file.
    """

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        path = _config_toml_path()
        if not path.exists():
            return {}
        try:
            data = tomllib.loads(path.read_text())
        except (tomllib.TOMLDecodeError, OSError):
            return {}

        values: dict[str, Any] = {}
        hosted = data.get("hosted", {})
        if isinstance(hosted, dict):
            for toml_key, field_name in _HOSTED_KEY_MAP.items():
                if toml_key in hosted:
                    values[field_name] = hosted[toml_key]
        # Allow flat top-level overrides for non-hosted fields too.
        for key, value in data.items():
            if key != "hosted" and key in self.settings_cls.model_fields:
                values.setdefault(key, value)
        return values


class EngineConfig(BaseSettings):
    """Configuration for the OpenAdapt Desktop engine.

    All settings can be overridden via environment variables with the
    OPENADAPT_ prefix (e.g., OPENADAPT_STORAGE_MODE=enterprise). Non-secret
    hosted settings may also be set in ``~/.openadapt/config.toml`` under a
    ``[hosted]`` table; environment variables take precedence over the file.
    """

    model_config = {"env_prefix": "OPENADAPT_"}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Priority: init > env > dotenv > config.toml > file secrets.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            HostedTomlSource(settings_cls),
            file_secret_settings,
        )

    # --- General ---
    data_dir: Path = Field(
        default=Path.home() / ".openadapt",
        description="Root directory for all OpenAdapt data (captures, archives, databases).",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging level (DEBUG, INFO, WARNING, ERROR).",
    )

    # --- Storage ---
    storage_mode: str = Field(
        default="air-gapped",
        description=(
            "Storage mode: air-gapped (local only), enterprise (S3), "
            "community (HF Hub + R2), full (all backends)."
        ),
    )
    max_storage_gb: float = Field(
        default=50.0,
        description="Maximum disk usage in GB before automatic cleanup.",
    )
    retention_days: int = Field(
        default=30,
        description="How many days to retain captures before deletion.",
    )
    archive_after_hours: int = Field(
        default=24,
        description="Compress hot-tier captures after this many hours.",
    )

    # --- Recording ---
    recording_quality: str = Field(
        default="standard",
        description="Recording quality preset: low, standard, high, lossless.",
    )
    idle_fps: float = Field(
        default=0.1,
        description="Frame rate when user is idle (frames per second).",
    )
    active_fps: float = Field(
        default=10.0,
        description="Frame rate when user is active (frames per second).",
    )
    burst_fps: float = Field(
        default=30.0,
        description="Frame rate during burst capture (frames per second).",
    )

    # --- Upload ---
    upload_require_review: bool = Field(
        default=True,
        description="Require user review before any upload.",
    )
    upload_bandwidth_limit_mbps: float = Field(
        default=5.0,
        description="Maximum upload bandwidth in MB/s.",
    )
    upload_schedule: str = Field(
        default="idle",
        description="Upload schedule: idle, always, manual, or cron expression.",
    )

    # --- S3 (enterprise) ---
    s3_bucket: str = Field(default="", description="S3 bucket name.")
    s3_region: str = Field(default="us-east-1", description="AWS region for S3.")
    s3_access_key_id: str = Field(default="", description="AWS access key ID.")
    s3_secret_access_key: str = Field(default="", description="AWS secret access key.")
    s3_endpoint: str = Field(
        default="",
        description="Custom S3 endpoint (for MinIO, R2).",
    )

    # --- Hosted control plane (app.openadapt.ai) ---
    hosted_host: str = Field(
        default="https://app.openadapt.ai",
        description="Base URL of the hosted control plane (ingest, needs-attention).",
    )
    deployment_lane: str = Field(
        default="cloud",
        description=(
            "Lane routing the PHI boundary: 'cloud' (non-PHI, push recordings to "
            "the server to compile) or 'byoc' (regulated -- recordings + teach "
            "stay local; only PHI-free descriptors sync up)."
        ),
    )
    phi_mode: str = Field(
        default="off",
        description="PHI mode: 'off' or 'on'. When 'on', outbound egress is PHI-fenced.",
    )
    poll_interval_s: int = Field(
        default=60,
        description="Seconds between needs-attention count polls (never < 30).",
    )

    # --- Audit ---
    network_audit_log: bool = Field(
        default=True,
        description="Log all outbound network requests to audit.jsonl.",
    )
    audit_log_path: Path = Field(
        default=Path.home() / ".openadapt" / "audit.jsonl",
        description="Path to the network audit log file.",
    )
