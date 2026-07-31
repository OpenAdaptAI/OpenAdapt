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
});
