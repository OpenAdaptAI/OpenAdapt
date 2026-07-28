/**
 * Behavioural tests for the runner-local decision portal shell.
 *
 * The shell was previously covered only by source-text assertions, and every
 * one of them passed while a real `halted` outcome rendered on a phone as
 * "That decision was refused". So this drives the actual script in a DOM:
 * stubbed transport in, rendered text out.
 *
 * Two things are pinned here:
 *
 *  1. the operator can tell WHAT broke and what each answer will do;
 *  2. every terminal outcome the engine can return maps to its own copy, and an
 *     outcome this build does not know is reported as unknown -- never as a
 *     refusal.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

// The shipped shell sources, exactly as the portal serves them.
// (The stylesheet's own rules are asserted in tests/test_portal/test_shell.py;
// vitest does not process CSS, so a `?raw` import of it would be empty.)
import APP from "../engine/portal/shell/app.js?raw";

const RESOLUTION_HALT = {
  task: {
    capability_digest: "sha256:" + "a".repeat(64),
    signature: "hmac-sha256:" + "b".repeat(64),
    delivery_state: "not_delivered",
    risk_class: "unknown",
    created_at: "2026-07-27T21:00:00+00:00",
    expires_at: "2026-07-27T22:00:00+00:00",
    allowed_actions: ["verify_and_resume", "reject", "teach", "escalate"],
    evidence: {},
  },
  task_digest: "sha256:" + "c".repeat(64),
  presentation: {
    question: "Can you prepare one unambiguous target in the live application?",
    explanation: "The compiled target could not be resolved uniquely.",
    assurance: "Your answer does not mark the run verified.",
    after_artifact_id: "artifact-1",
    halt: {
      category: "resolution",
      step_ordinal: 1,
      step_count: 6,
      action_kind: "click",
      target_role: "button",
      target_label: "Open",
      target_label_withheld: false,
      resolution_ladder: [
        { rung: "structural", evidence: "recorded", verdict: "failed" },
        { rung: "template", evidence: "recorded", verdict: "failed" },
        { rung: "template_global", evidence: "recorded", verdict: "failed" },
        { rung: "ocr", evidence: "recorded", verdict: "failed" },
        { rung: "geometry", evidence: "absent", verdict: "not_attempted" },
        { rung: "grounder", evidence: "unknown", verdict: "unknown" },
      ],
      will_recheck: [
        { check: "target_resolution", count: null },
        { check: "record_identity", count: 5 },
        { check: "postconditions", count: 2 },
      ],
    },
  },
};

/** The exact leak the projection is built to avoid ever carrying. */
const PROTECTED_VALUE = "Marta Quilligan 1974-03-08 MRN 40182";

const TYPED_HALT = {
  ...RESOLUTION_HALT,
  presentation: {
    ...RESOLUTION_HALT.presentation,
    halt: {
      ...RESOLUTION_HALT.presentation.halt,
      action_kind: "type",
      target_role: "textbox",
      target_label: null,
      target_label_withheld: true,
    },
  },
};

type Reply = { status: number; body: unknown };

let decisionReply: Reply = { status: 200, body: null };
let detail: unknown = RESOLUTION_HALT;

function stubTransport() {
  const fetchStub = vi.fn(async (url: string) => {
    let reply: Reply = { status: 404, body: null };
    if (url === "/api/portal/session") {
      reply = { status: 200, body: { device_label: "Phone" } };
    } else if (url === "/api/portal/tasks") {
      reply = {
        status: 200,
        body: [{ id: "run1", headline: "The compiled target could not be resolved uniquely.", category: "resolution" }],
      };
    } else if (url === "/api/portal/tasks/run1") {
      reply = { status: 200, body: detail };
    } else if (url.includes("/actions/")) {
      reply = decisionReply;
    }
    return {
      status: reply.status,
      ok: reply.status < 400,
      json: async () => reply.body,
    };
  });
  vi.stubGlobal("fetch", fetchStub);
}

