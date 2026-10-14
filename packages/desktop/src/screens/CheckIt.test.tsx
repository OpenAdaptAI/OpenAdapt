import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn(), engineInvoke: vi.fn() };
});

import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { CheckIt } from "./CheckIt";

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

type Case = { id: string; kind: string; status: "passed" | null };

function project({
  draft = false,
  confirmed = true,
  armed = true,
  tier = 1 as number | null,
  cases = [] as Case[],
  certified = false,
  encrypted = false,
} = {}) {
  return {
    ok: true,
    workflow_id: "wf-1",
    policy: "clinical-write",
    qualification_schema: "openadapt.qualification-project/v1",
    migration_required: false,
    draft_environment: draft,
    project: {
      revision: 3,
      cases: cases.map((item) => ({
        id: item.id,
        kind: item.kind,
        description: "",
        expected_outcome: "",
        required: true,
        results: item.status
          ? [{ project_revision: 3, runner_capabilities: [], status: item.status, observed_outcome: "", evidence: [] }]
          : [],
      })),
    },
    report: { refusals: [] },
    certification_current: certified,
    graph: {
      bundle: { name: "Add a claim note", encrypted, provenance: {} },
      nodes: [
        { id: "search", index: 0, kind: "action", title: "type <member_id>", effects: [], postconditions: [], halts: [], badges: [], risk: "reversible" },
        { id: "save", index: 1, kind: "action", title: "click 'Save'", effects: [], postconditions: [], halts: [], badges: [], risk: "irreversible" },
      ],
      edges: [],
    },
    controls: {
      parameters: [],
      actions: {
        save: {
          step_id: "save",
          execution_paths: ["gui"],
          classification: { step_id: "save", classification: "irreversible", explanation: "", operator_confirmed: confirmed },
          identity: {
            can_arm: true,
            armed,
            sources: [],
            policy: armed
              ? { step_id: "save", enforcement: "signal_quorum", quorum: 1, signals: [{ key: "record_id", source: "structured", match: "exact", normalizers: [], params: ["member_id"] }] }
              : null,
          },
          effects: [
            {
              index: 0,
              kind: "field_equals",
              match: { record_id: { source: "parameter", value: "member_id" } },
              field: "status_note",
              value: { source: "parameter", value: "note" },
              expected_count: 1,
              key_field: "id",
              count_new_only: false,
              risk: "irreversible",
              needs_operator_confirmation: false,
              verification_tier: tier,
              effect_contract_hash: "x",
            },
          ],
        },
      },
      business_decisions: { available: false, graphs: [] },
      judgment_cases: { available: false, contexts: [], report: null },
    },
  };
}

function engine(initial: ReturnType<typeof project>) {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_WORKFLOWS) return [{ id: "wf-1", name: "Add a claim note", steps: 2 }];
    if (command === CMD.GET_TASK_INPUTS) {
      return {
        ok: true,
        workflow_id: "wf-1",
        inputs: [
          { name: "member_id", type: "string", secret: false, required: true, example: "M-1", choices: [] },
          { name: "note", type: "string", secret: false, required: true, example: "Reviewed", choices: [] },
        ],
        target: { backend: "web", url: "https://emr.example.test" },
      };
    }
    if (command === CMD.PROPOSE_QUALIFICATION_FROM_DEMO) {
      return { ok: true, proposal: { status: "draft", pins: [] } };
    }
    return fallback;
  });
  vi.mocked(engineInvoke).mockImplementation(async (command) => {
    if (command === CMD.GET_QUALIFICATION) return initial;
    throw new Error(`unexpected ${command}`);
  });
}

