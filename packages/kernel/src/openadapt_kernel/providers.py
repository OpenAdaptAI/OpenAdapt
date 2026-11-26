"""Dynamic, zero-hardcoded-SDK provider registry and stdlib OpenAI-compatible HTTP client."""

from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Literal, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlparse

from openadapt_kernel.egress_guard import EgressBlockedError, EgressPolicy, LocalEgressGuard

ProviderKind = Literal["coding_agent", "llm", "edge_lora", "desktop"]
VALID_PROVIDER_KINDS: FrozenSet[str] = frozenset({"coding_agent", "llm", "edge_lora", "desktop"})
_LOOPBACK_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "localhost", "::1"})


class OptionalDependencyError(ImportError):
    """Raised when an optional provider backend dependency or OS bridge is unavailable."""

    def __init__(self, package: str, *, extra: str = "", install_hint: str = "") -> None:
        self.package = package
        self.extra = extra
        hint = install_hint or (f"pip install openadapt-kernel[{extra}]" if extra else f"pip install {package}")
        self.install_hint = hint
        super().__init__(f"Optional dependency {package!r} is required. Install with: {hint}")


def is_loopback_url(url: str) -> bool:
    """Return True only for http(s) URLs targeting local loopback interfaces."""
    parsed = urlparse((url or "").strip())
    return parsed.scheme in ("http", "https") and (parsed.hostname or "").lower() in _LOOPBACK_HOSTS


@dataclass(frozen=True)
class ProviderSpec:
    kind: ProviderKind
    name: str
    factory: Callable[..., Any]
    description: str = ""
    builtin: bool = False


