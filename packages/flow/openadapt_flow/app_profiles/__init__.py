"""Per-app profiles: what an app writes on its own, whatever a run does.

One profile per app, versioned, shared by the whole-database frame
(``frame.database.profile``), the rollback check and the RL grader, so all
three excuse exactly the same bookkeeping and nothing else. Every table a
profile names is compared; a rule only says which changes in it are the
app's own:

- ``append_only``: rows may be added; an existing row may change only in the
  rule's ``columns`` (none, by default) and may never go.
- ``columns``: an existing row may change in the named columns, and no row
  may be added or removed. Narrowed by ``fill_only`` (only from NULL or empty
  to a value: a backfill, never a swap, overwrite or clear), ``with_fill``
  (only when that column was backfilled in the same row) or ``owner`` (only
  on the acting user's own row).
- ``own_rows``: per-user UI state. The acting user's own rows may be added,
  changed or removed; everyone else's are compared. Another user's change to
  their own row is not excused here: the frame attributes it to them only
  when their own write entry in the audit window names the table
  (owner-scoped attribution, ``FrameSpec.owners``). Credentials never use
  this: their column rules stay on the acting user's row.

A rule may name only a table in the profile's reviewed ``bookkeeping_tables``
(lower-cased, unqualified), so anything else, patient data included, is
never excused; ``*`` may name only system columns, as a backfill. Profiles
load only from this package, by name, and a contract pins the digest.
"""

from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Optional, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

HERE = Path(__file__).resolve().parent
_NAME = re.compile(r"^[a-z0-9_-]+$")


def table_name(name: str) -> str:
    """How a profile compares table names: lower-cased, without a schema."""

    return name.rsplit(".", 1)[-1].lower()


