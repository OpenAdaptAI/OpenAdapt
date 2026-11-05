# Expressions: computed values and predicates

`openadapt_flow.transforms` is a small, typed and bounded expression language.
Use it to compute a value between steps, such as matching a name to a dropdown
option, adding 30 days to a date, or joining codes. Use it also for a yes/no
check over values a run has already read. It is one language for both uses.

It isn't Python. It has its own parser and checker, and nothing reaches
`eval`, `exec`, `compile` or `ast`.

## What you can write

- A bare name is a run parameter. Every parameter is text. Convert with
  `to_int`, `date` or `parse_date`.
- Literals: `'text'` or `"text"`, integers, decimals (only where a threshold
  is expected), `true`, `false` and `[ 'a', 'b' ]` lists of text.
- Operators: `+` joins two strings or adds two ints. `-`, `*` and unary `-`
  work on ints. `==` and `!=` compare two values of the same type. `<`, `<=`,
  `>` and `>=` compare ints or dates. `and`, `or` and `not` take bools only.
  There is no truthiness. Comparisons don't chain.
- There is no attribute access, no indexing syntax, no import, no assignment,
  no lambda and no comprehension. A name that starts with `__` is refused.

## Functions

| Group | Functions |
|---|---|
| Text | `lower`, `upper`, `trim`, `collapse_spaces`, `length`, `replace`, `substring`, `starts_with`, `ends_with`, `contains`, `pad_left`, `text`, `to_int` |
| Lists | `split`, `join`, `at`, `count` |
| Dates | `date` (ISO), `parse_date` and `format_date` (formats from `YYYY`, `MM`, `DD`, `M`, `D` and `- / .` or space), `add_days`, `add_months` (clamps to the month's end), `days_between`, `year`, `month`, `day` |
| Regex | `matches(s, pattern)`, `extract(s, pattern, group)`: full match only, literal pattern |
| Fuzzy | `fuzzy_match(value, options, threshold)` |
| Control | `if_else(cond, a, b)` |

`fuzzy_match` needs a literal threshold in `(0, 1]`. There is no default. An
exact match after folding case and spaces wins. Otherwise exactly one option
must clear the threshold. If none does, it refuses with `below_threshold`. If
two or more do, it refuses with `ambiguous`. It never guesses.

Regex patterns come from a conservative subset. Back-references, look-around,
inline flags and conditionals are refused. So is a repeated group that repeats
or alternates inside, and so are more than 2 unbounded or 4 variable
quantifiers. Input is capped at 512 characters.

There is no `today()` or `now()`. The language has no clock, randomness,
environment, files, network or locale, so the same inputs always give the same
result.

## Bounds and refusals

Source length, node count, nesting depth, string length, list length, integer
size and the number of transforms are all capped. `operator_set()` lists the
exact numbers. A value that would go past a bound is refused, not truncated.

Anything outside the language raises `ExpressionRefused` with a closed
`RefusalCode`: `syntax`, `unknown_name`, `unknown_function`, `type`, `bounds`,
`unsafe_pattern`, `invalid_value`, `no_match`, `below_threshold`, `ambiguous`
or `conflict`. Messages never repeat parameter values.

## Computed parameters

`ParamTransform(output_param, expression)` is checked when declared.
`apply_transforms(params, transforms)` runs the transforms in order on a copy
of the params. It is all or nothing, and a transform may not overwrite an
existing parameter. Each computed value comes back as a `TransformRecord`
that holds:

- the language version and operator-set digest
- the expression and its SHA-256
- the names of the inputs it read and the SHA-256 of each input value
- the value and its type
- for each fuzzy match: the score, the threshold, the runner-up score and the
  chosen option

The runtime doesn't call these yet. Wiring them into steps and the run
receipt is the next part of #1891.

## Predicates

`evaluate_predicate(source, params)` never raises. It returns `"true"`,
`"false"` or `"unknown"`. Any refusal, or an expression that isn't a bool, is
`"unknown"` with its refusal code. It is never `"true"`. A caller that checks a
write maps `"unknown"` to uncertain and treats `"false"` as an observed
contradiction.

## One language, versioned

`LANGUAGE_VERSION` (`openadapt.expr/v1`) and `operator_set_sha256()` name the
exact language. Anything that stores an expression should bind both. The
tests pin the digest, so adding or changing a function, operator or bound is a
reviewed change to a new version, never a silent one.
