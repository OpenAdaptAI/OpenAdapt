import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CMD, engineInvoke } from "../lib/engine";
import type { QualificationProject } from "../lib/types";
import { Qualification } from "./Qualification";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineInvoke: vi.fn() };
});

const mockedEngineInvoke = vi.mocked(engineInvoke);

function action(id: string, title: string, index: number) {
  return {
    id,
    index,
    kind: "action",
    title,
    action: "click",
    resolution: null,
    risk: "reversible",
    identity: {
      applicable: true,
      armed: true,
      phi_free: true,
      has_structured: true,
      has_identifier_crop: false,
    },
    effects: [],
    postconditions: [],
    halts: [],
    badges: [],
  };
}

function projectWithTiers(tiers: Record<string, number>): QualificationProject {
  const nodes = [action("review", "Review record", 0), action("submit", "Submit record", 1)];
  const actions = Object.fromEntries(
    nodes.map((node) => [
      node.id,
      {
        step_id: node.id,
        classification: {
          step_id: node.id,
          classification: "consequential",
          explanation: "Operator reviewed",
          operator_confirmed: true,
        },
        identity: {
          can_arm: true,
          armed: true,
          sources: [
            {
              kind: "structured",
              label: "Application identity fields",
              match: "Canonical Flow identity ladder",
            },
          ],
          policy: {
            step_id: node.id,
            enforcement: "canonical_ladder",
            signals: [],
            quorum: 0,
          },
        },
        effects: [
          {
            index: 0,
            kind: "record_written",
            match: {},
            expected_count: 1,
            key_field: "key",
            count_new_only: true,
            risk: "reversible",
            needs_operator_confirmation: false,
            verification_tier: tiers[node.id],
            effect_contract_hash: `sha256:${node.id.padEnd(64, "0")}`,
          },
        ],
      },
    ]),
  );

  // This fixture intentionally carries only fields the interaction renders.
  return {
    ok: true,
    certification_current: false,
    project: {
      revision: 2,
      minimum_effect_tier: 3,
      cases: [],
      environment: {
        target_kind: "rdp",
        application: "Reference app",
        application_version: "1",
        environment_digest: "a".repeat(64),
        runtime_version: "1.22.0",
      },
    },
    report: {
      passed: false,
      action_count: 2,
      consequential_action_count: 2,
      identity_covered_action_count: 2,
      effect_required_action_count: 2,
      effect_covered_action_count: 2,
      case_count: 0,
      passed_case_count: 0,
      refusals: [],
    },
    graph: {
      bundle: {
        name: "Qualification fixture",
        provenance: { content_digest: "sha256:fixture" },
      },
      nodes,
      edges: [],
    },
    controls: { parameters: [], actions },
    lint: { findings: [] },
  } as unknown as QualificationProject;
}

