"""The IR expression language for data transforms between steps (#1891).

Everything here is synthetic. The safety half of this file proves the
language fails closed: no code execution, no imports, no attribute walks,
bounded evaluation, and a clear refusal for anything outside the language.
"""

from __future__ import annotations

import hashlib
import time

import pytest
from pydantic import ValidationError

from openadapt_flow.transforms import (
    MAX_DEPTH,
    MAX_INT_ABS,
    MAX_NODES,
    MAX_SOURCE_CHARS,
    MAX_STRING_CHARS,
    ExpressionRefused,
    ParamTransform,
    RefusalCode,
    apply_transforms,
    compile_expression,
    evaluate,
    stdlib_names,
)

PROVIDERS = "Dr. Jane Smith|Dr. John Smith|Dr. Ann Lee"


def value(source: str, **params: str):
    return evaluate(source, params).value


def refused(source: str, **params: str) -> RefusalCode:
    with pytest.raises(ExpressionRefused) as info:
        evaluate(source, params)
    return info.value.code


# ---------------------------------------------------------------- behaviour


def test_string_functions() -> None:
    assert value("upper(trim(x))", x="  ab ") == "AB"
    assert value("lower(x) + '-' + y", x="AB", y="c") == "ab-c"
    assert value("collapse_spaces(x)", x=" a   b ") == "a b"
    assert value("replace(x, '-', '')", x="12-34-5") == "12345"
    assert value("substring(x, 0, 3)", x="ABC123") == "ABC"
    assert value("pad_left(x, 5, '0')", x="42") == "00042"
    assert value("length(x)", x="abcd") == 4
    assert value("starts_with(x, 'A') and not ends_with(x, 'Z')", x="ABC") is True
    assert value("contains(x, 'B')", x="ABC") is True
    assert value("to_int(x) * 2 - 1", x=" 21 ") == 41
    assert value("text(to_int(x) + 1)", x="9") == "10"


def test_split_and_join_codes() -> None:
    assert value("join(split(x, ','), '; ')", x="A1,B2,C3") == "A1; B2; C3"
    assert value("at(split(x, '|'), 1)", x="E11.9|I10") == "I10"
    assert value("count(split(x, ','))", x="a,b,c") == 3
    assert value("join(['a', x], '/')", x="b") == "a/b"


def test_date_offsets_and_formats() -> None:
    assert value("text(add_days(date(d), 30))", d="2026-01-31") == "2026-03-02"
    # Month arithmetic clamps to the end of a shorter month.
    assert value("text(add_months(date(d), 1))", d="2026-01-31") == "2026-02-28"
    assert value("text(add_months(date(d), -12))", d="2024-02-29") == "2023-02-28"
    assert value("format_date(date(d), 'MM/DD/YYYY')", d="2026-07-04") == ("07/04/2026")
    assert value("format_date(date(d), 'M/D/YYYY')", d="2026-07-04") == "7/4/2026"
    assert value("text(parse_date(d, 'DD.MM.YYYY'))", d="04.07.2026") == ("2026-07-04")
    assert value("days_between(date(a), date(b))", a="2026-01-01", b="2026-03-01") == 59
    assert value("date(a) < date(b)", a="2026-01-01", b="2026-01-02") is True
    assert value("year(date(d)) + month(date(d)) + day(date(d))", d="2026-07-04") == (
        2026 + 7 + 4
    )


def test_regex_extract_and_matches() -> None:
    assert value(r"extract(x, 'MRN[- ]?(\d{6})', 1)", x="MRN-123456") == "123456"
    assert value(r"matches(x, '[A-Z]\d{2}(\.\d{1,2})?')", x="E11.9") is True
    assert value(r"matches(x, '[A-Z]\d{2}(\.\d{1,2})?')", x="E11.") is False
    assert value("if_else(matches(x, '[0-9]+'), 'number', 'other')", x="12") == (
        "number"
    )


def test_fuzzy_match_picks_the_one_option_above_threshold() -> None:
    result = evaluate(
        "fuzzy_match(p, split(opts, '|'), 0.8)",
        {"p": "dr jane smyth", "opts": PROVIDERS},
    )
    assert result.value == "Dr. Jane Smith"
    (match,) = result.matches
    assert match["option_index"] == 0
    assert match["threshold"] == 0.8
    assert match["score"] >= 0.8 > match["runner_up"]


def test_fuzzy_match_exact_after_folding_wins() -> None:
    assert value("fuzzy_match(p, ['Smith', 'Smithe'], 0.5)", p=" SMITH ") == "Smith"