class OpenAICompatHTTPProvider:
    """Zero-dependency stdlib HTTP client for any OpenAI-compatible /v1/chat/completions endpoint."""

    def __init__(
        self, *, base_url: str, model: str = "default", api_key: str = "", timeout_sec: float = 30.0,
        egress_policy: Optional[EgressPolicy] = None, ledger_path: Optional[Path] = None, allow_local_phi: bool = False,
    ) -> None:
        self.base_url = (base_url or "").strip().rstrip("/")
        if not self.base_url:
            raise ValueError("base_url is required for OpenAICompatHTTPProvider")
        if allow_local_phi and not is_loopback_url(self.base_url):
            raise ValueError("allow_local_phi=True is permitted only for loopback (127.0.0.1/localhost) endpoints")
        self.model, self.api_key = model, api_key or os.environ.get("OPENAI_API_KEY", "")
        self.timeout_sec, self.allow_local_phi = timeout_sec, allow_local_phi
        self._egress_policy = egress_policy or EgressPolicy()
        self._ledger_path = ledger_path or (Path(tempfile.mkdtemp(prefix="oa_provider_egress_")) / "egress.jsonl")

    def _endpoint_url(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions" if self.base_url.endswith("/v1") else f"{self.base_url}/v1/chat/completions"

    def complete(
        self, messages: Sequence[Mapping[str, Any]], *, model: Optional[str] = None,
        temperature: float = 0.0, max_tokens: int = 1024, extra_body: Optional[Mapping[str, Any]] = None,
    ) -> str:
        payload: Dict[str, Any] = {"model": model or self.model, "messages": [dict(m) for m in messages], "temperature": temperature, "max_tokens": max_tokens}
        if extra_body:
            payload.update(extra_body)
        endpoint = self._endpoint_url()
        if not (self.allow_local_phi and is_loopback_url(self.base_url)):
            verdict = LocalEgressGuard(self._ledger_path, policy=self._egress_policy).check_and_record(payload, destination=endpoint)
            if not verdict.allowed:
                raise EgressBlockedError(verdict)
        headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})}
        req = urllib.request.Request(endpoint, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
            choices = (json.loads(resp.read().decode("utf-8")).get("choices") or [])
        return str((choices[0].get("message") or {}).get("content") or "") if choices else ""


@dataclass
class ProviderRegistry:
    """Dynamic plugin registry supporting explicit registration and importlib.metadata entry points."""

    _specs: Dict[Tuple[ProviderKind, str], ProviderSpec] = field(default_factory=dict)
    _discovered_groups: set[str] = field(default_factory=set)

    def register(
        self, kind: ProviderKind, name: str, factory: Callable[..., Any], *, description: str = "", builtin: bool = False
    ) -> ProviderSpec:
        if kind not in VALID_PROVIDER_KINDS:
            raise ValueError(f"Unsupported provider kind: {kind!r}")
        key_name = name.strip().lower()
        if not key_name:
            raise ValueError("Provider name must be non-empty")
        spec = ProviderSpec(kind, key_name, factory, description, builtin)
        self._specs[(kind, key_name)] = spec
        return spec

    def list_providers(self, kind: Optional[ProviderKind] = None) -> List[ProviderSpec]:
        return [s for s in self._specs.values() if kind is None or s.kind == kind]

    def discover_entry_points(self, group: str = "openadapt_kernel.providers") -> int:
        """Discover external provider factories registered under setuptools/PEP-621 entry points."""
        loaded = 0
        for ep in importlib.metadata.entry_points().select(group=group):
            raw_name = str(getattr(ep, "name", "")).strip()
            kind_str, prov_name = (raw_name.split(":", 1) if ":" in raw_name else raw_name.split(".", 1) if "." in raw_name else ("llm", raw_name))
            kind_low = kind_str.strip().lower()
            if kind_low not in VALID_PROVIDER_KINDS:
                continue
            self.register(kind_low, prov_name, ep.load(), description=f"entry_point:{group}")  # type: ignore[arg-type]
            loaded += 1
        self._discovered_groups.add(group)
        return loaded

    def resolve(self, kind: ProviderKind, name: str = "auto", **kwargs: Any) -> Any:
        if kind not in VALID_PROVIDER_KINDS:
            raise ValueError(f"Unsupported provider kind: {kind!r}")
        key_name = name.strip().lower()
        if "openadapt_kernel.providers" not in self._discovered_groups and (key_name == "auto" or (kind, key_name) not in self._specs):
            self.discover_entry_points()
        if key_name != "auto":
            spec = self._specs.get((kind, key_name))
            if spec is None:
                raise KeyError(f"Unknown {kind!r} provider: {name!r}")
            return spec.factory(**kwargs)
        custom = [s for s in self._specs.values() if s.kind == kind and not s.builtin]
        if custom:
            return custom[0].factory(**kwargs)
        if kind == "coding_agent":
            for binary in ("claude", "codex", "gemini"):
                if (resolved_bin := shutil.which(binary)):
                    return {"backend": "cli", "name": binary, "binary": resolved_bin}
            if (base := str(kwargs.get("base_url") or os.environ.get("OPENAI_BASE_URL", "")).strip()):
                return OpenAICompatHTTPProvider(base_url=base, **{k: v for k, v in kwargs.items() if k != "base_url"})
            raise OptionalDependencyError("coding_agent", install_hint="Install claude/codex/gemini CLI or set OPENAI_BASE_URL")
        if kind == "llm":
            base = str(kwargs.get("base_url") or os.environ.get("OPENAI_BASE_URL", "") or "http://127.0.0.1:8000").strip()
            return OpenAICompatHTTPProvider(base_url=base, **{k: v for k, v in kwargs.items() if k != "base_url"})
        if kind == "edge_lora":
            from openadapt_kernel.edge_lora import EdgeLoRAConfig, EdgeLoRARuntime

            return EdgeLoRARuntime(kwargs.pop("config", None) or EdgeLoRAConfig(**kwargs))
        from openadapt_kernel.desktop_driver import get_desktop_driver

        return get_desktop_driver(**kwargs)


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry()
DEFAULT_PROVIDER_REGISTRY.register(
    "llm", "openai_compat_http", OpenAICompatHTTPProvider, description="Zero-dependency stdlib OpenAI-compatible HTTP client", builtin=True
)
