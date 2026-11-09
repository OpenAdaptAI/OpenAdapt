# Governed API-first eligibility

OpenAdapt uses a sanctioned 270/271 API when an exact practice/payer route is
available, then uses compiled portal replay only as a reviewed fallback. Every
answer is qualified by service, date, network, coverage level, and time period;
ambiguous responses enter the attended queue.

## Execution contract

```text
exact practice payer binding
  ├─ API
  │   ├─ exact, unambiguous 271 → atomic local evidence → verify → complete
  │   ├─ transient failure    → bounded retry → reviewed portal or queue
  │   └─ identity/config/data ambiguity → attended queue
  ├─ reviewed portal route    → compiled replay + independent effect check
  ├─ queue                    → practice staff resolves with evidence
  └─ excluded                 → no automated action
```

Unknown payer names, unmatched payer IDs, unreviewed service types, and account
or test/production mode mismatches never select an API or portal implicitly.

The public `payer_routes.yaml` is one synthetic Stedi TEST-mode example.
Production payer maps are practice-scoped deployment data. Each API binding
records:

- the exact request payer ID, including leading zeroes;
- Stedi's stable five-character payer ID;
- a digest of the reviewed payer-directory record;
- supported service-type codes;
- the practice account and application mode;
- the verification date and an explicit reviewed-portal-fallback decision.

This keeps the route mechanism open while keeping productionized payer recipes
inside the deployment boundary.

The committed synthetic Cigna binding was rechecked against Stedi's public
Payer Network on 2026-07-21: primary payer ID `62308`, current Stedi payer ID
`HGJLR` (`SX071` is an alias), and dental eligibility support. Both exact IDs
resolve to the same reviewed route; fuzzy payer search never selects a route.

## Parsing without “first match” shortcuts

`parse_271` retains every qualified patient-responsibility entry and populates
practice-facing convenience values only when one value matches the request's:

- service type or procedure;
- in-network, out-of-network, or not-applicable qualifier;
- individual/family coverage level;
- benefit date and requested date of service;
- time qualifier, including separate total (`23`, calendar year) and remaining
  (`29`) deductible/out-of-pocket amounts.

Conflicting active/inactive signals, a response for another service, a payer or
application-mode mismatch, an active response without an explicit coverage
interval containing the requested service date, or two different values with
the same qualifiers is not an answer. Redirects and every other non-2xx HTTP
status are also never interpreted as successful eligibility responses.

The current request schema is deliberately subscriber-only and requires the
member ID. The returned 271 must contain the same subscriber member ID; every
name or birth date supplied in the request must also be present and match after
conservative Unicode, case, and whitespace normalization. The returned provider
NPI must also exactly match the request. A missing subscriber or provider, a
mismatched identity, or a response containing dependent subjects enters the
attended queue. A dependent response cannot satisfy this subscriber request
contract; it requires an explicitly typed dependent request rather than a guess
about which returned person is the patient.

## Error and fallback rules

Only explicitly transient outcomes retry automatically:

- HTTP `429`;
- HTTP `5xx` and network timeouts/failures;
- payer connectivity AAA `42`, `80`, or Stedi's documented `42` + `79`
  combination.

Retries are bounded. After they are exhausted, the result may use a portal
fallback only when that fallback was explicitly reviewed and the portal is not
barred. Authentication, invalid payer or
request, provider configuration, member identity, and ambiguous response
outcomes go to practice staff without automatic retry or portal substitution.

A 270 is a read, so retrying does not duplicate a business write. The local
`operation_id` binds attempts and artifact promotion to one logical check.

## Practice-held account and PHI boundary

The practice owns the Stedi account and credential. Production mode requires an
explicit practice-held-account and BAA confirmation. The API key is resolved
from an environment reference and never enters a result, artifact, or reason
string. Requests and response bodies are never logged.

PHI-bearing evidence is written only through `PracticeArtifactPolicy`, which
requires:

- an explicit local PHI boundary;
- an explicit TEST or PRODUCTION application mode that cannot be mixed within
  one artifact boundary;
- either attested encrypted-volume storage or application AES-256-GCM;
- owner-only artifact, transaction, and file paths that are rechecked on every
  reuse and index rebuild;
- a retention period and `egress: none`;
- symlink-safe, exclusive writes and a bounded single-writer lock.

Only an exact, unambiguous answer can enter the consumable result store. The
artifact writer requires the original `EligibilityRequest`, verifies its
canonical SHA-256 and the response-subject evidence digest, then reparses the
exact raw response and requires canonical semantic equality with the supplied
normalized result. The policy's application mode—not a caller-selected result
field—is authoritative for that reparse and is bound into `boundary.json`, the
transaction manifest, normalized JSON, and derived CSV. A TEST response cannot
enter a PRODUCTION artifact boundary. It derives member/date/benefit-selector
fields from the request rather than accepting caller-supplied identity labels. The subject
evidence digest binds the request digest, raw-response digest, and verified
subscriber role; it does not hash a standalone low-entropy identity tuple or add
extra plaintext identity fields to the manifest.
The raw response and normalized practice record are staged, hashed, fsynced,
re-read, and verified together before either the transaction directory or its
prospective derived CSV index is promoted. A failed promotion or independent
hash-effect verification rolls the new transaction back. The CSV is a derived
index, not the source of truth. Repeating the same `operation_id` and content is
idempotent; reusing it with different content fails. Spreadsheet formula
prefixes are neutralized. The persisted `committed_at` is generated by the
writer; a caller-supplied observation timestamp is not accepted as audit truth.
Hash effects independently re-read both stored files.

Retention expiry is bound to the policy and commit time. Every write refuses an
invalid retention manifest and purges expired transactions before idempotency
or index reuse. Deployments also call
`purge_expired_eligibility_artifacts(...)` on startup and their retention
schedule; it verifies the prospective index before atomically hiding expired
transactions, then deletes them. Malformed, insecure, or policy-mismatched
artifacts deny cleanup instead of deleting an unverified path.

## Qualification

The deterministic contract suite covers routing, qualifiers, conflicting
benefits, the HTTP/AAA taxonomy, retries, PHI-safe diagnostics, atomicity,
idempotency, encryption, tampering, symlink substitution, and package import
behavior.

The live test runs Stedi's published synthetic Cigna dental request three times:

```bash
export STEDI_API_KEY='<sandbox test key>'
pytest -q tests/test_eligibility_live_stedi.py -rs
```

All three trials must return an exact TEST-mode answer and independently verify
the encrypted raw and normalized artifacts. If the key is absent, the tests
skip and explicitly state that no live evidence was collected.

## Primary references

- [Real-Time Eligibility Check JSON](https://www.stedi.com/docs/healthcare/api-reference/post-healthcare-eligibility)
- [Submit eligibility checks](https://www.stedi.com/docs/healthcare/send-eligibility-checks)
- [Eligibility troubleshooting and retries](https://www.stedi.com/docs/healthcare/eligibility-troubleshooting)
- [Patient responsibility qualifiers](https://www.stedi.com/docs/healthcare/eligibility-patient-responsibility-benefits)
- [Payer retrieval](https://www.stedi.com/docs/healthcare/api-reference/get-payer)
- [Integrated practice accounts](https://www.stedi.com/docs/healthcare/integrated-account-overview)
- [Account, test keys, production, and BAA setup](https://www.stedi.com/docs/healthcare/account-settings)

The client is import-light and is not imported by the recorder, compiler, or
replayer unless the eligibility waterfall is used.
