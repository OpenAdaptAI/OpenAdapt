"""``NULL`` and ``""`` as one value, declared per field (#2391).

The judge compared the frame with strict equality, so an app that stores an
untouched optional field as ``NULL`` and writes ``""`` back on a form save got
every correct save called a clobber. The fix is a ``null_empty`` field codec:
learned from what the server did, scoped to the field, and never treating
``0``, ``False``, ``[]`` or any non-empty value as empty.
``test_tx_null_empty_live.py`` reproduces the case on a real app.
"""

from __future__ import annotations

import pytest

from openadapt_flow.tx.induce import induce_lens_from_har, learn_null_empty_codecs
from openadapt_flow.tx.judge import FrameAndIntentJudge, apply_field_codec
from openadapt_flow.tx.lens import _FallbackExactAndFrameJudge
from openadapt_flow.tx.types import FieldCodec

NE = FieldCodec(kind="null_empty")
BEFORE = {"id": 1, "phone": "555-0100", "email": None, "city": None, "note": "x"}
AFTER = {"id": 1, "phone": "555-0199", "email": "", "city": "", "note": "x"}
INTENT = {"phone": "555-0199"}


@pytest.mark.parametrize(
    "a,b,ok",
    [
        (None, "", True),
        ("", None, True),
        (None, None, True),
        ("a", "a", True),
        (None, 0, False),
        ("", False, False),
        (None, [], False),
        ("", "a", False),
        (None, "None", False),
    ],
)
def test_codec_equates_only_the_two_empty_spellings(a, b, ok) -> None:
    assert apply_field_codec(NE, a, b)[0] is ok


@pytest.mark.parametrize(
    "judge", [FrameAndIntentJudge(), _FallbackExactAndFrameJudge()]
)
def test_without_a_codec_the_rewrite_is_a_clobber(judge) -> None:
    verdict = judge.evaluate(BEFORE, AFTER, INTENT)
    assert not verdict.passed
    assert set(verdict.clobbered_fields) == {"email", "city"}


@pytest.mark.parametrize(
    "judge", [FrameAndIntentJudge(), _FallbackExactAndFrameJudge()]
)
def test_learned_codec_passes_the_rewrite_and_nothing_else(judge) -> None:
    codecs = learn_null_empty_codecs(BEFORE, AFTER, set(INTENT))
    assert set(codecs) == {"email", "city"}
    assert judge.evaluate(BEFORE, AFTER, INTENT, field_codecs=codecs).passed

    clobber = {**AFTER, "email": "someone@example.test", "note": ""}
    verdict = judge.evaluate(BEFORE, clobber, INTENT, field_codecs=codecs)
    assert not verdict.passed
    assert set(verdict.clobbered_fields) == {"email", "note"}


def test_learner_skips_written_fields_and_non_empty_changes() -> None:
    before = {"a": None, "b": "", "c": 0, "d": "x", "e": None}
    after = {"a": "", "b": None, "c": None, "d": "", "e": ""}
    assert set(learn_null_empty_codecs(before, after, {"e"})) == {"a", "b"}


def test_codec_on_an_unrelated_field_does_not_cover_others() -> None:
    codecs = {"email": NE}
    verdict = FrameAndIntentJudge().evaluate(BEFORE, AFTER, INTENT, field_codecs=codecs)
    assert verdict.clobbered_fields == ("city",)


def _entry(method: str, url: str, req: object, resp: object) -> dict:
    import json

    out: dict = {
        "request": {"method": method, "url": url, "headers": []},
        "response": {
            "status": 200,
            "headers": [],
            "content": {"mimeType": "application/json", "text": json.dumps(resp)},
        },
    }
    if req is not None:
        out["request"]["postData"] = {
            "mimeType": "application/json",
            "text": json.dumps(req),
        }
    return out


def test_har_induction_learns_the_codec_instead_of_replace_mode() -> None:
    base = "http://127.0.0.1:8000"
    r0 = {"id": 7, "phone": "1", "email": None, "city": None}
    r1 = {"id": 7, "phone": "2", "email": "", "city": ""}
    har = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry("GET", f"{base}/api/rec/7", None, r0),
                _entry("PUT", f"{base}/api/rec/7", {"phone": "2"}, r1),
                _entry("GET", f"{base}/api/rec/7", None, r1),
            ],
        }
    }
    lens = induce_lens_from_har(har)
    assert lens.field_codecs["email"].kind == "null_empty"
    assert lens.field_codecs["city"].kind == "null_empty"
    assert lens.write_mode == "patch"
