# Effect locations

A careful person who has just entered a form checks their work. They read the
review page before they press submit, and afterwards they look again where the
value should now appear: the summary, the saved record, a list row, a second
session or an export. An effect location records one of those places as data,
so the engine can do the same check for any application without knowing which
application it is.

The record is `openadapt.effect-location/v1`
(`openadapt_flow/effect_location/record.py`). A pack stores one per place,
compiled from the demonstration. The evaluator
(`openadapt_flow/effect_location/evaluator.py`) judges it from a reading that
an adapter (a browser, an accessibility tree, a terminal screen, an export
reader) takes when it goes back.

## The record

| Field | What it holds |
| --- | --- |
| `keys` | The intended values the location shows. Usually one. A summary row such as "Name (DOB)" shows two. |
| `path` | The demonstration steps that reach the location. |
| `start` | Where the re-observation starts: `same_screen`, `fresh_navigation`, `new_session` or `other_channel`. |
| `locator` | How to find it: `label`, `row_key` (a row whose `column` cell equals `value`) or `region`. The adapter reads it; the evaluator does not. |
| `expected` | The value each key should show, in the codec's canonical form. The run's plan fills it in. |
| `codec` | How displayed text maps to the value (below). |
| `expected_count` | How many times the value shows. Required; usually 1. |
| `baseline` | Optional. What the location showed before the action. |

## Codecs

Every codec reads the whole displayed text. A prefix, a longer text or a value
with extra words never matches, so "Jane" is not "Jane Doe" and "250" is not
"$1,250.00".

| Kind | Reads | Canonical value |
| --- | --- | --- |
| `text`, `fold` | Text, whitespace collapsed (`fold` also ignores case) | string |
| `number`, `unit` | A decimal number, for `unit` followed by one of `units` | decimal |
| `money` | An amount with optional `affixes` (currency signs), `group_marks` and `decimal_mark` | integer minor units |
| `date` | ISO, or numeric in `date_order` (`YMD`, `MDY`, `DMY`), or with `month_names` | ISO date |
| `bool` | One of `true_tokens` or `false_tokens` | bool |
| `enum` | An entry of `table` (shown text to value) | string |
| `split_date` | Three displayed parts, `year`, `month` and `day` | ISO date |
| `template` | One text matched in full by `pattern`; each of `parts` reads one named group as one key with its own codec | one value per key |

The engine has no default words. A bool codec with no tokens, a date codec
with no month names or a money codec with no currency sign accepts only the
bare form. The pack supplies the rest from the demonstration.

## How a value is judged

The obligations are the run's intended values, fixed before any reading is
judged. Each one ends in one of the verdict kernel's three statuses.

- `not_checked`: no location shows the value, or the location was not read,
  was not found, or was found a different number of times than expected. Also
  when the read failed or came from another run, the start condition was not
  met, a kind is unknown, the text can't be read, or the read was partial and
  showed no conflict. Also when the expected value equals the baseline, so
  the read can't tell the change from what was already there.
- `refuted`: the location shows a different value. A partial read can refute.
- `held`: every location for the value was read in full and showed exactly
  the expected value the expected number of times.

The run passes only when every planned value held and no reading is left
over. When several locations show one value, a refutation at any of them
refutes it, and it holds only when all of them hold, at the weakest level.

## Check level and receipt word

The check level comes from the start condition the adapter achieved:

| Start | Check level |
| --- | --- |
| `same_screen` | 1, `same_screen` |
| `fresh_navigation` | 2, `reopened`; 3 with basis `fresh_reload` when the adapter proves the navigation fetched from the server (cache bypassed, the request kept) |
| `new_session` | 3, `separate_session`, basis `separate_session` |
| `other_channel` | 4, `system_of_record`, with the basis the adapter names (`system_api`, `activity_log`, `db_frame` or `owned_connection`) |

The receipt word comes from `openadapt_types.check_level.outcome_word`, so
this package never decides what `VERIFIED` means. A same-screen read or a
fresh navigation without the reload proof is `SHOWN`; a proven fresh reload
is `VERIFIED`. A new session is `VERIFIED`. Another channel is
`VERIFIED` only with a basis, otherwise `RECORDED`. Until `outcome_word` is on
main, the word is `None`.

The adapter states the start condition it achieved and binds the reading to
the run. The evaluator can't see whether a navigation really fetched from the
server; the adapter must prove it (a cache-bypassing request and its
response, kept as evidence) before it names the `fresh_reload` basis.

## Fail-open testing

`evaluate_all` is a registered verdict site (`effect_location.evaluate_all`).
Its spec, `tests/failopen/sites/effect_location.py`, runs the generic
mutations against a plan with a label, a summary row, a split date and an
amount.
