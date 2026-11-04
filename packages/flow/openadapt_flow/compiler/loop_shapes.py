"""Loops that are not back-to-back repeats, and their per-item checks.

:mod:`openadapt_flow.compiler.induction` collapses a body that repeats step
for step. Real worklists rarely look like that:

- **search, open, act, return.** Each row is found, opened, worked and left
  ("Back to list"). The last row usually ends without the return, so the
  repeats are not identical.
- **row-varying bodies.** A field is filled for some rows and left empty for
  others; a step appears only for rows in some state.

This module holds the deterministic, model-free pieces induction uses for
those shapes:

- :func:`segment` finds the iterations that start at a position: chunks that
  begin with the same step and share a core, the last of which may stop
  before the trailing return;
- :func:`merge` lines up the iterations' steps into body positions (present
  in every iteration, or only in some);
- :func:`band_identity` decides whether a record-selecting click can verify
  each row's record, and which extra row columns that needs;
- :func:`bind_effect_columns` rewrites a write's record check so each row is
  checked against its own values, not the demonstrated row's.

Nothing here emits a program or decides certification; induction does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Iterator, Optional, Sequence

from openadapt_flow.ir import Anchor

#: A full iteration may carry this many steps outside the shared core before
#: the chunk is taken for unrelated work (scaled up for long bodies).
MAX_EXTRA_STEPS = 3


@dataclass
class Position:
    """One body position: its signature and where each iteration has it."""

    sig: Any
    at: dict[int, int] = field(default_factory=dict)  # iteration -> index


def merge(seqs: Sequence[Sequence[Any]]) -> list[Position]:
    """Line up several signature sequences into ordered body positions.

    The first sequence seeds the positions; each later one is folded in with
    a ``difflib`` alignment. A replaced run keeps both sides as separate
    positions (each present only where it was seen).
    """

    if not seqs:
        return []
    positions = [Position(sig=s, at={0: j}) for j, s in enumerate(seqs[0])]
    for t in range(1, len(seqs)):
        b = list(seqs[t])
        sm = SequenceMatcher(a=[p.sig for p in positions], b=b, autojunk=False)
        merged: list[Position] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                for k in range(i2 - i1):
                    positions[i1 + k].at[t] = j1 + k
                    merged.append(positions[i1 + k])
                continue
            if tag in ("delete", "replace"):
                merged.extend(positions[i1:i2])
            if tag in ("insert", "replace"):
                merged.extend(Position(sig=b[j], at={t: j}) for j in range(j1, j2))
        positions = merged
    return positions


@dataclass
class Segmentation:
    """Iterations found at one start position of one trace."""

    #: ``(start, end)`` of each iteration, end exclusive. The last one may
    #: stop before the trailing return.
    chunks: list[tuple[int, int]]
    #: Positions of the merged body (over the full iterations only).
    positions: list[Position]
    #: Indices into ``positions`` of the core shared by every full iteration.
    core: list[int]
    #: Indices into ``positions`` the last iteration did not reach (a suffix
    #: of the core): the return to the list.
    tail: list[int]

    @property
    def end(self) -> int:
        return self.chunks[-1][1]


def _match_last(
    sigs: Sequence[Any], start: int, positions: list[Position]
) -> tuple[int, int, int]:
    """Greedy match of the last iteration against the body.

    Returns ``(end, reached, matched)``: the trace index after the last
    matched step, the body index matching stopped at, and how many steps
    matched. Positions the last iteration skips become row-varying steps
    (or, at the end of the body, the return it never made).
    """

    ptr = 0
    j = start
    matched = 0
    while j < len(sigs) and ptr < len(positions):
        hit = next(
            (q for q in range(ptr, len(positions)) if positions[q].sig == sigs[j]),
            None,
        )
        if hit is None:
            break
        ptr = hit + 1
        j += 1
        matched += 1
    return j, ptr, matched


def segment(sigs: Sequence[Any], start: int) -> Iterator[Segmentation]:
    """Candidate iterations starting at ``start``, most iterations first.

    Every occurrence of ``sigs[start]`` after ``start`` may begin a new
    iteration. A candidate needs two or more iterations, a core of at least
    two steps shared by every full iteration, few extra steps per iteration,
    and a last iteration that matches at least half the core in order (what
    it skips at the end is the return it never made). Callers check the steps themselves (that the core varies
    by row, that the tail only navigates).
    """

    s0 = sigs[start]
    cands = [p for p in range(start, len(sigs)) if sigs[p] == s0]
    for k in range(len(cands), 1, -1):
        full = [(cands[m], cands[m + 1]) for m in range(k - 1)]
        positions = merge([list(sigs[a:b]) for a, b in full])
        n_full = len(full)
        core = [q for q, pos in enumerate(positions) if len(pos.at) == n_full]
        if len(core) < 2:
            continue
        allowed = max(MAX_EXTRA_STEPS, len(core) // 2)
        if any((b - a) - len(core) > allowed for a, b in full):
            continue
        last_start = cands[k - 1]
        end, reached, matched = _match_last(sigs, last_start, positions)
        if matched < max(2, (len(core) + 1) // 2):
            continue
        tail = [q for q in core if q >= reached]
        if len(tail) >= len(core) - 1:
            continue
        yield Segmentation(
            chunks=full + [(last_start, end)],
            positions=positions,
            core=core,
            tail=tail,
        )


# ---------------------------------------------------------------------------
# Identity of the row a record-selecting click opens
# ---------------------------------------------------------------------------


@dataclass
class BandIdentity:
    """Whether a record-selecting click can verify each row's record."""

    ok: bool
    reason: str = ""
    #: Extra row columns the identity band needs: name -> per-iteration
    #: value (None where the band was not readable for that iteration).
    identity_columns: dict[str, dict[int, Optional[str]]] = field(default_factory=dict)
    #: Extra columns' demonstrated values (from the template iteration).
    identity_examples: dict[str, str] = field(default_factory=dict)
    #: Hashed bands: token indices to index under each column name.
    token_indices: dict[str, list[int]] = field(default_factory=dict)
    basis: str = ""


