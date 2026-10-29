# Who wrote what, and how long it took: Medplum against OpenEMR

Both columns are agent wall-clock from file and commit timestamps, not a
person's hours: an agent did the work in both cases, and no hours ledger
exists for OpenEMR. The OpenEMR baseline is the referral-intake record check
(#1604's profile and the contracts before it), reconstructed from git.

## The Medplum contract, part by part

`ensure_patient.yaml` has 29 lines that aren't comments. `generate-parts`
(#1520, generator `13e2cbdb5`) wrote 14 of them from three demonstrations
of Medplum's new-patient form; a person wrote or corrected 15.

| Part | Generated | What a person did | Minutes |
| --- | --- | --- | --- |
| Read channel (server, resource type, credential) | yes, from the CapabilityStatement | nothing | 0 |
| Field paths: given, family, birth date, sex, street, city, state, postal code | yes, from the demonstrations' before and after reads | nothing | 0 |
| Phone and email | **no**: the generator read both telecom entries as one `telecom.value`, bound it to the phone, couldn't find the email, and its gate refused the contract for that | wrote the two `telecom[system=...]` paths | 1 |
| Comparisons (date, digits, case) | no | three words | 1 |
| State: the task names it, the record holds its code | no (the demonstrations typed `MA`) | one map entry | 1 |
| Identity: first, last and birth date | yes, the generator's calibrated rule | nothing | 0 |
| Identity: email | no | one rule | 1 |
| Search parameter per identity value | no | one line | 1 |
| Closed record | no | one line | 0.5 |
| Change feed (audit read, heartbeat, `_history`, no `meta.author`, sweep) | no | five lines, once per server | 0.5 |
| **Contract total** | 14 lines | 15 lines | **about 6** |

What the read path needed, found by probing the live server once
(`probe.py`, `probe.json`), not by fault rounds:

| Finding | What it took | Minutes |
| --- | --- | --- |
| Medplum never returns `meta.author`, so `_history` can't confirm who wrote | `history_author: false` | 4 |
| A plain member can create, change and delete AuditEvents, unlogged | an access policy for the agent (one AccessPolicy resource) | 8 |
| AuditEvents land up to 0.1 s after the save | a product change: close the window, read until stable | 10 |
| An empty AuditEvent search can mean "not logging" | a product change: the heartbeat | 8 |
| **Read-path qualification total** | | **about 30** |

The fixture also had to move its users out of Medplum's seeded Super Admin
project (9 minutes), and the demonstration harness had to type dates the
way headless Chromium's date input takes them (8 minutes). Neither is part
of qualifying the app; a deployment has its own project and its own clerk.

## Against OpenEMR

| | OpenEMR (MySQL, whole database) | Medplum (FHIR + AuditEvent) |
| --- | --- | --- |
| App-specific lines written by hand | about 234 on the base path, about 350 with the whole-database contract and its reader | 15 contract lines and one access policy |
| Agent wall-clock on app-specific contract work | about 6.7 h (base contract 1.2 h, whole-database contract 1.5 h, bookkeeping profile 4 h), plus 3 days of review fixes | about 36 minutes (6 on the contract, 30 on the read path) |
| Blind held-out rounds before the gate was met | 4 (14/17, 16/17, 14/17, 17/17) | see RESULTS.md |

What makes the difference, and what doesn't carry over:

- The record contract's field paths came from demonstrations instead of
  being written. That is most of OpenEMR's base contract.
- There was no bookkeeping profile to write. FHIR exposes resources, not
  tables, so the 4 hours #1604 spent excusing OpenEMR's own writes have no
  counterpart. The price is coverage: the FHIR read path can't see a write
  that doesn't go through FHIR (ENSURE_RECORD.md has the table).
- The verifier's generic parts (the change feed, the closed record, the
  heartbeat) were written once here and serve the next FHIR server
  unchanged. Their time is in the read-path row above, so this comparison
  counts them against Medplum.