def test_fuzzy_match_below_threshold_refuses_rather_than_guessing() -> None:
    assert (
        refused(
            "fuzzy_match(p, split(opts, '|'), 0.95)", p="dr jane smyth", opts=PROVIDERS
        )
        == RefusalCode.BELOW_THRESHOLD
    )
    assert (
        refused("fuzzy_match(p, split(opts, '|'), 0.9)", p="Bob Ray", opts=PROVIDERS)
        == RefusalCode.BELOW_THRESHOLD
    )


def test_fuzzy_match_two_options_above_threshold_is_ambiguous() -> None:
    assert (
        refused("fuzzy_match(p, split(opts, '|'), 0.6)", p="Dr J Smith", opts=PROVIDERS)
        == RefusalCode.AMBIGUOUS
    )
    assert refused("fuzzy_match(p, ['ab', 'AB'], 0.5)", p="ab") == RefusalCode.AMBIGUOUS


def test_fuzzy_match_threshold_is_explicit_literal_and_in_range() -> None:
    for src in (
        "fuzzy_match(p, ['a'])",  # no threshold: no default exists
        "fuzzy_match(p, ['a'], t)",  # not a literal
        "fuzzy_match(p, ['a'], '0.9')",
    ):
        assert refused(src, p="a", t="0.1") == RefusalCode.TYPE
    for src in ("fuzzy_match(p, ['a'], 0)", "fuzzy_match(p, ['a'], 1.5)"):
        assert refused(src, p="a") == RefusalCode.INVALID_VALUE


def test_apply_transforms_records_values_for_the_receipt() -> None:
    transforms = [
        ParamTransform(
            output_param="due", expression="text(add_days(date(visit), 30))"
        ),
        ParamTransform(
            output_param="provider_option",
            expression="fuzzy_match(provider, split(options, '|'), 0.8)",
        ),
        ParamTransform(
            output_param="due_us", expression="format_date(date(due), 'MM/DD/YYYY')"
        ),
    ]
    params = {"visit": "2026-01-31", "provider": "dr jane smyth", "options": PROVIDERS}
    out, records = apply_transforms(params, transforms)
    assert out["due"] == "2026-03-02"
    assert out["due_us"] == "03/02/2026"
    assert out["provider_option"] == "Dr. Jane Smith"
    assert params == {
        "visit": "2026-01-31",
        "provider": "dr jane smyth",
        "options": PROVIDERS,
    }
    assert [r.output_param for r in records] == ["due", "provider_option", "due_us"]
    rec = records[1]
    assert rec.schema_version == "openadapt.param-transform-record/v1"
    assert rec.value == "Dr. Jane Smith" and rec.value_type == "string"
    assert rec.inputs == ["options", "provider"]
    assert rec.input_sha256["provider"] == hashlib.sha256(b"dr jane smyth").hexdigest()
    assert rec.expression_sha256 == hashlib.sha256(rec.expression.encode()).hexdigest()
    assert rec.matches[0].option_index == 0 and rec.matches[0].threshold == 0.8
    assert records[2].inputs == ["due"]  # later transforms see earlier outputs


def test_apply_transforms_is_deterministic() -> None:
    t = [
        ParamTransform(
            output_param="o", expression="fuzzy_match(p, split(x, '|'), 0.8)"
        )
    ]
    params = {"p": "dr ann lee", "x": PROVIDERS}
    first = apply_transforms(params, t)
    second = apply_transforms(params, t)
    assert first == second
    assert [r.model_dump() for r in first[1]] == [r.model_dump() for r in second[1]]


def test_apply_transforms_is_all_or_nothing() -> None:
    params = {"visit": "2026-01-31", "provider": "Bob Ray", "options": PROVIDERS}
    transforms = [
        ParamTransform(
            output_param="due", expression="text(add_days(date(visit), 30))"
        ),
        ParamTransform(
            output_param="opt",
            expression="fuzzy_match(provider, split(options, '|'), 0.9)",
        ),
    ]
    with pytest.raises(ExpressionRefused) as info:
        apply_transforms(params, transforms)
    assert info.value.code == RefusalCode.BELOW_THRESHOLD
    assert "due" not in params


def test_transform_may_not_overwrite_a_parameter() -> None:
    with pytest.raises(ExpressionRefused) as info:
        apply_transforms(
            {"x": "1"}, [ParamTransform(output_param="x", expression="'2'")]
        )
    assert info.value.code == RefusalCode.CONFLICT


def test_non_text_parameter_values_are_refused() -> None:
    with pytest.raises(ExpressionRefused) as info:
        apply_transforms({"x": 1}, [])  # type: ignore[dict-item]
    assert info.value.code == RefusalCode.TYPE


