import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { QualificationProject } from "../lib/types";
import { ProgramWorkbench } from "./ProgramWorkbench";

const graph = {
  bundle: {
    name: "Reference program",
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
    { source: "open", target: "loop", kind: "next", label: "" },
    { source: "loop", target: "open", kind: "loop_body", label: "next item" },
    { source: "loop", target: "done", kind: "loop_exit", label: "complete" },
  ],
} as QualificationProject["graph"];

afterEach(cleanup);

describe("ProgramWorkbench", () => {
  it("renders every exact edge and marks the loop return", () => {
    const { container } = render(<ProgramWorkbench graph={graph} />);
    expect(container.querySelectorAll(".program-edges > g")).toHaveLength(3);
    expect(container.querySelectorAll('[data-back-edge="true"]')).toHaveLength(1);
  });

  it("switches from the map to the declared evidence lanes", () => {
    render(<ProgramWorkbench graph={graph} />);
    fireEvent.click(screen.getByRole("tab", { name: "Evidence lanes" }));
    expect(screen.getByText("Target evidence")).toBeTruthy();
    expect(screen.getByText("Structural")).toBeTruthy();
    expect(screen.getByText(/do not report a live verdict/i)).toBeTruthy();
  });

  it("updates the inspector from an exact selected node", () => {
    render(<ProgramWorkbench graph={graph} />);
    fireEvent.click(screen.getByRole("button", { name: /For each item/i }));
    const inspector = screen.getByLabelText("Selected program step");
    expect(inspector.textContent).toContain("For each item");
    expect(inspector.textContent).toContain("loop");
  });
});
