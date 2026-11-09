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

it("says what the workflow changes and how it checks, in plain words", async () => {
  engine(project());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText("Click “Save”")).toBeTruthy();
  expect(
    screen.getByText(
      "Sets the status note to the note you give it.",
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

it("never calls a workflow read-only while a step's effect is unknown", async () => {
  engine(unprovenProgram());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/can't tell yet whether this workflow saves anything/)).toBeTruthy();
  expect(screen.queryByText(/This workflow only reads/)).toBeNull();
  expect(screen.getByText(/2 steps might change something/)).toBeTruthy();
  expect(screen.getByText("Type the insurance no")).toBeTruthy();
  expect(screen.getByText("Click “日”")).toBeTruthy();
  // Parts 1-3 ask for an answer instead of saying "Not needed", and nobody is
  // offered a button to vouch for an answer OpenAdapt doesn't have.
  expect(screen.getAllByText("Needs an answer").length).toBeGreaterThanOrEqual(3);
  expect(screen.queryByText("Not needed")).toBeNull();
  expect(screen.queryByRole("button", { name: "These look right" })).toBeNull();
  const run = screen.getByRole("button", { name: "Yes, practice once with these test values" });
  expect((run as HTMLButtonElement).disabled).toBe(true);
});

it("says a workflow only reads when every step is proven read-only", async () => {
  const readOnly = unprovenProgram();
  readOnly.graph.nodes = [readOnly.graph.nodes[1]];
  engine(readOnly);
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/This workflow only reads/)).toBeTruthy();
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
  expect(screen.queryByText(/This workflow only reads/)).toBeNull();
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
    if (command === CMD.SEAL_QUALIFICATION_WORKFLOW) return { ok: true, workflow_id: "wf-1-v2-a1" };
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

  const run = await screen.findByRole("button", { name: "Yes, practice once with these test values" });
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

  fireEvent.click(await screen.findByRole("button", { name: "Approve this workflow" }));

  expect(await screen.findByText("Approved and locked")).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Run it" }));
  expect(onRun).toHaveBeenCalledWith("wf-1-v2");
});

it("keeps approval off until the practice item passed", async () => {
  engine(project());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);
  const approve = await screen.findByRole("button", { name: "Approve this workflow" });
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
  expect(screen.getByText(/Open the\s+screen where the workflow starts first/)).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "Run the next drill" }));

  expect(await screen.findByText(/then run the next drill/)).toBeTruthy();
  expect(ran).toEqual(["fault-identity-swap"]);
});

it("counts nothing when the practice save wasn't approved, and passes IT's settings file", async () => {
  engine(project());
  const calls: Record<string, unknown>[] = [];
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.SEAL_QUALIFICATION_WORKFLOW) return { ok: true, workflow_id: "wf-1-v2-a1" };
    if (command === CMD.GET_QUALIFICATION) return project();
    if (command === CMD.ADD_QUALIFICATION_CASE) {
      return project({ cases: [{ id: "practice-1", kind: "representative", status: null }] });
    }
    if (command === CMD.RUN_QUALIFICATION_CASE) {
      calls.push(params as Record<string, unknown>);
      return {
        ok: false,
        workflow_id: "wf-1",
        not_approved: true,
        error:
          "Nobody approved the save in time, so nothing was saved and the practice run didn't count. Run the practice item again and approve the save when OpenAdapt asks.",
        case_run: { outcome: "HALTED", transaction_outcome: "HALTED_BEFORE_EFFECT" },
      };
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  const run = await screen.findByRole("button", { name: "Yes, practice once with these test values" });
  expect(screen.getByText(/shows you the filled form and waits for\s+your OK first/)).toBeTruthy();
  fireEvent.change(screen.getByLabelText("Deployment settings file (for IT)"), {
    target: { value: "/tmp/deployment.claims.yaml" },
  });
  expect(screen.getByText(/with your deployment settings file/)).toBeTruthy();
  fireEvent.click(run);

  expect(await screen.findByText(/Nobody approved the save in time/)).toBeTruthy();
  expect(screen.queryByRole("alert")).toBeNull();
  expect(calls[0]).toEqual(expect.objectContaining({ deployment_config: "/tmp/deployment.claims.yaml" }));
});