it("says what the task changes and how it checks, in plain words", async () => {
  engine(project());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText("Click “Save”")).toBeTruthy();
  expect(
    screen.getByText(
      "Sets status note on the record for the member ID you give it to the note you give it.",
    ),
  ).toBeTruthy();
  expect(screen.getByText(/matches the member ID you give it/)).toBeTruthy();
  expect(screen.getByText(/reads the record back from your system's records/)).toBeTruthy();
  // The search step has no reviewed classification, so it isn't counted among
  // the steps that only read.
  expect(screen.queryByText(/only reads? or move/)).toBeNull();
  expect(screen.getByText(/can't tell yet whether 1 other step changes anything/)).toBeTruthy();
});

it("counts a step as read-only only when it is classified read-only", async () => {
  const reviewed = project();
  (reviewed.controls.actions as Record<string, unknown>).search = {
    step_id: "search",
    execution_paths: ["gui"],
    classification: { step_id: "search", classification: "read_only", explanation: "", operator_confirmed: true },
    identity: { can_arm: false, armed: false, sources: [], policy: null },
    effects: [],
  };
  engine(reviewed);
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/1 other step only reads or moves around in the app/)).toBeTruthy();
  expect(screen.queryByText(/can't tell yet/)).toBeNull();
});

/** The claims program as the old engine compiled it: every step reversible,
 * no step with an effect, Save included, and no reviewed classification. */
function unprovenProgram() {
  const base = project({ confirmed: false });
  base.graph.nodes = [
    { id: "step_000", index: 0, kind: "action", title: "type <insurance_no>", effects: [], postconditions: [], halts: [], badges: [], risk: "reversible" },
    { id: "step_008", index: 1, kind: "action", title: "scroll by (0, 400)", effects: [], postconditions: [], halts: [], badges: [], risk: "reversible" },
    { id: "step_012", index: 2, kind: "action", title: "click '日'", effects: [], postconditions: [], halts: [], badges: [], risk: "reversible" },
  ];
  const unknown = (id: string) => ({
    step_id: id,
    execution_paths: ["gui"],
    classification: { step_id: id, classification: "unknown", explanation: "", operator_confirmed: false },
    identity: { can_arm: false, armed: false, sources: [], policy: null },
    effects: [],
  });
  base.controls.actions = {
    step_000: unknown("step_000"),
    step_008: {
      ...unknown("step_008"),
      classification: { step_id: "step_008", classification: "read_only", explanation: "", operator_confirmed: true },
    },
    step_012: unknown("step_012"),
  } as unknown as typeof base.controls.actions;
  return base;
}

it("never calls a task read-only while a step's effect is unknown", async () => {
  engine(unprovenProgram());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/can't tell yet whether this task saves anything/)).toBeTruthy();
  expect(screen.queryByText(/This task only reads/)).toBeNull();
  expect(screen.getByText(/2 steps might change something/)).toBeTruthy();
  expect(screen.getByText("Type the insurance no")).toBeTruthy();
  expect(screen.getByText("Click “日”")).toBeTruthy();
  // Parts 1-3 ask for an answer instead of saying "Not needed", and nobody is
  // offered a button to vouch for an answer OpenAdapt doesn't have.
  expect(screen.getAllByText("Needs an answer").length).toBeGreaterThanOrEqual(3);
  expect(screen.queryByText("Not needed")).toBeNull();
  expect(screen.queryByRole("button", { name: "These look right" })).toBeNull();
  const run = screen.getByRole("button", { name: "Run the practice item" });
  expect((run as HTMLButtonElement).disabled).toBe(true);
});

it("says a task only reads when every step is proven read-only", async () => {
  const readOnly = unprovenProgram();
  readOnly.graph.nodes = [readOnly.graph.nodes[1]];
  engine(readOnly);
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/This task only reads/)).toBeTruthy();
});

it("lists a save the program declares without an effect and asks how to check it", async () => {
  // The fixed engine: the icon-only Save is irreversible with no effect yet.
  const fixed = unprovenProgram();
  fixed.graph.nodes[2] = { ...fixed.graph.nodes[2], title: "click recorded visual target", risk: "irreversible" };
  engine(fixed);
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.GET_QUALIFICATION) return fixed;
    if (command === CMD.SET_QUALIFICATION_RISK) {
      // Confirming must not record the save as weaker than Flow's floor.
      expect(params).toEqual(expect.objectContaining({ step_id: "step_012", risk: "irreversible" }));
      return fixed;
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText("Click the spot you clicked when you showed it")).toBeTruthy();
  expect(screen.getByText("OpenAdapt doesn't know yet what this step saves.")).toBeTruthy();
  expect(screen.getByText("OpenAdapt doesn't know yet how to confirm this save.")).toBeTruthy();
  expect(screen.queryByText(/This task only reads/)).toBeNull();
  expect(screen.getByText(/can't tell yet whether 1 other step changes anything/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "These look right" }));
  await waitFor(() =>
    expect(vi.mocked(engineInvoke)).toHaveBeenCalledWith(
      CMD.SET_QUALIFICATION_RISK,
      expect.objectContaining({ risk: "irreversible" }),
    ),
  );
});

it("accepts the recording's proposal when nothing is confirmed yet", async () => {
  const draft = project({ draft: true, confirmed: false });
  engine(draft);
  const accepted = project();
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.GET_QUALIFICATION) return draft;
    if (command === CMD.PROPOSE_QUALIFICATION_FROM_DEMO) {
      expect(params).toEqual(
        expect.objectContaining({ workflow_id: "wf-1", accept: true, policy_pack: "community" }),
      );
      return accepted;
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  fireEvent.click(await screen.findByRole("button", { name: "These look right" }));

  await waitFor(() => expect(screen.getAllByText("Confirmed").length).toBe(3));
});

it("runs the practice item on test values from the recording", async () => {
  engine(project());
  const after = project({ cases: [{ id: "practice-1", kind: "representative", status: "passed" }] });
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.GET_QUALIFICATION) return project();
    if (command === CMD.ADD_QUALIFICATION_CASE) {
      expect(params).toEqual(
        expect.objectContaining({
          case_id: "practice-1",
          kind: "representative",
          parameters_json: JSON.stringify({ member_id: "M-1", note: "Reviewed" }),
        }),
      );
      return project({ cases: [{ id: "practice-1", kind: "representative", status: null }] });
    }
    if (command === CMD.RUN_QUALIFICATION_CASE) {
      expect(params).toEqual(
        expect.objectContaining({ case_id: "practice-1", target: { backend: "web", url: "https://emr.example.test" } }),
      );
      return { ...after, case_run: { outcome: "VERIFIED", transaction_outcome: "VERIFIED" } };
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  const run = await screen.findByRole("button", { name: "Run the practice item" });
  expect((run as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(
    screen.getByRole("checkbox", {
      name: "These are test values, and I'll keep the app in view while it runs.",
    }),
  );
  fireEvent.click(run);

  expect(await screen.findByText(/Done and checked. It saved the entry/)).toBeTruthy();
});

it("approves, locks the version, and offers Run it", async () => {
  const ready = project({
    cases: [
      { id: "practice-1", kind: "representative", status: "passed" },
      { id: "fault-identity-swap", kind: "wrong_identity", status: "passed" },
    ],
  });
  engine(ready);
  vi.mocked(engineInvoke).mockImplementation(async (command) => {
    if (command === CMD.GET_QUALIFICATION) return ready;
    if (command === CMD.CERTIFY_QUALIFICATION) return { ...ready, certification_current: true };
    if (command === CMD.SEAL_QUALIFICATION_WORKFLOW) return { ok: true, workflow_id: "wf-1-v2" };
    throw new Error(`unexpected ${command}`);
  });
  const onRun = vi.fn();
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={onRun} />);

  fireEvent.click(await screen.findByRole("button", { name: "Approve for use" }));

  expect(await screen.findByText("Approved and locked")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Run it" }));
  expect(onRun).toHaveBeenCalledWith("wf-1-v2");
});

it("keeps approval off until the practice item passed", async () => {
  engine(project());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);
  const approve = await screen.findByRole("button", { name: "Approve for use" });
  expect((approve as HTMLButtonElement).disabled).toBe(true);
});

it("asks plain questions when the recording can't show how to confirm the save", async () => {
  const missing = project({ tier: null });
  missing.controls.actions.save.effects = [];
  engine(missing);
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.GET_QUALIFICATION) return missing;
    if (command === CMD.BIND_QUALIFICATION_EFFECT) {
      expect(params).toEqual(
        expect.objectContaining({
          step_id: "save",
          kind: "field_equals",
          match_param: "member_id",
          match_field: "member_id",
          field: "status_note",
          value_param: "note",
          verification_tier: 2,
        }),
      );
      return project({ tier: 2 });
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText("Needs an answer")).toBeTruthy();
  fireEvent.change(screen.getByLabelText("What is that ID called in your system's records?"), {
    target: { value: "member_id" },
  });
  fireEvent.change(screen.getByLabelText("Which field gets the new value?"), {
    target: { value: "status_note" },
  });
  fireEvent.click(screen.getByLabelText(/Read it back in a separate session/));
  fireEvent.click(screen.getByRole("button", { name: "Save how it confirms the save" }));

  expect(
    await screen.findByText(/opens the record again in a separate session and reads it/),
  ).toBeTruthy();
});

it("runs one drill at a time in the signed-in browser", async () => {
  const ready = project({
    cases: [
      { id: "practice-1", kind: "representative", status: "passed" },
      { id: "fault-identity-swap", kind: "wrong_identity", status: null },
      { id: "fault-missing-effect", kind: "missing_effect", status: null },
    ],
  });
  engine(ready);
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) => {
    if (command === CMD.GET_WORKFLOWS) return [{ id: "wf-1", name: "Add a claim note", steps: 2 }];
    if (command === CMD.GET_TASK_INPUTS) {
      return {
        ok: true,
        workflow_id: "wf-1",
        inputs: [{ name: "member_id", type: "string", secret: false, required: true, example: "M-1", choices: [] }],
        target: { backend: "web", url: "http://127.0.0.1:9401/front/", browser_cdp_endpoint: "http://127.0.0.1:9334" },
      };
    }
    return fallback;
  });
  const ran: string[] = [];
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.GET_QUALIFICATION) return ready;
    if (command === CMD.RUN_QUALIFICATION_CASE) {
      const call = params as { case_id: string; target: { browser_cdp_endpoint?: string } };
      expect(call.target.browser_cdp_endpoint).toBe("http://127.0.0.1:9334");
      ran.push(call.case_id);
      return ready;
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/in your signed-in browser/)).toBeTruthy();
  expect(screen.getByText(/Open the screen where the task starts before you run it/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Run the next drill" }));

  expect(await screen.findByText(/then run the next drill/)).toBeTruthy();
  expect(ran).toEqual(["fault-identity-swap"]);
});
