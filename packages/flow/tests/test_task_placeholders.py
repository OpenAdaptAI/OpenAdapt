"""The task's own values reach the model only as typed placeholders.

A scrubber hides names and record numbers, including the identity the task
names, so an agent that sees only redactions cannot tell the task's row from
any other and refuses at read-back. Values bound to the task (its declared
identity strings and its inputs) are sent as ``⟦task.NAME⟧`` instead: never
the value, never a generic label. Unbound personal data is still redacted,
and the listing's own ref tokens never reach the scrubber.
"""

from __future__ import annotations

import base64
import io
import json
import re
from types import SimpleNamespace
from typing import Any

import pytest

from openadapt_flow import privacy
from openadapt_flow.compile_agent.observe import scrub_lines

IDENTITY = ["Jane Sample", "MRN P1"]
NAME = privacy.placeholder("identity_1")
MRN = privacy.placeholder("identity_2")


class _Presidioish:
    """Redacts like the measured Presidio run: names, record numbers, and the
    false positive that broke structural listings (``e3``)."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        text = re.sub(r"\bMRN [A-Z]\d+\b", "<MRN>", text)
        text = re.sub(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", "<PERSON>", text)
        return re.sub(r"\be\d+\b", "<US_DRIVER_LICENSE>", text)

    def scrub_image(self, image, fill_color=None):
        # Detects nothing: a bound value must be covered by its label anyway.
        return image


@pytest.fixture(autouse=True)
def scrubber(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "on")
    monkeypatch.delenv(privacy.OUTBOUND_OPT_OUT_ENV, raising=False)
    fake = _Presidioish()
    privacy.set_text_scrubber(fake)
    privacy.set_image_scrubber(fake)
    yield fake
    privacy.reset_scrubbers()


LISTING = "\n".join(
    [
        "# Referral Tasks",
        "row: Patient | Reason | Priority | Action",
        'row: Jane Sample | Knee pain referral | High | [e3] button "Open"',
        'row: Alex Testcase | Cardiology follow-up | Medium | [e4] button "Open"',
        "text: MRN P1",
    ]
)


def test_bound_identity_is_sent_as_its_placeholder() -> None:
    bindings = privacy.task_bindings(identity=IDENTITY)
    assert bindings == {NAME: "Jane Sample", MRN: "MRN P1"}
    out = scrub_lines(LISTING, bindings=bindings)
    assert f'row: {NAME} | Knee pain referral | High | [e3] button "Open"' in out
    assert f"text: {MRN}" in out
    assert "Jane Sample" not in out and "MRN P1" not in out


def test_unbound_names_stay_generically_redacted() -> None:
    out = scrub_lines(LISTING, bindings=privacy.task_bindings(identity=IDENTITY))
    assert 'row: <PERSON> | Cardiology follow-up | Medium | [e4] button "Open"' in out
    assert "Alex Testcase" not in out


def test_ref_tokens_are_never_scrubbed() -> None:
    # Before: [e3] became [<US_DRIVER_LICENSE>], so no agent could act on it.
    out = scrub_lines(LISTING)
    assert '[e3] button "Open"' in out and '[e4] button "Open"' in out
    assert "<US_DRIVER_LICENSE>" not in out
    # The code-action listing's refs are structure too.
    assert scrub_lines('@12 button "Open"') == '@12 button "Open"'


def test_short_values_bind_in_task_text_but_not_on_page_text() -> None:
    bindings = privacy.task_bindings(identity=["MRN P1", "P1"], params={"note": "ok"})
    note = privacy.placeholder("note")
    # Every value is bound, so none is left raw in the task text...
    assert bindings == {NAME: "MRN P1", MRN: "P1", note: "ok"}
    assert privacy.bind_text("Find P1, say ok", bindings) == f"Find {MRN}, say {note}"
    # ...but page text replaces only values long enough not to hit ordinary
    # words; shorter ones are scrubbed like any other text.
    assert privacy.listing_bindings(bindings) == {NAME: "MRN P1"}
    assert scrub_lines("row: P1 | ok | MRN P1", bindings=bindings) == (
        f"row: P1 | ok | {NAME}"
    )
    assert privacy.bind_text("MRN P12 and MRN  P1", {NAME: "MRN P1"}) == (
        f"MRN P12 and {NAME}"
    )


def test_task_value_cannot_rewrite_another_placeholder() -> None:
    # Issue #2130: a short value that is also a word inside a placeholder name
    # must not match inside placeholders inserted in the same or earlier pass.
    bindings = privacy.task_bindings(identity=["Jane Sample"], params={"kind": "task"})
    kind = privacy.placeholder("kind")
    out = privacy.bind_text("Do the task for Jane Sample", bindings)
    assert out == f"Do the {kind} for {NAME}"
    assert f"\u27e6{kind}." not in (out or "")


def test_bind_text_preserves_preexisting_placeholders() -> None:
    bindings = privacy.task_bindings(identity=["Jane Sample"], params={"kind": "task"})
    kind = privacy.placeholder("kind")
    text = f"Do the task for {NAME}"
    assert privacy.bind_text(text, bindings) == f"Do the {kind} for {NAME}"


def test_raw_value_inside_brackets_is_bound() -> None:
    # A bracketed span containing a raw value (e.g. ⟦Jane Sample⟧) must not
    # be left unmasked; it must be replaced by its bound placeholder.
    bindings = privacy.task_bindings(identity=["Jane Sample"])
    raw_bracketed = "\u27e6Jane Sample\u27e7"
    out = privacy.bind_text(raw_bracketed, bindings)
    assert out == f"\u27e6{NAME}\u27e7"
    assert "Jane Sample" not in (out or "")


def test_short_value_overbinding_in_task_text_is_pinned() -> None:
    # Issue #2130: task_text binds every non-empty value, so a value under 3
    # characters (like "M") replaces whole-word occurrences in task text.
    bindings = privacy.task_bindings(params={"sex": "M"})
    sex = privacy.placeholder("sex")
    assert privacy.bind_text("sex M ... Mark M done", bindings) == (
        f"sex {sex} ... Mark {sex} done"
    )


def test_inputs_are_bound_under_their_own_names() -> None:
    bindings = privacy.task_bindings(
        identity=IDENTITY, params={"note": "Follow up in two weeks"}
    )
    note = privacy.placeholder("note")
    assert bindings[note] == "Follow up in two weeks"
    out = scrub_lines(
        '[e7] textarea "Note" value="Follow up in two weeks"', bindings=bindings
    )
    assert out == f'[e7] textarea "Note" value="{note}"'


def test_label_regions_cover_the_value_and_name_it() -> None:
    from PIL import Image

    raw = io.BytesIO()
    Image.new("RGB", (200, 60), (255, 255, 255)).save(raw, format="PNG")
    labelled = privacy.prepare_outbound_image(
        raw.getvalue(), "a test screenshot", [((10, 10, 80, 20), NAME)]
    )
    image = Image.open(io.BytesIO(labelled)).convert("RGB")
    assert image.getpixel((11, 11)) == (32, 32, 48)
    assert image.getpixel((150, 50)) == (255, 255, 255)


# -- a real page, a real harness, a fake model --------------------------------

PAGE = """<!doctype html><html><head><title>Referral Tasks</title></head>
<body style="font: 16px sans-serif; margin: 20px">
<h1>Referral Tasks</h1>
<table>
<tr><th>Patient</th><th>Reason</th><th>Action</th></tr>
<tr><td id="jane">Jane Sample</td><td>Knee pain referral</td>
    <td><button>Open</button></td></tr>
