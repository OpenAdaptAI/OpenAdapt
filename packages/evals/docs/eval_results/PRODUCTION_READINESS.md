# Production-readiness evidence boundary

OpenAdapt has production-capable mechanisms. It does not have one global
production-readiness state. Each workflow, application, version, environment,
identity rule, effect oracle, and deployment boundary needs its own
qualification.

The `current` label in `PUBLISHED_EVIDENCE.json` has one narrow meaning: the
evidence set matches the current published `openadapt-flow` release. It does
not mean the campaign is production acceptance. The evidence manifest records
that distinction in `campaigns[].evidence_scope.production_acceptance`.

That campaign field is now descriptive only and must remain `false`. A person
cannot promote an evidence set by changing it, changing an evidence class, or
adding summary counts. Production acceptance can enter this repository only
through `scripts/import_production_acceptance.py` after the private-export gate
opens.

## Independent hosted-acceptance verifier

The verifier mechanism is implemented. The private-evidence import is not
enabled. `import_files` refuses every file import until OpenAdapt approves one
exact private-export contract. Do not export an admission or campaign to this
repository. Do not set `production_acceptance: true` while this gate is closed.
The checked-in fixtures are synthetic test vectors only.

The future approval must bind the complete payload schema and digest, the
destination account, repository, ref, and path prefix, the encryption-key
identity, the recorded retention commitment, the authorized importer workflow
and ref, and the approval authority. A broad enable flag or approval
of only the admission and campaign digests is not sufficient.

After approval, the importer will compose four evidence inputs:

1. The closed `openadapt.execute-live-acceptance-record/v2` certificate from
   the protected Cloud production workflow.
2. The signed `openadapt.qualification-admission/v2` envelope from the
   protected qualification authority.
3. The full `openadapt.qualification-campaign/v2` artifact with every retained
   trial row and normalized evidence receipt for the exact qualification
   contract.
4. The Sigstore bundle signing the certificate bytes on the public-good
   instance.

The future importer also needs external control inputs: the approved Cloud
source commit, the approved qualification signer registry, the admission and
signer revocation lists, and the exact private-export approval. The evidence
artifacts cannot select these values. The full admission and campaign stay in
the encrypted retained evidence package until that approval exists. After
approval, a protected job can stage only the approved payload for
verification. The derived public result contains hashes and bounded counts. It
does not contain tenant or workflow identifiers.

The verifier asks GitHub CLI to verify the certificate against the exact
`OpenAdaptAI/openadapt-cloud` workflow on `refs/heads/main`, the GitHub Actions
OIDC issuer, and a GitHub-hosted runner. That repository is the proprietary
hosted control plane and stays private, so the verifier also requires
`sourceRepositoryVisibilityAtSigning` to equal `private`. A certificate signed
in a public source repository is not the reviewed Cloud workflow. The verifier
then validates the verified SLSA provenance. It refuses an unknown repository,
workflow, ref, source digest, issuer, runner class, signature, or empty
verification result.

The Cloud repository is private, and GitHub artifact attestations are limited
to public repositories on the GitHub Free, Pro, and Team plans. The Cloud
workflow therefore does not use `actions/attest`. It signs the certificate
bytes itself, with an Actions OIDC token, on the Sigstore public-good instance.
GitHub does not gate that token by plan, and the Sigstore public-good instance
does not check repository visibility. A measured run from the private Cloud
repository produced a certificate from `CN=sigstore-intermediate,O=sigstore.dev`
carrying `sourceRepositoryVisibilityAtSigning: private` and one Rekor entry.

That choice buys a public transparency log. GitHub's own private instance has
no log at all, so the public-good route is the stronger audit property, not a
concession. It costs one exposure: the Rekor entry publishes the repository
name, the workflow path, the source commit, the certificate digest, and the
time of the run. It publishes no certificate content, no tenant, and no
workflow identity.

The verifier binds that route exactly. It passes `--hostname github.com`, so a
local GitHub CLI host setting cannot select a different trust root. Every
verified timestamp must come from an approved observer: the Rekor log at
`https://rekor.sigstore.dev`, or the public-good timestamp authority at
`https://timestamp.sigstore.dev/api/v1/timestamp`. Exactly one Rekor time must
be present, at most one authority time may accompany it, and every observed
time must fall inside the fifteen minutes after the certificate
`acceptance_verified_at` value. The GitHub private timestamp authority, a
current-clock reading, an unknown log, a second Rekor time, and an unapproved
observer are all refused.

An external verifier owns the format of the times it writes, so the importer
reads them as RFC 3339 with an explicit offset rather than in the canonical
millisecond UTC form that OpenAdapt uses for its own fields. A real GitHub CLI
run emits both `2026-08-26T15:50:11-04:00` and `2026-08-26T19:50:10Z` in one
payload. A time without an offset is refused.

