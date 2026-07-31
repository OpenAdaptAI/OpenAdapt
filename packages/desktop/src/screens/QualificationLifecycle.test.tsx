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
});
