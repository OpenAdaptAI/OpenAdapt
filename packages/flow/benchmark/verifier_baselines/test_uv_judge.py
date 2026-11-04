"""Offline tests for the UV judge port (no network, fake client)."""

from __future__ import annotations

import asyncio
import io
import json
import re
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))

import uv_judge  # noqa: E402
import uv_prompts as P  # noqa: E402

PLACEHOLDER = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*")

RUBRIC = {
    "items": [
        {
            "criterion": "Open the patient form",
            "description": "d0",
            "max_points": 2,
            "justification": "",
            "earned_points": "",
        },
        {
            "criterion": "Save the patient record",
            "description": "d1",
            "max_points": 3,
            "justification": "",
            "earned_points": "",
        },
    ]
}


def _png(color=(255, 0, 0)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (16, 16), color + (255,)).save(buf, format="PNG")
    return buf.getvalue()


_TEMPLATES = {
    "0a": P.RUBRIC_GENERATION_PROMPT_TEMPLATE,
    "0b": P.RUBRIC_DEPENDENCY_CHECKING_PROMPT,
    "0c": P.ACTION_ONLY_RUBRIC_SCORER_PROMPT,
    "4": P.MM_SCREENSHOT_BATCHED_EVIDENCE_ANALYSIS_PROMPT,
    "4single": P.MM_SCREENSHOT_EVIDENCE_ANALYSIS_PROMPT,
    "6": P.MM_RUBRIC_RESCORING_PROMPT,
    "7": P.PENALIZE_UNSOLICITED_SIDE_EFFECTS_PROMPT,
    "8": P.OUTCOME_VERIFICATION_PROMPT,
}


def _unique_line(key: str) -> str:
    """Longest placeholder-free line found only in this template."""
    others = [t for k, t in _TEMPLATES.items() if k != key]
    lines = [ln.strip() for ln in _TEMPLATES[key].splitlines() if "$" not in ln]
    return max((ln for ln in lines if not any(ln in o for o in others)), key=len)


STEP_HEADS = {k: _unique_line(k) for k in _TEMPLATES}


class FakeClient:
    """Returns canned JSON chosen by which UV prompt it is given."""

    model = "fake"

    def __init__(self, output_success=True, penalty=False):
        self.log = []  # (step, messages)
        self.output_success = output_success
        self.penalty = penalty

    @staticmethod
    def _text(messages):
        first = next(m for m in messages if m["role"] == "user")["content"]
        if isinstance(first, list):
            return "".join(p.get("text", "") for p in first if p.get("type") == "text")
        return first

    async def create(self, messages, json_output=False):
        assert json_output is True
        text = self._text(messages)
        step = next(k for k, h in STEP_HEADS.items() if h in text)
        self.log.append((step, messages))
        if step == "0a":
            out = RUBRIC
        elif step == "0b":
            out = {
                "reasoning": "ok",
                "needs_reformulation": False,
                "reformulated_rubric": {},
            }
        elif step == "0c":
            out = {
                "items": [
                    {
                        "criterion": "Open the patient form",
                        "justification": "j",
                        "earned_points": 2,
                    },
                    {
                        "criterion": "Save the patient record",
                        "justification": "j",
                        "earned_points": 1,
                    },
                ]
            }
        elif step == "4":
            out = {
                "analyses": [
                    {
                        "criterion_idx": i,
                        "screenshot_evidence": "e",
                        "criterion_analysis": "a",
                        "discrepancies": "none",
                        "environment_issues_confirmed": False,
                    }
                    for i in range(2)
                ]
            }
        elif step == "6":
            out = {
                "items": [
                    {
                        "criterion_idx": 0,
                        "applicable_evidence": "x",
                        "post_image_justification": "y",
                        "post_image_earned_points": 2,
                    },
                    {
                        "criterion_idx": 1,
                        "applicable_evidence": "x",
                        "post_image_justification": "y",
                        "post_image_earned_points": 3,
                    },
                ]
            }
        elif step == "7":
            pcs = (
                [
                    {
                        "criterion": "Extra write",
                        "description": "d",
                        "max_points": 5,
                        "post_image_justification": "p",
                        "post_image_earned_points": 0,
                    }
                ]
                if self.penalty
                else []
            )
            out = {
                "reasoning": "r",
                "requires_penalty": self.penalty,
                "penalty_criteria": pcs,
            }
        else:
            out = {
                "primary_intent": "create patient",
                "reasoning": "because",
                "output_success": self.output_success,
            }
        return type(
            "R",
            (),
            {
                "content": json.dumps(out),
                "usage": {"tokens_in": 10, "tokens_out": 1, "usd": 0.001},
            },
        )()


