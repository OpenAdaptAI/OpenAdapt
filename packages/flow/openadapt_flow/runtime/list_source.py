"""Read every row of an on-screen list or worktable, page by page.

Legacy applications show records in lists: a search result grid, a work
queue, a ledger. This module walks such a list to the end and hands back one
row per record, keyed by the record schema's identity fields
(:class:`openadapt_flow.records.schema.RecordSchema`). It is channel-agnostic:
the caller supplies a :class:`PageReader` (clipboard copy of the grid, the
accessibility tree, or OCR) and a :class:`Pager` (a Next button, Page Down,
a scroll).

Rules:

- Before every read, the :class:`CopyGuard` checks that the expected window
  has focus and that no modal dialog is showing. A known, reversible modal
  with a taught dismissal is dismissed and the check runs again. Anything
  else halts.
- Rows are deduplicated by identity. Two rows with the same identity and
  different content halt the run: the list is ambiguous.
- Paging stops when the pager reports the end or a page is empty. A page
  that repeats rows already read after the pager said it advanced halts as
  ``pager_stalled``, unless the application is known to repeat its last
  page (:attr:`ListLimits.repeat_page_is_end`).
- Caps on pages and rows halt the run. The list is never silently cut short.
- Searches that the application caps (for example "first 1,000 results")
  are split by :func:`partition_search` until every part is under the cap.
  A part that is truncated when walked, or whose reported count differs
  from the rows read, halts.

Every stop that is not a clean end raises :class:`ListHalt` with a reason a
person can act on. Nothing here calls a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import (
    Callable,
    Generic,
    Iterator,
    Literal,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    TypeVar,
)

from openadapt_flow.records.schema import RecordSchema

HaltReason = Literal[
    "focus_lost",
    "modal_present",
    "modal_not_cleared",
    "row_identity_missing",
    "duplicate_identity_conflict",
    "page_cap_reached",
    "row_cap_reached",
    "partition_unsplittable",
    "partition_cap_reached",
    "partition_truncated",
    "partition_count_mismatch",
    "pager_stalled",
]

EndReason = Literal["pager_end", "empty_page", "repeat_page"]


class ListHalt(Exception):
    """The list walk stopped before a clean end and needs a person."""

    def __init__(self, reason: HaltReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: HaltReason = reason
        self.detail = detail


class PageReader(Protocol):
    """Reads the rows visible on the current page of the list."""

    def read_page(self) -> list[dict[str, str]]: ...


class Pager(Protocol):
    """Moves the list to its next page."""

    def next_page(self) -> bool:
        """Advance; return False when there is no next page."""
        ...


# -- focus and modal checks ------------------------------------------------


@dataclass(frozen=True)
class KnownModal:
    """A modal dialog the workflow was taught to recognize.

    Attributes:
        name: Shown in reports and halts.
        detect: Returns True while the modal is showing.
        dismiss: The taught dismissal (for example pressing Escape). None
            means the modal always halts the run.
        reversible: Dismissing it changes no data. Only reversible modals
            are dismissed automatically.
    """

    name: str
    detect: Callable[[], bool]
    dismiss: Optional[Callable[[], None]] = None
    reversible: bool = False


@dataclass
class CopyGuard:
    """Focus check and no-modal check, run before every copy or read.

    Attributes:
        focused_window: Returns the identity of the window with focus (a
            title or handle), or None when unknown.
        expected_window: The list window's identity, a string or regex.
        known_modals: Modals the workflow knows about.
        modal_open: Optional generic detector for ANY modal (for example a
            window-stack or accessibility query). Returns a description of
            the modal or None.
        max_dismissals: Dismissals allowed per check before halting.
    """

    focused_window: Callable[[], Optional[str]]
    expected_window: str
    known_modals: Sequence[KnownModal] = ()
    modal_open: Optional[Callable[[], Optional[str]]] = None
    max_dismissals: int = 2
    dismissed: list[str] = field(default_factory=list)

    def _focus_ok(self) -> Optional[str]:
        current = self.focused_window()
        if current is None:
            return None
        if current == self.expected_window or re.fullmatch(
            self.expected_window, current
        ):
            return current
        return None

    def check(self) -> None:
        """Raise :class:`ListHalt` unless it is safe to copy now."""
        for _ in range(self.max_dismissals + 1):
            showing = [m for m in self.known_modals if m.detect()]
            if showing:
                modal = showing[0]
                if modal.dismiss is None or not modal.reversible:
                    raise ListHalt(
                        "modal_present", f"known modal {modal.name!r} needs a person"
                    )
                modal.dismiss()
                self.dismissed.append(modal.name)
                continue
            if self.modal_open is not None:
                other = self.modal_open()
                if other:
                    raise ListHalt("modal_present", f"unrecognized modal: {other}")
            if self._focus_ok() is None:
                raise ListHalt(
                    "focus_lost",
                    f"expected window {self.expected_window!r} does not have focus",
                )
            return
        raise ListHalt(
            "modal_not_cleared",
            f"modal still showing after {self.max_dismissals} dismissals",
        )


# -- list walk -------------------------------------------------------------


@dataclass(frozen=True)
class ListRow:
    """One record read from the list."""

    key: tuple[str, ...]
    values: Mapping[str, str]
    page: int


@dataclass
class ListWalk:
    """Summary of one walk, filled in as rows are yielded."""

    pages: int = 0
    rows: int = 0
    duplicates_skipped: int = 0
    ended_by: Optional[EndReason] = None


@dataclass(frozen=True)
class ListLimits:
    """Caps. Reaching one halts; nothing is silently dropped."""

    max_pages: int = 1_000
    max_rows: int = 100_000
    #: The application re-shows its last page when asked for the next one.
    repeat_page_is_end: bool = False


def _norm(value: object) -> str:
    return " ".join(str(value).split()).casefold()


def row_key(row: Mapping[str, str], schema: RecordSchema) -> tuple[str, ...]:
    """Identity of a row: its normalized identity-field values."""
    key = []
    for name in schema.identity_fields:
        value = row.get(name)
        if value is None or not str(value).strip():
            raise ListHalt(
                "row_identity_missing", f"row has no value for identity field {name!r}"
            )
        key.append(_norm(value))
    return tuple(key)


def _guarded_read(
    reader: PageReader, guard: Optional[CopyGuard]
) -> Sequence[Mapping[str, str]]:
    """Read one page with the guard checked before and after the read.

    A modal or focus change that appears during the copy would leave the
    clipboard holding the previous page. If the check after the read had to
    dismiss a modal, the page is read again; any other failure halts.
    """
    if guard is None:
        return reader.read_page()
    for _ in range(guard.max_dismissals + 1):
        guard.check()
        page_rows = reader.read_page()
        before = len(guard.dismissed)
        guard.check()
        if len(guard.dismissed) == before:
            return page_rows
    raise ListHalt("modal_not_cleared", "a modal kept appearing during the copy")


def iterate_list(
    reader: PageReader,
    pager: Pager,
    schema: RecordSchema,
    *,
    guard: Optional[CopyGuard] = None,
    limits: ListLimits = ListLimits(),
    seen: Optional[dict[tuple[str, ...], Mapping[str, str]]] = None,
    walk: Optional[ListWalk] = None,
) -> Iterator[ListRow]:
    """Yield each record in the list once, paging to the end.

    Args:
        reader: Reads the visible rows.
        pager: Advances to the next page.
        schema: Gives the identity fields used to deduplicate rows.
        guard: Focus and no-modal check run before every read.
        limits: Page and row caps.
        seen: Rows already read (shared across search partitions).
        walk: Receives the summary.
    """
    seen = {} if seen is None else seen
    walk = ListWalk() if walk is None else walk
    fields = [f.name for f in schema.fields]
    local_pages = 0
    while True:
        if walk.pages >= limits.max_pages:
            raise ListHalt("page_cap_reached", f"read {walk.pages} pages")
        page_rows = _guarded_read(reader, guard)
        walk.pages += 1
        local_pages += 1
        if not page_rows:
            if local_pages > 1:
                # The pager said it advanced, so an empty page is a lost
                # click or a failed copy, not the end of the list.
                raise ListHalt(
                    "pager_stalled",
                    f"page {walk.pages} was empty after the pager advanced",
                )
            walk.ended_by = "empty_page"
            return
        # Check the whole page before any of its rows go downstream.
        fresh: list[ListRow] = []
        page_seen: dict[tuple[str, ...], Mapping[str, str]] = {}
        for raw in page_rows:
            key = row_key(raw, schema)
            values = {k: str(raw[k]) for k in fields if k in raw}
            prior = seen.get(key, page_seen.get(key))
            if prior is not None:
                if {k: _norm(v) for k, v in prior.items()} != {
                    k: _norm(v) for k, v in values.items()
                }:
                    raise ListHalt(
                        "duplicate_identity_conflict",
                        f"two rows share identity {key} with different values",
                    )
                walk.duplicates_skipped += 1
                continue
            if walk.rows + len(fresh) >= limits.max_rows:
                raise ListHalt("row_cap_reached", f"read {walk.rows} rows")
            page_seen[key] = values
            fresh.append(ListRow(key=key, values=values, page=walk.pages))
        new = len(fresh)
        for row in fresh:
            seen[row.key] = row.values
            walk.rows += 1
            yield row
        if new == 0 and local_pages > 1:
            if limits.repeat_page_is_end:
                walk.ended_by = "repeat_page"
                return
            raise ListHalt(
                "pager_stalled",
                f"page {walk.pages} added no new rows after the pager advanced",
            )
        if not pager.next_page():
            walk.ended_by = "pager_end"
            return


# -- capped searches -------------------------------------------------------

R = TypeVar("R")


@dataclass(frozen=True)
class SearchResult(Generic[R]):
    """What the application reports after running a search over one range.

    Attributes:
        count: Results the application says it found, if it says.
        truncated: The application showed only part of the results (for
            example a "more than 1,000 results" banner, or ``count`` at the
            cap).
    """

    count: Optional[int]
    truncated: bool


def partition_search(
    run_search: Callable[[R], SearchResult[R]],
    whole: R,
    split: Callable[[R], Optional[Sequence[R]]],
    *,
    max_partitions: int = 4_096,
) -> list[R]:
    """Split a search range until no part is truncated.

    Args:
        run_search: Runs the search over one range and reports the result.
        whole: The full range, for example a date range.
        split: Splits a range into smaller ranges, or returns None when it
            cannot be split further.
        max_partitions: Halt rather than run more searches than this.

    Returns:
        The ranges to walk, in order. Each one returned under the cap.
    """
    done: list[R] = []
    todo: list[R] = [whole]
    searches = 0
    while todo:
        part = todo.pop(0)
        searches += 1
        if searches > max_partitions:
            raise ListHalt("partition_cap_reached", f"ran {searches - 1} searches")
        result = run_search(part)
        if not result.truncated:
            done.append(part)
            continue
        pieces = split(part)
        if not pieces or len(pieces) < 2:
            raise ListHalt(
                "partition_unsplittable",
                f"range {part!r} is still truncated and cannot be split",
            )
        todo[0:0] = list(pieces)
    return done


DateRange = tuple[date, date]


def split_date_range(r: DateRange) -> Optional[list[DateRange]]:
    """Halve an inclusive date range; None for a single day."""
    start, end = r
    if end <= start:
        return None
    mid = start + timedelta(days=(end - start).days // 2)
    return [(start, mid), (mid + timedelta(days=1), end)]


def iterate_partitioned(
    open_partition: Callable[
        [R],
        tuple[PageReader, Pager] | tuple[PageReader, Pager, SearchResult[R]],
    ],
    partitions: Sequence[R],
    schema: RecordSchema,
    *,
    guard: Optional[CopyGuard] = None,
    limits: ListLimits = ListLimits(),
    walk: Optional[ListWalk] = None,
) -> Iterator[ListRow]:
    """Walk each partition's list, deduplicating rows across partitions.

    ``open_partition`` may also return the :class:`SearchResult` the
    application showed when the partition was opened. Data can change between
    :func:`partition_search` and the walk, so a partition that is truncated
    now halts, and one whose reported count differs from the rows read halts.
    """
    seen: dict[tuple[str, ...], Mapping[str, str]] = {}
    walk = ListWalk() if walk is None else walk
    for part in partitions:
        opened = open_partition(part)
        reader, pager = opened[0], opened[1]
        result = opened[2] if len(opened) == 3 else None
        if result is not None and result.truncated:
            raise ListHalt(
                "partition_truncated", f"range {part!r} is truncated when walked"
            )
        before = walk.rows + walk.duplicates_skipped
        yield from iterate_list(
            reader, pager, schema, guard=guard, limits=limits, seen=seen, walk=walk
        )
        read = walk.rows + walk.duplicates_skipped - before
        if result is not None and result.count is not None and read != result.count:
            raise ListHalt(
                "partition_count_mismatch",
                f"range {part!r} reported {result.count} results; read {read}",
            )
