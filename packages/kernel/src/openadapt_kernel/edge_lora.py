"""Local specialist Edge LoRA runtime with loopback HTTP and lazy PEFT/NF4 backends."""

from __future__ import annotations

import importlib
import json
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Literal, Mapping, Optional, Sequence, Tuple, Union

from openadapt_kernel.egress_guard import EgressBlockedError, EgressPolicy, LocalEgressGuard
from openadapt_kernel.providers import (
    OpenAICompatHTTPProvider,
    OptionalDependencyError,
    is_loopback_url,
)
from openadapt_kernel.zero_phi_repair import sanitize_ui_label_allowlist

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


@dataclass(frozen=True)
class LoRATrainingRecord:
    role: SpecialistRole
    prompt: str
    completion: str
    source_kind: str
    record_id: str


_LORA_EXPORT_EXTRA_TOKENS: FrozenSet[str] = frozenset(
    "blinded_crop_ocr canary completion extract field_type map pointer_stage portal_pack "
    "prepare_card probe_nav prompt record_id role shadow_trace source_kind stage verify".split()
)


def _lora_export_policy(policy: Optional[EgressPolicy] = None) -> EgressPolicy:
    base = policy or EgressPolicy()
    return EgressPolicy(
        allowed_tokens=base.allowed_tokens,
        include_html_aria_vocab=base.include_html_aria_vocab,
        include_clinical_vocab=base.include_clinical_vocab,
        extra_allowed_tokens=base.extra_allowed_tokens | _LORA_EXPORT_EXTRA_TOKENS,
        allowed_var_keys=base.allowed_var_keys | frozenset(f"$var_{i}" for i in range(1, 33)),
    )


def export_lora_training_records(
    traces_or_packs: Sequence[Any],
    *,
    output_jsonl_path: Optional[Union[str, Path]] = None,
    egress_policy: Optional[EgressPolicy] = None,
    ledger_path: Optional[Union[str, Path]] = None,
) -> List[LoRATrainingRecord]:
    """Convert verified ShadowTrace/PortalPack/PrepareCard objects into Zero-PHI LoRA JSONL records."""
    pol = _lora_export_policy(egress_policy)
    led = Path(ledger_path) if ledger_path else (Path(tempfile.mkdtemp(prefix="oa_lora_export_")) / "outbound_egress_ledger.jsonl")
    guard = LocalEgressGuard(led, policy=pol)
    records: List[LoRATrainingRecord] = []

    def _append_audited(rec: LoRATrainingRecord) -> None:
        verdict = guard.check_and_record(asdict(rec), destination="lora_dataset_export")
        if not verdict.allowed:
            raise EgressBlockedError(verdict)
        records.append(rec)

    for idx, item in enumerate(traces_or_packs):
        cls_name = type(item).__name__
        if cls_name == "ShadowTrace" or (isinstance(item, Mapping) and "submitted_fields" in item):
            fields = item.submitted_fields if hasattr(item, "submitted_fields") else item.get("submitted_fields", {})
            keys = sorted(str(k) for k in (fields or {}).keys())
            sanitized_keys = [sanitize_ui_label_allowlist(k.replace("_", " ")) or "field" for k in keys]
            var_pairs = ", ".join(f"{sk}=$var_{i}" for i, sk in enumerate(sanitized_keys, start=1)) or "field=$var_1"
            _append_audited(
                LoRATrainingRecord(
                    "pointer_stage",
                    f"stage form fields: {' '.join(sanitized_keys) or 'field'}",
                    var_pairs,
                    "shadow_trace",
                    f"CANARY_REC_{len(records) + 1}",
                )
            )
            _append_audited(
                LoRATrainingRecord(
                    "blinded_crop_ocr",
                    f"extract value: {sanitized_keys[0] if sanitized_keys else 'field'}",
                    "CANARY_STR_1",
                    "shadow_trace",
                    f"CANARY_REC_{len(records) + 1}",
                )
            )
        elif cls_name == "PortalPack" or (isinstance(item, Mapping) and "pack_id" in item):
            req_fields = (
                item.variable_fields
                if hasattr(item, "variable_fields")
                else item.get("variable_fields", item.get("required_fields", ()))
            )
            sanitized = [sanitize_ui_label_allowlist(str(f).replace("_", " ")) or "field" for f in (req_fields or ())]
            _append_audited(
                LoRATrainingRecord(
                    "probe_nav",
                    f"verify form fields: {' '.join(sanitized) or 'submit'}",
                    "click submit button",
                    "portal_pack",
                    f"CANARY_REC_{len(records) + 1}",
                )
            )
        elif cls_name == "PrepareCard" or (isinstance(item, Mapping) and "staged_bindings" in item):
            labels = item.control_labels if hasattr(item, "control_labels") else item.get("control_labels", {})
            sanitized = [sanitize_ui_label_allowlist(str(v)) or "field" for v in (labels or {}).values()]
            var_pairs = ", ".join(f"{lbl}=$var_{i}" for i, lbl in enumerate(sanitized, start=1)) or "field=$var_1"
            _append_audited(
                LoRATrainingRecord(
                    "pointer_stage",
                    f"map controls: {' '.join(sanitized) or 'field'}",
                    var_pairs,
                    "prepare_card",
                    f"CANARY_REC_{len(records) + 1}",
                )
            )

    if output_jsonl_path is not None:
        out_path = Path(output_jsonl_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with out_path.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(asdict(rec), sort_keys=True) + "\n")
    return records