def test_declaration_is_checked_at_load() -> None:
    ParamTransform(output_param="ok", expression="upper(x)")
    for bad in ("__import__('os')", "split(x, ',')", "fuzzy_match(x, ['a'])", "1.5"):
        with pytest.raises(ValidationError):
            ParamTransform(output_param="o", expression=bad)
    with pytest.raises(ValidationError):
        ParamTransform(output_param="a.b", expression="'x'")
    with pytest.raises(ValidationError):
        ParamTransform(output_param="o", expression="'x'", extra="nope")  # type: ignore[call-arg]


# ---------------------------------------------------------------- safety


@pytest.mark.parametrize(
    "source",
    [
        "__import__('os').system('true')",
        "x.__class__",
        "x.__class__.__mro__",
        "x.upper()",
        "x . lower",
        "().__class__.__base__.__subclasses__()",
        "lambda: 1",
        "[c for c in x]",
        "1 if true else 2",
        "x = 'a'",
        "x; y",
        "x[0]",
        "x[0:2]",
        "2 ** 10",
        "x / 2",
        "x % 2",
        "f'{x}'",
        "`x`",
        "x @ y",
        "{'a': 1}",
        "@decorator",
        "'a' 'b'",
        "1 < 2 < 3",
        "upper(x",
        "'unterminated",
        "",
        "   ",
    ],
)
def test_out_of_language_syntax_is_refused(source: str) -> None:
    assert refused(source, x="abc", y="def") == RefusalCode.SYNTAX


@pytest.mark.parametrize(
    "source",
    [
        "eval('1')",
        "exec('x = 1')",
        "compile('1', 'f', 'eval')",
        "open('/etc/passwd')",
        "getattr(x, 'upper')",
        "globals()",
        "locals()",
        "vars(x)",
        "type(x)",
        "breakpoint()",
        "input()",
        "print(x)",
        "importlib('os')",
        "system('true')",
        "today()",  # not deterministic, so not in the library
        "now()",
        "random()",
        "env('HOME')",
    ],
)
def test_functions_outside_the_library_are_refused(source: str) -> None:
    assert refused(source, x="abc") == RefusalCode.UNKNOWN_FUNCTION


def test_library_is_closed_and_has_no_escape_hatches() -> None:
    names = set(stdlib_names())
    for forbidden in ("eval", "exec", "open", "getattr", "import", "today", "now"):
        assert forbidden not in names


def test_names_resolve_only_to_parameters() -> None:
    for name in ("os", "sys", "builtins", "open", "self", "params", "True", "None"):
        assert refused(name, x="a") == RefusalCode.UNKNOWN_NAME
    # A parameter named like a builtin is just text.
    assert value("upper(open)", open="a") == "A"


@pytest.mark.parametrize(
    "source",
    [
        "'1' + 1",
        "x + 1",
        "not x",  # no truthiness
        "x and true",
        "if_else(x, 'a', 'b')",
        "if_else(true, 'a', 1)",
        "'a' * 3",
        "x < 'b'",  # strings have no order here
        "[1, 2]",
        "upper(1)",
        "upper(x, x)",
        "add_days(x, 1)",
        "matches(x, y)",  # pattern must be a literal
        "parse_date(x, y)",
        "extract(x, '(a)', y)",
        "split(x, ',') == split(x, ',')",
        "text(split(x, ','))",
        "-x",
    ],
)
def test_type_errors_are_refused_before_anything_runs(source: str) -> None:
    assert refused(source, x="abc", y="b") == RefusalCode.TYPE


@pytest.mark.parametrize(
    "pattern",
    [
        "(a+)+",
        "(a*)*b",
        "(a|aa)*",
        "(a|b)+c",
        "(?:x+y?)+",
        "(a{1,5}){2,}",
        r"(\w+)\1",
        r"(?P<n>a)(?P=n)",
        "a(?=b)",
        "a(?!b)",
        "(?<=a)b",
        "(?i)abc",
        "a*b*c*",
        "a?b?c?d?e?",
        "a{1,500}",
        "a" * 201,
    ],
)
def test_backtracking_prone_patterns_are_refused(pattern: str) -> None:
    src = "matches(x, '" + pattern.replace("\\", "\\\\") + "')"
    assert refused(src, x="aaaa") == RefusalCode.UNSAFE_PATTERN


