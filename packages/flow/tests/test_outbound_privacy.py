"""Anything that leaves the machine is scrubbed, refused, or a recorded opt-out.

One test group per outbound path that can carry patient data:

* compile annotation request (Anthropic)
* LLM qualification proposer request (Anthropic)
* model grounding (Anthropic and OpenAI-compatible endpoints)
* intake agent request
* authoring mailbox callbacks

Each group checks the scrubbed default, the missing-extra refusal, and (where an
opt-out exists) that ``OPENADAPT_FLOW_SEND_UNSCRUBBED`` is written to the local
ledger. Cloud push and report-break/report-run are covered in test_hosted.py and
test_sanitized_artifact.py.
"""

from __future__ import annotations

import base64
import io
import json
from types import SimpleNamespace

import pytest
from PIL import Image

from openadapt_flow import privacy
from openadapt_flow.ir import ActionKind, Anchor, Step, Workflow

PHI = "Jane Doe"
MRN = "MRN-99321"


class FakeScrubber:
    """Replaces two known identifiers and blacks out images."""

    def scrub_text(self, text, is_separated=False):
        return text.replace(PHI, "<PERSON>").replace(MRN, "<ID>")

    def scrub_image(self, image, fill_color=None):
        return Image.new("RGB", image.size, (0, 0, 0))


class BrokenScrubber(FakeScrubber):
    """An installed provider whose model is missing."""

    def scrub_text(self, text, is_separated=False):
        raise RuntimeError("spaCy model not installed")


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENADAPT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(privacy.OUTBOUND_OPT_OUT_ENV, raising=False)
    # OPENADAPT_FLOW_SCRUB relaxes local writes only; outbound ignores it.
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "off")
    yield
    privacy.reset_scrubbers()


@pytest.fixture
def scrubber():
    privacy.set_text_scrubber(FakeScrubber())


@pytest.fixture
def no_extra():
    privacy.set_text_scrubber(None)


@pytest.fixture
def opted_out(monkeypatch, no_extra):
    monkeypatch.setenv(privacy.OUTBOUND_OPT_OUT_ENV, "synthetic test data")


def _ledger() -> list[dict]:
    path = privacy.opt_out_ledger_path()
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (16, 16), (255, 255, 255)).save(out, format="PNG")
    return out.getvalue()


# -- compile annotation ------------------------------------------------------


class _RecordingClient:
    def __init__(self, reply: str = '{"steps": []}') -> None:
        self.requests: list[dict] = []

        def create(**kwargs):
            self.requests.append(kwargs)
            return SimpleNamespace(content=[SimpleNamespace(type="text", text=reply)])

        self.messages = SimpleNamespace(create=create)


def _phi_workflow() -> Workflow:
    return Workflow(
        name=f"intake for {PHI}",
        steps=[
            Step(
                id="step_000",
                intent=f"type {PHI}",
                action=ActionKind.TYPE,
                text=f"{PHI} {MRN}",
                anchor=Anchor(
                    template="templates/step_000.png",
                    region=(0, 0, 10, 10),
                    click_point=(5, 5),
                    ocr_text=f"Patient: {PHI}",
                ),
            )
        ],
    )


def _annotate(client):
    from openadapt_flow.compiler.annotate import AnthropicStepAnnotator

    return AnthropicStepAnnotator(model="stub", client=client).annotate(
        _phi_workflow(), captured_state={"title": f"Chart {MRN}"}
    )


def test_annotation_request_is_scrubbed(scrubber):
    client = _RecordingClient()
    _annotate(client)
    sent = json.dumps(client.requests)
    assert PHI not in sent and MRN not in sent
    assert "<PERSON>" in sent


def test_annotation_refuses_without_the_extra(no_extra):
    client = _RecordingClient()
    with pytest.raises(privacy.PrivacyNotAvailable):
        _annotate(client)
    assert client.requests == []


def test_annotation_opt_out_is_recorded(opted_out):
    client = _RecordingClient()
    _annotate(client)
    assert PHI in json.dumps(client.requests)
    (entry,) = _ledger()
    assert entry["purpose"] == "compile annotation request"
    assert entry["reason"] == "synthetic test data"


# -- qualification proposer --------------------------------------------------


def _propose(client):
    from openadapt_flow.compiler.qualification_proposer import (
        LazyLLMQualificationProposer,
        proposer_context,
    )

    context = proposer_context(_phi_workflow(), {"task_description": PHI})
    return LazyLLMQualificationProposer(client=client, model="stub").propose(
        "identity_field", "identity_field", context
    )


def test_proposer_request_is_scrubbed(scrubber):
    client = _RecordingClient("mrn")
    _propose(client)
    sent = json.dumps(client.requests)
    assert PHI not in sent


def test_proposer_refuses_without_the_extra(no_extra):
    client = _RecordingClient("mrn")
    with pytest.raises(privacy.PrivacyNotAvailable):
        _propose(client)
    assert client.requests == []


# -- model grounding ---------------------------------------------------------


class _HttpClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        body = {"choices": [{"message": {"content": '{"x": 1, "y": 2}'}}]}
        return SimpleNamespace(status_code=200, json=lambda: body)


