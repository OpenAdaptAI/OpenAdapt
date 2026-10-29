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

Suite `803b34c6dcdba0651d3367dd1ea146591a6ed047567d3bc637e50dbc9a36d278`,
20 cases from a second, independent author (duplicate 2, look-alike 3, wrong
field 6, collateral 2, timeout 3, concurrent 4; right answers: needs_review
16, created_verified 3, found_existing 1). Verifier at `2efc74000`, fixture
at `08dc2c58e`.

**17 of 20 right; no fault passed as verified.** Every one of the 16 faults
went to `needs_review`, and the clean found record was found.

The three misses were clean creates sent to `needs_review`: each record
carried `active: true`, which the task doesn't give, and the contract's
closed record refused it. The brief left `active` open, and FHIR clients
commonly send it. `5ce6e6b26` lets the contract allow one extra element
with one exact value (`allow: [active=true]`; `active: false` still fails).

## Both suites on the final version

Not blind: both suites were seen before these runs. Verifier and contract
at `5ce6e6b26`.

| Suite | Right | Fault passed as verified |
| --- | --- | --- |
| Round 1 (`5c1742e8`) | 18 of 18 | 0 |
| Round 2 (`803b34c6`) | 20 of 20 | 0 |

## Round 3, blind, on the final version

Suite `153d7899d16388af25b272356e4dcc6684def1ff7aafc6c6c023a0172f0503b2`,
20 cases from a third author who was asked to be adversarial within the
brief (duplicate 3, look-alike 4, wrong field 4, collateral 2, timeout 3,
concurrent 4; right answers: needs_review 15, created_verified 3,
found_existing 2). Verifier and contract at `5ce6e6b26`, unchanged since.

**19 of 20 right; no fault passed as verified.**

The miss was a clean create sent to `needs_review`: the record carried
optional `use` codes (`name.use: official`, `address.use: home`,
`telecom.use`), which the closed record refuses. A contract that should
accept them lists them in `allow`; this one was left as it was, so the
number above stays a blind one.

## Across the three rounds

No fault was passed as verified after round 1's state-map bug was fixed: 0
of 31 faults in rounds 2 and 3. Of 9 clean runs in those rounds, 5 were
judged right; the 4 others were sent to review because the record carried
an element the task didn't give (`active`, `use`). The closed record errs
toward review, and each such element is one line of contract.

## After the adversarial review

A review of the branch after these rounds found fail-open paths the suites
didn't exercise (REVIEW.md). The fixes change the verifier, so the numbers
above are for the verifier before them.

## What the rounds cover, and what they don't

The suites exercise what the brief describes: existing and duplicate
records, look-alikes, wrong and extra values, collateral writes of other
resource types, saves whose response was lost, and another user's writes
during the run. They don't exercise a server that has stopped logging
AuditEvents (the heartbeat's unit tests do), writes outside the FHIR API,
or an agent with administrator rights; ENSURE_RECORD.md states those limits.
