# Generated parts for the patient registration save

Approve these sentences, not the files. Nothing here runs until you do.

## How the system of record is read
- Through fhir search Patient (fhir).
- It showed each demonstration's record, read every page, and read as unreadable when the credentials, a page or the response were broken (next_page_fails, no_auth, not_json, status_401, status_500, truncated).

## What the save must do, and must not do
- Exactly one new patient registration record appears, found by address.line.
- Its address.city is the city the task names.
- Its address.postalCode is the postal_code the task names.
- Its address.state is the state_label the task names.
- Its birthDate is the DOB the task names.
- Its gender is the sex the task names.
- Its name.family is the lname the task names.
- Its name.given is the fname the task names.
- Its resourceType is 'Patient' (the same in every demonstration).
- Its telecom.system is 'phone' (the same in every demonstration).
- Its telecom.value is the phone_home the task names.
- No other patient registration record is added, removed or changed.
- Not checked: every demonstration typed the same 'state_label'; a field holding it is checked against the typed value, but whether it is a constant can't be told (vary it to find out).
- Not checked: typed value 'email' is not stored as typed in the saved record; its conversion is generated with the input types, or a person maps it.

## When a new record is already there
- The reviewed template patient-identity/1 decides; this system's fields feed it as follows:
  - dob from 'birthDate'
  - fname from 'name.given'
  - lname from 'name.family'
  - phone from 'telecom.value'
  - record_id from 'id'
  - street from 'address.line'
- Every demonstration's saved record read back as typed for 5 of 5 inputs.
- template patient-identity/1 needs openadapt_flow.runtime.checks.duplicate_record: the perturbation suite was not run.

A rule calibrated on this system's records alone, for comparison:
- Stop: the record exists when first the same and last the same and dob the same.
- Stop for a person when the new record lacks a name or a date of birth.
- Nicknames come from a generic table of given-name variants (name_variants.json, made by a model from common names only).
- On this system's own 0 pairs of existing records it fired 0 times; on 15 made-up duplicates it missed 93%.
- It mostly misses: both_names_typo+contact_changed, both_names_typo+contact_kept, dob_digit+contact_changed, dob_digit+contact_kept, first_initial+contact_changed, first_initial+contact_kept, first_nickname+contact_changed, first_nickname+contact_kept, first_typo+contact_changed, first_typo+contact_kept, last_typo+contact_changed, last_typo+contact_kept, names_swapped+contact_changed, names_swapped+contact_kept.

## Where the typed values come from
- No source documents were given; the task's values are checked as typed.

## Never generated
- The execution profile, approval modes, the go-live switch, which steps are consequential, the order of the gates, and attribution of changes to other users.
