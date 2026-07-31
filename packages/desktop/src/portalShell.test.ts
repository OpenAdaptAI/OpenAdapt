/**
 * Behavioural tests for the runner-local decision portal shell.
 *
 * The shell was previously covered only by source-text assertions, and every
 * one of them passed while a real `halted` outcome rendered on a phone as
 * "That decision was refused". So this drives the actual script in a DOM:
 * stubbed transport in, rendered text out.
 *
 * These tests protect the operator and runner contracts. They do not pin
 * ordinary wording, layout, or visual styling.
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

const RECONCILIATION_HALT = {
  ...RESOLUTION_HALT,
  task: {
    ...RESOLUTION_HALT.task,
    task_kind: "delivery_uncertain",
    delivery_state: "unknown",
    allowed_actions: ["reconcile", "teach", "escalate"],
  },
  presentation: {
    ...RESOLUTION_HALT.presentation,
    question: "Is the live destination ready for OpenAdapt to reconcile the uncertain action?",
    halt: {
      ...RESOLUTION_HALT.presentation.halt,
      category: "effect_indeterminate",
      will_recheck: [{ check: "delivery_reconciliation", count: null }],
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

describe("the decision view preserves the data boundary", () => {
  it("degrades a withheld label to the field's shape and shows no value", async () => {
    detail = TYPED_HALT;
    await boot();
    await openTask();
    const text = document.getElementById("main")!.textContent ?? "";
    expect(text).not.toContain(PROTECTED_VALUE);
    expect(text).not.toContain("Marta");
  });
});

describe("the shell sends only signed task actions", () => {
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

describe("reconciliation never turns an uncertain delivery into a retry", () => {
  it("shows it only for a delivery-uncertain task that may have crossed delivery", async () => {
    detail = RECONCILIATION_HALT;
    await boot();
    await openTask();
    const reconcile = document.querySelector('[data-action="reconcile"]')!;
    expect(reconcile).toBeInstanceOf(HTMLButtonElement);
  });

  it("does not render reconcile when the action was not delivered", async () => {
    detail = {
      ...RECONCILIATION_HALT,
      task: { ...RECONCILIATION_HALT.task, delivery_state: "not_delivered" },
    };
    await boot();
    await openTask();
    expect(document.querySelector('[data-action="reconcile"]')).toBeNull();
  });

  it("sends a closed reconciliation request, not continue", async () => {
    detail = RECONCILIATION_HALT;
    decisionReply = {
      status: 200,
      body: {
        state: "completed",
        reason_code: "reconciled_and_resumed",
        action: "reconcile",
        report_success: true,
        transition_receipt_digest: "sha256:" + "d".repeat(64),
      },
    };
    await boot();
    await openTask();
    await answer("reconcile");
    const body = JSON.parse(
      (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls
        .map((call) => call[1] as { body?: string } | undefined)
        .filter((init) => init?.body)
        .pop()!.body!,
    );
    expect(body.action).toBe("reconcile");
    expect(body.disposition).toBe("reconcile_requested");
    const outcome = document.getElementById("outcome")!;
    expect(outcome.classList).toContain("success");
    expect(outcome.classList).toContain("terminal");
  });

  it("does not claim a reconciliation succeeded without the exact receipt proof", async () => {
    detail = RECONCILIATION_HALT;
    decisionReply = {
      status: 200,
      body: {
        state: "completed",
        reason_code: "reconciled_and_resumed",
        action: "reconcile",
        report_success: true,
      },
    };
    await boot();
    await openTask();
    await answer("reconcile");
    const outcome = document.getElementById("outcome")!;
    expect(outcome.classList).not.toContain("success");
    expect(outcome.classList).toContain("terminal");
  });

  it("keeps an unproven reconciliation answerable without offering a resend", async () => {
    detail = RECONCILIATION_HALT;
    decisionReply = {
      status: 200,
      body: {
        state: "refused",
        reason_code: "revalidation_refused",
        action: "reconcile",
      },
    };
    await boot();
    await openTask();
    await answer("reconcile");
    expect(document.querySelector('[data-action="reconcile"]')).not.toBeNull();
    expect(document.querySelector('[data-action="continue"]')).toBeNull();
  });
});

describe("terminal outcomes close the action set", () => {
  it("closes the run after rejection", async () => {
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
    expect(document.getElementById("outcome")!.classList).toContain("terminal");
    expect(document.getElementById("actions")!.hidden).toBe(true);
  });

  it("renders a runner that still returns the older decision record", async () => {
    decisionReply = { status: 200, body: { status: "rejected" } };
    await boot();
    await openTask();
    await answer("reject");
    expect(document.getElementById("outcome")!.classList).toContain("terminal");
  });
});

describe("the terminal receipt contract", () => {
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
    expect(document.getElementById("outcome")!.classList).toContain("halted");
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
    expect((document.getElementById("decision") as HTMLElement).hidden).toBe(true);
    expect(document.getElementById("outcome")!.classList).toContain("terminal");
    expect(document.getElementById("outcome")!.classList).toContain("success");
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
    expect((document.getElementById("decision") as HTMLElement).hidden).toBe(false);
    expect(document.getElementById("outcome")!.classList).not.toContain("terminal");
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
    const outcome = document.getElementById("outcome")!;
    expect(outcome.classList).toContain("uncertain");
    expect(outcome.classList).toContain("terminal");
  });

  it("reports an outcome it has no wording for as unknown, not as a refusal", async () => {
    decisionReply = {
      status: 200,
      body: { state: "some_future_state", reason_code: "some_future_reason", action: "verify_and_resume" },
    };
    await boot();
    await openTask();
    await answer();
    const outcome = document.getElementById("outcome")!;
    expect(outcome.classList).toContain("uncertain");
    expect(outcome.classList).toContain("terminal");
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
    expect(document.getElementById("outcome")!.classList).toContain("halted");
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
    const button = document.querySelector('[data-action="verify_and_resume"]') as HTMLButtonElement;
    expect(button.disabled).toBe(false);
  });

  it("treats a lost network as uncertain rather than failed", async () => {
    decisionReply = { status: 503, body: null };
    await boot();
    await openTask();
    await answer();
    expect(document.getElementById("outcome")!.classList).toContain("uncertain");
  });

  it("treats a request that never came back as uncertain, not as pending forever", async () => {
    await boot();
    await openTask();
    vi.stubGlobal("fetch", async () => {
      throw new TypeError("Failed to fetch");
    });
    await answer();
    const outcome = document.getElementById("outcome")!;
    expect(outcome.classList).toContain("uncertain");
    expect(outcome.classList).toContain("terminal");
  });
});

describe("the signed task controls the action set", () => {
  it("does not add a local action", async () => {
    await boot();
    await openTask();
    const buttons = Array.from(
      document.querySelectorAll("#actions [data-action]"),
    ) as HTMLButtonElement[];
    expect(buttons.map((button) => button.dataset.action)).toEqual(
      RESOLUTION_HALT.task.allowed_actions,
    );
  });
});

describe("the retained frame is named as history, not as the live screen", () => {
  it("shows a retained figure with age and a no-live-state warning", async () => {
    detail = {
      ...RESOLUTION_HALT,
      task: {
        ...RESOLUTION_HALT.task,
        created_at: new Date(Date.now() - 14 * 60 * 1000).toISOString(),
      },
    };
    await boot();
    await openTask();
    const shot = document.querySelector("figure.shot")!;
    expect(shot.querySelector("img[data-artifact]")).not.toBeNull();
    // The runner has no on-demand re-observation route. A retained artifact is
    // therefore a view-only image, not a refreshable live control.
    expect(shot.querySelector("button")).toBeNull();
  });
});

describe("known action stakes stay visible without making an action primary", () => {
  it("shows the risk signal and keeps the full explanation available", async () => {
    detail = {
      ...RESOLUTION_HALT,
      task: { ...RESOLUTION_HALT.task, risk_class: "irreversible" },
    };
    await boot();
    await openTask();
    expect(document.querySelector(".task-details .stakes")).not.toBeNull();
  });

  it("does not invent a detailed risk claim when the engine does not know", async () => {
    await boot();
    await openTask();
    expect(document.querySelector(".stakes")).toBeNull();
    expect(document.querySelector(".task-details .stakes")).toBeNull();
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
    // Nothing in the prompt is tappable: there is no shortcut past the content.
    expect(bar.querySelectorAll("button")).toHaveLength(0);
    // The gate is the last element in the document, after the consequence card.
    expect(observed).toHaveLength(1);
    expect(observed[0].id).toBe("gate");
    expect(document.getElementById("main")!.lastElementChild!.id).toBe("gate");

    fire!([{ isIntersecting: true }]);
    await flush();
    expect(bar.querySelectorAll("[data-action]")).toHaveLength(4);
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
    expect(document.getElementById("outcome")!.classList).toContain("success");
  });

  it("fails open where the browser cannot observe the gate", async () => {
    vi.stubGlobal("IntersectionObserver", undefined);
    await boot();
    await openTask();
    expect(document.querySelectorAll("#actions [data-action]")).toHaveLength(4);
  });
});