<tr><td>Alex Testcase</td><td>Cardiology follow-up</td>
    <td><button>Open</button></td></tr>
</table>
<p>Selected: <b>MRN</b> P1</p>
<label>Search <input id="search" aria-label="Search"></label>
</body></html>"""


@pytest.fixture(scope="module")
def page():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - no browser installed
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 640, "height": 400})
            yield page
        finally:
            browser.close()


class _Computer:
    """The capture computer's surface: a page, its frames, generic actions."""

    dialect = "generic"

    def __init__(self, page: Any) -> None:
        self.page = page

    def screenshot(self) -> bytes:
        return self.page.screenshot()

    def execute(self, native: dict[str, Any]) -> Any:
        if native["kind"] == "click":
            self.page.mouse.click(native["x"], native["y"])
        elif native["kind"] == "type":
            self.page.keyboard.type(native["text"])
        return SimpleNamespace(png=self.page.screenshot(), notes=[])


class _Client:
    def __init__(self, script: list[tuple[str, dict[str, Any]]]) -> None:
        self.script = list(script)
        self.requests: list[dict[str, Any]] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **kw: Any) -> Any:
        # The loop appends to one list: copy what this request carried.
        self.requests.append(json.loads(json.dumps(kw, default=str)))
        name, args = self.script.pop(0)
        if name == "type_text" and args.get("ref") == "search":
            refs = re.findall(r"\[(e\d+)\] input", json.dumps(kw["messages"]))
            args = {"text": args["text"], "ref": refs[-1]}
        return SimpleNamespace(
            content=[
                {
                    "type": "tool_use",
                    "id": f"t{len(self.requests)}",
                    "name": name,
                    "input": args,
                }
            ],
            stop_reason="tool_use",
            usage=SimpleNamespace(
                input_tokens=10,
                output_tokens=5,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=0,
            ),
        )