it("describes each drill as the change Flow makes before the save", async () => {
  const ready = project({
    cases: [
      { id: "practice-1", kind: "representative", status: "passed" },
      { id: "drill-wrong-record-save", kind: "wrong_identity", status: null },
      { id: "drill-two-matches-save", kind: "ambiguity", status: null },
      { id: "drill-record-changed-save", kind: "stale_identity", status: null },
      { id: "drill-save-not-confirmed-save", kind: "missing_effect", status: null },
      { id: "drill-weak-record-check-save", kind: "weak_effect", status: null },
    ],
  });
  engine(ready);
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/a drill never saves/)).toBeTruthy();
  for (const label of [
    "Wrong record",
    "Two possible matches",
    "Record changed during the run",
    "No record check",
    "Weak record check",
  ]) {
    expect(screen.getByText(label)).toBeTruthy();
  }
  expect(screen.getByText(/A second copy of the save button appears/)).toBeTruthy();
  expect(screen.getByText(/The record check is taken away/)).toBeTruthy();
});

it("before the check starts, says drills come with the practice item and the record check is known", async () => {
  // The recorded program declares its record checks; no project exists yet.
  const fresh = project({ tier: null });
  (fresh as { project: unknown }).project = null;
  engine(fresh);
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText(/runs 5 safety drills\s+on its own/)).toBeTruthy();
  expect(screen.queryByText("Not needed")).toBeNull();
  expect(screen.getByText(/will read the saved record back to check the\s+entries listed in part 1/)).toBeTruthy();
  expect(screen.queryByText(/didn't see where this save lands/)).toBeNull();
});

it("says where and why a practice run that ran didn't pass", async () => {
  engine(project());
  const failed = project({ cases: [{ id: "practice-1", kind: "representative", status: "failed" as never }] });
  vi.mocked(engineInvoke).mockImplementation(async (command) => {
    if (command === CMD.SEAL_QUALIFICATION_WORKFLOW) return { ok: true, workflow_id: "wf-1-v2-a1" };
    if (command === CMD.GET_QUALIFICATION) return project();
    if (command === CMD.ADD_QUALIFICATION_CASE) {
      return project({ cases: [{ id: "practice-1", kind: "representative", status: null }] });
    }
    if (command === CMD.RUN_QUALIFICATION_CASE) {
      return {
        ...failed,
        stop_reason: "observe exact qualification environment: qualification environment observer does not match the project",
        case_run: {
          outcome: "HALTED",
          transaction_outcome: "HALTED_BEFORE_EFFECT",
          halt: { step_index: 0, step_intent: "type <member_id>", reason: "qualification environment observer does not match the project" },
        },
      };
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  const run = await screen.findByRole("button", { name: "Yes, practice once with these test values" });
  fireEvent.click(run);

  expect(await screen.findByText("The practice run didn't pass.")).toBeTruthy();
  expect(
    screen.getByText(/observe exact qualification environment: qualification environment observer does not match the project/),
  ).toBeTruthy();
});

function learnedSetup(answers: Record<string, unknown> = {}) {
  return {
    ok: true,
    workflow_id: "wf-1",
    record_check: {
      found: true,
      read: "GET /api/db",
      checked: ["note", "patient_id"],
      values_checked: ["note"],
      fixed_fields: { patient_id: "p1" },
      idempotency_field: "key",
      fault_test: {
        bad_saves: 8,
        caught: 5,
        caught_kinds: ["nothing saved"],
        missed_kinds: ["saved on another record"],
      },
    },
    saving_steps: ["save"],
    quiet_steps: { write_requests: 1, evidence: "In your recording the app sent data to its server only when you saved.", step_ids: ["search"] },
    unproven_steps: ["search"],
    values: [
      { name: "member_id", example: "M-1", changes: true, why: "You typed it while recording." },
      { name: "note", example: "Reviewed", changes: true, why: "You typed it while recording." },
    ],
    recordings: 1,
    answers,
    fixed_values: {},
  };
}

function guidedEngine(initial: ReturnType<typeof project>, setup: ReturnType<typeof learnedSetup>) {
  engine(initial);
  const base = vi.mocked(engineTry).getMockImplementation()!;
  vi.mocked(engineTry).mockImplementation(async (command, params, fallback) => {
    if (command === CMD.GET_CHECK_SETUP) return setup;
    return base(command, params, fallback);
  });
}

it("asks three things when OpenAdapt learned the record check, and shows the rest once", async () => {
  guidedEngine(project({ confirmed: false, armed: false, tier: null }), learnedSetup());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText("Is this the right record check?")).toBeTruthy();
  expect(screen.getByText("Which values change per item?")).toBeTruthy();
  expect(screen.getByText("Approve?")).toBeTruthy();
  expect(screen.getByText("GET /api/db")).toBeTruthy();
  expect(screen.getByText(/the note you give it and patient ID p1/)).toBeTruthy();
  // What it can't catch is said, not hidden.
  expect(screen.getByText(/caught 5 of 8 kinds of bad save\. Not caught: saved on another record\./)).toBeTruthy();
  expect(screen.getByText(/only when you saved/)).toBeTruthy();
  // The long parts aren't asked.
  expect(screen.queryByText("What it changes")).toBeNull();
  expect(screen.queryByText("How it confirms the save")).toBeNull();
});

it("a yes sends one answer, from the person, and Change opens the parts", async () => {
  guidedEngine(project({ confirmed: false, armed: false, tier: null }), learnedSetup());
  const sent: Record<string, unknown>[] = [];
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    if (command === CMD.GET_QUALIFICATION) {
      return sent.length ? project() : project({ confirmed: false, armed: false, tier: null });
    }
    if (command === CMD.ANSWER_CHECK_SETUP) {
      sent.push(params as Record<string, unknown>);
      return learnedSetup({ record_check: { by: "person", at: "2026-10-19T00:00:00Z", answer: "yes" } });
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  fireEvent.click(await screen.findByRole("button", { name: "Yes, use this check" }));
  expect(await screen.findByText("You answered yes.")).toBeTruthy();
  expect(sent).toEqual([
    { workflow_id: "wf-1", question: "record_check", answer: "yes", answered_by: "person" },
  ]);
});

it("Change shows the plain parts instead of the learned check", async () => {
  guidedEngine(project({ confirmed: false, armed: false, tier: null }), learnedSetup());
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  fireEvent.click(await screen.findByRole("button", { name: "Change" }));
  expect(await screen.findByText("What it changes")).toBeTruthy();
  expect(screen.getByText("How it confirms the save")).toBeTruthy();
  expect(screen.queryByText("Is this the right record check?")).toBeNull();
});

it("answers which values change, then runs the practice item", async () => {
  guidedEngine(project(), learnedSetup({ record_check: { by: "person", at: "x", answer: "yes" } }));
  const order: string[] = [];
  const ids: string[] = [];
  let changes: unknown;
  vi.mocked(engineInvoke).mockImplementation(async (command, params) => {
    order.push(command);
    ids.push((params as { workflow_id?: string } | undefined)?.workflow_id ?? "");
    if (command === CMD.SEAL_QUALIFICATION_WORKFLOW) return { ok: true, workflow_id: "wf-1-v2-a1" };
    if (command === CMD.GET_QUALIFICATION) return project();
    if (command === CMD.ANSWER_CHECK_SETUP) {
      changes = (params as { changes: unknown }).changes;
      return learnedSetup();
    }
    if (command === CMD.ADD_QUALIFICATION_CASE) {
      return project({ cases: [{ id: "practice-1", kind: "representative", status: null }] });
    }
    if (command === CMD.RUN_QUALIFICATION_CASE) {
      return {
        ...project({ cases: [{ id: "practice-1", kind: "representative", status: "passed" }] }),
        case_run: { outcome: "VERIFIED", transaction_outcome: "VERIFIED" },
      };
    }
    throw new Error(`unexpected ${command}`);
  });
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  // The member ID is the same on every item.
  fireEvent.click(await screen.findByRole("checkbox", { name: /Member ID/ }));
  fireEvent.click(screen.getByRole("button", { name: "Yes, practice once with these test values" }));

  expect(await screen.findByText(/Done and checked/)).toBeTruthy();
  expect(changes).toEqual(["note"]);
  const steps = order
    .map((command, index) => [command, ids[index]])
    .filter(([command]) => command !== CMD.GET_QUALIFICATION);
  // The version is sealed before the practice item, and the practice item
  // runs on the sealed version: that is the version approved and run.
  expect(steps.slice(0, 4)).toEqual([
    [CMD.ANSWER_CHECK_SETUP, "wf-1"],
    [CMD.SEAL_QUALIFICATION_WORKFLOW, "wf-1"],
    [CMD.ADD_QUALIFICATION_CASE, "wf-1-v2-a1"],
    [CMD.RUN_QUALIFICATION_CASE, "wf-1-v2-a1"],
  ]);
});

it("says when the person's agent answered, and leaves approval to the person", async () => {
  guidedEngine(
    project(),
    learnedSetup({
      record_check: { by: "agent", at: "x", answer: "yes" },
      values: { by: "agent", at: "x", changes: ["note"] },
    }),
  );
  render(<CheckIt workflowId="wf-1" onExactChecks={() => {}} onRun={() => {}} />);

  expect(await screen.findByText("Your agent answered for you")).toBeTruthy();
  expect(screen.getByText("Your agent answered yes.")).toBeTruthy();
  const approve = screen.getByRole("button", { name: "Approve this workflow" });
  expect((approve as HTMLButtonElement).disabled).toBe(true);
});
