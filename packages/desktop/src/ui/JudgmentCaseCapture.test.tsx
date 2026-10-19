import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { JudgmentCaseCaptureContextV1 } from "../lib/types";
import { JudgmentCaseCapture } from "./JudgmentCaseCapture";

const digest = "a".repeat(64);

function context(): JudgmentCaseCaptureContextV1 {
  return {
    decision: {
      graph_id: "main",
      state_id: "routing_review",
      workflow_contract_sha256: digest,
      decision_contract_sha256: "b".repeat(64),
    },
    fact_schema: {
      schema_version: "openadapt.judgment-fact-schema/v1",
      fields: {
        service_level: { type: "enum", allowed_values: ["standard", "urgent"] },
        reserved_capacity_available: { type: "boolean" },
        days_waiting: { type: "integer" },
      },
    },
    fact_schema_sha256: "c".repeat(64),
    options: [
      { id: "priority_review", label: "Priority review" },
      { id: "standard_review", label: "Standard review" },
      { id: "supervisor", label: "Supervisor" },
    ],
    authorized_roles: ["scheduling_lead", "supervisor"],
    cases: [],
  };
}

afterEach(cleanup);

describe("JudgmentCaseCapture", () => {
  it("captures reviewed typed facts and keeps the optional note as a local evidence reference", () => {
    const onCapture = vi.fn();
    render(<JudgmentCaseCapture context={context()} onCapture={onCapture} />);

    fireEvent.change(screen.getByLabelText("Local source SHA-256"), {
      target: { value: "f".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("Local reviewer reference SHA-256"), {
      target: { value: "d".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("service level"), {
      target: { value: "urgent" },
    });
    fireEvent.change(screen.getByLabelText("reserved capacity available"), {
      target: { value: "true" },
    });
    fireEvent.change(screen.getByLabelText("days waiting"), { target: { value: "4" } });
    fireEvent.click(screen.getByRole("button", { name: "Add local review note" }));
    fireEvent.click(screen.getByRole("button", { name: "Add local evidence" }));
    fireEvent.change(screen.getByLabelText("Evidence reference 1 local path"), {
      target: { value: "evidence/retained-frame.png" },
    });
    fireEvent.change(screen.getByLabelText("Evidence reference 1 SHA-256"), {
      target: { value: "1".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("Optional local review note local path"), {
      target: { value: "evidence/review-note.txt" },
    });
    fireEvent.change(screen.getByLabelText("Optional local review note SHA-256"), {
      target: { value: "e".repeat(64) },
    });
    fireEvent.click(screen.getByTestId("capture-judgment-case"));

    expect(onCapture).toHaveBeenCalledWith([
      expect.objectContaining({
        decision: expect.objectContaining({ state_id: "routing_review" }),
        fact_schema_sha256: "c".repeat(64),
        facts: {
          service_level: "urgent",
          reserved_capacity_available: true,
          days_waiting: 4,
        },
        option_id: null,
        disposition: "human_node",
        review_note_ref: expect.objectContaining({
          relative_path: "evidence/review-note.txt",
          sha256: "e".repeat(64),
        }),
        provenance: {
          source: "demonstration",
          source_ref_sha256: "f".repeat(64),
          reviewer_role: "scheduling_lead",
          reviewer_principal_ref_sha256: "d".repeat(64),
        },
      }),
    ]);
  });

  it("does not allow an automatic rule candidate without a selected qualified branch", () => {
    const onCapture = vi.fn();
    render(<JudgmentCaseCapture context={context()} onCapture={onCapture} />);

    fireEvent.change(screen.getByLabelText("Local source SHA-256"), {
      target: { value: "f".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("Local reviewer reference SHA-256"), {
      target: { value: "d".repeat(64) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Rule candidate" }));
    fireEvent.click(screen.getByTestId("capture-judgment-case"));

    expect(onCapture).not.toHaveBeenCalled();
    expect(screen.getByText("A rule candidate needs a reviewed rule id and a qualified branch.")).toBeTruthy();
  });

  it("creates a reciprocal contrasting pair for an automatic rule candidate", async () => {
    const onCapture = vi.fn().mockResolvedValue(undefined);
    render(<JudgmentCaseCapture context={context()} onCapture={onCapture} />);

    fireEvent.change(screen.getByLabelText("Local source SHA-256"), {
      target: { value: "f".repeat(64) },
    });
    fireEvent.change(screen.getByLabelText("Local reviewer reference SHA-256"), {
      target: { value: "d".repeat(64) },
    });
    fireEvent.click(screen.getByRole("button", { name: "Rule candidate" }));
    fireEvent.change(screen.getByLabelText("Qualified branch for a rule candidate"), {
      target: { value: "priority_review" },
    });
    fireEvent.change(screen.getByLabelText("Reviewed rule id"), {
      target: { value: "urgent_capacity_policy" },
    });
    fireEvent.click(screen.getByLabelText("Add a contrasting reviewed case now"));
    fireEvent.change(screen.getAllByLabelText("service level")[1], {
      target: { value: "urgent" },
    });
    fireEvent.change(screen.getByLabelText("Qualified branch for contrasting case"), {
      target: { value: "supervisor" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add local evidence" }));
    fireEvent.change(screen.getByLabelText("Evidence reference 1 local path"), {
      target: { value: "evidence/policy.pdf" },
    });
    fireEvent.change(screen.getByLabelText("Evidence reference 1 SHA-256"), {
      target: { value: "1".repeat(64) },
    });
    fireEvent.click(screen.getByTestId("capture-judgment-case"));

    await Promise.resolve();
    const pair = onCapture.mock.calls[0][0];
    expect(pair).toHaveLength(2);
    expect(pair[0]).toEqual(expect.objectContaining({
      disposition: "automatic_rule",
      option_id: "priority_review",
      reviewed_rule_id: "urgent_capacity_policy",
    }));
    expect(pair[1]).toEqual(expect.objectContaining({
      disposition: "automatic_rule",
      option_id: "supervisor",
      provenance: expect.objectContaining({ source: "counterfactual" }),
    }));
    expect(pair[0].facts).not.toEqual(pair[1].facts);
    expect(pair[0].contrast_case_ids).toEqual([pair[1].id]);
    expect(pair[1].contrast_case_ids).toEqual([pair[0].id]);
  });
});