def test_the_raw_identity_never_leaves_in_the_model_payload(page) -> None:
    from PIL import Image

    from openadapt_flow.compile_agent import ClaudeAgent
    from openadapt_flow.compile_agent.harness import Harness, SaveGate
    from openadapt_flow.jit.compile_agent import TaskRequest

    page.set_content(PAGE)
    bindings = privacy.task_bindings(
        identity=IDENTITY, params={"note": "Bring the form"}
    )
    harness = Harness(_Computer(page), SaveGate(IDENTITY), keep=[], bindings=bindings)
    request = TaskRequest(
        task_id="t",
        instruction="Open the referral for Jane Sample (MRN P1) and note: Bring the form",
        inputs={"note": "Bring the form"},
    )
    client = _Client(
        [
            ("screenshot", {}),
            ("type_text", {"text": MRN, "ref": "search"}),
            ("finish", {"summary": "found"}),
        ]
    )
    ClaudeAgent(vision="selective", client=client).drive(request, harness)  # type: ignore[arg-type]

    payload = json.dumps(client.requests, ensure_ascii=False)
    assert "Jane Sample" not in payload and "MRN P1" not in payload
    assert NAME in payload and MRN in payload
    # The task's input is a placeholder in the task text too: the agent types
    # the placeholder and the harness types the value.
    assert "Bring the form" not in payload
    task_text = client.requests[0]["messages"][0]["content"][0]["text"]
    assert privacy.placeholder("note") in task_text
    # Unbound personal data is still redacted generically.
    assert "Alex Testcase" not in payload and "<PERSON>" in payload
    # The listing's refs survived the scrubber.
    assert re.search(r'\[e\d+\] button \\"Open\\"', payload)
    # The placeholder the agent typed was typed locally as the real value.
    assert page.eval_on_selector("#search", "el => el.value") == "MRN P1"

    # The screenshot covers the name with its labelled box although the
    # scrubber detected nothing.
    blocks = client.requests[1]["messages"][-1]["content"][0]["content"]
    image = next(b for b in blocks if b["type"] == "image")
    png = base64.b64decode(image["source"]["data"])
    shot = Image.open(io.BytesIO(png)).convert("RGB")
    box = page.eval_on_selector(
        "#jane",
        "el => { const r = document.createRange(); r.selectNodeContents(el);"
        " const b = r.getBoundingClientRect();"
        " return [b.left, b.top, b.right, b.bottom]; }",
    )
    region = shot.crop(tuple(int(v) for v in box))
    pixels = list(region.getdata())
    assert pixels.count((32, 32, 48)) > len(pixels) // 2


# -- every adapter: no declared task value leaves raw -------------------------

