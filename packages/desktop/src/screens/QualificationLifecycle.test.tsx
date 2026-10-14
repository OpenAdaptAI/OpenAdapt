import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CMD, engineInvoke } from "../lib/engine";
import type { QualificationProject } from "../lib/types";
import { QualificationLifecycle } from "./QualificationLifecycle";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineInvoke: vi.fn() };
});

const mockedEngineInvoke = vi.mocked(engineInvoke);

function project(): QualificationProject {
  return {
    ok: true,
    workflow_id: "wf-1",
    certification_current: true,
    project: {
      revision: 4,
      cases: [
        {
          id: "representative-1",
          kind: "representative",
          description: "Representative case",
          expected_outcome: "verified",
          required: true,
          results: [],
        },
      ],
      environment: {
        target_kind: "web",
        environment_digest: "a".repeat(64),
      },
    },
    report: { case_count: 1, passed_case_count: 0 },
    graph: { bundle: { encrypted: true } },
    controls: {
      parameters: [
        {
          name: "record_id",
          type: "string",
          secret: false,
          required: true,
          example: null,
          choices: [],
        },
        {
          name: "amount",
          type: "number",
          secret: false,
          required: true,
          example: null,
          choices: [],
        },
        {
          name: "api_token",
          type: "string",
          secret: true,
          required: true,
          example: null,
          choices: [],
        },
        {
          name: "priority",
          type: "enum",
          secret: false,
          required: true,
          example: "routine",
          choices: ["routine", "urgent"],
        },
      ],
      actions: {},
    },
  } as unknown as QualificationProject;
}

function twoActionFaultProject(): QualificationProject {
  const value = project();
  value.project!.cases.push({
    id: "fault-wrong-identity",
    kind: "wrong_identity",
    description: "Deterministic wrong identity refusal case",
    expected_outcome: "halted",
    required: true,
    results: [],
  });
  value.controls.actions = {
    open: {
      step_id: "open",
      execution_paths: ["gui"],
      identity: { can_arm: false, armed: false, sources: [], policy: null },
      effects: [],
    },
    save: {
      step_id: "save",
      execution_paths: ["api", "gui"],
      identity: { can_arm: false, armed: false, sources: [], policy: null },
      effects: [],
    },
  };
  return value;
}

