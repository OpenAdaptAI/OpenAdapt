import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { engineInvoke, engineTry } from "../lib/engine";
import type { ExecutionResponse, RunReport } from "../lib/types";
import { WatchRun } from "./WatchRun";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineInvoke: vi.fn(),
    engineTry: vi.fn(),
    onEngineEvent: vi.fn(() => Promise.resolve(() => {})),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function preciseReport(
  outcome: "VERIFIED" | "ROLLED_BACK",
): RunReport {
  const verified = outcome === "VERIFIED";
  return {
    ok: verified,
    outcome,
    pre_action_refusal: false,
    run_id: "run-1",
    workflow_id: "workflow-1",
    workflow_name: "Reference workflow",
    total_steps: 1,
    steps: [],
    outcome_details: {
      profile: "standard",
      production_eligible: verified,
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
        effect: verified ? 1 : 0,
      },
      evidence_classes: verified
        ? ["authorization", "identity", "effect_tier_1"]
        : ["authorization", "identity", "compensation"],
      model_calls: verified ? 0 : 2,
      external_network_calls: verified ? "none" : "observed",
      compensation_actions: verified ? 0 : 1,
    },
  };
}

it("renders the precise execution evidence returned by the sidecar", async () => {
  const report = preciseReport("ROLLED_BACK");
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

it("clears prior terminal evidence while a new run is in flight", async () => {
  const verified = preciseReport("VERIFIED");
  let finishRun!: (report: ExecutionResponse) => void;
  const pendingRun = new Promise<ExecutionResponse>((resolve) => {
    finishRun = resolve;
  });
  vi.mocked(engineTry).mockResolvedValue(verified);
  vi.mocked(engineInvoke).mockReturnValue(pendingRun);

  render(
    <WatchRun
      workflowId="workflow-1"
      initialTarget={{ backend: "web" }}
      onTeach={() => {}}
    />,
  );
  await screen.findByText("Outcome evidence");

  fireEvent.click(screen.getByRole("button", { name: "Run with safety gates" }));

  await waitFor(() =>
    expect(screen.queryByText("Outcome evidence")).toBeNull(),
  );

  await act(async () => {
    finishRun(verified);
    await pendingRun;
  });
  expect(await screen.findByText("Outcome evidence")).toBeTruthy();
});
