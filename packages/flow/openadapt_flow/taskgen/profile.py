"""App profiles: everything the task generator knows about one app, as data.

A profile (``openadapt.taskgen-profile/v1``, a YAML file) names the app's
record types, the fields its screens show, which operations each record
allows (create, update, cancel, merge), the value pools tasks draw from, and
which record is the person a task is about. The engine reads it and nothing
else: no app name appears in the generator's code.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import yaml

PROFILE_SCHEMA = "openadapt.taskgen-profile/v1"
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"
#: More profile directories, searched after the bundled one.
PROFILE_PATH_ENV = "TASKGEN_PROFILE_PATH"
FIELD_TYPES = ("text", "date", "choice", "ref", "number")
OPS = ("create", "update", "cancel", "merge")


class ProfileError(ValueError):
    """A profile is malformed."""


@dataclass(frozen=True)
class FieldSpec:
    name: str
    type: str
    label: str
    choices: tuple[str, ...] = ()
    entity: Optional[str] = None
    #: ``phone`` or ``email``: how a value for a text field is made up.
    format: Optional[str] = None
    readonly: bool = False
    default: Optional[str] = None

    @property
    def sql_type(self) -> str:
        return {
            "text": "TEXT",
            "date": "DATE",
            "choice": "TEXT",
            "ref": "INTEGER",
            "number": "NUMERIC(12,2)",
        }[self.type]


@dataclass(frozen=True)
class EntitySpec:
    name: str
    table: str
    label: str
    title: str
    fields: tuple[FieldSpec, ...]
    key: Optional[str] = None
    list: bool = False
    search: tuple[str, ...] = ()
    ops: tuple[str, ...] = ()
    parent: Optional[tuple[str, str]] = None
    cancel: Mapping[str, str] = field(default_factory=dict)
    merge: Mapping[str, str] = field(default_factory=dict)
    #: Fields an update task changes, and the field that tells one of a
    #: person's records of this type from another.
    changes: tuple[str, ...] = ()
    identify_by: Optional[str] = None

    def field(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise ProfileError(f"{self.name} has no field {name!r}")

    @property
    def editable(self) -> list[FieldSpec]:
        """Fields a person types or picks on the edit and new forms."""
        return [f for f in self.fields if not f.readonly]


@dataclass(frozen=True)
class Profile:
    app: str
    version: int
    title: str
    users: Mapping[str, Mapping[str, str]]
    entities: Mapping[str, EntitySpec]
    person: str
    child: str
    lookup: str
    lookup_fields: Mapping[str, Mapping[str, str]]
    pools: Mapping[str, list[str]]
    #: The person's answers to the contract generator's questions
    #: (``answers.yaml``), kept with the app.
    answers: Mapping[str, Any] = field(default_factory=dict)
    #: The app's audit log, for attributing another user's writes.
    audit: Mapping[str, Any] = field(default_factory=dict)
    #: The person record's name and birth-date fields.
    names: Mapping[str, str] = field(
        default_factory=lambda: {
            "first": "first_name",
            "last": "last_name",
            "birth": "dob",
        }
    )
    path: Optional[Path] = None

    @property
    def name(self) -> str:
        return f"{self.app}@{self.version}"

    @property
    def agent(self) -> Mapping[str, str]:
        return self.users["agent"]

    @property
    def other(self) -> Mapping[str, str]:
        return self.users["other"]

    def entity(self, name: str) -> EntitySpec:
        try:
            return self.entities[name]
        except KeyError:
            raise ProfileError(f"{self.app} has no record type {name!r}") from None

    def by_table(self, table: str) -> EntitySpec:
        for e in self.entities.values():
            if e.table == table:
                return e
        raise ProfileError(f"{self.app} has no table {table!r}")

    def lookup_for(self, entity: str, field_name: str) -> Optional[str]:
        """The lookup record's field that holds a value for ``entity.field``."""
        for src, dst in self.lookup_fields.items():
            if dst.get("entity") == entity and dst.get("field") == field_name:
                return src
        return None


def _entity(name: str, raw: Mapping[str, Any]) -> EntitySpec:
    fields = []
    for fname, f in (raw.get("fields") or {}).items():
        ftype = str(f.get("type", "text"))
        if ftype not in FIELD_TYPES:
            raise ProfileError(f"{name}.{fname}: type {ftype!r} not in {FIELD_TYPES}")
        if ftype == "ref" and not f.get("entity"):
            raise ProfileError(f"{name}.{fname}: a ref field names its entity")
        fields.append(
            FieldSpec(
                name=str(fname),
                type=ftype,
                label=str(f.get("label", fname)),
                choices=tuple(str(c) for c in f.get("choices", ())),
                entity=f.get("entity"),
                format=f.get("format"),
                readonly=bool(f.get("readonly", False)),
                default=None if f.get("default") is None else str(f["default"]),
            )
        )
    ops = tuple(str(o) for o in raw.get("ops", ()))
    for op in ops:
        if op not in OPS:
            raise ProfileError(f"{name}: operation {op!r} not in {OPS}")
    parent = raw.get("parent")
    return EntitySpec(
        name=name,
        table=str(raw["table"]),
        label=str(raw.get("label", name)),
        title=str(raw.get("title", raw.get("label", name))),
        fields=tuple(fields),
        key=raw.get("key"),
        list=bool(raw.get("list", False)),
        search=tuple(raw.get("search", ())),
        ops=ops,
        parent=(str(parent["entity"]), str(parent["field"])) if parent else None,
        cancel=dict(raw.get("cancel") or {}),
        merge=dict(raw.get("merge") or {}),
        changes=tuple(raw.get("changes", ())),
        identify_by=raw.get("identify_by"),
    )


def load_profile(source: str | Path) -> Profile:
    """A profile by path, or by name from the bundled synthetic profiles."""

    path = Path(source)
    if not path.suffix:
        # By name: the bundled profiles, then each directory on
        # TASKGEN_PROFILE_PATH (held-out profiles are kept privately).
        dirs = [PROFILE_DIR] + [
            Path(d) for d in os.environ.get(PROFILE_PATH_ENV, "").split(os.pathsep) if d
        ]
        found = [d / f"{source}.yaml" for d in dirs if (d / f"{source}.yaml").is_file()]
        path = found[0] if found else PROFILE_DIR / f"{source}.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ProfileError(f"cannot read {path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != PROFILE_SCHEMA:
        raise ProfileError(f"{path}: not an {PROFILE_SCHEMA} document")
    entities = {n: _entity(n, e) for n, e in (raw.get("entities") or {}).items()}
    prof = Profile(
        app=str(raw["app"]),
        version=int(raw.get("version", 1)),
        title=str(raw.get("title", raw["app"])),
        users=dict(raw["users"]),
        entities=entities,
        person=str(raw["person"]),
        child=str(raw["child"]),
        lookup=str(raw["lookup"]),
        lookup_fields=dict(raw.get("lookup_fields") or {}),
        pools={k: list(v) for k, v in (raw.get("pools") or {}).items()},
        answers=dict(raw.get("answers") or {}),
        audit=dict(raw.get("audit") or {}),
        names={
            "first": "first_name",
            "last": "last_name",
            "birth": "dob",
            **dict(raw.get("person_fields") or {}),
        },
        path=path,
    )
    for role in (prof.person, prof.child, prof.lookup):
        prof.entity(role)
    if prof.entity(prof.person).key is None:
        raise ProfileError(f"{path}: the person record needs a key field")
    return prof
