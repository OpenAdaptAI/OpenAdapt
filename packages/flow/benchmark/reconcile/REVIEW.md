# Independent adversarial reviews of the reconciler

Three review rounds ran, each by a separate reviewer that read the code and
ran the tests without the author's reasoning. Each round looked first for a
unit where the systems disagree but the result is `CONFIRMED`. Every finding
below is fixed and has a regression test in `tests/test_reconcile.py`.

## Round 1 (fixed in `709a14c01`, `d722534f9`)

- **False `CONFIRMED`:** a `when`-skipped write-back counted as agreement.
  Now every leg counts. A leg that isn't due is held only when its record is
  absent.
- **False `CONFIRMED`:** the 278's decision wasn't compared with the payer's.
- **False `duplicate`:** a 277 acknowledgement followed by a later status was
  counted twice. This led to `group_by` and `latest_by`.
- **X12 unit boundaries:** a service-level TRN replaced the event trace, and
  a new HL leaked into the previous record.
- **Values in reasons and file names:** reasons are now cleaned, and files
  are named by their index.
- **Other fixes:** CLI exit codes (3 for misuse), a denial's null auth
  number, gaps in due times, and the design note brought in line with the
  schema.

## Round 2 (fixed in `b56e7ce91`, `b605751ce`, `8b65202a8`)

- **False `CONFIRMED`:** the status-history rule hid two payer claim numbers
  under one trace. With `distinct_by`, that group is now two submissions.
- **False `CONFIRMED`:** a denial and a pend were told apart only by the auth
  number. The decision is now compared as an HCR01 code.
- **Response hidden by a request:** a later 278 request hid the response.
  The history is now kept per trace and purpose.
- **Smaller fixes:**
  - full GS05 times;
  - stricter cleaning of reasons;
  - a file index instead of a reversible file-name hash;
  - the file index in every X12 reference.

## Round 3 (fixed in `9e55728fa`)

This round reviewed the changes made for the PAS reference implementation:
response Bundles, the latest ClaimResponse per submission, and the
disposition mapped to HCR01.

- **False `CONFIRMED`:** `latest_by` compared times as text. A grant at
  `06:00:05-05:00` lost to a pend at `10:00:00Z`, and so did
  `10:00:00.500Z`. Marks now compare as instants, or as one sender's X12
  clock. A group with missing, zoneless or mixed marks is kept whole.
- **Value in the result:** the reference implementation's ClaimResponse id
  is its auth number. A record whose id repeats one of its own values is now
  referenced as `ClaimResponse/…`.
- **Accepted and documented:** the decision is selected by the submission
  identifier the payer echoes, not by its `request` reference, which the
  server writes malformed on an update.
- **Accepted and documented:** two decisions stamped in the same second
  give a false `duplicate`. That fails closed.
- **Confirmed as claimed:** an unmapped or missing value is unbound, never
  null. Compares are left unchecked, and `required_when` and `due` stay
  fail-closed.

## Round 4 (fixed in `4c1c596b3`)

- **False `CONFIRMED`:** a join kept whichever of two different `Patient/p`
  records came last. Two different records under one reference now make
  the leg unreadable.
- **False `CONFIRMED`:** a FHIR search match of another resource type
  (an Observation for a Claim) was read as the requested record. A match
  must be the requested type, or a Bundle whose every field is projected
  from an entry of that type.
- **Unsafe history:** an impossible X12 clock (`999913992359`) ordered a
  group. It now orders nothing, and the group is kept whole.

## Round 5

- **False `CONFIRMED`:** the claim spec never checked the 277 status
  category, so a rejected claim (`STC A7:21`) agreed. The claim leg now has
  an `accept` rule: rejected, returned or not found is a `not_accepted`
  break, `REFUTED`; any other category is `UNCERTAIN`. The fixture set
  plants a rejected, a returned and an unknown-status 277.
- **Fails open:** repeated X12 segments were first-wins (`STC`, `REF 1K`,
  `REF BB/NT`, `HCR`, `DTP 472`, service lines, `NM1`, `DMG`). Each slot now
  collects every candidate and is resolved once; conflicting repeats are
  ambiguous.
- **The class, not the site:** every round found one more place that chose
  "the" record or value when the read held several. One rule
  (`reconcile/resolve.py`) now resolves every judged slot (selection,
  values, joins, `latest_by`, `distinct_by`, segment repeats, FHIR lists),
  and a Hypothesis property test generates reads with a conflict in one
  judged slot and checks the verdict is never `CONFIRMED`: `UNCERTAIN` for
  an ambiguity, `REFUTED` only for two certain records.
- **CI:** the `reconcile` subparser and both benchmark scripts now set
  `allow_abbrev=False`.
