import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProgramMap, ProgramWorkbench, StepInspector, type FoldSpec, type RunTrace, type SelectionOrigin, type TourStop, specNodes } from "../src/index.js";
import { processRecords } from "./fixtures.js";

const trace: RunTrace = {
  trace_version: 1,
  outcome: "completed",
  events: specNodes(processRecords).map((node, index) => ({ node: node.id, status: "ok", at_ms: index * 100, duration_ms: 100 })),
};
const folds: FoldSpec = { groups: [{ id: "review", title: "Review record changes", nodes: ["open_record", "write_update"] }] };
const tour: TourStop[] = [{ id: "review", title: "Review", body: "Inspect the selected step.", select: "write_update", cursor: 4 }];

afterEach(() => { cleanup(); vi.useRealTimers(); });

for (const [name, Map] of [["workbench", ProgramWorkbench], ["map", ProgramMap]] as const) {
  describe(`${name} selection origins`, () => {
    it("reports a pointer re-selection of the current node as user input", () => {
      const onSelect = vi.fn();
      render(<Map spec={processRecords} trace={trace} cursor={3} selectedId="open_record" onSelect={onSelect} />);
      expect(onSelect).toHaveBeenLastCalledWith("open_record", "follow");
      onSelect.mockClear();
      fireEvent.click(screen.getByRole("button", { name: /step 03.*Open current record/ }));
      expect(onSelect.mock.calls).toEqual([["open_record", "user"]]);
    });

    it("labels keyboard navigation and accessible button activation as user input", () => {
      const onSelect = vi.fn();
      render(<Map spec={processRecords} selectedId="for_each_record" onSelect={onSelect} />);
      fireEvent.keyDown(screen.getByRole("button", { name: /step 02.*For each record/ }), { key: "ArrowDown" });
      expect(onSelect).toHaveBeenLastCalledWith("open_record", "user");
      // Enter/Space and assistive technology activate a native button through click.
      fireEvent.click(screen.getByRole("button", { name: /step 03.*Open current record/ }), { detail: 0 });
      expect(onSelect).toHaveBeenLastCalledWith("open_record", "user");
    });

    it("does not emit when the host synchronizes controlled selection or folds", () => {
      const onSelect = vi.fn();
      const { rerender } = render(<Map spec={processRecords} folds={folds} selectedId="open_record" onSelect={onSelect} />);
      rerender(<Map spec={processRecords} folds={folds} selectedId="write_update" expanded={["review"]} onSelect={onSelect} />);
      expect(onSelect).not.toHaveBeenCalled();
    });

    it("labels host cursor and playhead movement as follow", () => {
      const onSelect = vi.fn();
      const { rerender } = render(<Map spec={processRecords} trace={trace} cursor={3} onSelect={onSelect} />);
      rerender(<Map spec={processRecords} trace={trace} cursor={4} onSelect={onSelect} />);
      expect(onSelect.mock.calls).toEqual([["open_record", "follow"], ["write_update", "follow"]]);
      rerender(<Map spec={processRecords} playhead={{ node: "record_complete", status: "running" }} onSelect={onSelect} />);
      expect(onSelect).toHaveBeenLastCalledWith("record_complete", "follow");
    });
  });
}

it("preserves existing one-argument callback compatibility", () => {
  const picked: string[] = [];
  const onSelect = (id: string) => { picked.push(id); };
  render(<ProgramMap spec={processRecords} onSelect={onSelect} />);
  fireEvent.click(screen.getByRole("button", { name: /step 03.*Open current record/ }));
  expect(picked).toEqual(["open_record"]);
});

it("labels player buttons, scrubbing and timed playback as follow", () => {
  vi.useFakeTimers();
  const onSelect = vi.fn();
  render(<ProgramWorkbench spec={processRecords} trace={trace} initialCursor={2} onSelect={onSelect} />);
  onSelect.mockClear();
  fireEvent.click(screen.getByRole("button", { name: "Step forward" }));
  expect(onSelect).toHaveBeenLastCalledWith("open_record", "follow");
  fireEvent.change(screen.getByRole("slider", { name: "Run position" }), { target: { value: "4" } });
  expect(onSelect).toHaveBeenLastCalledWith("write_update", "follow");
  fireEvent.click(screen.getByRole("button", { name: "Play" }));
  act(() => { vi.advanceTimersByTime(500); });
  expect(onSelect).toHaveBeenLastCalledWith("record_complete", "follow");
  expect(onSelect.mock.calls.every(([, origin]) => origin === "follow")).toBe(true);
});