def test_catastrophic_input_cannot_hang_evaluation() -> None:
    start = time.monotonic()
    assert refused("matches(x, '(a+)+b')", x="a" * 64) == RefusalCode.UNSAFE_PATTERN
    assert refused("matches(x, '[a-z]+')", x="a" * 10_000) == RefusalCode.BOUNDS
    assert value(r"matches(x, '\w*a\w*b')", x="a" * 512) is False
    assert time.monotonic() - start < 5


def test_bounds_refuse_rather_than_grow() -> None:
    assert refused("x" * (MAX_SOURCE_CHARS + 1)) == RefusalCode.BOUNDS
    assert refused("(" * (MAX_DEPTH + 1) + "x" + ")" * (MAX_DEPTH + 1), x="a") == (
        RefusalCode.BOUNDS
    )
    assert refused("not " * (MAX_DEPTH + 1) + "true") == RefusalCode.BOUNDS
    assert refused("+".join(["1"] * (MAX_NODES + 1))) == RefusalCode.BOUNDS
    assert refused(f"{MAX_INT_ABS + 1}") == RefusalCode.BOUNDS
    assert refused(f"{MAX_INT_ABS} * {MAX_INT_ABS}") == RefusalCode.BOUNDS
    assert refused(f"pad_left(x, {MAX_STRING_CHARS + 1}, '0')", x="1") == (
        RefusalCode.BOUNDS
    )
    big = "a" * (MAX_STRING_CHARS // 2 + 1)
    assert refused("x + x", x=big) == RefusalCode.BOUNDS
    assert refused("replace(x, 'a', 'aaaa')", x="a" * 3000) == RefusalCode.BOUNDS
    assert refused("split(x, ',')  == x", x="a") == RefusalCode.TYPE
    assert refused("count(split(x, ','))", x="," * 5000) == RefusalCode.BOUNDS
    assert refused("text(add_days(date(x), 9999999))", x="2026-01-01") == (
        RefusalCode.BOUNDS
    )
    assert refused("fuzzy_match(x, ['a'], 0.5)", x="a" * 300) == RefusalCode.BOUNDS


@pytest.mark.parametrize(
    "source, params",
    [
        ("date(x)", {"x": "2026-02-30"}),
        ("date(x)", {"x": "07/04/2026"}),
        ("to_int(x)", {"x": "1e3"}),
        ("to_int(x)", {"x": "0x10"}),
        ("at(split(x, ','), 5)", {"x": "a,b"}),
        ("substring(x, 2, 1)", {"x": "abc"}),
        ("split(x, '')", {"x": "abc"}),
        ("format_date(date(x), 'YYYY-%m')", {"x": "2026-01-01"}),
        ("format_date(date(x), 'YYYY-MM')", {"x": "2026-01-01"}),
    ],
)
def test_bad_values_and_formats_are_refused(source: str, params: dict) -> None:
    assert refused(source, **params) == RefusalCode.INVALID_VALUE


def test_regex_that_does_not_match_refuses_extract() -> None:
    assert refused(r"extract(x, 'MRN(\d{6})', 1)", x="none") == RefusalCode.NO_MATCH


def test_compile_expression_checks_without_running() -> None:
    compile_expression("upper(x)", {"x"})
    with pytest.raises(ExpressionRefused) as info:
        compile_expression("upper(y)", {"x"})
    assert info.value.code == RefusalCode.UNKNOWN_NAME


def test_refusal_messages_never_repeat_parameter_values() -> None:
    secret = "PATIENT-SECRET-VALUE"
    for src in ("date(x)", "to_int(x)", "fuzzy_match(x, ['zzz'], 0.99)"):
        with pytest.raises(ExpressionRefused) as info:
            evaluate(src, {"x": secret})
        assert secret not in str(info.value)


def test_random_input_only_ever_returns_or_refuses() -> None:
    """Seeded fuzz: no input may leak a Python error, run code, or hang."""

    import random

    rng = random.Random(1891)
    pieces = [
        "x", "y", "(", ")", "[", "]", ",", "+", "-", "*", "==", "<", "not ",
        " and ", " or ", "'a'", "'\\d+'", "1", "0.5", "true", "upper(",
        "split(", "join(", "at(", "date(", "add_days(", "fuzzy_match(",
        "matches(", "extract(", "if_else(", "text(", ".", "__", ";", "=",
        "pad_left(", "substring(", "to_int(", "'2026-01-01'", "'|'",
    ]  # fmt: skip
    start = time.monotonic()
    for _ in range(3000):
        src = "".join(rng.choice(pieces) for _ in range(rng.randint(1, 12)))
        try:
            evaluate(src, {"x": "a|b", "y": "2026-01-01"})
        except ExpressionRefused:
            pass
    assert time.monotonic() - start < 20
