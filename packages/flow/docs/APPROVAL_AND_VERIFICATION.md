# Who approves a save, and how it is checked

A short design note. The contracts live in openadapt-types
(`openadapt_types.approval`); Flow keeps a field-exact copy in
`openadapt_flow/approval_mode/approver.py` so its core runs without
openadapt-types, and a test checks the two match when both are installed.

## What customers asked for

- Choose, per workflow and per step, who answers a save: nobody, a person
  for each item, a person for a batch, a person only for problems, a rule
  set in advance, or the customer's own agent or system.
- Every decision says who or what made it.
- Autonomy is earned and can be capped: a policy can forbid agent-only
  approval for clinical writes.
- Every save says how it was checked, and saves nobody could check go to a
  person.

## Approval modes

| Mode | Where it lives | What waits |
|---|---|---|
| `each_item` (default) | Flow run | Every save, until someone allowed answers |
| `problems_only` | Flow run, with a confirmed Cloud switch | Only saves that fail a check |
| `not_gated` | Flow run, recorded | Nothing. Runs started from the customer's own software |
| `batch` | Decision triage, after the run | Clean saves are approved together; see [`DECISION_TRIAGE.md`](DECISION_TRIAGE.md) |

`batch` is a review of saves already made and checked, not a pre-save pause,
so Flow has no runtime `batch` gate.

## Who may answer

`ApproverIdentityV1` names the decider: `kind` (`person`, `rule`, `agent`,
`system`), how it was identified (`auth`), an opaque `id`, and `policy_ref`,
the policy that allowed it. These are the fields RFC-0002 expects under
`authorization.approver`.

`ApproverPolicyV1` says which kinds may answer each step class
(`read_only`, `reversible`, `consequential`, `irreversible`,
`clinical_write`). The default is a person for every class, so nothing
changes for a run without a policy. A `clinical_write` step takes a
non-person only when the policy pins the customer's opt-in digest. Under
the `clinical-write` and `regulated` policies every save is a clinical
write.

The run writes its policy once into `item-approval/approver-policy.json`.
Every reader (the gate, the CLI, a resume) applies that file. An unreadable
policy means person-only.

A delegated approver answers by one of three routes:

- `local_cli`: `openadapt-flow item-approval approve --decided-by automation
  --delegate-kind agent --delegate-id agent:... --route local_cli`.
- `mcp`: the same answer from an MCP tool, with `--route mcp`.
- `webhook`: the run posts PHI-free facts (request id and digest, step id
  and class, mode, form and screenshot digests, expiry; never the step's
  words, the screenshot or any value) to an https endpoint, signed with
  HMAC-SHA256. The reply must be signed, name the same request digest and
  say `approve`, `decline` with a closed reason, or `pending`. Any failure
  leaves the save to a person; a person can always answer in Desktop.

The run re-checks every answer: the policy must allow the approver's kind
for the step class, a person answers only from Desktop, and a delegate
never fills in the person fields. The report's `ItemApprovalRef` records
`decided_by: automation`, the route and the approver. Only a person's
decision becomes a training label. Triage never approves anything.

## How a save is checked

Every check has a check level (`openadapt_flow.check_level.CheckLevel`).
Higher is stronger, and when a save has several checks the weakest wins. The
older scales keep their published numbers and map onto it:

| Check level | Meaning | `VerificationTier` | Seal `OracleTier` | RFC-0002 label | Can be clean |
|---|---|---|---|---|---|
| 5 `counterparty_confirmed` | The other party confirmed it received the value | (none) | 3 `COUNTERPARTY` | `independent_readback` | Yes, when confirmed |
| 4 `system_of_record` | Read without the UI: REST, SQL, FHIR, GraphQL, file export | 1 `INDEPENDENT_SYSTEM` | 2 `SYSTEM_OF_RECORD` | `independent_readback` | Yes, when confirmed |
| 3 `separate_session` | The app's own screens in a separately signed-in, read-only session | 2 `INDEPENDENT_SESSION` | 1 `INDEPENDENT_SESSION` | `screen_readback` | No |
| 2 `reopened` | The acting session reopened the record and read it again | 3 `PERSISTED_STATE_REACQUISITION` | 0 `VISUAL` | `screen_readback` | No |
| 1 `same_screen` | Only the acting screen shows the value | 4 `IMMEDIATE_SCREEN` | 0 `VISUAL` | `screen_readback` | No |
| 0 `unchecked` | No read, unreachable or stale store, no tier | (no confirmed tier) | (no Seal) | `unchecked` | No |

`VerificationTier` counts the other way (lower is stronger), so compare
checks by `.check_level`, not by the tier number. A Seal at `VISUAL` reads as
`same_screen`, because the Seal does not record whether the screen was
reopened. The RFC-0002 labels (`ReadBackTier`) are the coarse rollup of this
table.

A save that declares effects and has no verifier stops before the write. A
run with no check at all ends **Finished, not checked**. The verifier now
keeps each field it compared on the local report
(`StepResult.effect_field_checks`: field, typed, on record, matched, tier).
Desktop's Check the record card shows that diff on the local screen only,
and decision triage uses it, so a confirmed `independent_readback` save can
now be clean.

## Earning "only problems"

`TriageEligibilityProvider` answers `AutonomyEligibilityV1` for a workflow:
a sealed, pinned calibration artifact that is eligible, plus the audit
sample (clean saves a person reviewed anyway, at least 1%). Every decline
there is a miss; the exact one-sided Clopper-Pearson bound on the miss rate
must be under the target (5% at 90% by default). With no artifact or no
audit labels it raises `EligibilityUnavailable` and the workflow stays per
item. `openadapt-flow triage eligibility` prints the result.

## Admitting a demonstrated read-back (planned)

`openadapt-flow check admit` (branch `feat/demonstrated-readback-verifier`,
not merged) lets a person accept a read-back that was taught by
demonstration. Today it needs an interactive terminal and a typed `admit`,
so Desktop and openadapt-agent users have no way to admit. The plan is a
person-only path through Desktop. No policy can widen it:

1. Flow: `check admit-request BUNDLE` writes a request (bundle digest, each
   check's SHA-256, step ids, and the plain description `check admit`
   prints) to a private directory in the bundle. It admits nothing.
2. Desktop shows the request on its own screen: the replayed steps, the
   reset and the fields read, with **Admit this check** and **Not yet**.
   Admitting needs a deliberate confirmation. The answer goes through the
   engine IPC only. It isn't offered to hosted runs, the phone or MCP.
3. Flow: `check admit --answer FILE` accepts an answer only if it is for the
   same request digest, `decided_by: human`, route `desktop_check_admit`, and
   its `ApproverIdentityV1` is `kind: person` (`cloud_user`, or
   `unauthenticated` when signed out). The admission records that approver.
   An answer from any other kind or route is refused, whatever the approver
   policy says. The terminal path stays.
4. openadapt-agent: a read-only tool reports whether a bundle's checks are
   admitted, and can ask for one, which puts an item in Desktop's Needs
   you. No agent tool admits. A test checks the tool list has no admit tool,
   and that Flow refuses an agent, rule or system answer.

The limit is the same as for item approvals: another local process with
the user's file access could write a Desktop-shaped answer. Signing answers
with a Desktop key kept in the keychain would close that later.

## Not built

- Desktop and Cloud don't show delegated approvers yet.
- Eligibility isn't yet one of the `problems_only` requirements; a person
  reads it before turning the switch on.
- The Desktop admission path above.
- openadapt-types isn't published with these contracts (repo freeze).
