"""Deterministic motif detection over a symbol stream.

The miner finds sequences of symbols that repeat, using a suffix array and its
longest-common-prefix array. Every internal node of the implied suffix tree is
a right-maximal repeat; each one is a candidate motif. A candidate fires only
when all of these hold:

1. It is between ``min_length`` and ``max_length`` symbols long and is not a
   shorter unit repeated (``ABAB`` is rejected; mine ``AB`` instead).
2. It touches at least ``min_apps`` applications.
3. It occurs, without overlap, at least ``min_occurrences`` times inside the
   last ``window_s`` seconds.
4. Its exact, non-overlapping repeat count is unlikely by chance. The test
   compares the count with a Poisson model whose rate comes from the stream's
   own symbol frequencies, with a Bonferroni correction across every window
   of every allowed length (every hypothesis the miner could have tested).
   This is what keeps a busy but random stream quiet.

After a motif fires, near-repeats join its episodes when their structural
similarity (``difflib.SequenceMatcher`` ratio over symbols) reaches
``similarity_threshold``. Near-repeats never count toward the chance test.

Everything here is deterministic: the same stream and configuration give the
same motifs in the same order. No model is called.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from openadapt_capture.shadow.config import ShadowConfig
from openadapt_capture.shadow.symbols import ShadowSymbol


@dataclass(frozen=True)
class Occurrence:
    """One episode of a motif: symbols ``start`` up to, not including, ``end``."""

    start: int
    end: int
    t_start: float
    t_end: float
    exact: bool
    similarity: float


@dataclass(frozen=True)
class Motif:
    """A repeated cross-application sequence."""

    motif_id: str
    tokens: tuple[str, ...]
    occurrences: tuple[Occurrence, ...]
    count_in_window: int
    exact_count: int
    expected_by_chance: float
    p_value: float
    apps: tuple[str, ...]


def motif_id_for(tokens: Sequence[str]) -> str:
    """Stable identifier of a token sequence."""

    return hashlib.sha256("\x1f".join(tokens).encode()).hexdigest()[:16]


def suffix_array(seq: Sequence[int]) -> list[int]:
    """Suffix array by prefix doubling. ``O(n log^2 n)``, deterministic."""

    n = len(seq)
    if n == 0:
        return []
    rank = list(seq)
    sa = list(range(n))
    k = 1
    while True:
        key = [(rank[i], rank[i + k] if i + k < n else -(1 << 62)) for i in range(n)]
        sa.sort(key=key.__getitem__)
        new_rank = [0] * n
        for idx in range(1, n):
            prev, cur = sa[idx - 1], sa[idx]
            new_rank[cur] = new_rank[prev] + (key[cur] != key[prev])
        rank = new_rank
        if rank[sa[-1]] == n - 1:
            return sa
        k <<= 1


def lcp_array(seq: Sequence[int], sa: Sequence[int]) -> list[int]:
    """Kasai's algorithm. ``lcp[i]`` is the LCP of ``sa[i-1]`` and ``sa[i]``."""

    n = len(seq)
    rank = [0] * n
    for i, p in enumerate(sa):
        rank[p] = i
    lcp = [0] * n
    h = 0
    for i in range(n):
        r = rank[i]
        if r > 0:
            j = sa[r - 1]
            while i + h < n and j + h < n and seq[i + h] == seq[j + h]:
                h += 1
            lcp[r] = h
            if h:
                h -= 1
        else:
            h = 0
    return lcp


def _lcp_intervals(lcp: Sequence[int]) -> list[tuple[int, int, int]]:
    """Bottom-up LCP intervals ``(lcp_value, left, right)`` with lcp_value > 0."""

    out: list[tuple[int, int, int]] = []
    stack: list[list[int]] = [[0, 0]]  # [lcp_value, left_bound]
    n = len(lcp)
    for i in range(1, n + 1):
        cur = lcp[i] if i < n else 0
        lb = i - 1
        while cur < stack[-1][0]:
            value, lb = stack.pop()
            out.append((value, lb, i - 1))
        if cur > stack[-1][0]:
            stack.append([cur, lb])
    return out