async function flush(times = 12) {
  for (let i = 0; i < times; i += 1) {
    await new Promise((r) => setTimeout(r, 0));
  }
}

async function boot() {
  document.body.innerHTML = `
    <header class="bar"><span class="brand">OpenAdapt</span><span id="device" class="device"></span></header>
    <main id="main" class="main">Loading…</main>
    <footer id="actions" class="actions" hidden></footer>`;
  window.sessionStorage.setItem("portal_token", "token");
  window.sessionStorage.setItem("portal_csrf", "csrf");
  // jsdom lays nothing out, so give the fixed bar a real height to measure.
  const bar = document.getElementById("actions") as HTMLElement;
  bar.getBoundingClientRect = () => ({ height: 204 }) as DOMRect;
  new Function(APP)();
  await flush();
}

async function openTask() {
  const row = document.querySelector("[data-run]") as HTMLButtonElement;
  row.click();
  await flush();
}

async function answer(action = "verify_and_resume") {
  const button = document.querySelector(`[data-action="${action}"]`) as HTMLButtonElement;
  button.click();
  await flush();
}

function outcomeText() {
  return document.getElementById("outcome")?.textContent ?? "";
}

beforeEach(() => {
  // The gate tests replace `IntersectionObserver`; drop every stub so one test
  // cannot decide whether the next one is gated.
  vi.unstubAllGlobals();
  detail = RESOLUTION_HALT;
  decisionReply = { status: 200, body: null };
  window.sessionStorage.clear();
  if (!globalThis.crypto?.randomUUID) {
    vi.stubGlobal("crypto", { randomUUID: () => "00000000-0000-4000-8000-000000000000" });
  }
  stubTransport();
});

describe("the decision view says what broke", () => {
  it("names the failed step, the action, and the target it could not find", async () => {
    await boot();
    await openTask();
    const text = document.getElementById("main")!.textContent ?? "";
    expect(text).toContain("Step 1 of 6");
    expect(text).toContain("could not find the button labelled “Open”");
    expect(text).toContain("did not click anything");
  });

  it("lists the rungs it tried and skips the ones it never had evidence for", async () => {
    await boot();
    await openTask();
    const tried = document.querySelector(".ladder")!.textContent ?? "";
    expect(tried).toContain("the recorded picture of the target");
    expect(tried).toContain("the target's text label");
    expect(tried).toContain("did not find it");
    // geometry had nothing recorded, so it is not presented as a failure.
    expect(tried).not.toContain("its position next to nearby text");
  });

  it("states what continuing will re-check before anything happens", async () => {
    await boot();
    await openTask();
    const recheck = document.querySelector(".recheck")!.textContent ?? "";
    expect(recheck).toContain("find the target again on the live screen");
    expect(recheck).toContain("confirm the record on screen is the intended one");
    expect(recheck).toContain("confirm the screen reaches the expected state");
    expect(recheck).toContain("changes nothing");
  });

  it("degrades a withheld label to the field's shape and shows no value", async () => {
    detail = TYPED_HALT;
    await boot();
    await openTask();
    const text = document.getElementById("main")!.textContent ?? "";
    expect(text).toContain("could not find the text field");
    expect(text).toContain("did not type into anything");
    expect(text).not.toContain(PROTECTED_VALUE);
    expect(text).not.toContain("Marta");
  });
});