The importer accepts only the reviewed GitHub CLI version `2.67.0`. The
protected importer image must pin that version and authenticate it with read
access to the private Cloud repository. A different CLI version, host, trust
instance, timestamp type, timestamp authority, or extra timestamp entry causes
refusal.

The expected Cloud commit is an external reviewer input. The importer requires
the certificate commit, the GitHub signing-certificate source commit, and the
SLSA resolved `gitCommit` to equal that value. It also requires the signing
certificate and the SLSA source dependency to use `refs/heads/main`. The
certificate cannot select its own approved commit.

The evidence manifest does not store or select this approval. The repository
checker reads it from the protected `APPROVED_CLOUD_ACCEPTANCE_COMMIT` GitHub
repository variable. A production declaration fails when that variable is
absent or differs from the certificate and verified provenance.

The certificate is necessary but not sufficient. It proves one authenticated,
qualified browser transaction and binds the private request, idempotent
responses, runner delivery, result, report, receipt, target attestation,
single-use runner permit, independent observer, webhook, billing outcome, and
separate signing identities by digest. The importer also verifies the external
qualification admission. It checks the exact issuer workflow and main-branch
commit, the Ed25519 signature, the derived key ID, the active time window, and
the external admission and key revocation lists. It refuses a trust key that
the imported evidence supplies for itself.

The admission signs one shared evidence identity for the exact workflow,
campaign, environment, Flow release and wheel, runner build and artifact,
browser image, runtime manifest, signer registry revision, and every admitted
contract. The certificate repeats the public domain-separated campaign,
admission, runtime-validation, and workflow-version digests. The importer
recomputes each digest from the retained admission. The admission also signs
canonical hashes for the full campaign, qualification contract, outcomes
projection, oracle, and exact task inventory. The campaign must retain every
condition in the bound qualification contract and at least three unique trials
for each condition. Trial indexes are one-based and contiguous. Attempt IDs
and run IDs cannot repeat. An excluded or hidden trial causes refusal.

The shared identity calls the receipt authority
`evidence_runner_signer_sha256`. This name applies to browser, native Desktop,
BYOC, RDP, Citrix, and offline customer-controlled evidence. It does not imply
that the evidence runner is an OpenAdapt-managed browser runner.

Each `openadapt.qualification-trial-row/v2` trial refers to hash-keyed
`openadapt.qualification-evidence-receipt/v2` Ed25519 envelopes for the runner,
independent observer, webhook, replay, cleanup, and cleanup-absence result.
Fault cases also require a signed fault receipt. The importer verifies each
envelope body, signature, authority key, source digest, task, condition, trial
index, attempt, run, workflow version, bundle artifact, runtime validation,
admission, evidence identity, verdict, and time. A digest with no signed body is
not evidence. An unused envelope is hidden evidence and causes refusal.

The importer classifies the verified rows itself. The signed observer receipt
contains the exact effect inventory. The signed runner receipt contains the
model-call counters, provider-model inventory, egress-policy digest, report
digest, and operator-intervention inventory. The importer derives failures
from these facts. It rejects a count-only claim, declared production class or
boolean, unsupported failure class, vacuous invariant, healthy-path contract
that permits a model call, healthy-path model call, silent incorrect success,
over-halt, wrong-record effect, duplicate effect, collateral effect, uncertain
delivery, platform failure, or operator intervention.

The derived result binds the certificate's campaign-outcomes and independent
oracle-contract digests. It repeats the validated task, condition, required
trial, and observed trial counts. It includes every closed failure-taxonomy
count used for the verdict. Task and condition identities use domain-separated
SHA-256 values. The public result does not contain the private task or condition
labels. The derived-result file digest therefore changes after a one-field
change to a binding, count, or privacy-safe identity.

## Target-neutral acceptance manifest

The importer contains a pure builder and validator for the closed
`openadapt.production-acceptance/v1` manifest. The mechanism does not write,
sign, export, attest, or publish a manifest. It accepts only a complete accepted
private result. It binds the target and its exact claim scope in both the fixed
policy and the manifest.

| Target | Exact claim scope |
| --- | --- |
| `agent` | `qualified_agent_bridge_release` |
| `capture` | `qualified_native_recorder_release` |
| `cloud` | `qualified_workflow_control_plane_deployment` |
| `desktop` | `qualified_native_workflow_desktop_release` |
| `docs` | `production_documentation_deployment` |
| `flow` | `qualified_workflow_runtime_release` |
| `openadapt` | `qualified_workflow_launcher_release` |

The current browser evidence adapter can build only the Flow target manifest.
The other targets require their own evidence adapter. Cloud also requires a
reviewed deployment-manifest binding. The builder refuses Cloud until that
binding exists. It does not emit a placeholder or failed record when an adapter
is absent.