class Owner(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: The column naming the row's user.
    column: str
    #: What it holds: the user's sign-in name, or their id in the users table.
    by: Literal["username", "user_id"]


class ProfileRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    #: A table name, or ``*`` for a system-column backfill in every table.
    table: str
    mode: Literal["append_only", "columns", "own_rows"]
    columns: list[str] = Field(default_factory=list)
    fill_only: bool = False
    with_fill: Optional[str] = None
    owner: Optional[Owner] = None
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> "ProfileRule":
        if self.mode == "columns" and not self.columns:
            raise ValueError(f"rule {self.id}: a column rule names its columns")
        if self.mode == "own_rows" and (self.owner is None or self.columns):
            raise ValueError(f"rule {self.id}: own_rows names an owner, not columns")
        if self.mode != "columns" and (self.fill_only or self.with_fill):
            raise ValueError(f"rule {self.id}: fill conditions go with column rules")
        if self.table == "*" and not (self.mode == "columns" and self.fill_only):
            raise ValueError(f"rule {self.id}: every table only for a backfill")
        return self

    @property
    def conditional(self) -> bool:
        return bool(self.fill_only or self.with_fill or self.owner) or (
            self.mode == "own_rows"
        )


class UsersTable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    id_column: str
    name_column: str


class AppProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["openadapt.app-profile/v2"]
    app: str
    version: str
    #: The reviewed tables the app writes on its own. A rule may name only
    #: these; every other table, patient data included, is never excused.
    bookkeeping_tables: list[str]
    #: Columns a ``*`` backfill rule may name.
    system_columns: list[str] = Field(default_factory=list)
    #: Where the acting user's id is looked up from their sign-in name.
    users: Optional[UsersTable] = None
    rules: list[ProfileRule]

    @model_validator(mode="after")
    def _guard(self) -> "AppProfile":
        allowed = {table_name(t) for t in self.bookkeeping_tables}
        for rule in self.rules:
            if rule.table == "*":
                extra = set(rule.columns) - set(self.system_columns)
                if extra:
                    raise ValueError(
                        f"rule {rule.id}: every table only for system columns, "
                        f"not {sorted(extra)}"
                    )
            elif table_name(rule.table) not in allowed:
                raise ValueError(
                    f"rule {rule.id}: {rule.table!r} is not a reviewed bookkeeping "
                    "table, so the rule could excuse patient data"
                )
            if rule.owner is not None and rule.owner.by == "user_id" and not self.users:
                raise ValueError(f"rule {rule.id}: owner by user_id needs users")
        ids = [r.id for r in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError("rule ids must be unique")
        return self

    def sha256(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True)
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()

    def allow_tables(self, present: Optional[set[str]] = None) -> list[dict[str, str]]:
        """``frame.database.allow_tables`` entries for append-only tables."""

        return [
            {
                "table": r.table,
                "mode": "append_only",
                "reason": f"{self.version} {r.id}: {r.reason}",
            }
            for r in self.rules
            if r.mode == "append_only" and (present is None or r.table in present)
        ]

    def ignore_columns(self) -> dict[str, list[str]]:
        """Columns left out of each row's contents MAC: unconditional column
        rules, and the columns an append-only table's rows may change in."""

        out: dict[str, list[str]] = {}
        for r in self.rules:
            if r.columns and not r.conditional:
                out.setdefault(r.table, []).extend(r.columns)
        return out

    def ref_columns(self) -> list[str]:
        """Columns the conditional rules read: kept as cell MACs in every
        table, however large."""

        cols: set[str] = set()
        for r in self.rules:
            if r.conditional:
                cols |= set(r.columns)
                cols |= {r.with_fill} if r.with_fill else set()
                cols |= {r.owner.column} if r.owner else set()
        return sorted(cols)

    def actor_ids_query(self, names: Sequence[str], dialect: str) -> Optional[str]:
        """The SELECT that finds the acting user's ids, read in the snapshot."""

        if self.users is None or not names:
            return None
        quote = (lambda n: f"`{n}`") if dialect == "mysql" else (lambda n: f'"{n}"')
        listed = ", ".join("'" + n.replace("'", "''") + "'" for n in names)
        u = self.users
        return (
            f"SELECT {quote(u.id_column)} AS id FROM {quote(u.table)} "
            f"WHERE {quote(u.name_column)} IN ({listed})"
        )

    def users_query(self, dialect: str) -> Optional[str]:
        """The SELECT of every user's id and sign-in name, read in the
        snapshot (in memory only), to find a colleague's own rows."""

        if self.users is None:
            return None
        quote = (lambda n: f"`{n}`") if dialect == "mysql" else (lambda n: f'"{n}"')
        u = self.users
        return (
            f"SELECT {quote(u.id_column)} AS id, {quote(u.name_column)} AS name "
            f"FROM {quote(u.table)}"
        )

    def excuse(
        self,
        table: str,
        before: Optional[Mapping[str, Any]],
        after: Optional[Mapping[str, Any]],
        fields: Sequence[str],
        hasher: Callable[[str, Any], str],
        names: set[str],
        ids: set[str],
    ) -> Optional[list[str]]:
        """The rules that make this row change the app's own, or None.

        ``before`` holds the baseline's cell MACs (``hasher`` MACs a value
        for a column of this table); ``after`` holds plain values. A row
        added has no ``before``, a row removed no ``after``; ``fields`` are
        the columns that changed. ``names`` and ``ids`` are the acting
        user's.
        """

        name = table_name(table)
        rules = [r for r in self.rules if r.table == "*" or table_name(r.table) == name]

        def owned(rule: ProfileRule) -> bool:
            assert rule.owner is not None
            who = names if rule.owner.by == "username" else ids
            col = rule.owner.column
            if after is not None:
                return str(after.get(col)) in who
            return before is not None and any(
                hasher(col, w) == before.get(col) for w in who
            )

        def filled(col: str) -> bool:
            if before is None or after is None:
                return False
            empty = before.get(col) in (hasher(col, None), hasher(col, ""))
            return empty and after.get(col) not in (None, "")

        if before is None or after is None:
            for rule in rules:
                if rule.mode == "own_rows" and owned(rule):
                    return [rule.id]
            return None
        used: list[str] = []
        for field in fields:
            for rule in rules:
                if rule.mode == "own_rows":
                    ok = owned(rule)
                elif field not in rule.columns or not rule.conditional:
                    continue
                else:
                    ok = (not rule.fill_only or filled(field)) and (
                        not rule.with_fill or filled(rule.with_fill)
                    )
                    ok = ok and (rule.owner is None or owned(rule))
                if ok:
                    used.append(rule.id)
                    break
            else:
                return None
        return sorted(set(used))


@lru_cache(maxsize=None)
def load_profile(name: str) -> AppProfile:
    """A profile shipped in this package, by app name (``openemr``)."""

    if not _NAME.match(name):
        raise ValueError(f"an app profile is named by app ([a-z0-9_-]), not {name!r}")
    path = HERE / f"{name}.yaml"
    if not path.exists():
        raise ValueError(f"no app profile named {name!r}")
    return AppProfile.model_validate(yaml.safe_load(path.read_text()))
