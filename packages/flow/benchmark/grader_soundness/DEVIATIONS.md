# Deviations from PROTOCOL.md

Dated entries, in the order they happened.

1. **2026-11-11, shakedown before the first scored cell.** To check the
   harness, the runner was run on AgentDojo banking `user_task_3` and
   `user_task_11` and on AppWorld `22cc237_1`, into a scratch directory
   outside this repository. Those cells are not used as data. The protocol
   did not mention a shakedown. Nothing in the protocol, the family
   definitions or the task sample changed after it. One runner change
   followed it: `run_meta.json` now keeps the union of tasks across
   invocations on one ledger, so AppWorld can run one task per invocation
   and delete that task's seed dump afterwards to stay above the 30 GiB disk
   floor.
2. **2026-11-11, tau-bench not added.** The protocol named tau-bench (MIT)
   as an optional third benchmark. Its source was cloned to a scratch
   directory and its license checked (MIT), but installing and running code
   from that fresh clone was blocked by this session's permission policy.
   It was dropped before any cell ran. No tau-bench cell exists.