For Flow, a separate lifecycle verifier accepts the exact raw lifecycle-policy
bytes, one closed public-package release, and the PyPI release metadata. It
requires the exact GitHub source-commit URL. It requires one sorted sdist and
one wheel. Each artifact includes its authority, kind, name, URL, size, and
SHA-256 digest. The verifier matches each field to exactly one non-yanked PyPI
file. It then returns an immutable verified-release object. A caller-supplied
mapping or digest cannot replace this object.

The pure manifest builder accepts only this verified-release object. It matches
the Flow version, source commit, and wheel digest to the private result. It
binds the complete sdist and wheel inventory. It uses the same release and
artifact digest domains as the Production lifecycle validator. The manifest
contains two separate policy digests. The acceptance-policy digest covers the
fixed Evals acceptance rules. The lifecycle-policy digest covers the exact raw
lifecycle-policy bytes.

The public qualification section contains aggregate trial counts and one
campaign-scoped task-condition inventory digest. It does not publish task or
condition labels. The closed source-result digest remains the authority for the
hidden per-condition inventory.

When the private-export gate opens, the output claim will be only
`qualified_browser_workflow_on_bound_environment`. It is not a general product
production-readiness claim. The checker can accept `production_acceptance: true`
only when the evidence manifest contains digest-bound links to all four inputs
and the exact derived output, the protected workflow supplies the external
trust, revocation, and export-approval controls, and the importer reproduces
that output. Until then, the importer refuses. A bare registry boolean or
campaign label fails the check.

Cloud does not issue a complete acceptance record before it verifies durable
retention. The private evidence envelope is encrypted with an AES-256-GCM data
key wrapped by KMS, then committed to a private repository and signed on the
Sigstore public-good instance. The public record retains only opaque digests and the
verified retention facts. The importer verifies the public retention receipt
and its exact binding to the candidate and retained envelope. GitHub artifact
retention alone does not satisfy this contract.

## What the current public Evals set establishes

The Flow 1.34.0 set contains four campaigns. Every condition has three trials.
This set is development evidence. It is not a Production lifecycle admission,
and this change does not emit a Production record for it.

| Campaign | Environment | What it measures | Production acceptance |
| --- | --- | --- | --- |
| Comparison | Synthetic MockMed, local macOS host, headless Chromium | Compiled replay and two Playwright controls under clean, theme, and bounded label drift | No |
| Independent replication | The same bounded environment in a second complete run | Repeatability of the comparison result | No |
| Transaction probe | Synthetic MockMed fault server and its independent persistence store | Outcome taxonomy under normal, timeout, optimistic-screen, session, and duplicate faults | No |
| Remote lease safety | An instrumented fake remote backend | The input-edge refusal contract when a frame lease is absent or changes | No |

The comparison and replication count silent incorrect success and over-halt.
The transaction and remote-lease probes do not count both metrics as production
reliability measures. Their manifest entries say `not_counted`. They therefore
cannot satisfy a production-acceptance gate.

No campaign in this set represents hosted execution, a customer workflow,
Windows UIA, native macOS Accessibility, Linux AT-SPI, a real RDP session, or a
real Citrix ICA/HDX session. Other repositories retain their own bounded
substrate campaigns. Those results remain specific to their exact tasks and
environments, and no campaign in this repository establishes production
acceptance.

## Acceptance exit conditions

A production claim for one workflow needs one immutable acceptance record that
contains all of these facts:

1. The exact task, application version, operating system, display/session
   properties, OpenAdapt release, runner, and deployment revisions.
2. At least three trials for each healthy and fault condition in the accepted
   matrix, with no hidden retry or excluded run.
3. An independent oracle for the intended effect, wrong-record effects,
   duplicates, collateral changes, and effect absence or uncertainty.
4. A complete failure taxonomy that counts silent incorrect success,
   over-halt, safe halt, uncertain delivery, platform failure, and operator
   intervention.
5. The full delivery path for that operating model. A managed-browser result
   includes authenticated submission, idempotency, polling, one-use delivery,
   execution, independent verification, a signed receipt, and a signed webhook.
6. Real configured production dependencies. A mock or simulated success cannot
   satisfy the gate.
7. A clean-machine install and first-run check on every operating system that
   the claim names.

For a real RDP or Citrix claim, the record also includes reconnect, session
change, stale-frame, resolution/DPI, and compression fault conditions. A fake
remote backend proves a runtime contract only. It does not replace a real
session result.

For a generally available Desktop release, distribution evidence is separate
from workflow qualification. The release needs the intended signing,
notarization or Authenticode, installer, update, rollback, and key-lifecycle
evidence. An unsigned or ad-hoc-signed Beta installer can still provide useful
evaluation evidence, but it is not a generally available signed release.
