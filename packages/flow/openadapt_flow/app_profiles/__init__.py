"""Per-app profiles: what an app writes on its own, whatever a run does.

One profile per app, versioned, shared by the whole-database frame
(``frame.database.profile``), the rollback check and the RL grader, so all
three excuse exactly the same bookkeeping and nothing else. A rule is
append-only, column-scoped, or per-session state (not compared, with a
reason); none may name a patient-data table except for a system column. The
receipt names the profile version and the rules it holds.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

HERE = Path(__file__).resolve().parent


class ProfileRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    #: A table name, or ``*`` for a column rule that applies to every table.
    table: str
    mode: Literal["append_only", "columns", "session"]
    columns: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> "ProfileRule":
        if (self.mode == "columns") != bool(self.columns):
            raise ValueError(f"rule {self.id}: columns go with mode columns only")
        if self.table == "*" and self.mode != "columns":
            raise ValueError(f"rule {self.id}: only a column rule may name every table")
        return self


class AppProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.app-profile/v1"]
    app: str
    version: str
    patient_table_prefixes: list[str]
    system_columns: list[str] = Field(default_factory=list)
    rules: list[ProfileRule]

    @model_validator(mode="after")
    def _no_patient_data(self) -> "AppProfile":
        prefixes = tuple(self.patient_table_prefixes)
        for rule in self.rules:
            on_patient_table = rule.table == "*" or rule.table.startswith(prefixes)
            if not on_patient_table:
                continue
            extra = set(rule.columns) - set(self.system_columns)
            if rule.mode != "columns" or extra:
                raise ValueError(
                    f"rule {rule.id} would excuse patient data in {rule.table!r}: "
                    "only system columns may be named there"
                )
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self

    def sha256(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def allow_tables(self, present: Optional[set[str]] = None) -> list[dict[str, str]]:
        """``frame.database.allow_tables`` entries: append-only, or skipped
        (session) with the rule's reason. ``present``: keep only tables the
        database has (a profile covers app versions with more tables)."""

        return [
            {
                "table": r.table,
                "mode": "append_only" if r.mode == "append_only" else "skip",
                "reason": f"{self.version} {r.id}: {r.reason}",
            }
            for r in self.rules
            if r.mode in ("append_only", "session")
            and (present is None or r.table in present)
        ]

    def ignore_columns(self) -> dict[str, list[str]]:
        """``frame.database.ignore_columns``: ``*`` applies to every table."""

        out: dict[str, list[str]] = {}
        for r in self.rules:
            if r.mode == "columns":
                out.setdefault(r.table, []).extend(r.columns)
        return out


@lru_cache(maxsize=None)
def load_profile(name: str) -> AppProfile:
    """A shipped profile by app name (``openemr``)."""

    path = HERE / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"no app profile named {name!r}")
    return AppProfile.model_validate(yaml.safe_load(path.read_text()))
