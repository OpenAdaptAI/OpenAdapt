"""Local Execute models: admitted bundles and the self-signed seal envelope.

The portable receipt body is ``ExecuteEvidenceReceiptV1`` from
``openadapt-types``. This module adds only the local issuer wrapper. Extra
keys are forbidden. Screenshot, OCR, parameter, and URL fields have no place
here; see :mod:`openadapt_flow.receipt` for the same allow-list discipline.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictStr

from openadapt_flow.execute import SELF_SIGNED_NOTICE

EXECUTE_ADMISSION_SCHEMA: Literal["openadapt.execute-admission/v1"] = (
    "openadapt.execute-admission/v1"
)
SELF_SIGNED_SEAL_SCHEMA: Literal["openadapt.execute-self-signed-seal/v1"] = (
    "openadapt.execute-self-signed-seal/v1"
)

#: Fields a shareable Execute receipt must never carry. The portable types
#: model already uses ``extra="forbid"``; this set is the regression net for
#: the local projector and the stored JSON.
FORBIDDEN_RECEIPT_KEYS = frozenset(
    {
        "screenshot",
        "screenshots",
        "ocr",
        "ocr_text",
        "typed_value",
        "typed_values",
        "parameters",
        "parameter",
        "url",
        "hostname",
        "coordinate",
        "coordinates",
        "halt_reason",
        "application_name",
        "organization_name",
        "user_name",
        "workflow_name",
        "phi",
        "image",
        "after_png",
        "before_png",
        "step_intent",
        "note",
        "record_id",
    }
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AdmittedBundle(_Strict):
    """One digest-pinned qualification an operator has admitted on this machine."""

    schema_version: Literal["openadapt.execute-admission/v1"] = EXECUTE_ADMISSION_SCHEMA
    qualification_id: StrictStr
    workflow_version: StrictStr
    workflow_digest: StrictStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    environment_id: StrictStr
    minimum_effect_strength: StrictStr
    bundle_dir: StrictStr | None = None
    target_url: StrictStr | None = None
    synthetic: StrictBool = False
    break_it: StrictBool = False
    policy: StrictStr = "clinical-write"


class SelfSignedSealV1(_Strict):
    """Local verify envelope around a portable Execute receipt.

    ``production_seal`` is always false. ``issuer`` is always ``self_signed``.
    Cloud's OpenAdapt Seal is a different artifact on a different host.
    """

    schema_version: Literal["openadapt.execute-self-signed-seal/v1"] = (
        SELF_SIGNED_SEAL_SCHEMA
    )
    issuer: Literal["self_signed"] = "self_signed"
    issuer_key_fingerprint: StrictStr = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    signature: StrictStr = Field(pattern=r"^ed25519:[0-9a-f]{128}$")
    production_seal: Literal[False] = False
    verify_host: Literal["local"] = "local"
    meter_usd: StrictFloat = Field(ge=0.0, le=0.0)
    notice: Literal[
        "Self-signed. Counterparties that require an OpenAdapt Seal still use Cloud."
    ] = SELF_SIGNED_NOTICE
    receipt: dict[str, Any]


def assert_no_forbidden_keys(payload: dict[str, Any]) -> None:
    """Refuse a receipt dict that carries a PHI or screenshot field."""

    extra = FORBIDDEN_RECEIPT_KEYS.intersection(payload)
    if extra:
        names = ", ".join(sorted(extra))
        raise ValueError(f"execute receipt forbids extra/PHI keys: {names}")
    nested = payload.get("contracts")
    if isinstance(nested, dict):
        extra = FORBIDDEN_RECEIPT_KEYS.intersection(nested)
        if extra:
            names = ", ".join(sorted(extra))
            raise ValueError(f"execute receipt contracts forbid keys: {names}")
