import { expect, it } from "vitest";

import { checkPlan } from "./checkPlan";
import type { QualificationProject } from "./types";

/**
 * The projection Desktop's engine returns for a program whose only visible
 * step waits, and whose Save sits inside a reusable sub-task. Flow's graph
 * draws the sub-task call as one node and leaves its steps out; Flow's report
 * still counts and checks the Save. Trimmed from a real `inspect_bundle`
 * result (see tests/test_engine/test_qualification.py, which pins the engine
 * side of this shape).
 */
function subTaskSave({ withCallNode = true, withReportCount = true } = {}) {
  return {
    ok: true,
    workflow_id: "wf-1",
    policy: "clinical-write",
    qualification_schema: "openadapt.qualification-project/v1",
    migration_required: false,
    draft_environment: false,
    project: { revision: 1, cases: [] },
    report: {
      ...(withReportCount
        ? { action_count: 2, state_changing_action_count: 0, consequential_action_count: 1 }
        : {}),
      refusals: [
        {
          code: "action_classification_unconfirmed",
          path: "qualification.action_classifications.save",
          message: "action risk is unknown or has not been operator-confirmed",
          step_id: "save",
          details: {},
        },
      ],
    },
    certification_current: false,
    graph: {
      bundle: { name: "Compiled program", encrypted: false, provenance: {} },
      nodes: [
        { id: "w", index: 0, kind: "action", title: "Wait", action: "wait", effects: [], postconditions: [], halts: [], badges: [], risk: "reversible" },
        ...(withCallNode
          ? [{ id: "call", index: 1, kind: "subflow_call", title: "Call subflow commit", effects: [], postconditions: [], halts: [], badges: [], risk: null }]
          : []),
        { id: "done", index: 2, kind: "terminal", title: "End of declared steps", effects: [], postconditions: [], halts: [], badges: [], risk: null },
      ],
      edges: [],
    },
    controls: {
      parameters: [],
      actions: {
        w: {
          step_id: "settle",
          execution_paths: ["gui"],
          classification: {
            step_id: "settle",
            classification: "read_only",
            explanation: "wait does not actuate business state",
            operator_confirmed: true,
          },
          identity: { can_arm: false, armed: false, sources: [], policy: null },
          effects: [],
        },
      },
      business_decisions: { available: false, graphs: [] },
      judgment_cases: { available: false, contexts: [], report: null },
    },
  } as unknown as QualificationProject;
}

it("never calls a task read-only when a save sits inside a sub-task the graph doesn't show", () => {
  const plan = checkPlan(subTaskSave());

  expect(plan.writes).toEqual([]);
  expect(plan.readCount).toBe(1);
  expect(plan.unproven.length).toBeGreaterThan(0);
  expect(plan.changes).toBe("missing");
  expect(plan.rightRecord).toBe("missing");
  expect(plan.recordCheck).toBe("missing");
});

it("still refuses the read-only claim when Flow counts steps the graph leaves out", () => {
  // No sub-task node at all (an older or different projection): Flow's own
  // step count and its refusals still name a step Check it can't see.
  for (const variant of [
    subTaskSave({ withCallNode: false }),
    subTaskSave({ withCallNode: false, withReportCount: false }),
  ]) {
    const plan = checkPlan(variant);
    expect(plan.unproven.length).toBeGreaterThan(0);
    expect(plan.changes).not.toBe("not_needed");
    expect(plan.recordCheck).not.toBe("not_needed");
  }
});

it("still says a task only reads when Flow sees exactly the steps the graph shows", () => {
  const readOnly = subTaskSave({ withCallNode: false, withReportCount: false });
  (readOnly.report as { action_count?: number }).action_count = 1;
  readOnly.report.refusals = [];

  const plan = checkPlan(readOnly);

  expect(plan.unproven).toEqual([]);
  expect(plan.changes).toBe("not_needed");
});

/**
 * Three clicks from a recorded referral task. OCR read the State list's
 * current value "Unassigned" as "Jnassigned", and Flow's step title kept the
 * misread. The recorder also saved each control's role and accessible name.
 */
function referralClicks() {
  const node = (id: string, index: number, title: string) => ({
    id, index, kind: "action", title, action: "click",
    effects: [], postconditions: [], halts: [], badges: [], risk: "reversible",
  });
  const control = (stepId: string, target: { role: string | null; name: string | null } | null) => ({
    step_id: stepId,
    execution_paths: ["gui"],
    classification: null,
    identity: { can_arm: false, armed: false, sources: [], policy: null },
    effects: [],
    target,
  });
  const project = subTaskSave({ withCallNode: false, withReportCount: false });
  project.report.refusals = [];
  project.graph.nodes = [
    node("state", 0, "click 'Jnassigned'"),
    node("fname", 1, "click 'FirstName'"),
    node("save", 2, "click 'Save'"),
  ] as unknown as QualificationProject["graph"]["nodes"];
  project.controls.actions = {
    state: control("state", { role: "combobox", name: "form_state" }),
    fname: control("fname", { role: "textbox", name: "First Name" }),
    save: control("save", null),
  } as unknown as QualificationProject["controls"]["actions"];
  return project;
}

it("names each clicked control from its structure, never from a misread word", () => {
  const plan = checkPlan(referralClicks());
  const captions = plan.unproven.map((step) => step.caption);

  expect(captions.join(" ")).not.toContain("Jnassigned");
  expect(captions).toContain("Open the “form state” list");
  expect(captions).toContain("Click the “First Name” field");
  // No structure was recorded for this one, so the screen text is all there is.
  expect(captions).toContain("Click “Save”");
});
