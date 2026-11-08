"""Keep one configured reader attached to an in-process verification call.

This is dependency injection, not a claim that a read is complete. The reader's
ordinary reachability, completeness, tier and effect checks still decide that.
No provider or authenticated session is serialized into a contract or receipt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

from openadapt_flow.deployment import EffectsConfig, _effect_verifier_config_identity
from openadapt_flow.runtime.effects.adapter import verifier_identity


def _binding(cfg: EffectsConfig, params: Mapping[str, object]) -> str:
    return json.dumps(
        {"config": cfg.model_dump(mode="json"), "params": dict(params)},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True, init=False)
class CompleteReadProvider:
    """Bind a normally built reader to exactly one config and parameter set.

    Construct this after the caller has acquired its actual read context. The
    same reader (and its session) serves baseline and post-write verification;
    it is never rebuilt, replaced by a fallback, or upgraded by this object.
    A pending completion authority remains pending and cannot yield a readable
    ordinary baseline. Callers retain responsibility for the session lifetime.
    """

    _reader: Any = field(repr=False)
    _binding: str = field(repr=False)
    _identity: str = field(repr=False)

    def __init__(
        self, cfg: EffectsConfig, params: Mapping[str, object], reader: Any
    ) -> None:
        if not callable(getattr(reader, "capture_pre_state", None)) or not callable(
            getattr(reader, "verify", None)
        ):
            raise ValueError("complete-read provider requires an effect reader")
        identity = verifier_identity(reader)
        if identity != _effect_verifier_config_identity(cfg, reader):
            raise ValueError("complete-read provider reader has a different config")
        object.__setattr__(self, "_reader", reader)
        object.__setattr__(self, "_binding", _binding(cfg, params))
        object.__setattr__(self, "_identity", identity)

    def resolve(self, cfg: EffectsConfig, params: Mapping[str, object]) -> Any:
        if _binding(cfg, params) != self._binding:
            raise ValueError("complete-read provider config or parameters changed")
        if verifier_identity(self._reader) != self._identity:
            raise ValueError("complete-read provider reader identity changed")
        return self._reader
