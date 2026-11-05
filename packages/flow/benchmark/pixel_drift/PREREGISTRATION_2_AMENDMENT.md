# Amendment 1 to pre-registration 2

Written on 2026-11-05, after the code changes and before any held-out
measurement. Nothing in the held-out set had been rendered or queried. The
held-out seed is unchanged, because it hashes `PREREGISTRATION_2.md`, which
is unchanged.

1. **(a) The outline-at-spot rule is dropped without measurement.** On the
   first round's development pages, the outline score (the edge strength of
   the crop's border ring) was measured. It is the same for the control at the
   recorded spot and for an identical control one row away. In a reordered
   list, the spot holds a different row's identical control while the
   recorded appearance moved with its row. In pixels that case looks the same
   as a state transfer. Choosing the spot would click the wrong row there, and
   the held-out set has no reorder condition that would reveal it. Per the
   pre-registration's fallback clause, the state-transfer targets stay
   refused. The refusal target for `transfer_missing` and `transfer_none` is
   reported as **not met by design**.
2. **(c) Narrowed.** The geometry estimate snaps when exactly one copy of the
   control's outline is within half its size, and it refuses when several are.
   When none is found, the estimate stands, as before. Refusing every
   uncorroborated estimate failed eight existing replay and repair tests whose
   synthetic frames have no outline to find. That broadens the change beyond
   (c), so it is deferred.

Thresholds and arms are otherwise unchanged.
