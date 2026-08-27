# Demo captures

Two small captures used as fixtures. The screenshot workflow in
`openadapt-viewer` reads them to regenerate the viewer images embedded in that
project's README, so the documented UI does not drift from the shipped one.

| capture | what it shows | frames | actions |
|---|---|---|---|
| `turn-off-nightshift` | macOS System Settings, turning Night Shift off | 20 | 20 |
| `demo_new` | Spotlight opening Calculator, computing 2 × 3 | 14 | 14 |

Each directory holds a single `recording.db`. Frames live in it as
`Screenshot.png_data`, so nothing else is needed to load or render a capture.

## Provenance, and one honest caveat

Both were recorded in December 2025, before PR #28 (2026-07-17) replaced the
bespoke `capture.db` with the current SQLAlchemy `recording.db`. They were
converted with `scripts/migrate_legacy_capture.py`.

Actions are exact: real timestamps, real coordinates, real keys, mapped through
the correspondence documented in `openadapt_capture/events.py`.

**Frame timestamps are reconstructed, not recovered.** A legacy capture kept its
frames in `video.mp4` and retained only a curated subset as PNGs; nothing
recorded which frame each PNG came from. The converter spreads them evenly
across the real recording window and binds each action to the newest frame at or
before it. That is fine for a fixture and for a screenshot of the viewer.

**Do not measure anything with these.** Frame timing is approximate by
construction. Record a fresh capture for anything that needs real timing.

One key event in `turn-off-nightshift` carried no key identity at all and was
dropped, because `capture.py` refuses such an event and is right to.

Audio and video are deliberately absent. Both repositories are public, the
original recordings contain the founder's voice, and the workflow reads neither.
`create_html(..., include_audio=True)` degrades cleanly when there is no audio.