describe("the answers are distinguishable by consequence", () => {
  it("puts the consequence on each button and spells it out in the card", async () => {
    await boot();
    await openTask();
    const bar = document.getElementById("actions")!.textContent ?? "";
    expect(bar).toContain("This run only");
    expect(bar).toContain("Ends this run");
    expect(bar).toContain("Changes future runs");
    expect(bar).toContain("Hands this to someone else");

    const consequences = document.querySelector(".consequences")!.textContent ?? "";
    expect(consequences).toContain("The saved workflow is not changed");
    expect(consequences).toContain("future runs handle this on their own");
    expect(consequences).toContain("The run stays paused exactly where it is");
    // Only the actions the signed task allows are described.
    expect(consequences).not.toContain("Skip this step");
  });

  it("distinguishes ending the run from parking it for someone else", async () => {
    await boot();
    await openTask();
    const briefs = Object.fromEntries(
      Array.from(document.querySelectorAll("#actions [data-action]")).map((b) => [
        (b as HTMLElement).dataset.action,
        b.querySelector(".brief")?.textContent ?? "",
      ]),
    );
    // The whole reason reject is its own wire action: these two answers do
    // opposite things to the run, and an operator who reads them as synonyms
    // has been told the wrong thing about what happens next.
    expect(briefs.reject).toBe("Ends this run");
    expect(briefs.escalate).toBe("Hands this to someone else");

    const consequences = document.querySelector(".consequences")!.textContent ?? "";
    expect(consequences).toContain("this run cannot be resumed afterwards");
    expect(consequences).toContain("The run stays paused exactly where it is");
    // Reject is about THIS RUN, never about the saved workflow.
    expect(consequences).toContain(
      "Ends this run now. Nothing in the application is touched",
    );
  });

  it("sends the closed reject disposition and never any free text", async () => {
    await boot();
    await openTask();
    await answer("reject");
    const body = JSON.parse(
      (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
        .map((call) => call[1] as { body?: string } | undefined)
        .filter((init) => init?.body)
        .pop()!.body!,
    );
    expect(body.action).toBe("reject");
    expect(body.disposition).toBe("rejected_by_operator");
    // No field on the wire could carry a reason an operator typed.
    expect(Object.keys(body).sort()).toEqual([
      "action",
      "capability_digest",
      "disposition",
      "idempotency_key",
      "task_digest",
      "task_signature",
    ]);
  });
});

describe("a rejection is reported as the end of the run", () => {
  it("never says someone will pick it up", async () => {
    decisionReply = {
      status: 200,
      body: {
        schema_version: "openadapt.human-decision-receipt/v1",
        action: "reject",
        state: "rejected",
        reason_code: "rejected_by_operator",
        report_success: null,
      },
    };
    await boot();
    await openTask();
    await answer("reject");
    const text = outcomeText();
    expect(text).toContain("This run is over and cannot be resumed");
    expect(text).toContain("nothing in the application was touched");
    // The escalation wording would be an actively wrong instruction here.
    expect(text).not.toContain("until someone picks it up");
    expect(text).not.toContain("stays paused");
    // Terminal: the answers go away.
    expect(document.getElementById("actions")!.hidden).toBe(true);
  });

  it("renders a runner that still returns the older decision record", async () => {
    decisionReply = { status: 200, body: { status: "rejected" } };
    await boot();
    await openTask();
    await answer("reject");
    expect(outcomeText()).toContain("This run is over and cannot be resumed");
  });
});

describe("the terminal receipt shape", () => {
  it("renders a real halt as a halt, not as a refusal", async () => {
    decisionReply = {
      status: 200,
      body: {
        schema_version: "openadapt.human-decision-receipt/v1",
        action: "verify_and_resume",
        state: "halted",
        reason_code: "continuation_halted",
      },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("continued and then stopped again");
    expect(outcomeText()).not.toContain("refused");
    // A terminal outcome takes the buttons away.
    expect((document.getElementById("actions") as HTMLElement).hidden).toBe(true);
  });

  it("renders a verified continuation as success", async () => {
    decisionReply = {
      status: 200,
      body: { state: "completed", reason_code: "verified_and_resumed", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("Checked and continued");
    expect((document.getElementById("actions") as HTMLElement).hidden).toBe(true);
  });

  it("keeps a live revalidation refusal answerable again", async () => {
    decisionReply = {
      status: 200,
      body: { state: "refused", reason_code: "revalidation_refused", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("still not in the state this step needs");
    const button = document.querySelector('[data-action="verify_and_resume"]') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });

  it("reports an uncertain delivery without ever inviting a retry", async () => {
    decisionReply = {
      status: 202,
      body: { state: "delivery_uncertain", reason_code: "delivery_uncertain", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("may already have been sent");
    expect(outcomeText()).toContain("Do not answer again");
  });

  it("reports an outcome it has no wording for as unknown, not as a refusal", async () => {
    decisionReply = {
      status: 200,
      body: { state: "some_future_state", reason_code: "some_future_reason", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("no wording for");
    expect(outcomeText()).not.toContain("refused");
  });

  it("still renders a runner that returns the older decision record", async () => {
    decisionReply = {
      status: 200,
      body: {
        action: "continue",
        status: "halted",
        message: "front-desk: the live session died at step_003",
        report_success: false,
      },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("continued and then stopped again");
    // The engine's free-text message is audit, not phone copy.
    expect(outcomeText()).not.toContain("front-desk");
  });

  it("shows a pre-admission refusal without claiming the engine acted", async () => {
    decisionReply = {
      status: 409,
      body: { detail: "the human decision task changed; reload the current pause" },
    };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("reload the current pause");
    const button = document.querySelector('[data-action="verify_and_resume"]') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });

  it("treats a lost network as uncertain rather than failed", async () => {
    decisionReply = { status: 503, body: null };
    await boot();
    await openTask();
    await answer();
    expect(outcomeText()).toContain("The result is uncertain");
  });

  it("treats a request that never came back as uncertain, not as pending forever", async () => {
    await boot();
    await openTask();
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    await answer();
    expect(outcomeText()).toContain("The result is uncertain");
    expect(outcomeText()).not.toContain("Waiting for this computer");
  });
});

describe("no answer is rendered as the recommended one", () => {
  it("gives every action the same styling, however the engine ordered them", async () => {
    await boot();
    await openTask();
    const buttons = Array.from(
      document.querySelectorAll("#actions [data-action]"),
    ) as HTMLButtonElement[];
    expect(buttons).toHaveLength(4);
    // `allowed_actions[0]` is `verify_and_resume` here, which is exactly the
    // case that used to be painted in filled accent. `reject` must not become
    // the new emphasised one either: removing a recommendation and then
    // recommending the opposite answer is the same mistake pointed the other
    // way, and nothing on this task means "recommended".
    expect(buttons[0].dataset.action).toBe("verify_and_resume");
    const classes = new Set(buttons.map((b) => b.className));
    expect(classes).toEqual(new Set([""]));
    expect(document.getElementById("actions")!.innerHTML).not.toContain("primary");
  });
});

describe("the assurance sentence names both halves of the boundary", () => {
  it("says what the engine cannot check and puts it above the actions", async () => {
    await boot();
    await openTask();
    const limit = document.querySelector(".limit")!.textContent ?? "";
    expect(limit).toContain("re-checks what it can measure");
    expect(limit).toContain("cannot check whether you actually looked");
    expect(limit).toContain("Answer from the live application");
    // The engine's one-sided sentence is replaced, not printed beside it.
    const text = document.getElementById("main")!.textContent ?? "";
    expect(text).not.toContain("Your answer does not mark the run verified");
    // Reading order: the limit precedes the action bar, which is the last
    // element the operator reaches.
    const card = document.querySelector(".card")!;
    const limitNode = card.querySelector(".limit")!;
    const outcome = card.querySelector(".outcome")!;
    expect(limitNode.compareDocumentPosition(outcome) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("the retained frame is named as history, not as the live screen", () => {
  it("labels it by when OpenAdapt stopped, ages it, and points at the app", async () => {
    detail = {
      ...RESOLUTION_HALT,
      task: {
        ...RESOLUTION_HALT.task,
        created_at: new Date(Date.now() - 14 * 60 * 1000).toISOString(),
      },
    };
    await boot();
    await openTask();
    const shot = document.querySelector(".shot")!.textContent ?? "";
    expect(shot).toContain("Screen when OpenAdapt stopped");
    expect(shot).toContain("about 14 minutes ago");
    expect(shot).toContain("not the live screen");
    expect(shot).toContain("Look at the application itself");
    expect(shot).not.toContain("current screen");
    // A refresh control would manufacture the liveness this wording removes,
    // and the runner does not re-observe on demand.
    expect(document.querySelector(".shot")!.innerHTML).not.toContain("Refresh");
  });
});

describe("stakes are shown above the question, and only when they are known", () => {
  it("renders the irreversible case", async () => {
    detail = {
      ...RESOLUTION_HALT,
      task: { ...RESOLUTION_HALT.task, risk_class: "irreversible" },
    };
    await boot();
    await openTask();
    const stakes = document.querySelector(".stakes")!.textContent ?? "";
    expect(stakes).toContain("This cannot be undone");
    // Above the question, not below it.
    const card = document.querySelector(".card")!;
    const heading = card.querySelector("h1")!;
    expect(
      card.querySelector(".stakes")!.compareDocumentPosition(heading) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("says nothing at all when the engine could not establish the stakes", async () => {
    await boot();
    await openTask();
    expect(document.querySelector(".stakes")).toBeNull();
  });
});

describe("the answers are gated on reaching the end of the decision", () => {
  let observed: Element[] = [];
  let fire: ((entries: unknown[]) => void) | null = null;

  function stubObserver() {
    observed = [];
    fire = null;
    vi.stubGlobal(
      "IntersectionObserver",
      class {
        constructor(callback: (entries: unknown[]) => void) {
          fire = callback;
        }
        observe(node: Element) {
          observed.push(node);
        }
        disconnect() {
          fire = null;
        }
      },
    );
  }

  it("shows a prompt instead of the answers until the gate is reached", async () => {
    stubObserver();
    await boot();
    await openTask();
    const bar = document.getElementById("actions")!;
    expect(bar.querySelectorAll("[data-action]")).toHaveLength(0);
    expect(bar.textContent).toContain("Read this decision to the end");
    // Nothing in the prompt is tappable: there is no shortcut past the content.
    expect(bar.querySelectorAll("button")).toHaveLength(0);
    // The gate is the last element in the document, after the consequence card.
    expect(observed).toHaveLength(1);
    expect(observed[0].id).toBe("gate");
    expect(document.getElementById("main")!.lastElementChild!.id).toBe("gate");

    fire!([{ isIntersecting: true }]);
    await flush();
    expect(bar.querySelectorAll("[data-action]")).toHaveLength(4);
    expect(bar.textContent).not.toContain("Read this decision to the end");
  });

  it("answers normally once the gate has opened", async () => {
    stubObserver();
    decisionReply = {
      status: 200,
      body: { state: "completed", reason_code: "verified_and_resumed", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    fire!([{ isIntersecting: true }]);
    await flush();
    await answer();
    expect(outcomeText()).toContain("Checked and continued");
  });

  it("fails open where the browser cannot observe the gate", async () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    await boot();
    await openTask();
    expect(document.querySelectorAll("#actions [data-action]")).toHaveLength(4);
  });
});

describe("the outcome is readable and the bar goes away", () => {
  it("reserves the measured height of the action bar, not a constant", async () => {
    await boot();
    await openTask();
    expect(document.documentElement.style.getPropertyValue("--action-bar-height")).toBe("204px");
  });

  it("releases the reserved space once the decision is over", async () => {
    decisionReply = {
      status: 200,
      body: { state: "completed", reason_code: "verified_and_resumed", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    await answer();
    expect(document.documentElement.style.getPropertyValue("--action-bar-height")).toBe("0px");
  });

});