describe("Qualification effect requirements", () => {
  beforeEach(() => mockedEngineInvoke.mockReset());
  afterEach(cleanup);

  it("saves the selected action's tier without carrying the prior action's value", async () => {
    mockedEngineInvoke
      .mockResolvedValueOnce(projectWithTiers({ review: 3, submit: 2 }))
      .mockResolvedValue(projectWithTiers({ review: 3, submit: 1 }));

    render(<Qualification workflowId="wf-1" onBack={() => {}} />);

    expect(await screen.findByText("Next: Run cases")).toBeTruthy();
    const actionSelect = await screen.findByLabelText("Action");
    const tierSelect = screen.getByLabelText(
      "Minimum evidence required for this effect",
    ) as HTMLSelectElement;
    expect(tierSelect.value).toBe("3");

    fireEvent.change(actionSelect, { target: { value: "submit" } });
    await waitFor(() => expect(tierSelect.value).toBe("2"));
    fireEvent.change(tierSelect, { target: { value: "1" } });
    fireEvent.click(screen.getByRole("button", { name: "Save required tier" }));

    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.SET_QUALIFICATION_EFFECT_VERIFICATION,
        expect.objectContaining({
          step_id: "submit",
          effect_index: 0,
          verification_tier: 1,
        }),
      ),
    );
  });

  it("saves a dedicated application identity expectation through the cockpit", async () => {
    const project = projectWithTiers({ review: 3, submit: 2 });
    project.controls.actions.review.identity.sources.push({
      kind: "application",
      label: "Live application identity",
      match: "Observed immediately before actuation",
    });
    project.controls.actions.review.identity.policy = {
      step_id: "review",
      enforcement: "signal_quorum",
      signals: [
        {
          key: "application",
          source: "application",
          match: "exact",
          normalizers: [],
          expected_value: "https://app.example",
          params: [],
        },
      ],
      quorum: 1,
    };
    mockedEngineInvoke.mockResolvedValue(project);

    render(<Qualification workflowId="wf-1" onBack={() => {}} />);

    const expectedInput = (await screen.findByDisplayValue(
      "https://app.example",
    )) as HTMLInputElement;
    expect(expectedInput.value).toBe("https://app.example");
    fireEvent.click(
      screen.getByRole("button", { name: "Save identity policy" }),
    );

    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.SET_QUALIFICATION_IDENTITY,
        expect.objectContaining({
          signals: [
            expect.objectContaining({
              key: "application",
              source: "application",
              expected_value: "https://app.example",
            }),
          ],
        }),
      ),
    );
  });

  it("saves an atomic reciprocal automatic-rule pair through the Flow-owned case command", async () => {
    const project = projectWithTiers({ review: 3, submit: 2 });
    (project.controls as Record<string, unknown>).judgment_cases = {
      available: true,
      required_flow_capability: "qualification.set_judgment_cases",
      report: null,
      contexts: [{
        decision: {
          graph_id: "__program__",
          state_id: "review_decision",
          workflow_contract_sha256: "a".repeat(64),
          decision_contract_sha256: "b".repeat(64),
        },
        fact_schema: {
          schema_version: "openadapt.judgment-fact-schema/v1",
          fields: { urgent: { type: "boolean" } },
        },
        fact_schema_sha256: "c".repeat(64),
        options: [
          { id: "priority_review", label: "Priority review" },
          { id: "supervisor", label: "Supervisor" },
        ],
        authorized_roles: ["supervisor"],
        cases: [],
      }],
    };
    mockedEngineInvoke.mockResolvedValue(project);

    render(<Qualification workflowId="wf-1" onBack={() => {}} />);

    await screen.findByText("Capture reviewed examples before you automate a choice");
    fireEvent.change(screen.getByLabelText("Local source SHA-256"), {
      target: { value: "d".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("Local reviewer reference SHA-256"), {
      target: { value: "e".repeat(64) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Rule candidate" }));
    fireEvent.change(screen.getByLabelText("Qualified branch for a rule candidate"), {
      target: { value: "priority_review" },
    });
    fireEvent.change(screen.getByLabelText("Reviewed rule id"), {
      target: { value: "urgent_policy" },
    });
    fireEvent.click(screen.getByLabelText("Add a contrasting reviewed case now"));
    fireEvent.change(screen.getAllByLabelText("urgent")[1], { target: { value: "true" } });
    fireEvent.change(screen.getByLabelText("Qualified branch for contrasting case"), {
      target: { value: "supervisor" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add local evidence" }));
    fireEvent.change(screen.getByLabelText("Evidence reference 1 local path"), {
      target: { value: "evidence/policy.pdf" },
    });
    fireEvent.change(screen.getByLabelText("Evidence reference 1 SHA-256"), {
      target: { value: "f".repeat(64) },
    });
    fireEvent.click(screen.getByTestId("capture-judgment-case"));

    await waitFor(() =>
      expect(mockedEngineInvoke).toHaveBeenCalledWith(
        CMD.SET_QUALIFICATION_JUDGMENT_CASES,
        expect.objectContaining({
          workflow_id: "wf-1",
          schemas: [expect.objectContaining({ state_id: "review_decision" })],
          cases: [
            expect.objectContaining({
              disposition: "automatic_rule",
              option_id: "priority_review",
              contrast_case_ids: [expect.any(String)],
            }),
            expect.objectContaining({
              disposition: "automatic_rule",
              option_id: "supervisor",
              contrast_case_ids: [expect.any(String)],
            }),
          ],
        }),
      ),
    );
    const call = mockedEngineInvoke.mock.calls.find(
      ([command]) => command === CMD.SET_QUALIFICATION_JUDGMENT_CASES,
    );
    const cases = call?.[1]?.cases as { id: string; contrast_case_ids: string[] }[];
    expect(cases[0].contrast_case_ids).toEqual([cases[1].id]);
    expect(cases[1].contrast_case_ids).toEqual([cases[0].id]);
  });
});
