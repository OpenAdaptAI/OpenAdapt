"""App packs and scenario packs: the data `openadapt test` runs.

An app pack (``data/apps/<name>.yaml``) says how to start the app from its
published images, sign the harness in, recognise the task's save request,
and read and change the record table. A scenario pack
(``data/scenarios/<name>.yaml``) holds the seed records, and for each
scenario the task's values and the one fault planted on its save.

Both are plain YAML, so a new app or a new scenario is a new file, not new
code. ``--app-pack`` and ``--scenarios`` take a path to your own.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Optional

import yaml

APP_SCHEMA = "openadapt.agent-test.app/v1"
SCENARIO_SCHEMA = "openadapt.agent-test.scenarios/v1"

#: Every fault kind a scenario may plant, and the keys each one takes.
FAULT_KINDS: dict[str, tuple[str, ...]] = {
    "none": (),
    "partial": ("fields",),
    "wrong_value": ("set",),
    "duplicate": ("replay_headers",),
    "no_persist": (),
    "collateral_loss": ("target",),
    "collateral_edit": ("target", "set"),
    "late_revert": ("after_s", "set", "delete"),
    "lost_response": (),
    "refused": (),
}

KINDS = ("text", "email", "phone", "date", "number")


class PackError(ValueError):
    """A pack that does not match its schema."""


def _builtin(kind: str, name: str) -> Path:
    root = resources.files("openadapt_flow.agent_test") / "data" / kind
    return Path(str(root / f"{name}.yaml"))


def builtin_apps() -> list[str]:
    root = Path(str(resources.files("openadapt_flow.agent_test") / "data" / "apps"))
    return sorted(p.stem for p in root.glob("*.yaml"))


def digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def fill(template: Any, values: dict[str, Any]) -> Any:
    """``template`` with every ``{name}`` replaced from ``values`` (recursively).

    Unknown names are left as they are, so a literal brace in a selector is safe.
    """

    if isinstance(template, str):
        return re.sub(
            r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
            lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0),
            template,
        )
    if isinstance(template, list):
        return [fill(t, values) for t in template]
    if isinstance(template, dict):
        return {k: fill(v, values) for k, v in template.items()}
    return template


@dataclass
class Field:
    name: str
    column: str
    kind: str = "text"
    form: str = ""


@dataclass
class AppPack:
    name: str
    title: str
    description: str
    record_noun: str
    stack: dict[str, Any]
    login: list[dict[str, Any]]
    save: dict[str, Any]
    store: dict[str, Any]
    fields: dict[str, Field]
    task: str
    scripted: list[dict[str, Any]]
    readback: dict[str, Any]
    signed_out_selector: str = ""
    signed_out_url_contains: str = ""
    start_path: Optional[str] = "/"
    licence: str = ""
    source: str = ""
    digest: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def images(self) -> list[str]:
        return sorted(str(s["image"]) for s in self.stack.get("services", {}).values())

    def task_text(self, values: dict[str, Any]) -> str:
        return " ".join(str(fill(self.task, values)).split())

    @property
    def ok_status(self) -> frozenset[int]:
        return frozenset(int(s) for s in self.save.get("ok_status") or [])


@dataclass
class Scenario:
    id: str
    note: str
    task: dict[str, Any]
    fault: dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.fault.get("kind", "none"))


@dataclass
class ScenarioPack:
    app: str
    version: str
    seed: list[dict[str, Any]]
    scenarios: list[Scenario]
    source: str = ""
    digest: str = ""


def _read(path_or_name: str, kind: str) -> tuple[dict[str, Any], str, str]:
    p = Path(path_or_name)
    if not (p.suffix in (".yaml", ".yml") and p.is_file()):
        p = _builtin(kind, path_or_name)
        if not p.is_file():
            raise PackError(
                f"no {kind[:-1]} pack named {path_or_name!r} (built in: {', '.join(builtin_apps())})"
            )
    text = p.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    if not isinstance(doc, dict):
        raise PackError(f"{p}: not a mapping")
    return doc, text, str(p)


def load_app(path_or_name: str) -> AppPack:
    doc, text, src = _read(path_or_name, "apps")
    if doc.get("schema") != APP_SCHEMA:
        raise PackError(f"{src}: schema must be {APP_SCHEMA}")
    for k in ("name", "stack", "login", "save", "store", "task"):
        if k not in doc:
            raise PackError(f"{src}: missing {k!r}")
    store = doc["store"]
    if store.get("kind") != "mariadb":
        raise PackError(
            f"{src}: store.kind {store.get('kind')!r} is not supported (mariadb)"
        )
    for k in (
        "service",
        "database",
        "user",
        "password",
        "table",
        "key",
        "fields",
        "identity",
        "insert_sql",
    ):
        if k not in store:
            raise PackError(f"{src}: store is missing {k!r}")
    fields: dict[str, Field] = {}
    for name, spec in store["fields"].items():
        kind = spec.get("kind", "text")
        if kind not in KINDS:
            raise PackError(f"{src}: field {name}: kind {kind!r} is not one of {KINDS}")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(spec["column"])):
            raise PackError(f"{src}: field {name}: column must be a plain identifier")
        fields[name] = Field(
            name, str(spec["column"]), kind, str(spec.get("form", name))
        )
    for ident in (store["table"], store["key"]):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(ident)):
            raise PackError(f"{src}: table and key must be plain identifiers")
    return AppPack(
        name=str(doc["name"]),
        title=str(doc.get("title", doc["name"])),
        description=str(doc.get("description", doc["name"])),
        record_noun=str(doc.get("record_noun", "record")),
        stack=doc["stack"],
        login=list(doc["login"]),
        save=dict(doc["save"]),
        store=store,
        fields=fields,
        task=str(doc["task"]),
        scripted=list(doc.get("scripted") or []),
        readback=dict(doc.get("readback") or {}),
        signed_out_selector=str(doc.get("signed_out_selector") or ""),
        signed_out_url_contains=str(doc.get("signed_out_url_contains") or ""),
        start_path=doc.get("start_path", "/"),
        licence=str(doc.get("licence", "")),
        source=src,
        digest=digest_text(text),
        raw=doc,
    )


def load_scenarios(path_or_name: str, app: AppPack) -> ScenarioPack:
    doc, text, src = _read(path_or_name, "scenarios")
    if doc.get("schema") != SCENARIO_SCHEMA:
        raise PackError(f"{src}: schema must be {SCENARIO_SCHEMA}")
    if doc.get("app") != app.name:
        raise PackError(f"{src}: written for app {doc.get('app')!r}, not {app.name!r}")
    seen: set[str] = set()
    out: list[Scenario] = []
    for i, s in enumerate(doc.get("scenarios") or []):
        sid = str(s.get("id") or f"#{i}")
        if sid in seen:
            raise PackError(f"{src}: scenario id {sid} repeats")
        seen.add(sid)
        fault = dict(s.get("fault") or {"kind": "none"})
        kind = fault.get("kind", "none")
        if kind not in FAULT_KINDS:
            raise PackError(
                f"{src}: {sid}: fault kind {kind!r} is not one of {sorted(FAULT_KINDS)}"
            )
        extra = set(fault) - {"kind", *FAULT_KINDS[kind]}
        if extra:
            raise PackError(f"{src}: {sid}: {kind} does not take {sorted(extra)}")
        task = dict(s.get("task") or {})
        missing = [f for f in app.fields if f not in task]
        if missing:
            raise PackError(f"{src}: {sid}: task is missing {missing}")
        for key in ("set",):
            for f in fault.get(key) or {}:
                if f not in app.fields:
                    raise PackError(f"{src}: {sid}: {kind} sets unknown field {f!r}")
        for f in fault.get("fields") or []:
            if f not in app.fields:
                raise PackError(f"{src}: {sid}: partial names unknown field {f!r}")
        if kind == "late_revert" and not (fault.get("set") or fault.get("delete")):
            raise PackError(f"{src}: {sid}: late_revert needs set or delete")
        out.append(Scenario(sid, str(s.get("note", "")), task, fault))
    if not out:
        raise PackError(f"{src}: no scenarios")
    return ScenarioPack(
        app=app.name,
        version=str(doc.get("version", "0")),
        seed=list(doc.get("seed") or []),
        scenarios=out,
        source=src,
        digest=digest_text(text),
    )


def compose_yaml(
    app: AppPack, project: str, app_port: int, proxy_port: int, bind: str
) -> str:
    """The Docker Compose file for one stack: the pack's services, filled in."""

    values = {
        "project": project,
        "app_port": app_port,
        "proxy_port": proxy_port,
        "bind": bind,
    }
    services = fill(app.stack["services"], values)
    return yaml.safe_dump({"name": project, "services": services}, sort_keys=False)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
