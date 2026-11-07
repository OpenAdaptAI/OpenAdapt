"""Operator task data into the existing record, verify and compile pipeline.

The read declaration is generated setup data, not an effect contract or an
admission. Actual SoR differences propose the effects; the normal compiler's
confirmation, canaries and admission still decide whether a tool can be used.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt_flow.compile_agent.pipeline import _PLACEHOLDER_RE
from openadapt_flow.deployment import EffectsConfig
from openadapt_flow.integrations.agents.skills import DEFAULT_CACHE_DIR, SkillCache
from openadapt_flow.jit.signature import TaskSignature
from openadapt_flow.record_sor import (
    _write_private,
    observer_from_config,
)

_REQUIRED = frozenset({"instruction", "parameters", "url", "verifier", "expect", "on_screen", "canary_params"})
_OPTIONAL = frozenset({"schema", "cache_dir", "environment", "policy", "task_id", "constants", "allow_origins", "headless"})
_MAX_TASK_BYTES = 65_536


class TaskSetupError(ValueError):
    """A fixed setup diagnostic, without values from operator configuration."""


@dataclass(frozen=True)
class TaskSetup:
    cache: SkillCache
    instruction: str
    parameters: tuple[str, ...]


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TaskSetupError("DUPLICATE_TASK_KEY")
        result[key] = value
    return result


def _invalid_constant(value: str) -> Any:
    raise TaskSetupError("INVALID_TASK_JSON")


def _strings(value: Any, *, nonempty: bool = False) -> list[str]:
    if type(value) is not list or any(type(item) is not str or not item.strip() for item in value):
        raise TaskSetupError("INVALID_TASK_STRINGS")
    if nonempty and not value:
        raise TaskSetupError("INVALID_TASK_STRINGS")
    return list(value)


def _verifier_keys(raw: Any) -> None:
    if type(raw) is not dict or not raw or not set(raw) <= set(EffectsConfig.model_fields):
        raise TaskSetupError("INVALID_GENERATED_READ")
    if raw.get("candidates"):
        # Recording's normal observer does not support choosing read candidates.
        raise TaskSetupError("INVALID_GENERATED_READ")
    surfaces = raw.get("surfaces", {})
    if type(surfaces) is not dict:
        raise TaskSetupError("INVALID_GENERATED_READ")
    for section in surfaces.values():
        _verifier_keys(section)


def load_task(
    path: str | Path,
    *,
    offer: Optional[Callable[[dict[str, Any]], Optional[str]]] = None,
    admitter: Any = None,
    person: Optional[str] = None,
    review: Any = None,
    hold: Any = None,
    model_key: Any = None,
    reset: Optional[Callable[[], None]] = None,
    key_dir: str | Path | None = None,
    teach_authorization: Any = None,
    write_adapter: Any = None,
) -> TaskSetup:
    """Build one ordinary SkillCache from bounded operator-owned JSON.

    The agent receives only run parameters through the façade, never this
    setup document or its approval callbacks. With no ``offer``, proposed
    checks remain a draft. This function performs no agent call, comparison,
    effect confirmation or admission.
    ``verifier`` uses the existing EffectsConfig fields; missing source/read
    semantics must be resolved by the app-pack generator, not guessed here.
    """
    source = Path(path).expanduser().resolve()
    try:
        with source.open("rb") as incoming:
            data = incoming.read(_MAX_TASK_BYTES + 1)
        if len(data) > _MAX_TASK_BYTES:
            raise TaskSetupError("TASK_TOO_LARGE")
        task = json.loads(data, object_pairs_hook=_object, parse_constant=_invalid_constant)
    except TaskSetupError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise TaskSetupError("INVALID_TASK_JSON") from None
    if type(task) is not dict or not _REQUIRED <= set(task) or not set(task) <= _REQUIRED | _OPTIONAL:
        raise TaskSetupError("INVALID_TASK_KEYS")
    if task.get("schema", "openadapt.wrap-task/v1") != "openadapt.wrap-task/v1":
        raise TaskSetupError("INVALID_TASK_SCHEMA")
    instruction = task["instruction"]
    parameters = _strings(task["parameters"], nonempty=True)
    if type(instruction) is not str or not instruction.strip() or len(set(parameters)) != len(parameters):
        raise TaskSetupError("INVALID_TASK_PARAMETERS")
    if set(_PLACEHOLDER_RE.findall(instruction)) != set(parameters) or set(parameters) & {"reset", "task_dir"}:
        raise TaskSetupError("INVALID_TASK_PARAMETERS")
    task_id = task.get("task_id", "skill")
    if type(task_id) is not str:
        raise TaskSetupError("INVALID_TASK_PARAMETERS")
    try:
        signature = TaskSignature(task_id, instruction, {name: str for name in parameters})
    except (TypeError, ValueError):
        raise TaskSetupError("INVALID_TASK_PARAMETERS") from None
    canaries = task["canary_params"]
    if type(canaries) is not list or not canaries:
        raise TaskSetupError("INVALID_CANARY_PARAMETERS")
    try:
        for values in canaries:
            if type(values) is not dict:
                raise ValueError
            signature.validate(values)
    except (TypeError, ValueError):
        raise TaskSetupError("INVALID_CANARY_PARAMETERS") from None
    expect = task["expect"]
    if type(expect) is not dict or not expect or any(type(k) is not str or not k or type(v) is not str for k, v in expect.items()):
        raise TaskSetupError("INVALID_TASK_EXPECT")
    on_screen = _strings(task["on_screen"], nonempty=True)
    strings = {}
    for name in ["url", "environment", "policy", "cache_dir"]:
        value = task.get(name)
        if value is not None and (type(value) is not str or not value.strip()):
            raise TaskSetupError("INVALID_TASK_OPTIONS")
        if value is not None:
            strings[name] = value
    if type(task.get("headless", True)) is not bool:
        raise TaskSetupError("INVALID_TASK_OPTIONS")
    constants = _strings(task.get("constants", []))
    origins = _strings(task.get("allow_origins", []))
    from openadapt_flow.compile_agent.pipeline import check_target
    try:
        check_target(task["url"])
        _verifier_keys(task["verifier"])
        # Reuse the normal loader's tier and field validation, before materializing.
        _, raw = observer_from_config(task["verifier"], for_run1=True)
        import yaml

        body = "# Generated setup read; no observed effects or approval.\n" + yaml.safe_dump(
            {"schema_version": "openadapt.verify/v1", "verifier": raw, "effects": []},
            sort_keys=False,
            allow_unicode=True,
        )
    except Exception:
        raise TaskSetupError("INVALID_GENERATED_READ") from None
    cache_dir = Path(strings.get("cache_dir", DEFAULT_CACHE_DIR)).expanduser()
    if not cache_dir.is_absolute():
        cache_dir = source.parent / cache_dir
    cache_dir = cache_dir.resolve()
    target = cache_dir / "generated-reads" / (hashlib.sha256(body.encode()).hexdigest() + ".yaml")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        if target.is_symlink() or not target.is_file() or target.read_text() != body:
            raise TaskSetupError("GENERATED_READ_CHANGED")
    else:
        _write_private(target, body)
    options: dict[str, Any] = {
        "url": task["url"], "verify": target, "expect": expect, "on_screen": on_screen,
        "offer": offer if offer is not None else (lambda _proposal: None),
        "canary_params": canaries, "cache_dir": cache_dir, "key_dir": key_dir,
        "admitter": admitter, "person": person, "model_key": model_key, "reset": reset,
        "environment": strings.get("environment", ""), "headless": task.get("headless", True),
        "constants": constants, "allow_origins": origins,
    }
    for name in ["policy", "task_id"]:
        if name in task:
            options[name] = task[name]
    for name, value in [("review", review), ("hold", hold), ("teach_authorization", teach_authorization), ("write_adapter", write_adapter)]:
        if value is not None:
            options[name] = value
    return TaskSetup(cache=SkillCache(**options), instruction=instruction, parameters=tuple(parameters))
