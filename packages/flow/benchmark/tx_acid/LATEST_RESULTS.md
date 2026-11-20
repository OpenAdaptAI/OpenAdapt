This historical measurement is unqualified. The original runner could replace failed qualification with an empty certified pack. Its measurements do not establish lens certification, independent application generalization, production reliability, or complete server side-effect coverage. Raw measurements are retained below for audit.

# OpenAdapt Transactional Commit Layer (`tx`) — Production Realism Benchmark

- **Rows Seeded per System of Record:** `50,000` (SQLite WAL mode)
- **Concurrent Background Writers per App:** `20`
- **Concurrent Background Writes Landed:** `11,845`
- **`tx` Engine Backend:** `openadapt_flow.tx`
- **Total Suite Wall Time:** `10.64s`

## Aggregate Comparison Across All 3 Systems of Record

| Arm | Clean Pass Rate | False Stop Rate | Fault Catch Rate | False Accept Rate | Mean Rows Read / Tx | p50 (ms) | p95 (ms) |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `naive_2xx` | 72.2% | 0.0% | 0.0% | 100.0% | 0.00 | 12.79 | 32.66 |
| `legacy_whole_table` | 0.0% | 100.0% | 100.0% | 0.0% | 169.23 | 7.90 | 12.66 |
| `tx_acid` | 100.0% | 0.0% | 100.0% | 0.0% | 1.85 | 41.60 | 64.55 |

## Per-Application Breakdown

| System of Record | Seed Time | BG Writes | Arm | Clean Pass | False Stop | Fault Catch | False Accept | Mean Rows Read | p50 / p95 (ms) |
| :--- | ---: | ---: | :--- | ---: | ---: | ---: | ---: | ---: | ---: |
| `clinical_chart_ehr` | 358 ms | 3,654 | `naive_2xx` | 100.0% | 0.0% | 0.0% | 100.0% | 0.00 | 11.14 / 30.56 |
| `clinical_chart_ehr` | 358 ms | 3,654 | `legacy_whole_table` | 0.0% | 100.0% | 100.0% | 0.0% | 153.85 | 8.08 / 10.47 |
| `clinical_chart_ehr` | 358 ms | 3,654 | `tx_acid` | 100.0% | 0.0% | 100.0% | 0.0% | 1.85 | 38.60 / 56.09 |
| `replace_task_erp` | 261 ms | 4,004 | `naive_2xx` | 16.7% | 0.0% | 0.0% | 100.0% | 0.00 | 13.55 / 36.80 |
| `replace_task_erp` | 261 ms | 4,004 | `legacy_whole_table` | 0.0% | 100.0% | 100.0% | 0.0% | 176.92 | 7.10 / 11.56 |
| `replace_task_erp` | 261 ms | 4,004 | `tx_acid` | 100.0% | 0.0% | 100.0% | 0.0% | 1.85 | 41.32 / 64.58 |
| `claims_ledger` | 263 ms | 4,187 | `naive_2xx` | 100.0% | 0.0% | 0.0% | 100.0% | 0.00 | 13.92 / 29.12 |
| `claims_ledger` | 263 ms | 4,187 | `legacy_whole_table` | 0.0% | 100.0% | 100.0% | 0.0% | 176.92 | 9.10 / 15.31 |
| `claims_ledger` | 263 ms | 4,187 | `tx_acid` | 100.0% | 0.0% | 100.0% | 0.0% | 1.85 | 47.42 / 67.66 |

## Per-Fault-Class Catch Rate Matrix (Aggregate)

| Fault Class | `naive_2xx` | `legacy_whole_table` | `tx_acid` |
| :--- | ---: | ---: | ---: |
| `partial_put_clobber` | 0.0% | 100.0% | 100.0% |
| `silent_drop_200_ok` | 0.0% | 100.0% | 100.0% |
| `wrong_value_mutation` | 0.0% | 100.0% | 100.0% |
| `duplicate_post_on_retry` | 0.0% | 100.0% | 100.0% |
| `collateral_extra_write` | 0.0% | 100.0% | 100.0% |
| `wrong_target_record` | 0.0% | 100.0% | 100.0% |
| `same_record_concurrent_race` | 0.0% | 100.0% | 100.0% |
