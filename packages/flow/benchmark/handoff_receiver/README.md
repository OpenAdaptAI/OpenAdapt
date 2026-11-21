# Referral intake: a real, synthetic OpenEMR as the receiving clinic

These are the real-app cases for counterparty confirmations by read-back
(see [HANDOFFS.md](../../docs/HANDOFFS.md)). A synthetic referring clinic
sends referrals to a **second, separate OpenEMR 7.0.3** that plays the
receiving clinic. OpenAdapt reads the receiver back and says whether each
referral was recorded as intended, recorded differently, or never arrived.

- **Sender:** `referral_cases.Sender`, a SQLite outbox of generated synthetic
  patients.
- **Delivery:** the receiving clinic's intake, written through OpenEMR's
  standard REST API (`/apis/default/api/patient`), with the sender's
  referral id as the patient's external id. It makes mistakes on purpose.
- **Read-back:** OpenEMR's FHIR R4 `Patient` search, with a separate
  read-only token, by the profile in
  [`tests/fixtures/handoffs/fhir-patient-intake.yaml`](../../tests/fixtures/handoffs/fhir-patient-intake.yaml).
  It never reads the delivery call's answer.

## Bring-up (lab, WSL Docker)

```bash
docker compose -f docker-compose.yml up -d    # project ho-openemr, loopback 9780/9781
# wait for "Starting apache!" in the logs (about 8 minutes on first boot), then:
OPENEMR_COMPOSE_FILE=$PWD/docker-compose.yml OPENEMR_BASE_HTTPS=https://127.0.0.1:9781 \
OPENEMR_SCOPES="openid api:oemr api:fhir user/patient.read user/patient.write user/Patient.read user/Patient.write" \
  ../openemr_live/setup.sh > write.env
OPENEMR_COMPOSE_FILE=$PWD/docker-compose.yml OPENEMR_BASE_HTTPS=https://127.0.0.1:9781 \
OPENEMR_SCOPES="openid api:fhir user/Patient.read" \
  ../openemr_live/setup.sh > read.env
```

Export these from the two tokens:

- `HANDOFF_RECEIVER_API_BASE=https://127.0.0.1:9781/apis/default/api`
- `HANDOFF_RECEIVER_WRITE_TOKEN` (from `write.env`)
- `HANDOFF_RECEIVER_FHIR_BASE=https://127.0.0.1:9781/apis/default/fhir`
- `HANDOFF_RECEIVER_READ_TOKEN` (from `read.env`; a POST with it gets 401)
- `HANDOFF_RECEIVER_VERIFY_TLS=0` (self-signed certificate)

Then run:

```bash
pytest tests/test_handoffs_live_openemr.py          # or:
python -m benchmark.handoff_receiver.referral_cases --repeat 5 --out results.json
```

Tear down with `docker compose -f docker-compose.yml down -v`.

## Cases

Each case is repeated with a fresh synthetic patient. The table is in
`referral_cases.py`, and results are in [`results/`](results/).

| Case | What the intake does | Expected |
|---|---|---|
| intended | records it as sent | `recorded_as_intended` |
| benign_format | names in capitals with extra spaces | `recorded_as_intended` (no false alarm) |
| wrong_date | birth date one day later | `recorded_differently` [birth_date] |
| swapped_date | day and month swapped | `recorded_differently` [birth_date] |
| wrong_patient_field | another family name | `recorded_differently` [family_name] |
| wrong_sex | the other sex | `recorded_differently` [sex] |
| never_arrived | drops it | `sent`, `not_found` (follow-up due) |
| duplicate | records it twice | `sent`, `ambiguous_key` |
| unreachable | records it; the read goes to a closed port | `sent`, `receiver_unreachable` |

Synthetic data only.
