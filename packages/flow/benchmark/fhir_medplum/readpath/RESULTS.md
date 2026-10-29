# Blind rounds on Medplum 5.2.8

Each suite was written by a separate agent from one brief (the task, the
specification of each outcome, and the suite format), without access to the
verifier, the contract or the other suite. Each was frozen by sha256 before
the verifier saw it, and is kept private with its per-case results. Every
case starts from an empty project; the bot is a plain member whose access
policy makes AuditEvent read-only (the runner checks both before the first
case).

"Fault passed as verified" counts the worst error: the verifier reported
`found_existing` or `created_verified` when the right answer was
`needs_review`, or reported the wrong record.

## Round 1

Suite `5c1742e8102838f2f6d41493091fb154c8241f2a5f8c3a2a24df0d9168eb1b8f`,
18 cases (duplicate 3, look-alike 3, wrong field 3, collateral 2, timeout 3,
concurrent 4; right answers: needs_review 12, created_verified 4,
found_existing 2). Verifier at `705900415`.

**17 of 18 right; 1 fault passed as verified.**

The miss: the bot stored the state as "Massachusetts" instead of the code
"MA". The contract maps the task's "Massachusetts" to "MA", and the
verifier applied that map to the stored value as well, so the wrong value
compared equal. Fixed in `2efc74000` (the map applies to the task's value
only), with a regression test. The suite isn't blind to that fix, so the
fix was tested on a second suite.

## Round 2

ROUND2
