import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CMD, engineInvoke } from "../lib/engine";
import type { QualificationProject } from "../lib/types";
import { BusinessDecisionAuthoring } from "./BusinessDecisionAuthoring";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineInvoke: vi.fn() };
});

const mockedEngineInvoke = vi.mocked(engineInvoke);

function project(): QualificationProject {
  return {
    ok: true,
    workflow_id: "wf-1",
    policy: "clinical-write",
    project: { revision: 3 },
    controls: {
      parameters: [],
      actions: {},
      business_decisions: {
        available: true,
        required_flow_capability: "qualification.set_business_decision",
        graphs: [
          {
            id: "__program__",
            label: "Main workflow",
            entry: "prepare",
            states: [
              {
                id: "prepare",
                kind: "action",
                title: "Prepare item",
                has_revalidation_anchor: true,
                can_insert_before: true,
                decision: null,
              },
              {
                id: "approved",
                kind: "action",
                title: "Continue approved path",
                has_revalidation_anchor: true,
                can_insert_before: true,
                decision: null,
              },
              {
                id: "manual_review",
                kind: "terminal",
                title: "Send for manual review",
                has_revalidation_anchor: false,
                can_insert_before: false,
                decision: null,
              },
            ],
          },
        ],
      },
    },
  } as unknown as QualificationProject;
}

describe("BusinessDecisionAuthoring", () => {
  beforeEach(() => mockedEngineInvoke.mockReset());
  afterEach(cleanup);

  it("submits one finite branch contract through the Flow-owned authoring command", async () => {
    const current = project();
    mockedEngineInvoke.mockResolvedValue(current);
    const onProject = vi.fn();
    render(
      <BusinessDecisionAuthoring
        workflowId="wf-1"
        project={current}
        onProject={onProject}
      />,
    );

    fireEvent.change(screen.getByLabelText("Question for the operator"), {
      target: { value: "Should this item continue on the approved path?" },
    });
    const labels = screen.getAllByLabelText("Answer shown to the operator");
    const values = screen.getAllByLabelText("Recorded value");
    const targets = screen.getAllByLabelText("Qualified next step");
    fireEvent.change(labels[0], { target: { value: "Continue" } });
    fireEvent.change(values[0], { target: { value: "approved" } });
    fireEvent.change(targets[0], { target: { value: "approved" } });
    fireEvent.change(labels[1], { target: { value: "Send for review" } });
    fireEvent.change(values[1], { target: { value: "manual_review" } });
    fireEvent.change(targets[1], { target: { value: "manual_review" } });
    fireEvent.change(screen.getByLabelText("Check"), {
      target: { value: "text_present" },
    });
    fireEvent.change(screen.getByLabelText("Visible text"), {
      target: { value: "Ready for review" },
    });
    fireEvent.click(screen.getByTestId("save-business-decision"));

    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.AUTHOR_QUALIFICATION_BUSINESS_DECISION,
        expect.objectContaining({
          graph_id: "__program__",
          state_id: "review_decision",
          insert_before_state_id: "prepare",
          authorized_roles: ["operator", "supervisor"],
          revalidation_kind: "text_present",
          revalidation_text: "Ready for review",
          options: [
            expect.objectContaining({
              label: "Continue",
              value: "approved",
              target: "approved",
            }),
            expect.objectContaining({
              label: "Send for review",
              value: "manual_review",
              target: "manual_review",
            }),
          ],
        }),
      ),
    );
    expect(onProject).toHaveBeenCalledWith(current);
  });
});
