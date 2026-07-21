"""Backend factory: build the right :class:`~openadapt_flow.backend.Backend`
from a :class:`~openadapt_flow.deployment.BackendConfig`.

The CLI historically hardcoded the Playwright browser backend, leaving the
Windows (WAA) and RDP/remote-display backends library-only and unreachable from
``replay`` / ``run``. This factory is the single seam that turns a declarative
``BackendConfig`` (``kind`` + per-backend fields) into a live backend, so the
desktop / Citrix product path is drivable from the CLI.

It only *constructs* existing backend objects; it changes no backend behavior.
Selection is fail-loud by design: an unknown ``kind`` or a missing required
field raises :class:`ValueError` (mirroring
:func:`openadapt_flow.deployment.build_effect_verifier`) rather than silently
falling back to a different substrate — a wrong backend is a wrong action.

Backend dependencies (playwright, requests, aardwolf, pyobjc, AT-SPI) are imported
lazily inside each branch, so importing this module — and building any one
backend — never requires the others' extras.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

    from openadapt_flow.backend import Backend
    from openadapt_flow.backends.linux_backend import LinuxClient
    from openadapt_flow.backends.macos_backend import MacOSClient
    from openadapt_flow.backends.rdp_backend import RDPTransport
    from openadapt_flow.backends.remote_display import WindowClient
    from openadapt_flow.deployment import BackendConfig


def _normalize_kind(kind: Optional[str]) -> str:
    """Canonicalize a backend ``kind`` token (lower-cased, aliases folded)."""
    k = (kind or "web").strip().lower()
    if k in ("remote-display", "remote_display", "citrix"):
        # Convenience aliases: the remote-display client-window path is the
        # local variant of the `rdp` (pixel-only remote desktop) family.
        return "rdp"
    return k


def build_backend(
    cfg: "BackendConfig",
    *,
    page: Optional["Page"] = None,
    rdp_transport: Optional["RDPTransport"] = None,
    window_client: Optional["WindowClient"] = None,
    macos_client: Optional["MacOSClient"] = None,
    linux_client: Optional["LinuxClient"] = None,
) -> "Backend":
    """Construct the backend selected by ``cfg.kind``.

    Args:
        cfg: The resolved backend configuration (``kind`` + fields).
        page: A live Playwright ``Page`` — REQUIRED for ``kind: web`` (the
            browser lifecycle is owned by the caller, which navigates the page
            before handing it here). Ignored by the other kinds.
        rdp_transport: An injected :class:`RDPTransport` for ``kind: rdp``
            (network path). When omitted, a real ``AardwolfTransport`` is built
            from ``cfg.rdp_*``. Primarily a test seam (a live RDP server is
            otherwise required to construct the backend).
        window_client: An injected remote-display ``WindowClient`` for the
            ``kind: rdp`` local-window path. When omitted, the host's native
            client is used (macOS Quartz or Win32). Primarily a test seam.
        macos_client: An injected native macOS client for ``kind: macos``.
        linux_client: An injected native Linux/AT-SPI client for ``kind: linux``.

    Returns:
        A live backend implementing the :class:`Backend` protocol.

    Raises:
        ValueError: On an unknown ``kind`` or a missing required field for the
            selected backend (fail loud rather than drive the wrong substrate).
    """
    kind = _normalize_kind(cfg.kind)

    if kind == "web":
        if page is None:
            raise ValueError(
                "backend.kind 'web' needs a live Playwright page; the CLI "
                "launches the browser and passes it in"
            )
        from openadapt_flow.backends.playwright_backend import PlaywrightBackend

        return PlaywrightBackend(page)

    if kind == "windows":
        if not cfg.agent_url:
            raise ValueError(
                "backend.kind 'windows' requires backend.agent_url (the "
                "in-guest WAA agent base URL, e.g. --agent-url http://localhost:5001)"
            )
        from openadapt_flow.backends.windows_backend import WindowsBackend

        return WindowsBackend(
            cfg.agent_url,
            auth_token=cfg.agent_token,
            pin_fingerprint=cfg.agent_tls_pin,
        )

    if kind == "macos":
        if not cfg.macos_app:
            raise ValueError(
                "backend.kind 'macos' requires backend.macos_app "
                "(e.g. --macos-app TextEdit)"
            )
        from openadapt_flow.backends.macos_backend import MacOSBackend

        return MacOSBackend(
            macos_client,
            app=cfg.macos_app,
            window_title=cfg.macos_window_title,
        )

    if kind == "linux":
        if not cfg.linux_app:
            raise ValueError(
                "backend.kind 'linux' requires backend.linux_app "
                "(e.g. --linux-app gedit)"
            )
        if not cfg.linux_window_title:
            raise ValueError(
                "backend.kind 'linux' requires backend.linux_window_title "
                "(an exact top-level window title)"
            )
        from openadapt_flow.backends.linux_backend import LinuxBackend

        return LinuxBackend(
            linux_client,
            app=cfg.linux_app,
            window_title=cfg.linux_window_title,
            allow_physical_input=cfg.linux_allow_physical_input,
        )

    if kind == "rdp":
        return _build_rdp_backend(
            cfg, rdp_transport=rdp_transport, window_client=window_client
        )

    raise ValueError(
        "unknown backend.kind "
        f"{cfg.kind!r} (expected: web | windows | macos | linux | rdp)"
    )


def _build_rdp_backend(
    cfg: "BackendConfig",
    *,
    rdp_transport: Optional["RDPTransport"],
    window_client: Optional["WindowClient"],
) -> "Backend":
    """Build the ``rdp`` backend: network RDP (FreeRDP) or local client window.

    ``rdp_host`` selects a network RDP session over an
    :class:`AardwolfTransport`; ``rdp_window`` (no host) selects the local
    remote-display client-window backend (the faithful Citrix analog). Exactly
    one target must be given.
    """
    has_host = bool(cfg.rdp_host)
    has_window = bool(cfg.rdp_window)
    readiness_probe = _build_rdp_readiness_probe(cfg)

    if rdp_transport is not None or has_host:
        from openadapt_flow.backends.rdp_backend import FreeRDPBackend

        transport = rdp_transport
        if transport is None:
            from openadapt_flow.backends.rdp_backend import AardwolfTransport

            transport = AardwolfTransport.from_credentials(
                cfg.rdp_host or "",
                cfg.rdp_username or "",
                cfg.rdp_password or "",
                domain=cfg.rdp_domain,
                port=cfg.rdp_port,
            )
        return FreeRDPBackend(
            transport,
            max_frame_age_s=cfg.rdp_max_frame_age_s,
            readiness_probe=readiness_probe,
        )

    if window_client is not None or has_window:
        from openadapt_flow.backends.remote_display import RemoteDisplayBackend

        kwargs: dict[str, Any] = {}
        if cfg.rdp_window:
            kwargs["owner_substr"] = cfg.rdp_window
        if cfg.rdp_window_title:
            kwargs["title_substr"] = cfg.rdp_window_title
        kwargs["max_frame_age_s"] = cfg.rdp_max_frame_age_s
        kwargs["readiness_probe"] = readiness_probe
        return RemoteDisplayBackend(window_client, **kwargs)

    raise ValueError(
        "backend.kind 'rdp' requires backend.rdp_host (network RDP, e.g. "
        "--rdp-host 10.0.0.5) or backend.rdp_window (a local remote-display "
        "client window, e.g. a Citrix/Parallels window title)"
    )


def _build_rdp_readiness_probe(cfg: "BackendConfig"):
    """Build the configured current-frame OCR readiness predicate.

    The import stays lazy with the backend factory. A stable deployment-level
    text marker (for example application chrome) is intentionally distinct from
    target resolution: it is a coarse lock/login/disconnect/wrong-app refusal.
    """
    expected = (cfg.rdp_readiness_text or "").strip()
    if not expected:
        return None

    def _probe(png: bytes) -> bool:
        from openadapt_flow import vision

        return (
            vision.find_text(
                png, expected, min_ratio=float(cfg.rdp_readiness_min_ratio)
            )
            is not None
        )

    return _probe