def _smallest_period(tokens: Sequence[int]) -> int:
    n = len(tokens)
    fail = [0] * n
    k = 0
    for i in range(1, n):
        while k and tokens[i] != tokens[k]:
            k = fail[k - 1]
        if tokens[i] == tokens[k]:
            k += 1
        fail[i] = k
    period = n - fail[-1]
    return period if n % period == 0 else n


def poisson_tail(k: int, mean: float) -> float:
    """``P(X >= k)`` for ``X ~ Poisson(mean)``."""

    if k <= 0:
        return 1.0
    if mean <= 0:
        return 0.0
    log_term = -mean + k * math.log(mean) - math.lgamma(k + 1)
    total = 0.0
    i = k
    while True:
        term = math.exp(log_term)
        total += term
        if term < 1e-300 or (i > mean and term < total * 1e-15) or i > k + 100_000:
            break
        i += 1
        log_term += math.log(mean) - math.log(i)
    return min(1.0, total)


def _non_overlapping(positions: Sequence[int], length: int) -> list[int]:
    chosen: list[int] = []
    end = -1
    for p in sorted(positions):
        if p >= end:
            chosen.append(p)
            end = p + length
    return chosen


def mine_motifs(
    symbols: Sequence[ShadowSymbol],
    config: ShadowConfig,
    *,
    now: float | None = None,
) -> list[Motif]:
    """Return the motifs in ``symbols`` that pass every threshold, best first."""

    n = len(symbols)
    if n < config.min_length * config.min_occurrences:
        return []
    now = symbols[-1].t if now is None else now

    vocab = {tok: i + 1 for i, tok in enumerate(sorted({s.token for s in symbols}))}
    session = [0] * n
    for i in range(1, n):
        gap = symbols[i].t - symbols[i - 1].t
        session[i] = session[i - 1] + (1 if gap > config.idle_gap_s or gap < 0 else 0)

    # Separate sessions with unique sentinels so no repeat spans a boundary.
    seq: list[int] = []
    to_symbol: list[int] = []
    next_sentinel = len(vocab) + 1
    for i in range(n):
        if i and session[i] != session[i - 1]:
            seq.append(next_sentinel)
            to_symbol.append(-1)
            next_sentinel += 1
        seq.append(vocab[symbols[i].token])
        to_symbol.append(i)

    sa = suffix_array(seq)
    lcp = lcp_array(seq, sa)

    candidates: dict[tuple[int, ...], set[int]] = {}
    for value, left, right in _lcp_intervals(lcp):
        if value < config.min_length or right - left + 1 < config.min_occurrences:
            continue
        length = min(value, config.max_length)
        key = tuple(seq[sa[left] : sa[left] + length])
        candidates.setdefault(key, set()).update(sa[left : right + 1])

    counts = Counter(vocab[s.token] for s in symbols)
    total = float(n)
    session_lengths = Counter(session)
    # Every window of every allowed length is a hypothesis the miner could have
    # tested, not just the ones that happened to repeat. Correcting only for
    # the repeats that were seen would let a busy random stream through.
    tested = sum(
        max(0, m - length + 1)
        for length in range(config.min_length, config.max_length + 1)
        for m in session_lengths.values()
    )
    alpha = config.significance / max(1, tested)

    passing: list[tuple[tuple[int, ...], list[int], float, float]] = []
    for key, seq_positions in candidates.items():
        length = len(key)
        if _smallest_period(key) < length:
            continue
        if len(set(key)) < min(3, length):
            continue
        sym_positions = [to_symbol[p] for p in seq_positions]
        chosen = _non_overlapping(sym_positions, length)
        apps = {symbols[chosen[0] + j].app for j in range(length)}
        if len(apps) < config.min_apps:
            continue
        in_window = [p for p in chosen if symbols[p].t >= now - config.window_s]
        if len(in_window) < config.min_occurrences:
            continue
        starts = sum(max(0, m - length + 1) for m in session_lengths.values())
        log_p = sum(math.log(counts[t] / total) for t in key)
        expected = starts * math.exp(log_p)
        p_value = poisson_tail(len(chosen), expected)
        if p_value > alpha:
            continue
        passing.append((key, chosen, expected, p_value))

    # Best first: the most symbols covered by exact and near repeats together,
    # then the token order for determinism. Counting near repeats here lets a
    # full task with one variant episode win over its shorter exact prefix.
    inverse = {v: k for k, v in vocab.items()}

    def _coverage(item: tuple[tuple[int, ...], list[int], float, float]) -> int:
        key, chosen = item[0], item[1]
        own = {p + j for p in chosen for j in range(len(key))}
        near = _approximate(
            symbols,
            session,
            tuple(inverse[t] for t in key),
            set(own),
            config.similarity_threshold,
        )
        return len(own) + sum(o.end - o.start for o in near)

    passing.sort(key=lambda item: (-_coverage(item), item[0]))
    covered: set[int] = set()
    motifs: list[Motif] = []
    for key, chosen, expected, p_value in passing:
        length = len(key)
        span = {p + j for p in chosen for j in range(length)}
        if len(span & covered) * 2 >= len(span):
            continue
        tokens = tuple(inverse[t] for t in key)
        occurrences = [
            Occurrence(p, p + length, symbols[p].t, symbols[p + length - 1].t, True, 1.0)
            for p in chosen
            if not ({p + j for j in range(length)} & covered)
        ]
        occupied = covered | {i for o in occurrences for i in range(o.start, o.end)}
        occurrences.extend(
            _approximate(symbols, session, tokens, occupied, config.similarity_threshold)
        )
        occurrences.sort(key=lambda o: o.start)
        in_window = [o for o in occurrences if o.t_start >= now - config.window_s]
        if len(in_window) < config.min_occurrences:
            continue
        for o in occurrences:
            covered.update(range(o.start, o.end))
        apps = tuple(
            sorted(
                {
                    symbols[occurrences[0].start + j].app or "unknown"
                    for j in range(occurrences[0].end - occurrences[0].start)
                }
            )
        )
        motifs.append(
            Motif(
                motif_id=motif_id_for(tokens),
                tokens=tokens,
                occurrences=tuple(occurrences),
                count_in_window=len(in_window),
                exact_count=sum(1 for o in occurrences if o.exact),
                expected_by_chance=expected,
                p_value=p_value,
                apps=apps,
            )
        )
    return motifs


def _approximate(
    symbols: Sequence[ShadowSymbol],
    session: Sequence[int],
    tokens: tuple[str, ...],
    occupied: set[int],
    threshold: float,
) -> list[Occurrence]:
    """Near-repeats of ``tokens`` that start with the same symbol."""

    if threshold >= 1.0:
        return []
    length = len(tokens)
    slack = max(1, length // 5)
    out: list[Occurrence] = []
    n = len(symbols)
    i = 0
    while i < n:
        if i in occupied or symbols[i].token != tokens[0]:
            i += 1
            continue
        best: tuple[float, int] | None = None
        for size in range(max(2, length - slack), length + slack + 1):
            end = i + size
            if end > n or session[end - 1] != session[i]:
                break
            if any(j in occupied for j in range(i, end)):
                break
            window = [symbols[j].token for j in range(i, end)]
            ratio = SequenceMatcher(None, tokens, window, autojunk=False).ratio()
            if ratio >= threshold and (best is None or ratio > best[0]):
                best = (ratio, end)
        if best is None:
            i += 1
            continue
        ratio, end = best
        out.append(Occurrence(i, end, symbols[i].t, symbols[end - 1].t, False, round(ratio, 4)))
        occupied.update(range(i, end))
        i = end
    return out
