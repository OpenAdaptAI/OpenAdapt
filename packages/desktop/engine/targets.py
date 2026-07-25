"""Typed execution-target contract shared by Desktop and Flow deployment config.

``openadapt-flow`` owns backend construction.  Desktop collects the small set
of direct target overrides that Flow exposes, merges them over an optional
operator deployment config, and stages the result in a private mode-0600 file.
Window owners, titles, readiness selectors, and endpoints are PHI-capable and
must never be forwarded on the process command line or written to logs.

The model is deliberately closed and backend-specific: stale fields from a
previous UI selection are refused rather than being silently forwarded to the
wrong substrate.  Secret-bearing Flow fields such as ``agent_token`` and
``rdp_password`` are not part of this IPC schema at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

TargetBackend = Literal["web", "windows", "macos", "linux", "rdp", "citrix"]

_TARGET_FIELDS = {
    "url",
    "agent_url",
    "macos_app",
    "macos_window_title",
    "linux_app",
    "linux_window_title",
    "linux_allow_physical_input",
    "rdp_host",
    "rdp_window",
    "rdp_window_title",
    "rdp_readiness_text",
}

_FIELDS_BY_BACKEND: dict[str, set[str]] = {
    "web": {"url"},
    "windows": {"agent_url"},
    "macos": {"macos_app", "macos_window_title"},
    "linux": {
        "linux_app",
        "linux_window_title",
        "linux_allow_physical_input",
    },
    "rdp": {
        "rdp_host",
        "rdp_window",
        "rdp_window_title",
        "rdp_readiness_text",
    },
    "citrix": {
        "rdp_window",
        "rdp_window_title",
        "rdp_readiness_text",
    },
}


class ExecutionTarget(BaseModel):
    """PHI-capable Flow backend selection received over Desktop's local IPC."""

    model_config = ConfigDict(extra="forbid")

    backend: TargetBackend = "web"
    url: str | None = None
    agent_url: str | None = None
    macos_app: str | None = None
    macos_window_title: str | None = None
    linux_app: str | None = None
    linux_window_title: str | None = None
    linux_allow_physical_input: bool = False
    rdp_host: str | None = None
    rdp_window: str | None = None
    rdp_window_title: str | None = None
    rdp_readiness_text: str | None = None

    @field_validator(
        "url",
        "agent_url",
        "macos_app",
        "macos_window_title",
        "linux_app",
        "linux_window_title",
        "rdp_host",
        "rdp_window",
        "rdp_window_title",
        "rdp_readiness_text",
        mode="before",
    )
    @classmethod
    def _strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def _reject_cross_backend_fields(self) -> "ExecutionTarget":
        relevant = _FIELDS_BY_BACKEND[self.backend]
        supplied: set[str] = set()
        for name in _TARGET_FIELDS:
            value = getattr(self, name)
            if value is True or (value is not None and value is not False):
                supplied.add(name)
        irrelevant = sorted(supplied - relevant)
        if irrelevant:
            raise ValueError(
                f"target fields do not apply to backend {self.backend!r}: " + ", ".join(irrelevant)
            )
        if self.backend == "rdp" and self.rdp_host and (self.rdp_window or self.rdp_window_title):
            raise ValueError(
                "RDP target must select one connection: rdp_host (network) or "
                "rdp_window/rdp_window_title (local client window), not both"
            )
        return self

    def validate_required(self, *, deployment_config: bool) -> None:
        """Fail early when direct target fields cannot construct the backend.

        A supplied deployment config may provide any missing backend field, so
        Flow remains the authoritative validator for that path.  Citrix needs
        no direct owner because Flow defaults it by host OS.
        """

        if deployment_config:
            return
        required: tuple[str, ...]
        if self.backend == "windows":
            required = ("agent_url",)
        elif self.backend == "macos":
            required = ("macos_app",)
        elif self.backend == "linux":
            required = ("linux_app", "linux_window_title")
        else:
            required = ()
        missing = [name for name in required if not getattr(self, name)]
        if self.backend == "rdp" and not (self.rdp_host or self.rdp_window):
            missing.append("rdp_host or rdp_window")
        if missing:
            raise ValueError(
                f"{self.backend} target requires "
                + ", ".join(missing)
                + " (or select a deployment config that provides it)"
            )

    def validate_record_required(self) -> None:
        """Require the direct fields Flow's authoring path needs."""

        required: tuple[str, ...]
        if self.backend == "windows":
            required = ("agent_url",)
        elif self.backend == "macos":
            required = ("macos_app",)
        elif self.backend == "linux":
            required = ("linux_app", "linux_window_title")
        else:
            required = ()
        missing = [name for name in required if not getattr(self, name)]
        if self.backend == "rdp" and not (self.rdp_host or self.rdp_window):
            missing.append("rdp_host or rdp_window")
        if missing:
            raise ValueError(f"{self.backend} recording target requires " + ", ".join(missing))

    def deployment_overrides(self) -> dict[str, object]:
        """Render Flow 1.20's backend config keys without exposing argv values."""

        overrides: dict[str, object] = {"kind": self.backend}
        values: tuple[tuple[str, str | None], ...]
        if self.backend == "web":
            values = (("url", self.url),)
        elif self.backend == "windows":
            values = (("agent_url", self.agent_url),)
        elif self.backend == "macos":
            values = (
                ("macos_app", self.macos_app),
                ("macos_window_title", self.macos_window_title),
            )
        elif self.backend == "linux":
            values = (
                ("linux_app", self.linux_app),
                ("linux_window_title", self.linux_window_title),
            )
        else:
            values = (
                ("rdp_host", self.rdp_host),
                ("rdp_window", self.rdp_window),
                ("rdp_window_title", self.rdp_window_title),
                ("rdp_readiness_text", self.rdp_readiness_text),
            )
        for key, value in values:
            if value:
                overrides[key] = value
        if self.backend == "linux" and self.linux_allow_physical_input:
            overrides["linux_allow_physical_input"] = True
        return overrides

    def record_args(self, out_dir: Path, *, task: str = "") -> list[str]:
        """Build canonical bundled-Flow ``record`` arguments in private memory.

        The caller must never place this list on Desktop's own process command
        line. The private record helper reconstructs it only after reading and
        deleting a mode-0600 request file.
        """

        self.validate_record_required()
        if self.backend == "web" and not self.url:
            # Flow's interactive web recorder deliberately requires an exact
            # URL. Its separate demo-record command is the canonical bundled
            # MockMed quickstart, so keep Desktop's blank-URL path useful
            # without inventing an implicit public URL.
            return [
                "demo-record",
                "--out",
                str(out_dir),
            ]
        args = [
            "record",
            "--out",
            str(out_dir),
            "--backend",
            self.backend,
        ]
        if task:
            args.extend(["--task", task])

        values: tuple[tuple[str, str | None], ...]
        if self.backend == "web":
            values = (("--url", self.url),)
        elif self.backend == "windows":
            values = (("--agent-url", self.agent_url),)
        elif self.backend == "macos":
            values = (
                ("--macos-app", self.macos_app),
                ("--macos-window-title", self.macos_window_title),
                # Flow's authoring path scopes desktop capture with the shared
                # window flags while retaining native replay metadata above.
                ("--window", self.macos_app),
                ("--window-title", self.macos_window_title),
            )
        elif self.backend == "linux":
            values = (
                ("--linux-app", self.linux_app),
                ("--linux-window-title", self.linux_window_title),
            )
        else:
            values = (
                ("--rdp-host", self.rdp_host),
                ("--rdp-window", self.rdp_window),
                ("--rdp-window-title", self.rdp_window_title),
                ("--rdp-readiness-text", self.rdp_readiness_text),
            )
        for flag, value in values:
            if value:
                args.extend([flag, value])
        if self.backend == "linux" and self.linux_allow_physical_input:
            args.append("--linux-allow-physical-input")
        return args