def _squash(text: str) -> str:
    return "".join(str(text).casefold().split())


def _band(anchor: Anchor) -> Optional[str]:
    return anchor.structured_identity or anchor.context_text


def band_identity(
    *,
    column: str,
    template_iter: int,
    anchors: dict[int, Anchor],
    row_values: dict[int, dict[str, str]],
    same_trace: dict[int, int],
    column_prefix: str,
) -> BandIdentity:
    """Can the click at this body position verify the record of every row?

    Args:
        column: The row column whose value selects the record.
        template_iter: The iteration the body's step is taken from.
        anchors: The click's anchor in each iteration that has it.
        row_values: Each iteration's row column values.
        same_trace: Iteration -> trace index (hashed bands compare only
            within one trace, which shares one salt).
        column_prefix: Prefix for extra identity column names.

    A readable band must embed the column's value; any other text that
    changes from row to row becomes an identity column whose value each row
    must supply. A hashed band must contain the value's tokens, carry no
    hashed structured identity (that cannot follow a new row), and change
    from row to row only where the row's columns explain it.
    """

    from openadapt_flow.runtime import identity as _id

    anchor = anchors[template_iter]
    value = row_values[template_iter].get(column, "")
    if not value:
        return BandIdentity(ok=False, reason="the demonstrated row has no value")
    band = _band(anchor)
    if band:
        if column not in _id.embedded_params(band, {column: value}):
            return BandIdentity(
                ok=False,
                reason=(
                    "the row's identity text does not contain the value that selects it"
                ),
            )
        return _readable_band_columns(
            band=band,
            template_iter=template_iter,
            anchors=anchors,
            row_values=row_values,
            column_prefix=column_prefix,
        )
    tmpl = anchor.identity_template
    if tmpl is None or not tmpl.tokens:
        return BandIdentity(ok=False, reason="the click carries no identity evidence")
    if tmpl.structured and column not in tmpl.structured_params:
        return BandIdentity(
            ok=False,
            reason=(
                "its hashed structured identity names the demonstrated record "
                "and cannot follow another row; induce from the recordings"
            ),
        )
    indices = _hashed_indices(tmpl, value)
    if indices is None:
        return BandIdentity(
            ok=False,
            reason="the row's hashed identity does not contain the selecting value",
        )
    explained = {tmpl.tokens[j].r for j in indices}
    base = {t.r for j, t in enumerate(tmpl.tokens) if j not in indices}
    for it, other in anchors.items():
        if it == template_iter or same_trace.get(it) != same_trace[template_iter]:
            continue
        otmpl = other.identity_template
        if otmpl is None or otmpl.salt != tmpl.salt:
            continue
        other_value = row_values[it].get(column, "")
        other_idx = _hashed_indices(otmpl, other_value) or []
        rest = {t.r for j, t in enumerate(otmpl.tokens) if j not in other_idx}
        if rest != base:
            return BandIdentity(
                ok=False,
                reason=(
                    "its hashed identity changes from row to row beyond the "
                    "selecting value (for example a date of birth), and hashed "
                    "text cannot name it; induce from the recordings"
                ),
            )
    del explained
    return BandIdentity(
        ok=True, token_indices={column: indices}, basis="hashed identity band"
    )


def _hashed_indices(tmpl: Any, value: str) -> Optional[list[int]]:
    from openadapt_flow.runtime import identity as _id
    from openadapt_flow.runtime import identity_template as _it

    salt = _it._salt_bytes(tmpl.salt)
    out: list[int] = []
    for tok in _id.tokenize(value):
        key = _it._hash(salt, _id.squash(tok))
        hits = [j for j, t in enumerate(tmpl.tokens) if t.r == key and j not in out]
        if not hits:
            return None
        out.append(hits[0])
    return sorted(out) or None


