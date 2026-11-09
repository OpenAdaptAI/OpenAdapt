import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProgramWorkbench, sectionFolds, sectionReview, type ProgramGraphSpec } from "../src/index.js";

afterEach(cleanup);

// A linear form as Flow emits it: two field sections and the save, with the
// compiled sections from the bundle's sections.json.
const action = (id: string, index: number, title: string) => ({ id, index, kind: "action" as const, title, action: "click" });
const spec: ProgramGraphSpec = {
  spec_version: 2,
  bundle: { name: "patient-form", schema_version: 2 },
  nodes: [
    action("step_000", 0, "click First name"),
    action("step_001", 1, "type <first>"),
    action("step_002", 2, "click Street"),
    action("step_003", 3, "type <street>"),
    { ...action("step_004", 4, "click Save"), risk: "irreversible" },
    { id: "__end__", index: 5, kind: "terminal", title: "End of declared steps", outcome: "success" },
  ],
  edges: [
    { source: "step_000", target: "step_001", kind: "sequence" },
    { source: "step_001", target: "step_002", kind: "sequence" },
    { source: "step_002", target: "step_003", kind: "sequence" },
    { source: "step_003", target: "step_004", kind: "sequence" },
    { source: "step_004", target: "__end__", kind: "sequence" },
  ],
  sections: [
    {
      id: "section:fields-step_000", title: "Who", subtitle: "2 steps · 1 field", kind: "fields",
      title_source: "ui", nodes: ["step_000", "step_001"], section: "fields-step_000",
      steps: ["step_000", "step_001"], fields: ["first name"],
    },
    {
      id: "section:fields-step_002", title: "Contact", subtitle: "2 steps · 1 field", kind: "fields",
      title_source: "ui", nodes: ["step_002", "step_003"], section: "fields-step_002",
      steps: ["step_002", "step_003"], fields: ["street"],
    },
    {
      id: "section:save-step_004", title: "Save", subtitle: "1 step", kind: "save",
      title_source: "ui", nodes: ["step_004"], section: "save-step_004", steps: ["step_004"],
    },
  ],
};

describe("compiled sections", () => {
  it("become folds, one card per section", () => {
    expect(sectionFolds(spec)?.groups.map((g) => [g.id, g.title, g.nodes.length])).toEqual([
      ["section:fields-step_000", "Who", 2],
      ["section:fields-step_002", "Contact", 2],
      ["section:save-step_004", "Save", 1],
    ]);
    expect(sectionFolds({ ...spec, sections: [] })).toBeNull();
  });

  it("fold the map by default, and an explicit null turns them off", () => {
    const { container, unmount } = render(<ProgramWorkbench spec={spec} />);
    expect(container.querySelectorAll('[data-fold="folded"]')).toHaveLength(3);
    unmount();
    const off = render(<ProgramWorkbench spec={spec} folds={null} />);
    expect(off.container.querySelectorAll('[data-fold="folded"]')).toHaveLength(0);
  });

  it("never offer to merge the save section with fields or to split it", () => {
    expect(sectionReview(spec, "section:fields-step_000")?.mergeWith?.section).toBe("fields-step_002");
    expect(sectionReview(spec, "section:fields-step_002")?.mergeWith).toBeUndefined();
    expect(sectionReview(spec, "section:fields-step_000", "step_001")?.splitAt).toBe("step_001");
    expect(sectionReview(spec, "section:fields-step_000", "step_000")?.splitAt).toBeUndefined();
    expect(sectionReview(spec, "section:save-step_004", "step_004")?.splitAt).toBeUndefined();
  });

  it("let a reviewer rename, merge and split, in the bundle's terms", () => {
    const onEditSection = vi.fn();
    render(<ProgramWorkbench spec={spec} onEditSection={onEditSection} />);
    // The first card is selected.
    fireEvent.click(screen.getByRole("button", { name: "Rename this section" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Section title" }), { target: { value: "  Patient  name " } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onEditSection).toHaveBeenLastCalledWith({ op: "rename", section: "fields-step_000", title: "Patient name" });

    fireEvent.click(screen.getByRole("button", { name: "Merge with the next section" }));
    expect(onEditSection).toHaveBeenLastCalledWith({ op: "merge", section: "fields-step_000", other: "fields-step_002" });

    fireEvent.click(screen.getByRole("button", { name: "Show the 2 steps" }));
    fireEvent.click(screen.getByRole("button", { name: /type <first>/ }));
    fireEvent.click(screen.getByRole("button", { name: "Start a new section at this step" }));
    expect(onEditSection).toHaveBeenLastCalledWith({ op: "split", section: "fields-step_000", at: "step_001" });
  });

  it("are read-only without a handler", () => {
    render(<ProgramWorkbench spec={spec} />);
    expect(screen.queryByRole("button", { name: /Rename/ })).toBeNull();
  });
});
