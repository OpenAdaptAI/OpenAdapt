import { expect, it } from "vitest";
import type { FirstWorkflowState } from "./lib/types";
import { routeForFirstWorkflow } from "./App";

function state(
  stage: FirstWorkflowState["stage"],
): FirstWorkflowState {
  return {
    stage,
    capture_id: "capture-1",
    workflow_id: "workflow-1",
    target: { backend: "web", url: "https://example.test" },
    task: "Read one test record",
    updated_at: "2026-08-26T00:00:00Z",
  };
}

it("resumes the durable first workflow at its exact user stage", () => {
  expect(routeForFirstWorkflow(state("record"))).toEqual({
    name: "record",
    firstWorkflow: true,
    target: { backend: "web", url: "https://example.test" },
    task: "Read one test record",
  });
  // Nothing has run yet: the short Check it path replaces the read-only
  // first run and the review-only loop.
  for (const stage of ["review", "qualification", "qualification_after_result"] as const) {
    expect(routeForFirstWorkflow(state(stage))).toEqual({
      name: "check",
      id: "workflow-1",
      firstWorkflow: true,
    });
  }
  // A run that started stays on its own screen until a person has checked
  // the target.
  expect(routeForFirstWorkflow(state("executing"))).toEqual({
    name: "watch",
    id: "workflow-1",
    target: { backend: "web", url: "https://example.test" },
    firstWorkflow: true,
    firstRunComplete: false,
    firstRunLocked: true,
  });
  expect(routeForFirstWorkflow(state("reconciliation"))).toEqual({
    name: "watch",
    id: "workflow-1",
    target: { backend: "web", url: "https://example.test" },
    firstWorkflow: true,
    firstRunComplete: false,
    firstRunLocked: true,
  });
  expect(routeForFirstWorkflow(state("result"))).toEqual({
    name: "watch",
    id: "workflow-1",
    target: { backend: "web", url: "https://example.test" },
    firstWorkflow: true,
    firstRunComplete: true,
    firstRunLocked: false,
  });
  expect(routeForFirstWorkflow(state("complete"))).toBeNull();
});