describe("Qualification lifecycle", () => {
  beforeEach(() => {
    mockedEngineInvoke.mockReset();
    mockedEngineInvoke.mockImplementation(async (command) => {
      if (command === CMD.GET_CAPABILITIES) return null;
      if (command === CMD.VERSION_QUALIFICATION_WORKFLOW) {
        return { ok: true, workflow_id: "wf-2" };
      }
      return project();
    });
  });
  afterEach(cleanup);

  it("runs the selected case through the sidecar and opens immutable versions", async () => {
    const onOpenWorkflow = vi.fn();
    render(
      <QualificationLifecycle
        workflowId="wf-1"
        project={project()}
        onProject={() => {}}
        onOpenWorkflow={onOpenWorkflow}
      />,
    );

    fireEvent.change(screen.getByLabelText("record id"), {
      target: { value: "CASE-42" },
    });
    fireEvent.change(screen.getByLabelText("amount"), {
      target: { value: "75.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run and sign case" }));
    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.RUN_QUALIFICATION_CASE,
        expect.objectContaining({
          workflow_id: "wf-1",
          case_id: "representative-1",
          parameters_json: JSON.stringify({
            record_id: "CASE-42",
            amount: 75.5,
            priority: "routine",
          }),
          target: { backend: "web" },
        }),
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Create working version" }));
    await waitFor(() => expect(onOpenWorkflow).toHaveBeenCalledWith("wf-2"));
  });

  it("prefills a typed fault case instead of requiring a hand-written case id", async () => {
    render(
      <QualificationLifecycle
        workflowId="wf-1"
        project={project()}
        onProject={() => {}}
        onOpenWorkflow={() => {}}
      />,
    );

    fireEvent.click(screen.getByText("Add another qualification case"));
    fireEvent.click(screen.getByRole("button", { name: "Use Wrong record" }));

    expect((screen.getByLabelText("Case id") as HTMLInputElement).value).toBe(
      "wrong-identity-1",
    );
    expect((screen.getByLabelText("Case type") as HTMLSelectElement).value).toBe(
      "wrong_identity",
    );
    expect((screen.getByLabelText("Description") as HTMLInputElement).value).toBe(
      "The live record does not match the qualified identity; the run must halt.",
    );

    fireEvent.change(screen.getByLabelText("record id"), {
      target: { value: "CASE-42" },
    });
    fireEvent.change(screen.getByLabelText("amount"), {
      target: { value: "75.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add case" }));
    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.ADD_QUALIFICATION_CASE,
        expect.objectContaining({
          workflow_id: "wf-1",
          case_id: "wrong-identity-1",
          kind: "wrong_identity",
          description:
            "The live record does not match the qualified identity; the run must halt.",
          parameters_json: JSON.stringify({
            record_id: "CASE-42",
            amount: 75.5,
            priority: "routine",
          }),
        }),
      ),
    );
  });

  it("reuses a default fault case and sends its exact target for two actions", async () => {
    const value = twoActionFaultProject();
    render(
      <QualificationLifecycle
        workflowId="wf-1"
        project={value}
        onProject={() => {}}
        onOpenWorkflow={() => {}}
      />,
    );

    fireEvent.click(screen.getByText("Add another qualification case"));
    fireEvent.click(screen.getByRole("button", { name: "Select Wrong record" }));
    expect(screen.getByText("Run fault-wrong-identity")).toBeTruthy();
    expect(mockedEngineInvoke).not.toHaveBeenCalledWith(
      CMD.ADD_QUALIFICATION_CASE,
      expect.anything(),
    );

    fireEvent.change(screen.getByLabelText("Fault action"), {
      target: { value: "save" },
    });
    fireEvent.change(screen.getByLabelText("Actuation path"), {
      target: { value: "api" },
    });
    fireEvent.change(screen.getByLabelText("record id"), {
      target: { value: "CASE-42" },
    });
    fireEvent.change(screen.getByLabelText("amount"), {
      target: { value: "75.5" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Run and sign case" }));

    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.RUN_QUALIFICATION_CASE,
        expect.objectContaining({
          workflow_id: "wf-1",
          case_id: "fault-wrong-identity",
          fault_target: { step_id: "save", actuation_path: "api" },
        }),
      ),
    );
  });

  function deployProject(
    overrides: {
      certified?: boolean;
      encrypted?: boolean;
      covered?: boolean;
    } = {},
  ): QualificationProject {
    const value = project();
    value.certification_current = overrides.certified ?? true;
    value.graph.bundle.encrypted = overrides.encrypted ?? true;
    value.capability_coverage = {
      required: ["web"],
      observed: overrides.covered === false ? [] : ["web"],
      missing: overrides.covered === false ? ["web"] : [],
      satisfied: overrides.covered ?? true,
      cases: [],
    };
    return value;
  }

  function renderDeploy(value: QualificationProject) {
    render(
      <QualificationLifecycle
        workflowId="wf-1"
        project={value}
        onProject={() => {}}
        onOpenWorkflow={() => {}}
      />,
    );
    return screen.getByRole("button", {
      name: "Deploy qualified artifact",
    }) as HTMLButtonElement;
  }

  it.each([
    [
      "the version is not sealed",
      { encrypted: false },
      "Seal and encrypt this version before you deploy it.",
    ],
    [
      "required capabilities are not covered",
      { covered: false },
      "Run cases that cover every required runner capability before you deploy.",
    ],
    [
      "the exact version is not certified",
      { certified: false },
      "Certify this exact version before you deploy it.",
    ],
  ])("disables deploy and says why when %s", (_label, overrides, reason) => {
    const deploy = renderDeploy(deployProject(overrides));

    expect(deploy.disabled).toBe(true);
    expect(deploy.className).not.toContain("btn-primary");
    expect(screen.getByText(reason)).toBeTruthy();
    expect(deploy.getAttribute("aria-describedby")).toBe(
      screen.getByText(reason).id,
    );
    fireEvent.click(deploy);
    expect(mockedEngineInvoke).not.toHaveBeenCalledWith(
      CMD.DEPLOY_QUALIFICATION_WORKFLOW,
      expect.anything(),
    );
  });

  it("offers deploy as the primary action once the sealed version is certified", async () => {
    const deploy = renderDeploy(deployProject());

    expect(deploy.disabled).toBe(false);
    expect(deploy.className).toContain("btn-primary");
    expect(deploy.getAttribute("aria-describedby")).toBeNull();
    fireEvent.click(deploy);
    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.DEPLOY_QUALIFICATION_WORKFLOW,
        { workflow_id: "wf-1" },
      ),
    );
  });
});
