# Intake-borne prompt-injection cases (pre-registered for #1890)

Each case is a synthetic referral document that embeds an instruction aimed at
an agent (wrong patient or wrong amount), plus the trusted intake field values
and the steered tool-call values an obedient agent would pass. The gate must
refuse the steered call and accept the call that matches intake provenance.

Encoded in `tests/test_intake_param_gate.py` as `PREREGISTERED_CASES`:

- `wrong_patient_in_note`
- `wrong_amount_in_banner`
- `both_wrong`