STEPS = [
    {
        "url": "http://localhost:9801/Patient?x=1",
        "state_description": "list",
        "action_name": "left_click",
        "action_args": {"coordinate": [1, 2]},
    },
    {
        "url": "http://localhost:9801/Patient/new",
        "state_description": "form",
        "action_name": "type",
        "action_args": {"text": "Feld"},
        "error": "boom",
    },
]


def _run(client, **kw):
    return asyncio.run(
        uv_judge.judge(
            "Create patient Noah Feld and save it.",
            "http://localhost:9801/",
            STEPS,
            "Done.",
            [_png(), _png((0, 255, 0))],
            client,
            **kw,
        )
    )


def test_pipeline_order_and_output_from_outcome():
    c = FakeClient(output_success=False)
    r = _run(c)
    assert [s for s, _ in c.log] == ["0a", "0b", "0c", "4", "4", "6", "7", "8"]
    # rescoring gave 5/5, so rubric says success but outcome says failure
    assert r["rubric_score"] == 1.0 and r["rubric_is_success"] is True
    assert r["output_success"] is False
    assert r["primary_intent"] == "create patient" and r["reasoning"] == "because"
    assert r["calls"] and len(r["calls"]) == 8 and r["tokens_in"] == 80
    assert abs(r["usd"] - 0.008) < 1e-9


def test_no_leftover_placeholders_and_message_shape():
    c = FakeClient()
    _run(c)
    for step, messages in c.log:
        text = FakeClient._text(messages)
        assert not PLACEHOLDER.search(text), (step, PLACEHOLDER.findall(text)[:5])
        if step in ("0a", "0b", "0c"):
            assert len(messages) == 1 and messages[0]["role"] == "user"
        else:
            assert messages[0] == P.DEFAULT_SYSTEM_MESSAGES[0]
        if step == "4":
            img = messages[1]["content"][0]
            assert img["image_url"]["detail"] == "high"
            assert img["image_url"]["url"].startswith("data:image/jpeg;base64,")
    outcome_text = FakeClient._text(dict(c.log)["8"])
    assert P._CP_RULES_PERMISSION_GRANTED in outcome_text
    assert P.USER_SIMULATOR_DISABLED_OUTCOME_BLOCK in outcome_text
    assert "PERMISSION_GRANTED_INFO_PROVIDED_TASK_SPECIFIED" in outcome_text
    gen_text = FakeClient._text(dict(c.log)["0a"])
    assert P.USER_SIMULATOR_DISABLED_RUBRIC_BLOCK in gen_text


def test_penalty_enlarges_denominator():
    r = _run(FakeClient(penalty=True))
    assert r["penalty_applied"] is True
    assert r["rubric_score"] == 5 / 10
    assert r["rubric_is_success"] is False


def test_precomputed_rubric_skips_generation():
    c = FakeClient()
    _run(c, rubric=RUBRIC)
    assert [s for s, _ in c.log][:2] == ["0c", "4"]


def test_format_action_history_matches_uv():
    h = uv_judge.format_action_history(STEPS)
    assert h.startswith(
        "State 1: http://localhost:9801/Patient, state_description: list\nAction 1: left_click({"
    )
    assert "Error! The above Action 2 encountered an Error: boom" in h


def test_retry_nudge_on_bad_json():
    class Flaky(FakeClient):
        n = 0

        async def create(self, messages, json_output=False):
            text = self._text(messages)
            if P.OUTCOME_VERIFICATION_PROMPT[:200] in text and self.n == 0:
                self.n += 1
                return type(
                    "R", (), {"content": '{"primary_intent": "x"}', "usage": {}}
                )()
            return await super().create(messages, json_output)

    c = Flaky()
    r = _run(c)
    assert r["output_success"] is True
    last_outcome_msgs = [m for s, m in c.log if s == "8"][-1]
    assert last_outcome_msgs[-1]["content"].startswith(
        "Error: Missing required field: reasoning"
    )
