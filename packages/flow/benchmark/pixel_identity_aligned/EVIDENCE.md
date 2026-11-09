# Jitter-robust pixel-identity battery

Evidence for the positive VERIFY (MATCH) path of the pixel identity tier (`runtime.identity.verify_pixel_identity`). Self-contained (`cv2`+`numpy`, no browser/system fonts): `cv2.putText`-rendered MRNs and the committed real-browser-render crops, each re-rendered under sub-pixel jitter, JPEG q<=10, 105-150% DPI, and theme inversion, then scored by the SAME production metric the runtime uses.

## Safety invariant (the hard requirement)

- **false-accept (different record -> MATCH): 0 / 504 different-record trials** — MUST be 0.
- false-mismatch (same record -> MISMATCH): 3 / 384 (0.8%) — safe over-halt.

## Utility

- same-record MATCH rate on matching renders: 67%
- glyph-collapse sibling MISMATCH (HALT) on matching renders: 67%

## Why VERIFY is now safe (the clean gap)

The worst aligned window (whole-crop match statistic) separates:

- same-record matching renders: max 0.0615 (p95 0.0547)
- every different-record: min 0.0705 (p5 0.0860)
- VERIFY gate `PIXEL_VERIFY_MAX_WINDOW` = 0.0400 sits in the gap (margin to nearest different-record = 0.0090).

## Enable bar

This evidence is SYNTHETIC (rendered + committed browser crops), so `PIXEL_VERIFY_ENABLED` stays `False` by default. The exact bar to flip it: reproduce `false_accept == 0` with a comparable gap on a REAL captured RDP/Citrix/HDX identifier corpus. See `docs/LIMITS.md`.
