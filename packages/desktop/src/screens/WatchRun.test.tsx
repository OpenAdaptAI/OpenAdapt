import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { CMD, engineInvoke, engineTry, EVT } from "../lib/engine";
import type {
  ExecutionResponse,
  QualificationProject,
  RunReport,
} from "../lib/types";
import { WatchRun } from "./WatchRun";

const watchEventMocks = vi.hoisted(() => ({
  handlers: new Map<string, (payload: unknown) => void>(),
}));

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineInvoke: vi.fn(),
    engineTry: vi.fn(),
    onEngineEvent: vi.fn(
      (event: string, handler: (payload: unknown) => void) => {
        watchEventMocks.handlers.set(event, handler);
        return Promise.resolve(() => watchEventMocks.handlers.delete(event));
      },
    ),
  };
});

afterEach(() => {
  cleanup();
  watchEventMocks.handlers.clear();
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
    persistence: {
      state: "persisted",
      retryable: false,
      message: "The report is saved in local history.",
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
      onQualify={() => {}}
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
      onQualify={() => {}}
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

it("keeps a report visible while local history is retried", async () => {
  const degraded = {
    ...preciseReport("VERIFIED"),
    persistence: {
      state: "degraded" as const,
      retryable: true,
      message: "The report is visible, but local history is not saved.",
    },
  };
  const persisted = {
    ...degraded,
    persistence: {
      state: "persisted" as const,
      retryable: false,
      message: "The report is saved in local history.",
    },
  };
  vi.mocked(engineTry).mockResolvedValue(degraded);
  vi.mocked(engineInvoke).mockResolvedValue({ ok: true, report: persisted });

  render(
    <WatchRun
      workflowId="workflow-1"
      initialTarget={{ backend: "web" }}
      onQualify={() => {}}
      onTeach={() => {}}
    />,
  );

  fireEvent.click(
    await screen.findByRole("button", { name: "Retry local history save" }),
  );
  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith("retry_run_persistence", {
      workflow_id: "workflow-1",
      run_id: "run-1",
    }),
  );
  await waitFor(() =>
    expect(screen.queryByText("Local run history needs attention")).toBeNull(),
  );
  expect(screen.getByText("Outcome evidence")).toBeTruthy();
});

function firstWorkflowReview(
  workflowId = "workflow-1",
  revision = 4,
  digest = "a".repeat(64),
): QualificationProject {
  return {
    ok: true,
    workflow_id: workflowId,
    project: { revision },
    graph: {
      bundle: {
        name: "Save a test note",
        action_count: 2,
        irreversible_count: 0,
        identity_armed_count: 0,
        identity_unarmed_count: 1,
        effect_count: 0,
        encrypted: false,
        provenance: { content_digest: digest },
      },
      nodes: [
        {
          id: "step-1",
          index: 0,
          kind: "action",
          title: "Enter the test note",
          effects: [],
          postconditions: [],
          halts: [],
          badges: [],
        },
        {
          id: "step-2",
          index: 1,
          kind: "action",
          title: "Save the note",
          effects: [],
          postconditions: [],
          halts: [],
          badges: [],
        },
      ],
      edges: [],
    },
    controls: {
      parameters: [
        {
          name: "note_text",
          type: "string",
          secret: false,
          required: true,
          example: null,
          choices: [],
        },
      ],
      actions: {
        "step-1": {
          step_id: "step-1",
          execution_paths: ["gui"],
          classification: {
            step_id: "step-1",
            classification: "read_only",
            explanation: "The action only reads the test page.",
            operator_confirmed: true,
          },
          identity: { can_arm: false, armed: false, sources: [] },
          effects: [],
        },
        "step-2": {
          step_id: "step-2",
          execution_paths: ["gui"],
          classification: {
            step_id: "step-2",
            classification: "read_only",
            explanation: "The action only reads the test page.",
            operator_confirmed: true,
          },
          identity: { can_arm: false, armed: false, sources: [] },
          effects: [],
        },
      },
      business_decisions: {
        available: false,
        required_flow_capability: "qualification.set_business_decision",
        graphs: [],
      },
      judgment_cases: {
        available: false,
        required_flow_capability: "qualification.set_judgment_cases",
        contexts: [],
        report: null,
      },
    },
  } as unknown as QualificationProject;
}

it("requires the first user to review the compiled workflow before a supervised run", async () => {
  const qualification = firstWorkflowReview();
  vi.mocked(engineTry).mockImplementation(async (command) => {
    if (command === CMD.GET_QUALIFICATION) return qualification;
    return null;
  });
  vi.mocked(engineInvoke).mockResolvedValue(preciseReport("VERIFIED"));
  const onQualify = vi.fn();

  render(
    <WatchRun
      workflowId="workflow-1"
      initialTarget={{ backend: "web", url: "https://example.test" }}
      firstWorkflow
      onQualify={onQualify}
      onTeach={() => {}}
    />,
  );

  expect(await screen.findByText("Enter the test note")).toBeTruthy();
  expect(screen.getByText("Save the note")).toBeTruthy();
  expect(screen.getByText("note_text")).toBeTruthy();
  expect(screen.getAllByText("read only")).toHaveLength(2);

  const runButton = screen.getByRole("button", {
    name: "Run once while I watch",
  }) as HTMLButtonElement;
  expect(runButton.disabled).toBe(true);

  fireEvent.click(
    screen.getByRole("checkbox", {
      name: "I reviewed these steps. The task uses test data, and I will keep the target app in view.",
    }),
  );
  expect(runButton.disabled).toBe(false);
  fireEvent.click(runButton);

  await waitFor(() =>
    expect(engineInvoke).toHaveBeenCalledWith("replay_first_workflow", {
      workflow_id: "workflow-1",
      target: { backend: "web", url: "https://example.test" },
      admission: {
        workflow_id: "workflow-1",
        project_revision: 4,
        bundle_content_digest: "a".repeat(64),
      },
    }),
  );
  fireEvent.click(
    await screen.findByRole("button", {
      name: "Review identity, effects, and policy",
    }),
  );
  expect(onQualify).toHaveBeenCalledWith(
    "workflow-1",
    true,
    { backend: "web", url: "https://example.test" },
  );
});

it("keeps the supervised run locked until its command returns", async () => {
  const qualification = firstWorkflowReview();
  let finishRun!: (report: ExecutionResponse) => void;
  const pendingRun = new Promise<ExecutionResponse>((resolve) => {
    finishRun = resolve;
  });
  vi.mocked(engineTry).mockImplementation(async (command) =>
    command === CMD.GET_QUALIFICATION ? qualification : null,
  );
  vi.mocked(engineInvoke).mockReturnValue(pendingRun);
  const onRunningChange = vi.fn();

  render(
    <WatchRun
      workflowId="workflow-1"
      initialTarget={{ backend: "web", url: "https://example.test" }}
      firstWorkflow
      onRunningChange={onRunningChange}
      onQualify={() => {}}
      onTeach={() => {}}
    />,
  );

  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(
    screen.getByRole("button", { name: "Run once while I watch" }),
  );
  await waitFor(() => expect(onRunningChange).toHaveBeenLastCalledWith(true));
  await waitFor(() => expect(watchEventMocks.handlers.has(EVT.REPLAY_PROGRESS)).toBe(true));

  act(() => {
    watchEventMocks.handlers.get(EVT.REPLAY_PROGRESS)?.({
      workflow_id: "workflow-1",
      state: "unknown",
      backend: "web",
    });
  });
  expect(onRunningChange).toHaveBeenLastCalledWith(true);

  await act(async () => {
    finishRun(preciseReport("VERIFIED"));
    await pendingRun;
  });
  await waitFor(() => expect(onRunningChange).toHaveBeenLastCalledWith(false));
});

it("requires a target check before an uncertain supervised replay can return to review", async () => {
  vi.mocked(engineTry).mockImplementation(async (command) =>
    command === CMD.GET_QUALIFICATION ? firstWorkflowReview() : null,
  );
  const onReconcile = vi.fn();

  render(
    <WatchRun
      workflowId="workflow-1"
      initialTarget={{ backend: "web", url: "https://example.test" }}
      firstWorkflow
      firstRunLocked
      onReconcile={onReconcile}
      onQualify={() => {}}
      onTeach={() => {}}
    />,
  );

  expect(
    await screen.findByText("Check the target before another attempt"),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Run once while I watch" }),
  ).toBeNull();
  fireEvent.click(
    screen.getByRole("button", {
      name: "I checked the target. Review again",
    }),
  );
  expect(onReconcile).toHaveBeenCalledWith("workflow-1");
});

it("routes an unreviewed or state-changing first action to qualification without replay", async () => {
  const qualification = firstWorkflowReview();
  qualification.controls.actions["step-2"].classification = {
    step_id: "step-2",
    classification: "state_changing",
    explanation: "The action changes application state.",
    operator_confirmed: true,
  };
  vi.mocked(engineTry).mockImplementation(async (command) =>
    command === CMD.GET_QUALIFICATION ? qualification : null,
  );
  const onQualify = vi.fn();

  render(
    <WatchRun
      workflowId="workflow-1"
      firstWorkflow
      onQualify={onQualify}
      onTeach={() => {}}
    />,
  );

  expect(await screen.findByText("state changing")).toBeTruthy();
  expect(
    screen.queryByRole("button", { name: "Run once while I watch" }),
  ).toBeNull();
  fireEvent.click(
    screen.getByRole("button", { name: "Review before running" }),
  );
  expect(onQualify).toHaveBeenCalledWith(
    "workflow-1",
    false,
    { backend: "web" },
  );
  expect(engineInvoke).not.toHaveBeenCalled();
});

it("clears the review acknowledgement when the workflow revision changes", async () => {
  const reviews = {
    "workflow-1": firstWorkflowReview("workflow-1", 4, "a".repeat(64)),
    "workflow-2": firstWorkflowReview("workflow-2", 5, "b".repeat(64)),
  };
  vi.mocked(engineTry).mockImplementation(async (command, params) => {
    if (command !== CMD.GET_QUALIFICATION) return null;
    return reviews[params.workflow_id as keyof typeof reviews];
  });
  const view = render(
    <WatchRun
      workflowId="workflow-1"
      firstWorkflow
      onQualify={() => {}}
      onTeach={() => {}}
    />,
  );

  const firstCheckbox = await screen.findByRole("checkbox");
  fireEvent.click(firstCheckbox);
  expect((firstCheckbox as HTMLInputElement).checked).toBe(true);

  view.rerender(
    <WatchRun
      workflowId="workflow-2"
      firstWorkflow
      onQualify={() => {}}
      onTeach={() => {}}
    />,
  );

  const nextCheckbox = await screen.findByRole("checkbox");
  expect((nextCheckbox as HTMLInputElement).checked).toBe(false);
  expect(
    (screen.getByRole("button", {
      name: "Run once while I watch",
    }) as HTMLButtonElement).disabled,
  ).toBe(true);
});

it("keeps an unsaved first result on screen until local history succeeds", async () => {
  const qualification = firstWorkflowReview();
  const degraded = {
    ...preciseReport("VERIFIED"),
    persistence: {
      state: "degraded" as const,
      retryable: true,
      message: "The report is visible, but local history is not saved.",
    },
  };
  const persisted = preciseReport("VERIFIED");
  vi.mocked(engineTry).mockImplementation(async (command) =>
    command === CMD.GET_QUALIFICATION ? qualification : null,
  );
  vi.mocked(engineInvoke)
    .mockResolvedValueOnce(degraded)
    .mockResolvedValueOnce({ ok: true, report: persisted });
  const onPersistencePendingChange = vi.fn();

  render(
    <WatchRun
      workflowId="workflow-1"
      firstWorkflow
      onPersistencePendingChange={onPersistencePendingChange}
      onQualify={() => {}}
      onTeach={() => {}}
    />,
  );

  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(
    screen.getByRole("button", { name: "Run once while I watch" }),
  );
  expect(
    await screen.findByText("Local run history needs attention"),
  ).toBeTruthy();
  expect(
    screen.queryByRole("button", {
      name: "Review identity, effects, and policy",
    }),
  ).toBeNull();
  expect(onPersistencePendingChange).toHaveBeenLastCalledWith(true);

  fireEvent.click(
    screen.getByRole("button", { name: "Retry local history save" }),
  );
  expect(
    await screen.findByRole("button", {
      name: "Review identity, effects, and policy",
    }),
  ).toBeTruthy();
  expect(onPersistencePendingChange).toHaveBeenLastCalledWith(false);
});
