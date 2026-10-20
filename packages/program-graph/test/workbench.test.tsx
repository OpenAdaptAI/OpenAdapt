import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ProgramWorkbench } from "../src/index.js";
import { processRecords, referral } from "./fixtures.js";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("ProgramWorkbench", () => {
  it("renders every exact edge and marks the loop return", () => {
    const { container } = render(<ProgramWorkbench spec={processRecords} />);
    expect(container.querySelectorAll(".opg-edges > g")).toHaveLength(7);
    expect(container.querySelectorAll('[data-back-edge="true"]')).toHaveLength(1);
    expect(screen.getByText("No live trace bound")).toBeTruthy();
    expect(screen.getByText("6 nodes · 7 exact edges")).toBeTruthy();
  });

  it("switches from the map to the declared evidence lanes", () => {
    render(<ProgramWorkbench spec={processRecords} />);
    fireEvent.click(screen.getByRole("tab", { name: "Evidence lanes" }));
    const panel = screen.getByRole("tabpanel");
    expect(within(panel).getByText("Finds the field")).toBeTruthy();
    expect(within(panel).getAllByText("Structural, Template").length).toBeGreaterThan(0);
    expect(within(panel).getByText(/do not report a live verdict/i)).toBeTruthy();
  });

  it("moves between tabs with the arrow keys", () => {
    render(<ProgramWorkbench spec={processRecords} />);
    const map = screen.getByRole("tab", { name: "Program map" });
    expect(map.getAttribute("tabindex")).toBe("0");
    expect(screen.getByRole("tab", { name: "Stop rules" }).getAttribute("tabindex")).toBe("-1");
    fireEvent.keyDown(map, { key: "ArrowLeft" });
    const stops = screen.getByRole("tab", { name: "Stop rules" });
    expect(stops.getAttribute("aria-selected")).toBe("true");
    expect(document.activeElement).toBe(stops);
    expect(screen.getByRole("tabpanel").getAttribute("aria-labelledby")).toBe(stops.id);
  });

  it("updates the inspector from an exact selected node", () => {
    render(<ProgramWorkbench spec={processRecords} />);
    fireEvent.click(screen.getByRole("button", { name: /For each record/i }));
    const inspector = screen.getByLabelText("Selected program step");
    expect(inspector.textContent).toContain("For each record");
    expect(inspector.textContent).toContain("for_each_record");
  });

  it("walks the map with the arrow keys, one focusable card at a time", () => {
    render(<ProgramWorkbench spec={processRecords} />);
    const first = screen.getByRole("button", { name: /step 01, action: Open worklist/ });
    expect(first.getAttribute("tabindex")).toBe("0");
    fireEvent.keyDown(first, { key: "ArrowDown" });
    const loop = screen.getByRole("button", { name: /For each record/ });
    expect(document.activeElement).toBe(loop);
    expect(loop.getAttribute("aria-pressed")).toBe("true");
    expect(first.getAttribute("tabindex")).toBe("-1");
    fireEvent.keyDown(loop, { key: "End" });
    expect(document.activeElement?.getAttribute("aria-label")).toMatch(/^end, terminal/);
  });

  it("describes each card and each edge for a screen reader", () => {
    render(<ProgramWorkbench spec={processRecords} />);
    expect(
      screen.getByRole("button", {
        name: "step 04, action: Write declared update. Checks the right record. Reads the saved record back. 2 stop rules.",
      }),
    ).toBeTruthy();
    const edges = screen.getByRole("list", { name: "Program edges" });
    expect(within(edges).getByText(/More records\? to For each record, returns to an earlier step \(branch: more\)/)).toBeTruthy();
  });

  it("folds a long program into sections and unfolds one on request", () => {
    render(<ProgramWorkbench spec={referral.spec} folds={referral.folds} />);
    expect(screen.getByText("31 nodes · 31 exact edges · 7 cards")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Steps 29–30, 2 steps: Create the chart/ }));
    fireEvent.click(screen.getByRole("button", { name: "Show the 2 steps" }));
    const member = screen.getByRole("button", { name: /^step 29, action:/ });
    expect(document.activeElement).toBe(member);
    fireEvent.click(screen.getByRole("button", { name: /Fold into “Create the chart”/ }));
    expect(screen.getByRole("button", { name: /Steps 29–30/ })).toBeTruthy();
  });

  it("plays a recorded run: steps light up, the loop counts, the stop shows where and why", () => {
    vi.useFakeTimers();
    const { container } = render(
      <ProgramWorkbench
        spec={referral.spec}
        folds={referral.folds}
        stopExits={referral.folds.stops}
        trace={referral.trace}
        iterationNoun="referral"
      />,
    );
    expect(screen.getByText(/Run trace bound/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    act(() => {
      vi.advanceTimersByTime(4000);
    });
    expect(container.querySelector('[data-run="active"]')).toBeTruthy();
    act(() => {
      vi.runAllTimers();
    });
    // Each timer advances one event; drain until the run ends.
    for (let i = 0; i < referral.trace.events.length; i += 1) {
      act(() => {
        vi.runOnlyPendingTimers();
      });
    }
    expect(screen.getByText(/Run stopped/)).toBeTruthy();
    expect(screen.getByText(/Referral 4 of 4/)).toBeTruthy();
    const save = screen.getByRole("button", { name: /Steps 29–30/ });
    expect(save.getAttribute("data-run")).toBe("halted");
    expect(save.getAttribute("aria-label")).toMatch(/This run: stopped here/);
    const loop = screen.getByRole("button", { name: /Repeat the bounded steps/ });
    expect(loop.textContent).toContain("×4");
    const inspector = screen.getByLabelText("Selected program step");
    expect(inspector.textContent).toContain("Stopped here");
    expect(inspector.textContent).toContain("OpenEMR found a matching chart");
    expect(container.querySelector('.opg-edges g[data-kind="exception"][data-run]')).toBeTruthy();
  });

  it("steps and scrubs the trace without playing", () => {
    render(<ProgramWorkbench spec={referral.spec} trace={referral.trace} iterationNoun="referral" />);
    fireEvent.click(screen.getByRole("button", { name: "Step forward" }));
    expect(screen.getByText("Referral 1 of 4")).toBeTruthy();
    fireEvent.change(screen.getByRole("slider", { name: "Run position" }), { target: { value: "50" } });
    expect(screen.getByText("Referral 2 of 4")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back to the start" }));
    expect(screen.getByText("Ready")).toBeTruthy();
  });

  it("matches the rendered map snapshot", () => {
    const { container } = render(
      <ProgramWorkbench spec={referral.spec} folds={referral.folds} stopExits={referral.folds.stops} />,
    );
    expect(container.querySelector(".opg-canvas")?.outerHTML).toMatchSnapshot();
  });
});
