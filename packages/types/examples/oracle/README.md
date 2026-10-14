# Oracle adapter

A production `VERIFIED` receipt needs a read that did not come from the
acting screen. The adapter is `channel` plus `read`. That's the whole
interface.

Tiers are in [`docs/ORACLE.md`](../../docs/ORACLE.md). Tier 0 (pixels, OCR)
cannot mint production `VERIFIED`. Tier 2 is an API, DB, file, or ack
read. Tier 1 is a second session; same adapter, no production Seal.

`file_oracle.py` is the file-channel shape. Swap the body of `read` for an
HTTP GET, a SQL SELECT, or a second-session UI query. Do not copy a
vendor recipe into this package.