it("reports guided tour selection separately without a follow feedback event", () => {
  const onSelect = vi.fn();
  render(<ProgramWorkbench spec={processRecords} trace={trace} tour={tour} onSelect={onSelect} />);
  expect(onSelect.mock.calls).toEqual([["write_update", "tour"]]);
});

it("a controlled host can accept tours while ignoring automatic follow", () => {
  function Host() {
    const [selected, setSelected] = useState("open_worklist");
    return <><output data-testid="picked">{selected}</output>
      <ProgramWorkbench spec={processRecords} trace={trace} tour={tour} selectedId={selected}
        onSelect={(id, origin) => { if (origin !== "follow") setSelected(id); }} />
    </>;
  }
  render(<Host />);
  expect(screen.getByTestId("picked").textContent).toBe("write_update");
  expect(screen.getByLabelText("Selected program step").textContent).toContain("Write declared update");
});

for (const standalone of [false, true]) {
  it(`labels ${standalone ? "standalone" : "map"} inspector unfold, member pick and refold as user`, () => {
    const onSelect = vi.fn();
    function Host() {
      const [selected, setSelected] = useState("review");
      const [expanded, setExpanded] = useState<string[]>([]);
      const select = (id: string, origin: SelectionOrigin) => { onSelect(id, origin); setSelected(id); };
      return standalone
        ? <StepInspector spec={processRecords} folds={folds} nodeId={selected} expanded={expanded} onExpandedChange={setExpanded} onSelect={select} />
        : <ProgramWorkbench spec={processRecords} folds={folds} selectedId={selected} expanded={expanded} onExpandedChange={setExpanded} onSelect={select} />;
    }
    render(<Host />);
    const inspector = () => within(screen.getByLabelText("Selected program step"));
    fireEvent.click(inspector().getByRole("button", { name: "Show the 2 steps" }));
    expect(onSelect).toHaveBeenLastCalledWith("open_record", "user");
    fireEvent.click(inspector().getByRole("button", { name: "Fold into “Review record changes”" }));
    expect(onSelect).toHaveBeenLastCalledWith("review", "user");
    fireEvent.click(inspector().getByRole("button", { name: /Write declared update/ }));
    expect(onSelect).toHaveBeenLastCalledWith("write_update", "user");
  });
}

it("labels double-click unfolding as user", () => {
  const onSelect = vi.fn();
  render(<ProgramWorkbench spec={processRecords} folds={folds} initialSelectedId="review" onSelect={onSelect} />);
  fireEvent.doubleClick(screen.getByRole("button", { name: /Steps 03–04.*Review record changes/ }));
  expect(onSelect).toHaveBeenLastCalledWith("open_record", "user");
});

for (const pick of ["current", "noncurrent", "folded"] as const) {
  for (const input of ["pointer", "keyboard"] as const) {
    it(`preserves a controlled ${pick} ${input} pick while playback advances`, () => {
      function Host() {
        const [selected, setSelected] = useState<string | null>(null);
        const [cursor, setCursor] = useState(3);
        return <><output data-testid="picked">{selected ?? "none"}</output>
          <button onClick={() => setCursor((value) => value + 1)}>Advance host</button>
          <ProgramMap spec={processRecords} folds={pick === "folded" ? folds : undefined} trace={trace} cursor={cursor}
            selectedId={selected ?? trace.events[cursor - 1].node}
            onSelect={(id, origin) => { if (origin !== "follow") setSelected(id); }} />
        </>;
      }
      render(<Host />);
      const expected = pick === "current" ? "open_record" : pick === "noncurrent" ? "for_each_record" : "review";
      const target = screen.getByRole("button", { name: pick === "folded" ? /Steps 03–04.*Review record changes/ : pick === "current" ? /step 03.*Open current record/ : /step 02.*For each record/ });
      // Native keyboard activation uses the same click path; the separate arrow
      // navigation test above also checks the map's direct keydown path.
      fireEvent.click(target, { detail: input === "keyboard" ? 0 : 1 });
      expect(screen.getByTestId("picked").textContent).toBe(expected);
      fireEvent.click(screen.getByRole("button", { name: "Advance host" }));
      expect(screen.getByTestId("picked").textContent).toBe(expected);
    });
  }
}
