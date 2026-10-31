### Live OpenEMR 8.0.0.3, 2026-10-24

Raw per-case rows, evidence and audit rows: private, in openadapt-corpus
`reliability_recipes/verification_coverage/live-2026-10-24/`.

| class | cases | shipped | framed | presave |
|---|---|---|---|---|
| clean | 1 | 1/1 ok | 1/1 ok | 1/1 ok |
| concurrent_legitimate | 2 | 0/2 ok, **2 false alarm** | 0/2 ok, **2 false alarm** | 2/2 ok |
| wrong_record | 2 | 2/2 caught | 2/2 caught | 0/2 caught, **2 missed** |
| unrelated_change | 7 | 3/7 caught, **4 missed** | 3/7 caught, **4 missed** | 0/7 caught, **7 missed** |
| misread_source | 3 | 0/3 caught, **3 missed** | 2/3 caught, **1 missed** | 3/3 caught |
| missing_side_record | 1 | 0/1 caught, **1 missed** | 0/1 caught, **1 missed** | 0/1 caught, **1 missed** |
| duplicate | 1 | 1/1 caught | 1/1 caught | 0/1 caught, **1 missed** |

| case | expect | shipped | framed | pre-save |
|---|---|---|---|---|
| clean | clean | verified (ok) | verified (ok) | agree |
| wrong_record_existing_chart | fault | reconciliation_required (caught) | reconciliation_required (caught) | agree |
| wrong_record_lookalike | fault | reconciliation_required (caught) | reconciliation_required (caught) | agree |
| unrelated_other_chart | fault | reconciliation_required (caught) | reconciliation_required (caught) | agree |
| unrelated_other_history | fault | verified (missed) | verified (missed) | agree |
| unrelated_table_note_other_patient | fault | verified (missed) | verified (missed) | agree |
| unrelated_table_allergy_other_patient | fault | verified (missed) | verified (missed) | agree |
| unrelated_table_insurance_new_patient | fault | verified (missed) | verified (missed) | agree |
| unrelated_other_chart_unattributed | fault | reconciliation_required (caught) | reconciliation_required (caught) | agree |
| misread_dob | fault | verified (missed) | reconciliation_required (caught) | hold |
| misread_swapped_names | fault | verified (missed) | reconciliation_required (caught) | hold |
| misread_wrong_provider | fault | verified (missed) | verified (missed) | hold |
| missing_history_record | fault | verified (missed) | verified (missed) | agree |
| duplicate_save | fault | reconciliation_required (caught) | reconciliation_required (caught) | agree |
| concurrent_other_chart | clean | reconciliation_required (false_alarm) | reconciliation_required (false_alarm) | agree |
| concurrent_new_patient | clean | reconciliation_required (false_alarm) | reconciliation_required (false_alarm) | agree |
| concurrent_plus_bot_stray | fault | reconciliation_required (caught) | reconciliation_required (caught) | agree |

