"""Learned excuses: the app's own writes on each request, approved by a person.

Some apps write on every request: the acting user's ``last_login``, an API
token's ``last_seen``, a first-login row for the acting user. The
whole-database frame (``frame.database``) sees those rows move and refutes
every write, correct or not. ``openadapt-flow learn`` watches checked
traffic, proposes the moves a person may excuse, and on approval writes
them into the contract as ``frame.learned``. This module holds that key and
the one rule both the learner and the frame use to say how a moved row
relates to the write.

A row relates to the write in one of two ways, or not at all:

- ``target``: a row the write declares or adds (its effects' records and
  side records). The frame already knows these rows.
- ``actor_row``: a row of the acting user. Its ``actor_column`` holds the
  actor's id, before and after the write. The users table's own row has its
  key there; a token or user-property row has its user column there.

Nothing else can be excused, however often it moves. A learned excuse names
a table, the exact columns, the change (``changed`` or ``added``, never a
removal) and the relation. At check time the relation is derived again from
this run's actor and declared rows, so an excuse never reaches another
user's row, another column, a look-alike of the target, or a row added for
someone else. See ``docs/LEARNED_EXCUSES.md``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Relation = Literal["target", "actor_row"]
LearnedChange = Literal["changed", "added"]

#: What a moved row is called when its compared columns can't be named.
NOT_COMPARED = "(columns not compared)"

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class LearnedExcuse(BaseModel):
    """One move a person approved: this table, these columns, this change,
    on a row with this relation to the write."""

    model_config = ConfigDict(extra="forbid")

    table: str = Field(min_length=1)
    #: Exactly the columns seen to move: a changed row may move only these;
    #: an added row may set only these (any other column stays NULL).
    columns: list[str] = Field(min_length=1)
    change: LearnedChange
    relation: Relation
    #: ``actor_row`` only: the column that holds the acting user's id (the
    #: users table's key, or a user column of another table).
    actor_column: Optional[str] = None

    @field_validator("table")
    @classmethod
    def _plain_table(cls, value: str) -> str:
        if "*" in value or value.strip() != value:
            raise ValueError(f"a learned excuse names one table, not {value!r}")
        return value

    @field_validator("columns")
    @classmethod
    def _plain_columns(cls, value: list[str]) -> list[str]:
        for col in value:
            if not col or "*" in col or col == NOT_COMPARED or col.startswith("__"):
                raise ValueError(f"a learned excuse names real columns, not {col!r}")
        if len(set(value)) != len(value):
            raise ValueError("a learned excuse lists each column once")
        return sorted(value)

    @model_validator(mode="after")
    def _shape(self) -> "LearnedExcuse":
        if self.relation == "actor_row":
            if not self.actor_column:
                raise ValueError("an actor_row excuse names its actor_column")
            if self.change == "changed" and self.actor_column in self.columns:
                raise ValueError(
                    "an actor_row excuse can't let the actor column change: the "
                    "row would move to another user"
                )
            if self.change == "added" and self.actor_column not in self.columns:
                raise ValueError(
                    "an added actor_row sets its actor column: list it in columns"
                )
        elif self.actor_column is not None:
            raise ValueError("actor_column goes with relation actor_row")
        return self

    def label(self) -> str:
        """A short, value-free name for receipts: which excuse was used."""

        how = f"{self.relation}:{self.actor_column}" if self.actor_column else "target"
        return f"learned:{self.table}:{self.change}:{how}:{','.join(self.columns)}"


class LearnedFrame(BaseModel):
    """``frame.learned``: the excuses a person approved from a proposal."""

    model_config = ConfigDict(extra="forbid")

    excuses: list[LearnedExcuse] = Field(min_length=1)
    #: The proposal this approval came from, and who approved it, when.
    proposal_sha256: str
    approved_by: str = Field(min_length=1)
    approved_at: str = Field(min_length=1)

    @field_validator("proposal_sha256")
    @classmethod
    def _digest(cls, value: str) -> str:
        if not _DIGEST.match(value):
            raise ValueError("proposal_sha256 is sha256:<64 hex>")
        return value

    def columns_to_keep(self) -> list[str]:
        """Columns the snapshot must keep per row, however large the table,
        so an excuse can be checked (the actor column and the moved ones)."""

        cols: set[str] = set()
        for e in self.excuses:
            cols |= set(e.columns)
            if e.actor_column:
                cols.add(e.actor_column)
        return sorted(cols)


def text(value: Any) -> Optional[str]:
    """A stored value as exact text (``db_snapshot.canonical``); None for
    NULL. An id stored as ``2`` and the actor id ``"2"`` are the same text."""

    from openadapt_flow.runtime.effects.db_snapshot import canonical

    if value is None:
        return None
    return canonical(value)


def actor_of(run_actors: Sequence[str]) -> Optional[str]:
    """The acting user's id for this run: the actor's id (``Actor.id``, the
    receipt's executor id), which a deployment that learns sets to the
    agent's user id in the app. None when there isn't exactly one (then
    nothing is the actor's row)."""

    ids = {str(a) for a in run_actors if str(a)}
    if len(ids) != 1:
        return None
    return text(next(iter(ids)))


def holds_actor(
    column: str,
    actor: str,
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
    hasher: Optional[Callable[[str, Any], str]],
) -> bool:
    """Does ``column`` hold the actor's id on every side the row has? The
    baseline row holds cell MACs (``hasher``), the row now plain values. A
    column the baseline didn't keep can't be shown to hold it: False."""

    if before is None and after is None:
        return False
    if before is not None:
        if hasher is None or column not in before:
            return False
        # ``canonical`` gives the number 2 and the text "2" one form.
        if before.get(column) != hasher(column, actor):
            return False
    if after is not None:
        if column not in after or text(after.get(column)) != actor:
            return False
    return True


def actor_columns(
    actor: Optional[str],
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
    hasher: Optional[Callable[[str, Any], str]],
) -> list[str]:
    """The columns that hold the actor's id on every side of a moved row.
    Names only, for the learner; :func:`holds_actor` decides each."""

    from openadapt_flow.runtime.effects.db_snapshot import KEY, ROW

    if actor is None:
        return []
    names = set(after or {}) | set(before or {})
    return sorted(
        c for c in names - {KEY, ROW} if holds_actor(c, actor, before, after, hasher)
    )


def set_columns(after: Mapping[str, Any]) -> list[str]:
    """The columns an added row sets (holds a value other than NULL)."""

    from openadapt_flow.runtime.effects.db_snapshot import KEY, ROW

    return sorted(c for c, v in after.items() if c not in (KEY, ROW) and v is not None)


def covers(
    learned: Optional[LearnedFrame],
    *,
    table: str,
    change: str,
    relation: Relation,
    fields: Sequence[str],
    before: Optional[Mapping[str, Any]],
    after: Optional[Mapping[str, Any]],
    hasher: Optional[Callable[[str, Any], str]],
    actor: Optional[str],
) -> Optional[str]:
    """The label of the learned excuse that covers this move, or None.

    ``fields`` are the columns that changed (``changed``), or for an added
    row the columns it set beyond what the write declares. ``relation`` is
    what the frame derived for this row in this run: ``target`` for a row
    the write declares, else ``actor_row`` (checked here, against
    ``actor``). Fail closed: an unnamed column, a removal, an actor column
    that moved or can't be read, or any column outside the excuse.
    """

    if learned is None or change not in ("changed", "added"):
        return None
    if change == "changed" and not fields:
        return None
    if any(f == NOT_COMPARED for f in fields):
        return None
    for excuse in learned.excuses:
        if (
            excuse.table != table
            or excuse.change != change
            or excuse.relation != relation
        ):
            continue
        if not set(fields) <= set(excuse.columns):
            continue
        if relation == "actor_row":
            if actor is None or not excuse.actor_column:
                continue
            if change == "changed" and (before is None or after is None):
                continue
            if change == "added" and (before is not None or after is None):
                continue
            if not holds_actor(excuse.actor_column, actor, before, after, hasher):
                continue
        return excuse.label()
    return None
