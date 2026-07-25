import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { engineTry } from "../lib/engine";
import type { RunReport } from "../lib/types";
import { WatchRun } from "./WatchRun";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineTry: vi.fn(),
    onEngineEvent: vi.fn(() => Promise.resolve(() => {})),
  };
});

afterEach(cleanup);

it("renders the precise execution evidence returned by the sidecar", async () => {
  const report: RunReport = {
    ok: false,
    outcome: "ROLLED_BACK",
    pre_action_refusal: false,
    run_id: "run-1",
    workflow_id: "workflow-1",
    workflow_name: "Reference workflow",
    total_steps: 1,
    steps: [],
    outcome_details: {
      profile: "standard",
      production_eligible: false,
      execution_completed: true,
      required_contracts: {
        authorization: 1,
        identity: 1,
        postcondition: 1,
        effect: 1,
      },
      passed_contracts: {
        authorization: 1,
        identity: 1,
        postcondition: 1,
        effect: 0,
      },
      evidence_classes: ["authorization", "identity", "compensation"],
      model_calls: 2,
      external_network_calls: "observed",
      compensation_actions: 1,
    },
  };
  vi.mocked(engineTry).mockResolvedValue(report);

  render(
    <WatchRun
      workflowId="workflow-1"
      initialTarget={{ backend: "web" }}
      onTeach={() => {}}
    />,
  );

  const modelCalls = await screen.findByText("Model calls");
  expect(modelCalls.parentElement?.textContent).toContain("2");
  expect(screen.getByText("External network").parentElement?.textContent).toContain(
    "observed",
  );
  expect(
    screen.getByText("Compensating actions").parentElement?.textContent,
  ).toContain("1");
  expect(screen.getByText("0/1")).toBeTruthy();
});
