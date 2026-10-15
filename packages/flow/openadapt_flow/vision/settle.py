"""Screen settle detection: poll until the frame stops changing."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from openadapt_flow.backend import Backend
from openadapt_flow.vision.hashing import phash_distance, phash_png

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SettleResult:
    """Outcome of a settle poll.

    ``settled`` is the load-bearing field the bare :func:`wait_settled` used to
    throw away: it is ``True`` only when the screen reached the required run of
    identical frames BEFORE the timeout. A caller that must not act on a
    mid-transition frame (a still-loading page, an animating dialog) reads this
    to HALT rather than proceed-anyway (docs/LIMITS.md "state dependency").
    """

    png: bytes
    settled: bool
    stable_frames: int
    required_frames: int
    elapsed_s: float
    # Perceptual hash of ``png`` and the monotonic time it was captured, so a
    # caller can hand the frame to :func:`wait_settled_seeded` as the first
    # frame of the next settle window.
    phash: str = ""
    captured_at: float = 0.0


def wait_settled_result(
    backend: Backend,
    *,
    interval_s: float = 0.1,
    stable_frames: int = 2,
    timeout_s: float = 3.0,
) -> SettleResult:
    """Poll screenshots until the screen is visually stable, reporting whether
    it actually settled.

    Screenshots are taken every ``interval_s`` seconds; the screen counts as
    settled once ``stable_frames`` consecutive frames have identical perceptual
    hashes (phash distance 0). Unlike :func:`wait_settled`, this returns a
    :class:`SettleResult` whose ``settled`` flag is ``False`` when the timeout
    was reached WITHOUT the required run of stable frames -- so the caller can
    decide (HALT vs proceed) instead of silently acting on a frame that never
    stopped changing.

    Args:
        backend: Screen source implementing the :class:`Backend` protocol.
        interval_s: Seconds between polls.
        stable_frames: Number of consecutive identical frames required.
        timeout_s: Maximum seconds to wait before giving up.

    Returns:
        A :class:`SettleResult` carrying the last captured frame and whether it
        settled within the timeout.
    """
    return _poll_settled(
        backend,
        interval_s=interval_s,
        stable_frames=stable_frames,
        timeout_s=timeout_s,
        seed=None,
    )


@dataclass(frozen=True)
class SettleSeed:
    """The last frame of a run that already settled, to extend that run.

    ``stable_frames`` is the length of the identical-frame run that ended in
    this frame, and ``captured_at`` the ``time.monotonic()`` capture time.
    """

    phash: str
    stable_frames: int
    captured_at: float


def _poll_settled(
    backend: Backend,
    *,
    interval_s: float,
    stable_frames: int,
    timeout_s: float,
    seed: SettleSeed | None,
) -> SettleResult:
    start = time.monotonic()
    deadline = start + timeout_s
    png = backend.screenshot()
    captured_at = time.monotonic()
    last_hash = phash_png(png)
    streak = 1
    if seed is not None and phash_distance(last_hash, seed.phash) == 0:
        # This fresh frame is identical to the last frame of a run that was
        # already observed stable (frames polled interval_s apart), so it
        # extends that run: the screen has now been identical for at least
        # the seed's window plus the time since. The streak below the
        # requirement still polls as usual.
        streak = seed.stable_frames + 1
    while streak < stable_frames and time.monotonic() < deadline:
        time.sleep(interval_s)
        previous_png = png
        png = backend.screenshot()
        captured_at = time.monotonic()
        # Byte-identical PNGs have identical perceptual hashes; skip the
        # decode + DCT for the common unchanged frame.
        current = last_hash if png == previous_png else phash_png(png)
        if phash_distance(current, last_hash) == 0:
            streak += 1
        else:
            streak = 1
        last_hash = current
    return SettleResult(
        png=png,
        settled=streak >= stable_frames,
        stable_frames=streak,
        required_frames=stable_frames,
        elapsed_s=time.monotonic() - start,
        phash=last_hash,
        captured_at=captured_at,
    )


def wait_settled_seeded(
    backend: Backend,
    seed: SettleSeed | None,
    *,
    interval_s: float = 0.1,
    stable_frames: int = 2,
    timeout_s: float = 3.0,
    max_seed_age_s: float = 2.0,
) -> SettleResult:
    """Like :func:`wait_settled_result`, but may extend an already-settled run.

    The caller must only pass a seed from this backend's previous settle with
    no input delivered since. A fresh frame is always captured and returned.
    If it is identical to the seed, it extends the seed's stable run; if not,
    or the seed is older than ``max_seed_age_s`` or never reached
    ``stable_frames``, this is exactly :func:`wait_settled_result`.
    """
    if seed is not None and (
        time.monotonic() - seed.captured_at > max_seed_age_s
        or seed.stable_frames < stable_frames
    ):
        seed = None
    result = _poll_settled(
        backend,
        interval_s=interval_s,
        stable_frames=stable_frames,
        timeout_s=timeout_s,
        seed=seed,
    )
    if not result.settled:
        logger.warning(
            "wait_settled: screen did not settle within %.1fs "
            "(%d/%d stable frames); proceeding with the most recent frame",
            timeout_s,
            result.stable_frames,
            result.required_frames,
        )
    return result


def wait_settled(
    backend: Backend,
    *,
    interval_s: float = 0.1,
    stable_frames: int = 2,
    timeout_s: float = 3.0,
) -> bytes:
    """Poll screenshots until the screen is visually stable, then return it.

    Thin wrapper over :func:`wait_settled_result` that returns only the frame,
    for callers that do not gate on readiness. On timeout the most recent frame
    is returned anyway and a warning is logged: a screen that never settles
    (animation, spinner, slow load) means downstream template matching and
    postcondition checks would run against an arbitrary mid-transition frame.
    Callers that must NOT proceed-anyway on that frame use
    :func:`wait_settled_result` and inspect ``SettleResult.settled`` (the
    replayer's opt-in ``require_settled`` readiness gate does exactly this).

    Args:
        backend: Screen source implementing the :class:`Backend` protocol.
        interval_s: Seconds between polls.
        stable_frames: Number of consecutive identical frames required.
        timeout_s: Maximum seconds to wait before giving up.

    Returns:
        The last captured frame as PNG bytes.
    """
    result = wait_settled_result(
        backend,
        interval_s=interval_s,
        stable_frames=stable_frames,
        timeout_s=timeout_s,
    )
    if not result.settled:
        logger.warning(
            "wait_settled: screen did not settle within %.1fs "
            "(%d/%d stable frames); proceeding with the most recent frame -- "
            "resolution and postconditions may be evaluated mid-transition",
            timeout_s,
            result.stable_frames,
            result.required_frames,
        )
    return result.png
