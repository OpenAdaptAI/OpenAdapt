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
    expires_at: "2026-07-27T22:00:00+00:00",
    allowed_actions: ["verify_and_resume", "teach", "escalate"],
    evidence: {},
  },
  task_digest: "sha256:" + "c".repeat(64),
  presentation: {
    question: "Can you prepare one unambiguous target in the live application?",
    explanation: "The compiled target could not be resolved uniquely.",
    assurance: "Your answer does not mark the run verified.",
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

describe("the three answers are distinguishable by consequence", () => {
  it("puts the consequence on each button and spells it out in the card", async () => {
    await boot();
    await openTask();
    const bar = document.getElementById("actions")!.textContent ?? "";
    expect(bar).toContain("This run only");
    expect(bar).toContain("Changes future runs");
    expect(bar).toContain("Hands this to someone else");

    const consequences = document.querySelector(".consequences")!.textContent ?? "";
    expect(consequences).toContain("The saved workflow is not changed");
    expect(consequences).toContain("future runs handle this on their own");
    expect(consequences).toContain("The run stays paused exactly where it is");
    // Only the actions the signed task allows are described.
    expect(consequences).not.toContain("Skip this step");
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
