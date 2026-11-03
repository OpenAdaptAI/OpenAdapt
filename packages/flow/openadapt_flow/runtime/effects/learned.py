"""Learned excuses: the app's own writes on each request, approved by a person.

Some apps write on every request: the acting user's ``last_login``, an API
token's ``last_seen``, a first-login row for the acting user. The
whole-database frame (``frame.database``) sees those rows move and refutes
every write, correct or not. ``openadapt-flow learn`` watches checked
traffic, proposes the moves a person may excuse, and on approval writes
them into the contract as ``frame.learned``. This module holds that key and
the one rule both the learner and the frame use to say how a moved row
relates to the write.

Only one relation can be excused:

- ``actor_row``: a row of the acting user. Each of its ``actor_columns``
  holds the actor's id, before and after the write. The users table's own
  row has its key there; a token or user-property row has its user columns
  there. Every user column the row was seen to hold the actor's id in must
  hold it: a token the actor issued for someone else is not the actor's.

Nothing else can be excused, however often it moves. In particular a
``target`` excuse (a column of the row the write declares) is refused when
the contract loads: the learner can't tell a column the app sets on the
written row from one the agent's own request sets under another field
name, so collateral on the written row stays a stop. A person excuses such
a column in the contract generator's bookkeeping answer instead. A learned excuse names
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
    #: Exactly the column set seen to move together: a changed row must
    #: move exactly these; an added row must set exactly these (every other
    #: column NULL).
    columns: list[str] = Field(min_length=1)
    change: LearnedChange
    relation: Relation
    #: ``actor_row`` only: the columns that must all hold the acting user's
    #: id (the users table's key, or the user columns of another table).
    actor_columns: Optional[list[str]] = None
    #: ``added`` ``actor_row`` only: the most such rows one check may add (the
    #: most seen in one learning check). More are collateral.
    max_rows: Optional[int] = Field(default=None, ge=1)

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
        if self.relation == "target":
            raise ValueError(
                "a learned excuse can't cover the written row (relation target): "
                "collateral on it stays a stop; excuse an app-set column in the "
                "contract generator's bookkeeping answer instead"
            )
        if self.relation == "actor_row":
            cols = self.actor_columns or []
            if not cols or any(not c or c.startswith("__") for c in cols):
                raise ValueError("an actor_row excuse names its actor_columns")
            if len(set(cols)) != len(cols):
                raise ValueError("an actor_row excuse lists each actor column once")
            self.actor_columns = sorted(cols)
            if self.change == "changed" and set(cols) & set(self.columns):
                raise ValueError(
                    "an actor_row excuse can't let an actor column change: the "
                    "row would move to another user"
                )
            if self.change == "added" and not set(cols) <= set(self.columns):
                raise ValueError(
                    "an added actor_row sets its actor columns: list them in columns"
                )
        elif self.actor_columns is not None:
            raise ValueError("actor_columns go with relation actor_row")
        added_actor = self.change == "added" and self.relation == "actor_row"
        if added_actor and self.max_rows is None:
            raise ValueError("an added actor_row excuse names its max_rows")
        if not added_actor and self.max_rows is not None:
            raise ValueError("max_rows goes with an added actor_row excuse")
        return self

    def label(self) -> str:
        """A short, value-free name for receipts: which excuse was used."""

        how = (
            f"{self.relation}:{'+'.join(self.actor_columns)}"
            if self.actor_columns
            else "target"
        )
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
            cols |= set(e.actor_columns or [])
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

    from openadapt_flow.runtime.effects.db_snapshot import KEY, ROW

    if learned is None or change not in ("changed", "added"):
        return None
    if change == "changed" and not fields:
        return None
    if any(f == NOT_COMPARED for f in fields):
        return None
    if change == "changed":
        # Over ``max_rows`` the baseline keeps cell MACs for some columns
        # only, so ``fields`` can't show a move in the others: a row whose
        # every column wasn't compared is never excused.
        if before is None or after is None:
            return None
        if any(c not in before for c in after if c not in (KEY, ROW)):
            return None
    for excuse in learned.excuses:
        if (
            excuse.table != table
            or excuse.change != change
            or excuse.relation != relation
        ):
            continue
        if set(fields) != set(excuse.columns):
            continue
        if relation == "actor_row":
            if actor is None or not excuse.actor_columns:
                continue
            if change == "changed" and (before is None or after is None):
                continue
            if change == "added" and (before is not None or after is None):
                continue
            if not all(
                holds_actor(c, actor, before, after, hasher)
                for c in excuse.actor_columns
            ):
                continue
        return excuse.label()
    return None
