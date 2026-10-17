"""Optional compile-time qualification proposer. Off by default.

Same shape as :class:`openadapt_flow.compiler.induction.Proposer`: it may
suggest an identity field name or an effect-contract SKETCH from sanitized
recording metadata. Suggestions are flagged, versioned in proposal.json, and
still face the oracle gates. They are never auto-applied. Tests pass with the
proposer absent. The LLM adapter is lazy-imported; CI does not need a key.
"""

from __future__ import annotations

from typing import Any, Literal, Mapping, Optional

from pydantic import BaseModel, ConfigDict, Field

from openadapt_flow.compiler.induction import Proposer
from openadapt_flow.ir import Workflow
from openadapt_flow.traversal import iter_workflow_steps

SUGGESTION_SCHEMA: Literal["openadapt.qualification-suggestion/v1"] = (
    "openadapt.qualification-suggestion/v1"
)
SuggestionKind = Literal["identity_field", "effect_contract_sketch"]

_ALLOWED_META_KEYS = frozenset(
    {
        "app_url",
        "application",
        "application_version",
        "surface",
        "viewport",
        "execution_mode",
        "param_names",
    }
)
_BLOCKED_META_KEYS = frozenset(
    {
        "frame",
        "frames",
        "png",
        "screenshot",
        "screenshots",
        "image",
        "images",
        "pixels",
        "ocr",
        "phi",
        "ssn",
        "note",
        "patient",
        "payload",
        "dom",
    }
)


class FlaggedSuggestion(BaseModel):
    """One proposer hint. ``trusted`` is always False."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.qualification-suggestion/v1"] = SUGGESTION_SCHEMA
    target: str
    kind: SuggestionKind
    content: str = Field(min_length=1, max_length=512)
    source: str = "proposer"
    trusted: Literal[False] = False


def sanitize_recording_metadata(
    meta: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep field names and surface facts. Drop frames and raw values."""

    if not meta:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in meta.items():
        lowered = str(key).lower()
        if lowered in _BLOCKED_META_KEYS or lowered not in _ALLOWED_META_KEYS:
            continue
        if lowered in {"frame", "frames", "png", "screenshot", "image"}:
            continue
        cleaned[key] = value
    params = meta.get("params")
    if isinstance(params, Mapping):
        cleaned["param_names"] = sorted(str(name) for name in params)
    return cleaned


def _refuse_raw_frames(context: Mapping[str, Any]) -> None:
    blob = json_ready(context)
    lowered = json_ready_text(blob).lower()
    if "data:image" in lowered or "base64," in lowered:
        raise ValueError("refusing to send frames or encoded images to a proposer")


def json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def json_ready_text(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def proposer_context(
    workflow: Workflow, meta: Optional[Mapping[str, Any]]
) -> dict[str, Any]:
    """Sanitized facts a proposer may see. No raw PHI frames."""

    context = {
        "bundle_name": workflow.name,
        "surface": workflow.surface,
        "param_names": sorted(workflow.params),
        "recording": sanitize_recording_metadata(meta),
        "write_step_ids": [
            step.id
            for step in iter_workflow_steps(workflow)
            if step.risk == "irreversible" or list(step.effects)
        ],
    }
    _refuse_raw_frames(context)
    return context


def collect_suggestions(
    proposer: Optional[Proposer],
    workflow: Workflow,
    *,
    meta: Optional[Mapping[str, Any]] = None,
) -> list[FlaggedSuggestion]:
    """Ask the optional proposer. Absent proposer -> no suggestions."""

    if proposer is None:
        return []
    context = proposer_context(workflow, meta)
    found: list[FlaggedSuggestion] = []
    for kind in ("identity_field", "effect_contract_sketch"):
        content = proposer.propose(kind, kind, context)
        if not content or not str(content).strip():
            continue
        found.append(
            FlaggedSuggestion(
                target=kind,
                kind=kind,  # type: ignore[arg-type]
                content=str(content).strip()[:512],
                source=getattr(proposer, "source", "proposer") or "proposer",
            )
        )
    return found


class LazyLLMQualificationProposer:
    """Opt-in compile-time sketch proposer. Lazy-imports the SDK.

    Never called at replay. Importing this module needs no key. Constructing
    this class needs no key. :meth:`propose` resolves the client lazily.
    """

    source = "llm"

    def __init__(self, *, client: Any = None, model: Optional[str] = None) -> None:
        self._client = client
        self._model = model

    def propose(self, target: str, kind: str, context: dict[str, Any]) -> Optional[str]:
        if kind not in {"identity_field", "effect_contract_sketch"}:
            return None
        _refuse_raw_frames(context)
        client = self._client
        model = self._model
        if client is None:
            import anthropic

            from openadapt_flow.benchmark.agent_baseline import load_api_key

            client = anthropic.Anthropic(api_key=load_api_key())
        if model is None:
            from openadapt_flow.benchmark.agent_baseline import MODEL as DEFAULT_MODEL

            model = DEFAULT_MODEL
        prompt = (
            "From this sanitized recording metadata, suggest either an identity "
            "field name or a one-line effect-contract sketch. Do not emit the "
            "next click. Do not invent an API endpoint that is not already "
            "named in the metadata. Reply with one line of text only.\n"
            f"kind={kind}\ntarget={target}\n"
            f"{json_ready_text(context)}"
        )
        response = client.messages.create(
            model=model,
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            block.text
            for block in response.content
            if getattr(block, "type", "") == "text"
        ).strip()
        return text or None
