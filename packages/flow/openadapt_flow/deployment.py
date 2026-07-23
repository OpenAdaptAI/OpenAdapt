"""Deployment configuration: one YAML that wires a full openadapt-flow run.

A recorded-and-compiled bundle is portable, but *running it in production*
needs deployment-specific wiring the bundle deliberately does NOT carry: which
GUI URL to drive, which system of record to verify writes against, whether an
API actuation tier is available, whether the run is durable, and which safety
policy certifies it. Scattering those across a dozen CLI flags makes a real
deployment un-reviewable.

This module is the single, documented schema for that wiring. One
``deployment.yaml`` (see ``docs/deployment.example.yaml``) is read by
``record`` / ``compile`` / ``certify`` / ``replay`` / ``run`` / ``resume`` so
the same backend / actuation / effects / runtime / policy configuration drives
every stage.

The config only *constructs and injects* existing library objects
(``EffectVerifier`` subclasses, ``ApiActuator``); it changes no library
behavior. Every section is optional — an empty file is a valid (fully-default,
fully-local, zero-egress) deployment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional

from pydantic import BaseModel, Field, field_validator

# Import-light (pydantic only): the effect CONTRACT types double as the
# declarative config vocabulary, so a deployment YAML binds run parameters
# with the exact ``{param: ...}`` / ``{literal: ...}`` form bundles use.
from openadapt_flow.runtime.effects.auth import AuthRef
from openadapt_flow.runtime.effects.effect import Region, ValueExpr


class BackendConfig(BaseModel):
    """Where and how to drive the target application's GUI.

    ``kind`` selects the concrete :class:`~openadapt_flow.backend.Backend` the
    CLI drives (built by :func:`openadapt_flow.backends.factory.build_backend`):

    - ``web`` (default) — the Playwright/Chromium browser backend. Reproduces
      the historical behavior exactly (``url`` / ``headed`` are browser fields);
      every other field here is ignored.
    - ``windows`` — the WAA (Windows Agent Arena) HTTP backend for native
      Windows desktops. Requires ``agent_url`` (the in-guest agent's base URL);
      ``agent_token`` authenticates a token-protected agent.
    - ``macos`` — a local native macOS application window. Requires
      ``macos_app``; ``macos_window_title`` should uniquely identify the target
      window when the application owns more than one.
    - ``linux`` — one exact local Linux application window through AT-SPI.
      Requires ``linux_app`` and ``linux_window_title``. Native accessibility
      actions are preferred; global pointer/keyboard fallback is opt-in.
    - ``rdp`` — a pixel-only network-RDP or local remote-display backend.
      ``rdp_host`` drives a network RDP session (FreeRDP/aardwolf);
      ``rdp_window`` instead drives a local remote-display client window.
    - ``citrix`` — the local Citrix Workspace/Viewer window preset over the
      remote-display backend. It defaults the owner selector for the host OS,
      accepts ``rdp_window`` / ``rdp_window_title`` overrides, and refuses an
      ``rdp_host`` rather than silently constructing the wrong RDP substrate.

    Every non-``web`` field is optional and defaults to None, so an existing
    ``web`` deployment (or an empty config) is byte-for-byte unchanged.
    """

    #: Which backend to drive:
    #: ``web`` (default) | ``windows`` | ``macos`` | ``linux`` | ``rdp`` |
    #: ``citrix``.
    kind: str = "web"

    #: The GUI URL to drive (the app under automation). None => the caller's
    #: own default (e.g. ``replay`` serves the bundled MockMed demo).
    #: ``web`` only.
    url: Optional[str] = None
    #: Run the browser headed (visible). Default headless. ``web`` only.
    headed: bool = False

    # -- windows (WAA HTTP agent) --------------------------------------------
    #: Base URL of the in-guest WAA agent (e.g. ``http://localhost:5001`` over
    #: the standard SSH tunnel). REQUIRED for ``kind: windows``.
    agent_url: Optional[str] = None
    #: Optional bearer token for a token-authenticated WAA agent (sent as
    #: ``Authorization: Bearer <token>``). None => no auth header (loopback).
    agent_token: Optional[str] = None
    #: SHA-256 fingerprint of the in-guest agent's TLS certificate. When set,
    #: the Windows backend pins the exact certificate before sending screenshots
    #: or UIA/input requests; a mismatch fails at the TLS handshake.
    agent_tls_pin: Optional[str] = None

    # -- native macOS --------------------------------------------------------
    #: Owner application name/substring (e.g. ``"TextEdit"``). REQUIRED for
    #: ``kind: macos``.
    macos_app: Optional[str] = None
    #: Window-title substring. The backend refuses ambiguous matches rather
    #: than selecting the first window.
    macos_window_title: Optional[str] = None

    # -- native Linux / AT-SPI -----------------------------------------------
    #: Exact case-insensitive AT-SPI application name. REQUIRED for
    #: ``kind: linux``.
    linux_app: Optional[str] = None
    #: Exact case-insensitive top-level AT-SPI window title. REQUIRED for
    #: ``kind: linux``; zero or multiple matches are refused.
    linux_window_title: Optional[str] = None
    #: Permit window-bound X11 pointer/keyboard synthesis when the uniquely
    #: resolved target exposes no suitable native AT-SPI action. Disabled by
    #: default; consequential deployments qualify and enable it explicitly.
    linux_allow_physical_input: bool = False

    # -- rdp (network RDP via FreeRDP/aardwolf) ------------------------------
    #: RDP host/IP for a network RDP session. REQUIRED for ``kind: rdp`` unless
    #: ``rdp_window`` is given instead (the local remote-display path).
    rdp_host: Optional[str] = None
    rdp_username: Optional[str] = None
    rdp_password: Optional[str] = None
    rdp_domain: Optional[str] = None
    rdp_port: int = 3389
    #: A resolved coordinate/input lease older than this is refused. The backend
    #: intentionally halts so the governed runtime can capture and re-resolve.
    rdp_max_frame_age_s: float = Field(default=10.0, gt=0)
    #: Optional text that must be OCR-visible on the CURRENT remote frame before
    #: each input. Deployments use stable app chrome here to reject lock/login/
    #: disconnect or wrong-application screens. None leaves the generic hook
    #: unwired and is not suitable for a governed consequential RDP write.
    rdp_readiness_text: Optional[str] = None
    rdp_readiness_min_ratio: float = Field(default=0.85, ge=0.0, le=1.0)

    # -- rdp/citrix (local remote-display client window) ---------------------
    #: Exact case-insensitive owner-app name of the local client WINDOW to drive
    #: (e.g. ``"Citrix Workspace"`` / ``"Parallels Desktop"``). When set (and
    #: ``rdp_host`` is not), the backend captures and injects into that on-screen
    #: window instead of opening a network RDP session (macOS host).
    rdp_window: Optional[str] = None
    #: Optional exact case-insensitive title. Zero or multiple exact matches are
    #: refused; the backend never selects a largest partial match.
    rdp_window_title: Optional[str] = None


class EffectsConfig(BaseModel):
    """Which system of record to verify consequential writes against.

    ``kind`` selects the concrete
    :class:`~openadapt_flow.runtime.effects.EffectVerifier`. ``none`` (default)
    wires no verifier — a bundle that declares NO effects then replays exactly
    as before, but a step that DOES declare effects HALTs (fail-safe: an
    unverifiable consequential write is never silently accepted).

    Effect-verifier kit conventions (``docs/EFFECT_KIT.md``):

    - **Secrets are references, never literals.** ``auth`` /
      ``access_token_env`` / ``sql_password_env`` name environment variables
      staged by the operator; a missing variable fails LOUD at construction.
    - **Run-parameter binding is explicit.** Every ``*_params`` /
      ``*_exprs`` mapping takes the same ``{param: name}`` / ``{literal: v}``
      :class:`~openadapt_flow.runtime.effects.ValueExpr` form bundles use (a
      bare string is a literal), and is resolved against the governed run
      parameters (``--params-file`` / ``--param``) when the verifier is
      BUILT — so one bundle + one deployment YAML ships with its verification
      bound to the record each run actually writes.
    """

    #: ``none`` | ``onscreen`` | ``rest`` | ``fhir`` | ``sql`` | ``file`` |
    #: ``document-hash``. ``onscreen`` is the no-API screen read-back oracle
    #: (the auto-derived default for GUI-only recordings); it reads the saved
    #: value off the live backend, so it needs no external system of record.
    kind: str = "none"

    # -- onscreen (no-API screen read-back; auto-derived per-effect region) ---
    #: Explicit read-back region ``(x, y, w, h)`` for a hand-configured
    #: deployment. Normally left None — the compiler auto-derives a per-effect
    #: region into each effect's ``ReadbackSpec`` (this is only the fallback
    #: for an effect that carries no region of its own).
    readback_region: Optional[Region] = None
    #: Fuzzy-match floor for accepting an OCR read-back as the expected value.
    readback_min_ratio: float = 0.9

    # -- rest (JSON REST system of record, e.g. MockMed /api/db) -------------
    base_url: Optional[str] = None
    records_path: str = "/api/db"
    records_key: Optional[str] = "records"
    #: Secret-isolated auth headers for the records read (bearer_env /
    #: header+value_env / basic_env). Never a credential literal.
    auth: Optional[AuthRef] = None
    #: Values for ``{placeholder}``s in ``records_path``, resolved (and
    #: URL-quoted) at construction — e.g.
    #: ``records_path: "/api/resource/Loan Application?filters=...{applicant}..."``
    #: with ``path_params: {applicant: {param: applicant}}``. Empty -> the
    #: path is used verbatim (no formatting), byte-identical to before.
    path_params: dict[str, ValueExpr] = Field(default_factory=dict)

    # -- fhir (FHIR R4 search, e.g. OpenEMR) ---------------------------------
    resource_type: str = "Observation"
    search_params: dict[str, str] = Field(default_factory=dict)
    field_paths: Optional[dict[str, str]] = None
    #: OAuth2 bearer token literal. DEPRECATED for committed configs — prefer
    #: ``access_token_env`` so the YAML never carries the secret.
    access_token: Optional[str] = None
    #: Name of the env var holding the OAuth2 bearer token (secret-isolated;
    #: wins over ``access_token``; missing var fails loud).
    access_token_env: Optional[str] = None
    verify_tls: bool = True
    #: FHIR search params whose VALUES bind to run parameters (merged over
    #: ``search_params`` after resolution) — e.g.
    #: ``search_param_exprs: {patient: {param: patient_id}}``.
    search_param_exprs: dict[str, ValueExpr] = Field(default_factory=dict)

    # -- sql (read-only SELECT against the app database) ---------------------
    #: The single read-only SELECT returning candidate records (validated by
    #: the kit's statement whitelist; a mutating query refuses to construct).
    #: Use the driver's native parameter placeholders for dynamic values.
    sql_query: Optional[str] = None
    #: DB-API parameter values for ``sql_query`` (literal or ``{param: ...}``).
    sql_query_params: dict[str, ValueExpr] = Field(default_factory=dict)
    #: Path to a SQLite database file (stdlib driver; the CI-proven path).
    sqlite_database: Optional[str] = None
    #: Alternatively: an importable DB-API driver module name (``pymysql``,
    #: ``mariadb``, ``psycopg``) ...
    sql_driver: Optional[str] = None
    #: ... plus its non-secret ``connect(**kwargs)`` arguments ...
    sql_connect_args: dict[str, Any] = Field(default_factory=dict)
    #: ... and the env var holding the database password (injected as the
    #: ``password`` connect kwarg; secret-isolated, fails loud when missing).
    sql_password_env: Optional[str] = None

    # -- file (file / SFTP arrival; local directory via YAML) ----------------
    #: (uses the shared ``root``) filename glob for candidate records.
    file_pattern: str = "*"
    #: Minimum byte size for a conforming arrival (size > 0 by default).
    file_min_size: int = 1
    #: Freshness window in seconds (``fresh`` flag); None -> always fresh.
    file_mtime_window_s: Optional[float] = None
    #: Optional regex probed against each candidate's leading content.
    file_content_probe: Optional[str] = None

    # -- document-hash (filesystem document store) ---------------------------
    root: Optional[str] = None
    glob: str = "*"

    # -- shared --------------------------------------------------------------
    timeout_s: float = 5.0
    poll_interval_s: float = 0.2

    @field_validator(
        "path_params", "search_param_exprs", "sql_query_params", mode="before"
    )
    @classmethod
    def _coerce_value_exprs(cls, v: Any) -> Any:
        """A bare YAML string is a literal (the same coercion ``Effect`` uses)."""
        if isinstance(v, dict):
            return {
                k: ({"literal": val} if isinstance(val, str) else val)
                for k, val in v.items()
            }
        return v


class ActuationConfig(BaseModel):
    """The API/tool actuation tier (top of the capability ladder).

    When enabled, a step carrying an ``ir.ApiBinding`` has its write PERFORMED
    via the API (deterministic, $0, no GUI) and confirmed by the effect
    verifier. Disabled (default) => every step actuates through the GUI ladder.
    """

    #: Wire an :class:`~openadapt_flow.runtime.actuators.ApiActuator`.
    api: bool = False
    #: API base URL for relative ``ApiBinding.url_template``s.
    base_url: str = ""
    timeout_s: float = 5.0


class GroundingModelConfig(BaseModel):
    """Operator-selected ("bring your own") model-grounding endpoint.

    The last rung of the resolution ladder is a Grounder that PROPOSES a click
    point when the deterministic rungs abstain. This section lets a deployment
    point that fallback rung at a model of its choice instead of the built-in
    Anthropic default: a self-hosted vLLM/Ollama, an OpenRouter/Azure/Bedrock
    proxy, or the Anthropic API directly.

    Two hard invariants:

    * **Configuring a model NEVER enables egress by itself.** This section only
      names WHICH model would be used; whether ANYTHING leaves the box is still
      governed entirely by ``allow_model_grounding`` (default False => fully
      local). An enabled model with egress off stays dormant.
    * **The API key is a reference, never a literal.** ``api_key_env`` names the
      environment variable holding the key; the key itself is never stored in
      the config or in the repository.

    Fail-safe: the constructed grounder only ever proposes a point; the identity
    band and risk gate still dispose before any click, and any endpoint error
    yields no proposal (the ladder halts).
    """

    #: Off by default. When False, no configured model grounder is built and the
    #: on-prem appliance (if any) remains the only VLM fallback. Setting True
    #: still does nothing unless ``allow_model_grounding`` is also True.
    enabled: bool = False
    #: ``anthropic`` (the built-in Anthropic API path) | ``openai_compatible``
    #: (any ``{base_url}/chat/completions`` vision endpoint: OpenRouter, Azure
    #: OpenAI, a Bedrock/OpenAI proxy, self-hosted vLLM / Ollama / LM Studio).
    provider: Literal["anthropic", "openai_compatible"] = "anthropic"
    #: Root URL of an ``openai_compatible`` endpoint (``/chat/completions`` is
    #: appended). Required for ``openai_compatible``; ignored for ``anthropic``.
    base_url: str = ""
    #: The vision-capable model id to request. For ``anthropic`` an empty value
    #: uses the shipped default; for ``openai_compatible`` it is required.
    model: str = ""
    #: NAME of the environment variable holding the API key (never the key
    #: itself). Empty => no auth header sent (a loopback vLLM/Ollama needs none);
    #: for ``anthropic`` an empty value falls back to the SDK's ``ANTHROPIC_API_KEY``.
    api_key_env: str = ""


class RuntimeSection(BaseModel):
    """Runtime posture: durability and model-egress opt-in."""

    #: Tier-3 durable runtime: checkpoint each verified step, durably pause on
    #: halt, resumable via ``resume``. Off by default.
    durable: bool = False
    #: EGRESS OPT-IN (PHI audit REM-3): permit wiring an off-box model
    #: grounder / identity-VLM / state-verifier. Off by default => fully local,
    #: zero outbound calls.
    allow_model_grounding: bool = False
    #: Arm the pixel-compare identity tier's VERIFY branch (experimental,
    #: evidence-gated). Off by default => the pixel tier only ever HALTs on a
    #: localized glyph change or ABSTAINs; it never pixel-VERIFIES (a
    #: would-be verify falls through to the next tier). See
    #: :func:`openadapt_flow.runtime.identity.verify_pixel_identity` and
    #: docs/LIMITS.md.
    pixel_verify_enabled: bool = False
    #: Operator-selected ("bring your own") model-grounding endpoint (the VLM
    #: fallback rung). Off by default; configuring it never enables egress on its
    #: own (``allow_model_grounding`` still governs whether anything leaves).
    grounding_model: GroundingModelConfig = Field(default_factory=GroundingModelConfig)
    #: PHI-mode egress allowlist (fail-closed). Admin-attested hostnames (or
    #: URLs) for model-grounding endpoints. When PHI mode is active, a configured
    #: grounding endpoint whose host is NOT listed here is REFUSED — the run
    #: stays fully local. Empty (default) => no endpoint may egress under PHI.
    #: Non-PHI runs ignore this list (the normal egress opt-in governs).
    phi_grounding_allowlist: list[str] = Field(default_factory=list)
    #: PHI-mode attestation that a Business Associate Agreement (or equivalent)
    #: covers the known public aggregators (OpenRouter, OpenAI, Anthropic,
    #: Google). Default False => those aggregators are BLOCKED under PHI even if
    #: allowlisted. Non-aggregator allowlisted endpoints do not need it.
    phi_egress_attested: bool = False


class PolicySection(BaseModel):
    """The safety policy that certifies a bundle for this deployment."""

    #: Policy YAML path, or a shipped built-in name (permissive, clinical-write).
    policy: Optional[str] = None


class DeploymentConfig(BaseModel):
    """The whole-deployment configuration (one ``deployment.yaml``)."""

    #: Human-readable deployment name (audit / logs only).
    name: str = "deployment"
    backend: BackendConfig = Field(default_factory=BackendConfig)
    actuation: ActuationConfig = Field(default_factory=ActuationConfig)
    effects: EffectsConfig = Field(default_factory=EffectsConfig)
    runtime: RuntimeSection = Field(default_factory=RuntimeSection)
    policy: PolicySection = Field(default_factory=PolicySection)


def load_deployment(source: str | Path) -> DeploymentConfig:
    """Load a :class:`DeploymentConfig` from a YAML file.

    Raises:
        FileNotFoundError: If ``source`` is not an existing file.
        ValueError: If the YAML is malformed or violates the schema.
    """
    import yaml

    path = Path(source)
    if not path.is_file():
        raise FileNotFoundError(f"deployment config {source!r} is not an existing file")
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:  # pragma: no cover - passthrough
        raise ValueError(f"could not parse deployment YAML {path}: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(
            f"deployment config {path} must be a YAML mapping, got "
            f"{type(data).__name__}"
        )
    try:
        return DeploymentConfig.model_validate(data)
    except Exception as e:
        raise ValueError(f"invalid deployment config {path}: {e}") from e


# --------------------------------------------------------------------------
# Bring-your-own model grounding — construction + fail-closed PHI enforcement.
# --------------------------------------------------------------------------

_DEFAULT_ANTHROPIC_HOST = "api.anthropic.com"

# Public multi-tenant aggregators. BLOCKED under PHI mode even if allowlisted,
# unless the operator sets ``phi_egress_attested`` (an admin attestation that a
# BAA / equivalent agreement covers the destination). Fail-closed default: no
# PHI screenshot reaches a shared public endpoint without an explicit
# attestation, whatever the allowlist says.
_PHI_AGGREGATOR_DENYLIST = frozenset(
    {
        "openrouter.ai",
        "api.openai.com",
        "api.anthropic.com",
        "generativelanguage.googleapis.com",
    }
)


def _host_of(url_or_host: str) -> str:
    """Extract a lowercase hostname from a URL or a bare host[:port] string.

    Fail-closed helper: returns "" when nothing host-like can be parsed, so the
    caller treats an unparseable endpoint as not-on-the-allowlist (refused).
    """
    from urllib.parse import urlsplit

    raw = (url_or_host or "").strip()
    if not raw:
        return ""
    candidate = raw if "//" in raw else f"//{raw}"
    host = urlsplit(candidate).hostname or ""
    return host.lower()


def phi_grounding_endpoint_allowed(
    host: str, *, allowlist: list[str], attested: bool
) -> tuple[bool, str]:
    """Decide whether a grounding endpoint may egress under PHI mode.

    Fail-closed. Returns ``(allowed, reason)`` where ``reason`` is a
    human-readable refusal message when ``allowed`` is False.

    Rules (every wired PHI endpoint must clear ALL of them):

    1. The host must be resolvable and present on the attested ``allowlist``.
       Ambiguity (an empty/unparseable host) or absence => refuse.
    2. A host on the public-aggregator denylist additionally requires
       ``attested`` (a BAA attestation). Without it => refuse, even if the host
       was allowlisted.
    """
    h = (host or "").strip().lower()
    if not h:
        return (False, "grounding endpoint host could not be determined")
    allow = {_host_of(entry) for entry in allowlist}
    allow.discard("")
    if h not in allow:
        return (False, f"grounding endpoint {h} is not on the attested allowlist")
    if h in _PHI_AGGREGATOR_DENYLIST and not attested:
        return (
            False,
            f"grounding endpoint {h} is a public aggregator blocked in PHI mode "
            "without an explicit phi_egress_attested BAA attestation",
        )
    return (True, "")


def _phi_mode_active(explicit: Optional[bool] = None) -> bool:
    """Resolve PHI mode without importing the heavy hosted module.

    Mirrors ``hosted._phi_mode``: explicit arg -> ``OPENADAPT_FLOW_PHI_MODE``
    env -> ``OPENADAPT_FLOW_SCRUB=on``. Any failure resolves to False (PHI
    enforcement then does not apply and the normal egress opt-in governs).
    """
    if explicit is not None:
        return explicit
    import os

    env = os.environ.get("OPENADAPT_FLOW_PHI_MODE")
    if env is not None:
        return env.strip().lower() in ("1", "true", "yes", "on")
    try:
        from openadapt_flow import privacy

        return privacy.scrub_mode() == "on"
    except Exception:
        return False


def _grounding_model_host(cfg: GroundingModelConfig) -> str:
    """The egress host a configured grounding model would reach.

    ``anthropic`` always reaches the Anthropic API host; ``openai_compatible``
    reaches the host of its ``base_url``.
    """
    if cfg.provider == "anthropic":
        return _DEFAULT_ANTHROPIC_HOST
    return _host_of(cfg.base_url)


def build_model_grounder(cfg: GroundingModelConfig) -> Optional[Any]:
    """Construct the configured "bring your own" model grounder, or None.

    Returns None (and prints a clear reason) when the section is disabled, its
    provider dependency is missing, or its configuration is incomplete —
    degrading to fully local rather than raising. The caller is responsible for
    the egress opt-in and PHI allowlist checks BEFORE wiring the result.
    """
    if not cfg.enabled:
        return None
    provider = (cfg.provider or "anthropic").strip().lower()

    import os

    api_key = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""

    if provider == "anthropic":
        from openadapt_flow.runtime.grounder import _DEFAULT_MODEL, AnthropicGrounder

        model = (cfg.model or "").strip() or _DEFAULT_MODEL
        try:
            client = None
            if api_key:
                import anthropic

                client = anthropic.Anthropic(api_key=api_key)
            return AnthropicGrounder(model=model, client=client)
        except ImportError:
            print(
                "grounding_model.provider 'anthropic' requires the 'anthropic' "
                "package (pip install 'openadapt-flow[grounder]'); replaying "
                "FULLY LOCAL."
            )
            return None

    if provider == "openai_compatible":
        if not cfg.base_url.strip():
            print(
                "grounding_model.provider 'openai_compatible' requires a "
                "base_url; replaying FULLY LOCAL."
            )
            return None
        from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder

        try:
            return OpenAICompatibleGrounder(
                base_url=cfg.base_url, model=cfg.model, api_key=api_key
            )
        except (ValueError, ImportError) as exc:
            print(
                f"grounding_model 'openai_compatible' not wired ({exc}); "
                "replaying FULLY LOCAL."
            )
            return None

    print(
        f"unknown grounding_model.provider {cfg.provider!r} "
        "(expected: anthropic | openai_compatible); replaying FULLY LOCAL."
    )
    return None


def build_replayer(
    backend: Any,
    *,
    allow_egress: bool,
    effect_verifier: Any,
    api_actuator: Any,
    durable: bool,
    use_structural: bool,
    pixel_verify_enabled: bool = False,
    governed_authorization: Any = None,
    runtime_config: Optional["RuntimeSection"] = None,
    phi_mode: Optional[bool] = None,
) -> Any:
    """Wire one deployment-qualified backend into the governed Replayer.

    The caller owns the backend lifecycle. Keeping this construction in the
    public deployment module lets CLI, console, and embedding services share
    one egress/grounding/effect/actuation posture without importing CLI-private
    helpers.

    ``runtime_config`` (the deployment's ``runtime`` section) carries the
    optional operator-selected model grounder (``grounding_model``) and the
    fail-closed PHI allowlist (``phi_grounding_allowlist`` /
    ``phi_egress_attested``). When absent, the behavior is exactly the historic
    one: the on-prem appliance is the only VLM fallback and no model grounder is
    configured.
    """
    import os

    from openadapt_flow.runtime import Replayer
    from openadapt_flow.runtime.grounder import build_grounder
    from openadapt_flow.runtime.remote_vlm import appliance_from_env

    grounding_cfg = (
        runtime_config.grounding_model
        if runtime_config is not None
        else GroundingModelConfig()
    )
    allowlist = (
        list(runtime_config.phi_grounding_allowlist)
        if runtime_config is not None
        else []
    )
    attested = (
        bool(runtime_config.phi_egress_attested)
        if runtime_config is not None
        else False
    )
    phi_on = _phi_mode_active(phi_mode)

    # The on-screen read-back verifier reads the SAME live backend the replay
    # drives; it is constructed backend-less (the backend does not exist at
    # config time) and bound here. Never a system-of-record egress — the
    # backend is the local screen it already drives.
    if effect_verifier is not None and hasattr(effect_verifier, "bind_backend"):
        effect_verifier.bind_backend(backend)

    appliance = appliance_from_env()
    if appliance is not None and not allow_egress:
        print(
            "On-prem VLM appliance is configured "
            f"({os.environ.get('OPENADAPT_FLOW_VLM_URL')}) but NOT "
            "wired: enable model grounding to send screenshots to it. "
            "Replaying FULLY LOCAL (zero outbound calls)."
        )
        appliance = None
    # PHI fail-closed: a configured on-prem appliance whose host is not attested
    # is dropped entirely (identity veto tier + grounder + state verifier), so no
    # PHI screenshot reaches it.
    if appliance is not None and phi_on:
        host = _host_of(os.environ.get("OPENADAPT_FLOW_VLM_URL", ""))
        ok, reason = phi_grounding_endpoint_allowed(
            host, allowlist=allowlist, attested=attested
        )
        if not ok:
            print(f"PHI mode: {reason}; replaying FULLY LOCAL.")
            appliance = None
    if appliance is not None:
        print(
            "Using on-prem VLM appliance at "
            f"{os.environ.get('OPENADAPT_FLOW_VLM_URL')} "
            "(identity veto tier + remote-VLM grounder fallback; "
            "fail-safe to halt). WARNING: screenshots WILL leave "
            "the box for this run (model grounding is enabled)."
        )

    # Operator-selected ("bring your own") model grounder. Only ever built when
    # egress is opted in; PHI mode further requires the endpoint be on the
    # attested allowlist (and, for public aggregators, an explicit attestation).
    model_grounder: Optional[Any] = None
    if grounding_cfg.enabled:
        if not allow_egress:
            print(
                "grounding_model is configured but model grounding is not "
                "enabled (runtime.allow_model_grounding=false); replaying FULLY "
                "LOCAL (zero outbound calls)."
            )
        else:
            wire = True
            if phi_on:
                host = _grounding_model_host(grounding_cfg)
                ok, reason = phi_grounding_endpoint_allowed(
                    host, allowlist=allowlist, attested=attested
                )
                if not ok:
                    print(f"PHI mode: {reason}; replaying FULLY LOCAL.")
                    wire = False
            if wire:
                model_grounder = build_model_grounder(grounding_cfg)
                if model_grounder is not None:
                    print(
                        "Model grounder wired: "
                        f"{type(model_grounder).__name__} "
                        f"(provider={grounding_cfg.provider}). WARNING: "
                        "screenshots WILL leave the box for this run."
                    )

    # The configured model grounder takes precedence as the VLM fallback; the
    # on-prem appliance grounder is used only when no model grounder is wired.
    fallback = model_grounder
    if fallback is None and appliance is not None:
        fallback = appliance.grounder
    grounder = build_grounder(fallback=fallback)
    if grounder is not None:
        print(f"Grounding rung active: {type(grounder).__name__}")
    return Replayer(
        backend,
        grounder=grounder,
        identity_vlm=appliance.identity_vlm if appliance else None,
        state_verifier=(appliance.state_verifier if appliance else None),
        allow_model_grounding=allow_egress,
        effect_verifier=effect_verifier,
        api_actuator=api_actuator,
        durable=durable,
        use_structural=use_structural,
        pixel_verify_enabled=pixel_verify_enabled,
        governed_authorization=governed_authorization,
    )


def _resolve_config_exprs(
    section: str,
    exprs: Mapping[str, ValueExpr],
    params: Optional[Mapping[str, str]],
) -> dict[str, str]:
    """Resolve a config's ``ValueExpr`` mapping against the run's params.

    Fail-safe is fail-LOUD here: a ``{param: ...}`` reference that the run did
    not supply raises (the verifier would otherwise silently probe the wrong
    record), and the message names the missing parameter.
    """
    resolved: dict[str, str] = {}
    for key, expr in exprs.items():
        value = expr.resolve(dict(params or {}))
        if value is None:
            raise ValueError(
                f"effects.{section}[{key!r}] references run parameter "
                f"{expr.param!r}, which was not supplied — pass it via "
                "--param / --params-file (a verifier is never wired against "
                "an unresolved selector)"
            )
        resolved[key] = value
    return resolved


def _require_env(name: str, what: str) -> str:
    """Read a secret env var named by the config; fail loud when absent."""
    import os

    value = os.environ.get(name, "")
    if not value:
        raise ValueError(
            f"{what} references environment variable {name!r}, which is not "
            "set (or empty) — refusing to wire an unauthenticated/broken "
            "effect verifier"
        )
    return value


def build_effect_verifier(
    cfg: EffectsConfig, params: Optional[Mapping[str, str]] = None
) -> Optional[Any]:
    """Construct the configured ``EffectVerifier`` (or None for ``kind: none``).

    Args:
        cfg: The deployment's ``effects`` section.
        params: The governed run parameters (``--params-file`` / ``--param``
            values). Configs that bind ``{param: ...}`` references
            (``path_params`` / ``search_param_exprs`` / ``sql_query_params``)
            are resolved against these AT CONSTRUCTION, so the verifier probes
            the record THIS run writes. A config with no references ignores
            ``params`` entirely (fully back-compatible).

    Raises:
        ValueError: on an unknown ``kind``, a missing required field, an
            unresolved ``{param: ...}`` reference, or a missing secret env var
            (fail loud rather than wire a broken verifier).
    """
    kind = (cfg.kind or "none").strip().lower()
    if kind in ("none", ""):
        return None

    if kind == "onscreen":
        # No-API screen read-back oracle. The live backend is bound later (it
        # does not exist at config-build time) by ``build_replayer`` via
        # ``bind_backend``; each mined effect carries its own read-back region
        # and (for the different-path variant) re-navigation.
        from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier

        return OnScreenReadbackVerifier(
            backend=None,
            region=cfg.readback_region,
            min_ratio=cfg.readback_min_ratio,
        )

    if kind == "rest":
        if not cfg.base_url:
            raise ValueError("effects.kind 'rest' requires effects.base_url")
        from openadapt_flow.runtime.effects import RestRecordVerifier

        headers = cfg.auth.resolve_headers() if cfg.auth is not None else None
        records_path = cfg.records_path
        if cfg.path_params:
            from urllib.parse import quote

            values = {
                key: quote(value, safe="")
                for key, value in _resolve_config_exprs(
                    "path_params", cfg.path_params, params
                ).items()
            }
            try:
                records_path = records_path.format(**values)
            except (KeyError, IndexError, ValueError) as e:
                raise ValueError(
                    f"effects.records_path template does not match "
                    f"effects.path_params: {e!r}"
                ) from e
        return RestRecordVerifier(
            cfg.base_url,
            records_path=records_path,
            records_key=cfg.records_key,
            headers=headers,
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "fhir":
        if not cfg.base_url:
            raise ValueError("effects.kind 'fhir' requires effects.base_url")
        from openadapt_flow.runtime.effects import FhirEffectVerifier

        access_token = cfg.access_token
        if cfg.access_token_env:
            access_token = _require_env(
                cfg.access_token_env, "effects.access_token_env"
            )
        search_params = dict(cfg.search_params)
        if cfg.search_param_exprs:
            search_params.update(
                _resolve_config_exprs(
                    "search_param_exprs", cfg.search_param_exprs, params
                )
            )
        return FhirEffectVerifier(
            cfg.base_url,
            resource_type=cfg.resource_type,
            search_params=search_params or None,
            field_paths=cfg.field_paths,
            access_token=access_token,
            verify_tls=cfg.verify_tls,
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "sql":
        if not cfg.sql_query:
            raise ValueError("effects.kind 'sql' requires effects.sql_query")
        from openadapt_flow.runtime.effects import SqlRecordVerifier

        connect: Callable[[], Any]
        if cfg.sqlite_database:
            import sqlite3

            database = cfg.sqlite_database
            connect = lambda: sqlite3.connect(database)  # noqa: E731
        elif cfg.sql_driver:
            import importlib

            module = importlib.import_module(cfg.sql_driver)
            connect_args = dict(cfg.sql_connect_args)
            if cfg.sql_password_env:
                connect_args["password"] = _require_env(
                    cfg.sql_password_env, "effects.sql_password_env"
                )
            connect = lambda: module.connect(**connect_args)  # noqa: E731
        else:
            raise ValueError(
                "effects.kind 'sql' requires effects.sqlite_database or "
                "effects.sql_driver (+ sql_connect_args)"
            )
        return SqlRecordVerifier(
            connect,
            cfg.sql_query,
            query_params=(
                _resolve_config_exprs("sql_query_params", cfg.sql_query_params, params)
                or None
            ),
            timeout_s=cfg.timeout_s,
            poll_interval_s=cfg.poll_interval_s,
        )

    if kind == "file":
        if not cfg.root:
            raise ValueError("effects.kind 'file' requires effects.root")
        from openadapt_flow.runtime.effects import FileArrivalVerifier

        return FileArrivalVerifier(
            cfg.root,
            pattern=cfg.file_pattern,
            min_size=cfg.file_min_size,
            mtime_window_s=cfg.file_mtime_window_s,
            content_probe=cfg.file_content_probe,
        )

    if kind in ("document-hash", "document_hash", "doc-hash"):
        if not cfg.root:
            raise ValueError("effects.kind 'document-hash' requires effects.root")
        from openadapt_flow.runtime.effects import DocumentHashVerifier

        return DocumentHashVerifier(cfg.root, glob=cfg.glob)

    raise ValueError(
        f"unknown effects.kind {cfg.kind!r} "
        "(expected: none | onscreen | rest | fhir | sql | file | document-hash)"
    )


def build_api_actuator(cfg: ActuationConfig) -> Optional[Any]:
    """Construct the configured ``ApiActuator`` (or None when ``api`` is off)."""
    if not cfg.api:
        return None
    from openadapt_flow.runtime.actuators import ApiActuator

    return ApiActuator(cfg.base_url, timeout_s=cfg.timeout_s)
