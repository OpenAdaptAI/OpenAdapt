"""The executor: an agent that drives the app, shaped by an editable harness.

The harness is the text a Harness² proposer edits: an always-on
``AGENTS.md`` plus optional ``skills/<name>/SKILL.md`` files. The agent reads
all of it as its standing instructions. The proposer never touches the agent's
code, only those instructions, which is the c1..c8 contract Harness² assumes.

The agent sees the page as text: the visible text plus a numbered list of the
controls it can use. It answers with one action at a time and ends with a
report, ``saved`` or ``stopped``. That report becomes the runtime signal the
reward worker receives. The worker decides the outcome from the store; the
report only picks which no-effect outcome applies when nothing landed.

Two agents ship:

* :class:`ScriptedGuiAgent`, deterministic and free, for tests. Its care
  depends on rules it finds in the harness text, so a harness edit changes
  what it does in a predictable way.
* :class:`ModelGuiAgent`, a model behind :class:`~.llm.ChatModel`, for
  measured runs.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional, Protocol, runtime_checkable

from openadapt_flow.integrations.harness2.llm import ChatModel

#: The final report statuses and the runtime signal each maps to.
STATUS_SIGNAL = {"saved": "completed", "stopped": "halted_before_effect"}

_OBSERVE_JS = r"""
() => {
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none';
  };
  document.querySelectorAll('[data-oa-n]').forEach(el => el.removeAttribute('data-oa-n'));
  const els = [];
  const all = document.querySelectorAll('button, input, textarea, select, a[href]');
  for (const el of all) {
    if (!visible(el)) continue;
    const n = els.length + 1;
    el.setAttribute('data-oa-n', String(n));
    let label = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
    if (!label && el.id) {
      const l = document.querySelector('label[for="' + el.id + '"]');
      if (l) label = l.innerText.trim();
    }
    const row = el.closest('tr');
    let context = '';
    if (row) context = row.innerText.replace(label, '').replace(/\s+/g, ' ').trim();
    els.push({n, tag: el.tagName.toLowerCase(), id: el.id || '', label,
              context, selected: el.classList.contains('selected'),
              value: (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') ? el.value : ''});
  }
  const text = (document.body.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
  return {text: text.slice(0, 4000), elements: els, hash: location.hash};
}
"""


@dataclass
class Element:
    n: int
    tag: str
    id: str
    label: str
    context: str = ""
    selected: bool = False
    value: str = ""

    def line(self) -> str:
        parts = [f"[{self.n}] {self.tag}"]
        if self.label:
            parts.append(repr(self.label))
        if self.context:
            parts.append(f"(row: {self.context})")
        if self.selected:
            parts.append("(selected)")
        if self.value:
            parts.append(f"value={self.value[:80]!r}")
        return " ".join(parts)


@dataclass
class Observation:
    text: str
    elements: list[Element]
    where: str = ""

    def render(self) -> str:
        controls = "\n".join(e.line() for e in self.elements) or "(no controls)"
        return f"SCREEN ({self.where or 'page'}):\n{self.text}\n\nCONTROLS:\n{controls}"

    def find(self, pattern: str) -> list[Element]:
        rx = re.compile(pattern, re.I)
        return [e for e in self.elements if rx.search(e.label)]


@dataclass
class Step:
    """One observe-act cycle, the unit of the trajectory."""

    index: int
    observation: str
    action: dict[str, Any]
    error: str = ""
    thought: str = ""


@dataclass
class AgentResult:
    status: str  # saved | stopped | unfinished
    report: str
    steps: list[Step] = field(default_factory=list)
    cost_usd: float = 0.0
    model_calls: int = 0

    @property
    def finished(self) -> bool:
        return self.status in STATUS_SIGNAL

    @property
    def runtime_signal(self) -> str:
        # An agent that never reported is treated as halted: the read decides
        # whether anything landed anyway (then a person reconciles it).
        return STATUS_SIGNAL.get(self.status, "halted_before_effect")

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["runtime_signal"] = self.runtime_signal
        return d


@runtime_checkable
class Driver(Protocol):
    """What an agent drives: a page's DOM, or a window's pixels."""

    def observe(self) -> "Observation": ...

    def act(self, action: dict[str, Any]) -> None: ...


class PageDriver:
    """The agent's hands and eyes on one Playwright page, through its DOM."""

    surface = "dom"
    #: ``type`` replaces the field's contents (``page.fill``).
    type_replaces = True

    def __init__(self, page: Any, *, settle_ms: int = 3000) -> None:
        self.page = page
        self.settle_ms = settle_ms
        self._inflight = 0

        def started(_request: Any) -> None:
            self._inflight += 1

        def ended(_request: Any) -> None:
            self._inflight = max(0, self._inflight - 1)

        page.on("request", started)
        page.on("requestfinished", ended)
        page.on("requestfailed", ended)

    def observe(self) -> Observation:
        raw = self.page.evaluate(_OBSERVE_JS)
        return Observation(
            text=str(raw["text"]),
            elements=[Element(**e) for e in raw["elements"]],
            where=str(raw.get("hash") or ""),
        )

    def act(self, action: dict[str, Any]) -> None:
        kind = action.get("action")
        n = action.get("element")
        sel = f'[data-oa-n="{int(n)}"]' if n is not None else ""
        if kind == "click":
            self.page.click(sel, timeout=5000)
        elif kind == "type":
            self.page.fill(sel, str(action.get("text", "")), timeout=5000)
        elif kind == "wait":
            self.page.wait_for_timeout(1500)
        else:
            raise ValueError(f"unknown action {kind!r}")
        self._settle()

    def _settle(self) -> None:
        """Wait until no request is in flight (or ``settle_ms`` passes).

        ``wait_for_load_state("networkidle")`` returns at once on a page that
        already loaded, so a save's fetch would still be pending when the
        agent looks. Counting requests sees it.
        """

        waited = 0
        quiet = 0
        while waited < self.settle_ms and quiet < 300:
            self.page.wait_for_timeout(100)
            waited += 100
            quiet = quiet + 100 if self._inflight == 0 else 0


@runtime_checkable
class GuiAgent(Protocol):
    """Drives one task to a report."""

    name: str

    def run(self, driver: Driver, task_prompt: str, harness: str) -> AgentResult: ...


# -- scripted agent ------------------------------------------------------------

_TASK_RX = re.compile(
    r"for (?P<name>.+?) \(date of birth (?P<dob>[0-9-]+)\) with this note: (?P<note>.+)$",
    re.S,
)

_TYPE_LABEL = re.compile(r"(triage|consult|triage\s*assessment)", re.I)


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


#: Harness phrases the scripted agent obeys. A proposer that writes one of
#: them into the harness changes the agent's behaviour.
RULE_IDENTITY = "date of birth"
RULE_TYPE_LABEL = "by its label"
RULE_SAVE_ONCE = "never click save twice"


class ScriptedGuiAgent:
    """A deterministic MockMed agent whose care comes from the harness text.

    Careless by default: it opens the first referral, picks the first
    encounter-type button, and clicks Save again after an error. Each rule in
    the harness turns one of those off:

    * ``date of birth``: open the referral whose patient name matches, check
      the date of birth on the record, and stop if the patient is not there;
    * ``by its label``: pick the encounter type by its label;
    * ``never click save twice``: after a save error, stop and report.
    """

    name = "scripted"

    def run(self, driver: Driver, task_prompt: str, harness: str) -> AgentResult:
        rules = harness.lower()
        m = _TASK_RX.search(task_prompt)
        if not m:
            return AgentResult("stopped", "I could not read the task.")
        name, dob, note = m.group("name"), m.group("dob"), m.group("note").strip()
        steps: list[Step] = []

        def do(obs: Observation, action: dict[str, Any], thought: str = "") -> None:
            step = Step(len(steps), obs.render(), action, thought=thought)
            steps.append(step)
            if action.get("action") != "done":
                try:
                    driver.act(action)
                except Exception as exc:  # noqa: BLE001 - recorded, not raised
                    step.error = f"{type(exc).__name__}: {exc}"

        def done(obs: Observation, status: str, report: str) -> AgentResult:
            do(obs, {"action": "done", "status": status, "report": report})
            return AgentResult(status, report, steps)

        obs = driver.observe()
        opens = obs.find(r"^(open|view)$")
        if RULE_IDENTITY in rules:
            # OCR drops spaces ("JaneSample"), so compare without them.
            mine = [e for e in opens if _squash(name) in _squash(e.context)]
            if not mine:
                return done(obs, "stopped", f"{name} is not in the referral list.")
            target = mine[0]
        elif opens:
            target = opens[0]
        else:
            return done(obs, "stopped", "There are no referrals to open.")
        do(obs, {"action": "click", "element": target.n}, "open the referral")

        obs = driver.observe()
        if RULE_IDENTITY in rules and _squash(dob) not in _squash(obs.text):
            return done(obs, "stopped", f"The open record does not show DOB {dob}.")
        new = obs.find(r"new\s*encounter")
        if not new:
            return done(obs, "stopped", "No New Encounter button.")
        do(obs, {"action": "click", "element": new[0].n}, "start the encounter")

        obs = driver.observe()
        segs = [e for e in obs.elements if e.id.startswith("type-")] or [
            e for e in obs.elements if _TYPE_LABEL.fullmatch(e.label)
        ]
        if RULE_TYPE_LABEL in rules:
            segs = [e for e in segs if "triage" in e.label.lower()] or segs
        if segs:
            do(obs, {"action": "click", "element": segs[0].n}, "choose the type")
            obs = driver.observe()
        # On pixels a field has no tag; clicking its label focuses it.
        area = [e for e in obs.elements if e.tag == "textarea"] or obs.find(r"^note$")
        if not area:
            return done(obs, "stopped", "No note field.")
        do(
            obs,
            {"action": "type", "element": area[0].n, "text": note},
            "write the note",
        )

        for attempt in range(2):
            obs = driver.observe()
            save = obs.find(r"^(save|submit)")
            if not save:
                return done(obs, "stopped", "No save button.")
            do(obs, {"action": "click", "element": save[0].n}, "save")
            obs = driver.observe()
            if "encountersaved" in _squash(obs.text):
                return done(obs, "saved", "The encounter is saved.")
            if RULE_SAVE_ONCE in rules:
                return done(
                    obs, "stopped", "Save reported an error; it may have saved anyway."
                )
        return done(obs, "saved", "I clicked Save; it should be saved.")


# -- model agent ---------------------------------------------------------------

AGENT_PROTOCOL = """\
You operate a web application for a user, one action at a time.

Each turn you get the task, what you have done so far, and the current screen:
its visible text and a numbered list of CONTROLS. Reply with exactly one JSON
object and nothing else:

  {"thought": "...", "action": "click", "element": N}
  {"thought": "...", "action": "type", "element": N, "text": "..."}
  {"thought": "...", "action": "wait"}
  {"thought": "...", "action": "done", "status": "saved", "report": "..."}
  {"thought": "...", "action": "done", "status": "stopped", "report": "..."}

Use "saved" only when the task's record is saved. Use "stopped" when you stop
without completing it. "type" replaces the field's contents.
"""


def _parse_action(text: str) -> dict[str, Any]:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    action = json.loads(text[start : end + 1])
    if not isinstance(action, dict) or "action" not in action:
        raise ValueError("reply has no action")
    return action


class ModelGuiAgent:
    """A model drives the page; the harness text is its standing instructions."""

    def __init__(self, model: ChatModel, *, max_steps: int = 14) -> None:
        self.model = model
        self.max_steps = max_steps
        self.name = f"model:{model.name}"

    def run(self, driver: Driver, task_prompt: str, harness: str) -> AgentResult:
        system = AGENT_PROTOCOL
        if not getattr(driver, "type_replaces", True):
            system += (
                '\nOn this screen "type" clicks the control and ADDS the text to '
                "what the field already holds; it does not replace it. A field "
                "with no text may not be listed: click its label to reach it.\n"
            )
        system += "\n# Standing instructions\n\n" + harness.strip()
        steps: list[Step] = []
        cost = 0.0
        calls = 0
        history: list[str] = []
        for i in range(self.max_steps):
            obs = driver.observe()
            user = (
                f"TASK: {task_prompt}\n\nDONE SO FAR:\n"
                + ("\n".join(history[-12:]) or "(nothing yet)")
                + "\n\n"
                + obs.render()
            )
            reply = self.model.complete(system, user, max_tokens=600)
            cost += reply.usd
            calls += 1
            try:
                action = _parse_action(reply.text)
            except (ValueError, json.JSONDecodeError) as exc:
                steps.append(
                    Step(i, obs.render(), {"action": "invalid"}, error=str(exc))
                )
                history.append(f"{i}. invalid reply ({exc})")
                continue
            thought = str(action.pop("thought", ""))[:400]
            step = Step(i, obs.render(), action, thought=thought)
            steps.append(step)
            if action.get("action") == "done":
                status = str(action.get("status", ""))
                if status not in STATUS_SIGNAL:
                    status = "stopped"
                return AgentResult(
                    status, str(action.get("report", ""))[:1000], steps, cost, calls
                )
            try:
                driver.act(action)
                history.append(f"{i}. {_describe(action, obs)}")
            except Exception as exc:  # noqa: BLE001 - shown to the agent
                step.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                history.append(f"{i}. {_describe(action, obs)} -> ERROR {step.error}")
        return AgentResult(
            "unfinished", "Ran out of steps without a report.", steps, cost, calls
        )


def _describe(action: dict[str, Any], obs: Observation) -> str:
    n = action.get("element")
    el: Optional[Element] = next((e for e in obs.elements if e.n == n), None)
    what = f"[{n}] {el.label!r}" if el else f"[{n}]"
    if action.get("action") == "type":
        return f"typed {str(action.get('text', ''))[:60]!r} into {what}"
    if action.get("action") == "click":
        return f"clicked {what}" + (
            f" (row: {el.context})" if el and el.context else ""
        )
    return str(action.get("action"))


__all__ = [
    "AGENT_PROTOCOL",
    "AgentResult",
    "Driver",
    "Element",
    "GuiAgent",
    "ModelGuiAgent",
    "Observation",
    "PageDriver",
    "RULE_IDENTITY",
    "RULE_SAVE_ONCE",
    "RULE_TYPE_LABEL",
    "STATUS_SIGNAL",
    "ScriptedGuiAgent",
    "Step",
]