VALUES = {
    "note": "Bring the form",
    "dob": "1970-01-02",
    "code": "Z9",  # short: bound in the task text only
}
TASK = (
    "Open the referral for Jane Sample (MRN P1), check DOB 1970-01-02, "
    "use code Z9 and note: Bring the form"
)


def _harness(page: Any) -> Any:
    from openadapt_flow.compile_agent.harness import Harness, SaveGate

    page.set_content(PAGE.replace("</body>", "<p>DOB 1970-01-02 code Z9</p></body>"))
    bindings = privacy.task_bindings(identity=IDENTITY, params=VALUES)
    return Harness(_Computer(page), SaveGate(IDENTITY), keep=[], bindings=bindings)


def _assert_no_raw_value(payload: str) -> None:
    for value in [*IDENTITY, "Bring the form", "1970-01-02"]:
        assert value not in payload, value
    task_text = re.search(r"Task: [^\n\"]*", payload)
    assert task_text is not None and "Z9" not in task_text.group(0)
    for name in ("identity_1", "identity_2", "note", "dob", "code"):
        assert privacy.placeholder(name) in payload, name


def _request() -> Any:
    from openadapt_flow.jit.compile_agent import TaskRequest

    return TaskRequest(task_id="t", instruction=TASK, inputs=dict(VALUES))


def test_claude_payload_has_no_raw_task_value(page) -> None:
    from openadapt_flow.compile_agent import ClaudeAgent

    client = _Client([("screenshot", {}), ("finish", {"summary": "done"})])
    agent = ClaudeAgent(vision="selective", client=client)
    agent.drive(_request(), _harness(page))  # type: ignore[arg-type]
    _assert_no_raw_value(json.dumps(client.requests, ensure_ascii=False))


def test_framework_agent_payload_has_no_raw_task_value(page) -> None:
    from openadapt_flow.integrations.agents.skills import FrameworkAgent

    sent: list[str] = []

    def run(task: str, tools: Any) -> Any:
        sent.append(task)
        sent.append(tools.observe())
        tools.finish("done")
        return {"calls": []}

    FrameworkAgent(run).drive(_request(), _harness(page))
    _assert_no_raw_value("\n".join(sent))


def test_openai_payload_has_no_raw_task_value(page, monkeypatch) -> None:
    import sys
    import types

    from openadapt_flow.integrations.agents.providers import OpenAIBrowserAgent

    sent: list[str] = []

    class FakeAgent:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class FakeRunner:
        @staticmethod
        async def run(agent: Any, task: str, **_kwargs: Any) -> Any:
            sent.extend([agent.instructions, task])
            observe = next(t for t in agent.tools if t.__name__ == "observe")
            sent.append(observe())
            return SimpleNamespace(final_output="done")

    class FakeClient:
        async def close(self) -> None:
            return None

    fake_agents = types.ModuleType("agents")
    fake_agents.__dict__.update(
        Agent=FakeAgent,
        ModelSettings=lambda **kw: SimpleNamespace(**kw),
        OpenAIResponsesModel=lambda **_kw: object(),
        RunConfig=lambda **kw: SimpleNamespace(**kw),
        RunHooks=type("RunHooks", (), {}),
        Runner=FakeRunner,
        function_tool=lambda function: function,
    )
    fake_openai = types.ModuleType("openai")
    fake_openai.__dict__.update(AsyncOpenAI=lambda **_kw: FakeClient())
    monkeypatch.setitem(sys.modules, "agents", fake_agents)
    monkeypatch.setitem(sys.modules, "openai", fake_openai)
    agent = OpenAIBrowserAgent(
        model="scripted-test-model",
        max_turns=1,
        max_cost_usd=1,
        prices={"input": 1, "output": 1, "cache_write": 1, "cache_read": 1},
        price_model="scripted-test-model",
        pricing_source_url="https://developers.openai.com/api/docs/pricing",
        pricing_verified_at="2026-11-07",
    )
    import dataclasses

    request = dataclasses.replace(
        _request(),
        model_key=SimpleNamespace(provider="openai", secret=lambda: "opaque"),
    )
    agent.drive(request, _harness(page))
    _assert_no_raw_value("\n".join(sent))
