# Adversarial review of #1640, and what changed

A separate agent reviewed the branch at `cb849fc56` with the code and the
claims, not the author's reasoning, and without the private suites. Its
brief: find ways the verifier reports `found_existing` or
`created_verified` when it shouldn't, and claims the code doesn't support.
It demonstrated six defects with throwaway tests. Each finding below has a
regression test in `tests/test_ensure_record.py` (section "adversarial
review of #1640"), and the reviewer's own six tests no longer show their
defects.

These fixes came after the three blind rounds. RESULTS.md's numbers are for
the verifier before them.

| # | Finding | Kind | Change |
| --- | --- | --- | --- |
| 1 | `found_existing` ignored a duplicate that now existed | fail-open | the found path now needs no other record to be the person afterwards |
| 2 | a save still in flight when the window closed was never seen | fail-open | a last read with no end; an agent write outside the window sends the run to review. A write after that read is still unseen, so the check runs once the agent has stopped (stated in ENSURE_RECORD.md) |
| 3 | identity read only the first name and the first email | fail-open | every name and telecom entry counts. Candidates also come from each identity value searched alone. Name case stays exact, as the specification says; an email stored in another case is a stated limit |
| 4 | the heartbeat was off by default | fail-open | on by default |
| 5 | audit events with action `E` or none were skipped | fail-open | `E` counts as a write; a missing or unknown action is unreadable. Bot writes are logged as the Bot's, stated as a deployment requirement |
| 6 | the closed record expected every contract field, and exempted all of `meta` | fail-open | it expects only the fields the task gave; `meta.security` and `meta.tag` count |
| 7 | actor references compared as plain strings | fail-open, configuration-dependent | references compare as `Type/id`, and every agent of an event counts |
| - | a timestamp with no time zone raised `TypeError` | crash | unreadable |
| - | a rule whose values the task lacks raised unreadable | false alarm | the rule is skipped |
| - | a repeated page entry counted twice against `total` | fail-open, minor | entries count once |
| - | `allow` compared non-ASCII values escaped | false alarm | compared unescaped |
| - | PR body: test count, a third round "running", unqualified rerun row | doc | corrected |

Not changed:

- An `allow` entry without a value allows any value, any number of times.
  That is what it is for (`address.use`); a contract that cares names the
  value.
- Phone numbers compare by digits, so "+1 508..." and "508..." differ.
  That's a false alarm, the safe side.
- The heartbeat doesn't filter by actor. Any read of the record logged
  after the window closed proves the trail is recording, whoever made it.
- Clock skew across several server nodes behind a load balancer is not
  handled. The fixture is one node.