def _readable_band_columns(
    *,
    band: str,
    template_iter: int,
    anchors: dict[int, Anchor],
    row_values: dict[int, dict[str, str]],
    column_prefix: str,
) -> BandIdentity:
    """Name each run of band text that changes by row and no column explains."""

    def tokens(text: str, values: dict[str, str]) -> list[str]:
        # Replace each column value by a placeholder so explained text aligns.
        out = text
        for name, val in sorted(values.items(), key=lambda kv: -len(kv[1] or "")):
            if val and len(_squash(val)) >= 2:
                out = _replace_ci(out, val, f"\x00{name}\x00")
        return out.split()

    base = tokens(band, row_values[template_iter])
    varying: set[int] = set()
    aligned: dict[int, list[tuple[int, int, int, int]]] = {}
    for it, other in anchors.items():
        if it == template_iter:
            continue
        oband = _band(other)
        if not oband:
            continue
        other_toks = tokens(oband, row_values.get(it, {}))
        ops = SequenceMatcher(a=base, b=other_toks, autojunk=False).get_opcodes()
        aligned[it] = [
            (i1, i2, j1, j2) for tag, i1, i2, j1, j2 in ops if tag != "equal"
        ]
        for tag, i1, i2, _j1, _j2 in ops:
            if tag == "insert":
                return BandIdentity(
                    ok=False,
                    reason="another row's identity text has words the demonstrated row lacks",
                )
            if tag != "equal":
                varying.update(range(i1, i2))
    if any(base[j].startswith("\x00") for j in varying):
        return BandIdentity(
            ok=False, reason="a row column's value does not line up across rows"
        )
    runs: list[list[int]] = []
    for j in sorted(varying):
        if runs and runs[-1][-1] == j - 1:
            runs[-1].append(j)
        else:
            runs.append([j])
    result = BandIdentity(ok=True, basis="readable identity band")
    for n, run in enumerate(runs, start=1):
        name = f"{column_prefix}{n}"
        lo, hi = run[0], run[-1] + 1
        result.identity_examples[name] = " ".join(base[lo:hi])
        per_iter: dict[int, Optional[str]] = {template_iter: " ".join(base[lo:hi])}
        for it, other in anchors.items():
            if it == template_iter:
                continue
            oband = _band(other)
            if not oband:
                per_iter[it] = None
                continue
            other_toks = tokens(oband, row_values.get(it, {}))
            got: Optional[list[str]] = None
            for i1, i2, j1, j2 in aligned.get(it, []):
                if i1 <= lo and hi <= i2 and (i1, i2) == (lo, hi):
                    got = other_toks[j1:j2]
            if got is None:
                # This row's text matched the template here.
                got = base[lo:hi]
            per_iter[it] = " ".join(got)
        result.identity_columns[name] = per_iter
    return result


def _replace_ci(text: str, old: str, new: str) -> str:
    import re

    return re.sub(re.escape(old), lambda _m: new, text, flags=re.IGNORECASE)


# ---------------------------------------------------------------------------
# Per-row record checks
# ---------------------------------------------------------------------------


def bind_effect_columns(
    effects: list[Any], template_values: dict[str, str]
) -> tuple[list[Any], set[str]]:
    """Rebind a write's record check to the row's columns.

    A literal equal to the template row's value of a column becomes a
    reference to that column, so each iteration checks the record it wrote.
    Returns the rebound effects and the columns they now reference
    (including columns they referenced already).
    """

    from openadapt_flow.runtime.effects.effect import ValueExpr

    by_value: dict[str, list[str]] = {}
    for name, val in template_values.items():
        if val:
            by_value.setdefault(val, []).append(name)
    used: set[str] = set()

    def rebind(expr: Any) -> Any:
        if expr is None:
            return None
        if expr.param is not None:
            used.add(expr.param)
            return expr
        names = by_value.get(expr.literal or "")
        if names is not None and len(names) == 1:
            used.add(names[0])
            return ValueExpr(param=names[0])
        return expr

    out = []
    for eff in effects:
        updates: dict[str, Any] = {
            "match": {k: rebind(v) for k, v in eff.match.items()},
            "new_records": [
                {k: rebind(v) for k, v in rec.items()} for rec in eff.new_records
            ],
        }
        if eff.value is not None:
            updates["value"] = rebind(eff.value)
        if eff.idempotency_key is not None:
            updates["idempotency_key"] = rebind(eff.idempotency_key)
        if eff.composite_key:
            updates["composite_key"] = {
                k: rebind(v) for k, v in eff.composite_key.items()
            }
        out.append(eff.model_copy(update=updates, deep=True))
    return out, used
