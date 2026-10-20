"""``guard.yaml``: what the operator fixes before the agent runs.

::

    schema_version: openadapt.agent-guard/v1
    app_url: http://127.0.0.1:8765/#tasks   # the guarded origin
    verify: verify.yaml                      # how verify-run reads the save back
    screen_selector: "#patient-banner"       # optional: where the record is shown
    not_saves:                               # optional: writes that aren't the save
      - {method: POST, path: /chart/signin}
    approval: not_gated                      # or each_item: a person approves each save
    teach:                                   # optional: compile run 1 for run 2
      dir: .openadapt-teach
      name: triage_note
      inputs: [note]

Relative paths are relative to the file. The task (which record, which
values) is a separate ``openadapt.guard-task/v1`` file from whoever assigned
the work (:func:`openadapt_flow.guard.load_task`), never from the agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from openadapt_flow.agent_guard.requests import NotASave, origin_of
from openadapt_flow.guard import APPROVAL_MODES, ApprovalSetting

__all__ = [
    "CONFIG_SCHEMA",
    "ConfigError",
    "GuardConfig",
    "TeachSection",
    "load_config",
    "parse_config",
]

CONFIG_SCHEMA = "openadapt.agent-guard/v1"
_KEYS = {
    "schema_version",
    "app_url",
    "verify",
    "screen_selector",
    "not_saves",
    "approval",
    "observe_only",
    "response_wait_s",
    "teach",
}
_TEACH_KEYS = {
    "dir",
    "name",
    "inputs",
    "instruction",
    "key_dir",
    "policy",
    "db",
    "complete_read",
}


class ConfigError(ValueError):
    """``guard.yaml`` is not usable. Nothing was attached."""


@dataclass(frozen=True)
class TeachSection:
    """Where a run that reads back correctly becomes a draft program."""

    dir: Path
    name: str = "guarded_save"
    #: The task values that become the program's inputs. Default: the
    #: ``intended_fields`` the agent typed.
    inputs: Optional[tuple[str, ...]] = None
    instruction: Optional[str] = None
    key_dir: Optional[Path] = None
    policy: str = "clinical-write"
    #: A read-only copy of the app's SQLite database for scaffold-verifier
    #: (``--db``): every other table gets a "no row added" guard.
    db: Optional[Path] = None
    #: A person's statement that the reads return everything the save can
    #: change (scaffold-verifier ``--complete-read``). Recorded as declared.
    complete_read: bool = False


@dataclass(frozen=True)
class GuardConfig:
    app_url: str
    verify: Path
    screen_selector: Optional[str] = None
    not_saves: tuple[NotASave, ...] = ()
    approval: ApprovalSetting = "not_gated"
    observe_only: bool = False
    response_wait_s: float = 30.0
    teach: Optional[TeachSection] = None

    def __post_init__(self) -> None:
        if not origin_of(self.app_url).startswith(("http://", "https://")):
            raise ConfigError("app_url must be an http(s) URL")
        if self.approval not in APPROVAL_MODES:
            raise ConfigError(f"approval must be one of {APPROVAL_MODES}")
        if not Path(self.verify).is_file():
            raise ConfigError(f"verify file not found: {self.verify}")

    @property
    def app_origin(self) -> str:
        return origin_of(self.app_url)


def _path(base: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def parse_config(raw: Mapping[str, Any], *, base: Path) -> GuardConfig:
    if raw.get("schema_version") != CONFIG_SCHEMA:
        raise ConfigError(f"schema_version must be {CONFIG_SCHEMA}")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise ConfigError(f"unknown keys: {', '.join(unknown)}")
    rules = []
    for item in raw.get("not_saves") or []:
        if not isinstance(item, Mapping) or not {"method", "path"} <= set(item):
            raise ConfigError("each not_saves entry needs method and path")
        rules.append(
            NotASave(
                method=str(item["method"]),
                path=str(item["path"]),
                origin=origin_of(str(item["origin"])) if item.get("origin") else None,
            )
        )
    teach = None
    section = raw.get("teach")
    if section is not None:
        if not isinstance(section, Mapping) or "dir" not in section:
            raise ConfigError("teach needs dir")
        extra = sorted(set(section) - _TEACH_KEYS)
        if extra:
            raise ConfigError(f"unknown teach keys: {', '.join(extra)}")
        inputs = section.get("inputs")
        teach = TeachSection(
            dir=_path(base, section["dir"], "teach.dir"),
            name=str(section.get("name") or "guarded_save"),
            inputs=tuple(str(i) for i in inputs) if inputs is not None else None,
            instruction=section.get("instruction"),
            key_dir=(
                _path(base, section["key_dir"], "teach.key_dir")
                if section.get("key_dir")
                else None
            ),
            policy=str(section.get("policy") or "clinical-write"),
            db=_path(base, section["db"], "teach.db") if section.get("db") else None,
            complete_read=bool(section.get("complete_read", False)),
        )
    return GuardConfig(
        app_url=str(raw.get("app_url") or ""),
        verify=_path(base, raw.get("verify"), "verify"),
        screen_selector=raw.get("screen_selector") or None,
        not_saves=tuple(rules),
        approval=raw.get("approval") or "not_gated",
        observe_only=bool(raw.get("observe_only", False)),
        response_wait_s=float(raw.get("response_wait_s", 30.0)),
        teach=teach,
    )


def load_config(path: str | Path) -> GuardConfig:
    import yaml

    source = Path(path).expanduser().resolve()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"cannot read {source}: {exc}") from exc
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{source} is not a mapping")
    return parse_config(raw, base=source.parent)
