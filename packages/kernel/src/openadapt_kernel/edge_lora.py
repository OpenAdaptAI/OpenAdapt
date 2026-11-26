"""Local specialist Edge LoRA runtime with loopback HTTP and lazy PEFT/NF4 backends."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, Literal, Optional, Tuple

from openadapt_kernel.providers import (
    OpenAICompatHTTPProvider,
    OptionalDependencyError,
    is_loopback_url,
)

SpecialistRole = Literal["pointer_stage", "blinded_crop_ocr", "probe_nav"]
VALID_SPECIALIST_ROLES: FrozenSet[str] = frozenset({"pointer_stage", "blinded_crop_ocr", "probe_nav"})
EdgeLoRABackend = Literal["local_http", "local_peft"]


@dataclass(frozen=True)
class LoRAAdapterSpec:
    name: str
    path_or_uri: str
    role: SpecialistRole

    def __post_init__(self) -> None:
        if self.role not in VALID_SPECIALIST_ROLES:
            raise ValueError(f"Unsupported specialist role: {self.role!r}")
        if not self.name.strip():
            raise ValueError("LoRAAdapterSpec.name must be non-empty")


@dataclass(frozen=True)
class EdgeLoRAConfig:
    backend: EdgeLoRABackend = "local_http"
    base_model: str = ""
    base_url: str = "http://127.0.0.1:8000"
    adapters: Tuple[LoRAAdapterSpec, ...] = ()
    timeout_sec: float = 30.0
    load_in_4bit: bool = True


@dataclass
class EdgeLoRARuntime:
    """Runs local on-premise specialist LoRA adapters with strict zero-egress loopback guarantees."""

    config: EdgeLoRAConfig
    active_role: Optional[SpecialistRole] = None
    _adapters_by_role: Dict[SpecialistRole, LoRAAdapterSpec] = field(default_factory=dict, init=False)
    _peft_model: Any = field(default=None, init=False, repr=False)
    _tokenizer: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.config.backend == "local_http" and not is_loopback_url(self.config.base_url):
            raise ValueError(
                f"EdgeLoRARuntime local_http backend requires a loopback URL (127.0.0.1/localhost), got: {self.config.base_url!r}"
            )
        for spec in self.config.adapters:
            self._adapters_by_role[spec.role] = spec

    def register_adapter(self, spec: LoRAAdapterSpec) -> None:
        self._adapters_by_role[spec.role] = spec

    def get_adapter(self, role: SpecialistRole) -> LoRAAdapterSpec:
        if role not in self._adapters_by_role:
            raise KeyError(f"No LoRA adapter configured for specialist role {role!r}")
        return self._adapters_by_role[role]

    def _ensure_peft_loaded(self) -> None:
        if self._peft_model is not None and self._tokenizer is not None:
            return
        missing, mods = [], {}
        for pkg in ("torch", "transformers", "peft"):
            try:
                mods[pkg] = importlib.import_module(pkg)
            except ImportError:
                missing.append(pkg)
        if missing:
            raise OptionalDependencyError(", ".join(missing), extra="edge-lora", install_hint="pip install torch transformers peft bitsandbytes")
        if not self.config.base_model or not self._adapters_by_role:
            raise ValueError("EdgeLoRAConfig.base_model and at least one LoRAAdapterSpec are required for local_peft")
        transformers, peft = mods["transformers"], mods["peft"]
        self._tokenizer = transformers.AutoTokenizer.from_pretrained(self.config.base_model)
        load_kwargs: Dict[str, Any] = {"device_map": "auto"}
        if self.config.load_in_4bit:
            load_kwargs["quantization_config"] = transformers.BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4")
        base = transformers.AutoModelForCausalLM.from_pretrained(self.config.base_model, **load_kwargs)
        specs = list(self._adapters_by_role.values())
        model = peft.PeftModel.from_pretrained(base, specs[0].path_or_uri, adapter_name=specs[0].name)
        for extra in specs[1:]:
            model.load_adapter(extra.path_or_uri, adapter_name=extra.name)
        self._peft_model = model

    def set_adapter(self, role: SpecialistRole) -> LoRAAdapterSpec:
        spec = self.get_adapter(role)
        self.active_role = role
        if self.config.backend == "local_peft":
            self._ensure_peft_loaded()
            self._peft_model.set_adapter(spec.name)
        return spec

    def infer(self, role: SpecialistRole, prompt: str, *, system_prompt: str = "", max_tokens: int = 256, temperature: float = 0.0) -> str:
        spec = self.set_adapter(role)
        if self.config.backend == "local_http":
            if not is_loopback_url(self.config.base_url):
                raise ValueError("EdgeLoRARuntime refuses non-loopback HTTP inference")
            client = OpenAICompatHTTPProvider(base_url=self.config.base_url, model=spec.name, timeout_sec=self.config.timeout_sec, allow_local_phi=True)
            msgs = ([{"role": "system", "content": system_prompt}] if system_prompt else []) + [{"role": "user", "content": prompt}]
            return client.complete(msgs, model=spec.name, temperature=temperature, max_tokens=max_tokens)
        self._ensure_peft_loaded()
        inputs = self._tokenizer(f"{system_prompt}\n{prompt}".strip() if system_prompt else prompt, return_tensors="pt")
        out_ids = self._peft_model.generate(**inputs, max_new_tokens=max_tokens, do_sample=temperature > 0.0)
        return str(self._tokenizer.decode(out_ids[0], skip_special_tokens=True))
