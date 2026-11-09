import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { ProgramWorkbench } from "@openadapt/program-graph";
import { PROGRAM_WORKBENCH_NOTES, PROGRAM_WORKBENCH_SURFACE } from "../lib/programWorkbench";
import type { QualificationProject } from "../lib/types";

// Desktop renders the shared program map from @openadapt/program-graph
// (packages/program-graph, which has the component's own tests). These check
// Desktop's wiring: the alias resolves, the qualification graph type is a
// program-graph spec, and Desktop's surface and notes apply.

const graph = {
  bundle: {
    name: "Reference program",
    schema_version: 2,
    action_count: 2,
    irreversible_count: 0,
    identity_armed_count: 1,
    identity_unarmed_count: 0,
    effect_count: 1,
    encrypted: false,
    provenance: { content_digest: "a".repeat(64) },
  },
  nodes: [
    {
      id: "open",
      index: 0,
      kind: "action",
      title: "Open worklist",
      action: "click",
      resolution: {
        top_rung: "structural",
        rungs: [
          { name: "structural", label: "Structural", present: true, detail: "" },
          { name: "template", label: "Template", present: false, detail: "" },
        ],
      },
      risk: "reversible",
      identity: { applicable: true, armed: true, phi_free: true, has_structured: true, has_identifier_crop: false },
      effects: [],
      postconditions: ["worklist visible"],
      halts: ["target is ambiguous"],
      badges: [],
    },
    {
      id: "loop",
      index: 1,
      kind: "loop",
      title: "For each item",
      action: null,
      resolution: null,
      risk: null,
      identity: null,
      effects: [],
      postconditions: [],
      halts: [],
      badges: [],
    },
    {
      id: "done",
      index: 2,
      kind: "terminal",
      title: "End of declared steps",
      action: null,
      resolution: null,
      risk: null,
      identity: null,
      effects: [],
      postconditions: [],
      halts: [],
      badges: [],
    },
  ],
  edges: [
    { source: "open", target: "loop", kind: "sequence", label: "" },
    { source: "loop", target: "open", kind: "loop_body", label: "next item" },
    { source: "loop", target: "done", kind: "sequence", label: "complete" },
  ],
} as QualificationProject["graph"];

afterEach(cleanup);

const renderDesktop = () =>
  render(<ProgramWorkbench spec={graph} className={PROGRAM_WORKBENCH_SURFACE} notes={PROGRAM_WORKBENCH_NOTES} />);

describe("ProgramWorkbench in Desktop", () => {
  it("renders every exact edge and marks the loop return", () => {
    const { container } = renderDesktop();
    expect(container.querySelectorAll(".opg-edges > g")).toHaveLength(3);
    expect(container.querySelectorAll('[data-back-edge="true"]')).toHaveLength(1);
    expect(container.querySelector(".opg-root")?.classList.contains("surface-phosphor")).toBe(true);
  });

  it("switches from the map to the declared evidence lanes", () => {
    renderDesktop();
    fireEvent.click(screen.getByRole("tab", { name: "Evidence lanes" }));
    expect(screen.getByText("Finds the field")).toBeTruthy();
    expect(screen.getByText("Structural")).toBeTruthy();
    expect(screen.getByText(/do not report a live verdict/i)).toBeTruthy();
    expect(screen.getByText(/Desktop binds live evidence only after/)).toBeTruthy();
  });

  it("updates the inspector from an exact selected node", () => {
    renderDesktop();
    fireEvent.click(screen.getByRole("button", { name: /For each item/i }));
    const inspector = screen.getByLabelText("Selected program step");
    expect(inspector.textContent).toContain("For each item");
    expect(inspector.textContent).toContain("loop");
  });
});