def _sent_image(call: dict) -> Image.Image:
    part = call["json"]["messages"][0]["content"][0]["image_url"]["url"]
    data = base64.b64decode(part.split(",", 1)[1])
    return Image.open(io.BytesIO(data)).convert("RGB")


def test_remote_grounding_redacts_the_screenshot(scrubber):
    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder

    client = _HttpClient()
    grounder = OpenAICompatibleGrounder(
        base_url="https://models.example.test/v1", model="m", client=client
    )
    assert grounder.locate(_png(), f"open chart for {PHI}", MRN) is not None
    (call,) = client.calls
    assert _sent_image(call).getpixel((0, 0)) == (0, 0, 0)
    sent = json.dumps(call["json"])
    assert PHI not in sent and MRN not in sent


def test_remote_grounding_refuses_without_the_extra(no_extra):
    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder

    with pytest.raises(privacy.PrivacyNotAvailable):
        OpenAICompatibleGrounder(
            base_url="https://models.example.test/v1", model="m", client=_HttpClient()
        )


def test_anthropic_grounding_refuses_without_the_extra(no_extra):
    pytest.importorskip("anthropic")
    from openadapt_flow.runtime.grounder import AnthropicGrounder

    with pytest.raises(privacy.PrivacyNotAvailable):
        AnthropicGrounder(client=_RecordingClient())


def test_loopback_grounding_needs_no_scrubbing(no_extra):
    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder

    client = _HttpClient()
    grounder = OpenAICompatibleGrounder(
        base_url="http://127.0.0.1:8000/v1", model="m", client=client
    )
    grounder.locate(_png(), f"open chart for {PHI}", None)
    assert PHI in json.dumps(client.calls[0]["json"])
    assert _ledger() == []


def test_grounding_opt_out_is_recorded(opted_out):
    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder

    client = _HttpClient()
    grounder = OpenAICompatibleGrounder(
        base_url="https://models.example.test/v1", model="m", client=client
    )
    grounder.locate(_png(), f"open chart for {PHI}", None)
    assert PHI in json.dumps(client.calls[0]["json"])
    (entry,) = _ledger()
    assert entry["purpose"] == "screenshots for model grounding"


def test_a_failed_image_scrub_sends_nothing(scrubber):
    from openadapt_flow.runtime.grounder import OpenAICompatibleGrounder

    client = _HttpClient()
    grounder = OpenAICompatibleGrounder(
        base_url="https://models.example.test/v1", model="m", client=client
    )
    assert grounder.locate(b"not a png", "click", None) is None
    assert client.calls == []


# -- intake agent request ----------------------------------------------------


def _intake_request():
    from openadapt_flow.intake.document import IntakeDocument
    from openadapt_flow.intake.extract import build_agent_request

    doc = IntakeDocument(
        source="fax.txt", kind="text", lines=[f"Patient: {PHI}", f"Record: {MRN}"]
    )
    return build_agent_request(doc, {})


def test_intake_request_never_falls_back_to_plaintext(monkeypatch):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    privacy.set_text_scrubber(BrokenScrubber())
    with pytest.raises(RuntimeError, match="spaCy model"):
        _intake_request()


def test_intake_request_refuses_without_the_extra(monkeypatch, no_extra):
    monkeypatch.setenv("OPENADAPT_FLOW_SCRUB", "auto")
    with pytest.raises(RuntimeError, match="openadapt-privacy"):
        _intake_request()


# -- authoring mailbox -------------------------------------------------------


def _mailbox_callback() -> dict:
    from openadapt_flow.agent.mailbox import MailboxTransport

    posted: list[dict] = []

    def post(path, body, headers):
        posted.append(body)
        return 202, {"cache-control": "no-store"}, {}

    transport = MailboxTransport(post=post)
    payload = {
        "command_id": "c1",
        "result": {
            "tree": [
                {"node_id": "n1", "role": "button", "name": "Save"},
                {"node_id": "n2", "role": "text", "name": PHI},
            ]
        },
    }
    transport.callback("p" * 8, "s" * 43, payload)
    return posted[0]


@pytest.fixture
def _lenient_mailbox(monkeypatch):
    from openadapt_flow.agent import mailbox

    monkeypatch.setattr(mailbox, "valid_lease_secret", lambda _s: True)
    monkeypatch.setattr(mailbox, "valid_pack_id", lambda _p: True)


def test_mailbox_drops_labels_the_scrubber_changes(_lenient_mailbox, scrubber):
    sent = _mailbox_callback()
    names = [node.get("name") for node in sent["result"]["tree"]]
    assert names == ["Save", None]


def test_mailbox_withholds_labels_without_the_extra(_lenient_mailbox, no_extra):
    sent = _mailbox_callback()
    assert all("name" not in node for node in sent["result"]["tree"])
    assert [node["node_id"] for node in sent["result"]["tree"]] == ["n1", "n2"]


def test_mailbox_opt_out_is_recorded(_lenient_mailbox, opted_out):
    sent = _mailbox_callback()
    assert PHI in json.dumps(sent)
    (entry,) = _ledger()
    assert entry["purpose"] == "authoring mailbox labels"
