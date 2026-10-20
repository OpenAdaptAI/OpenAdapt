import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  EvidenceLanes,
  ProgramMap,
  ProgramWorkbench,
  StepInspector,
  StopRules,
  traceStateFromPlayhead,
  deriveDisplayGraph,
  type Playhead,
} from "../src/index.js";
import { processRecords, referral } from "./fixtures.js";

afterEach(cleanup);

describe("host-driven workbench", () => {
  it("a controlled selection follows the host, and clicks report through onSelect", () => {
    const onSelect = vi.fn();
    const { rerender } = render(<ProgramWorkbench spec={processRecords} selectedId="write_update" onSelect={onSelect} />);
    expect(screen.getByLabelText("Selected program step").textContent).toContain("Write declared update");
    fireEvent.click(screen.getByRole("button", { name: /For each record/ }));
    expect(onSelect).toHaveBeenCalledWith("for_each_record");
    // The host has not changed selectedId yet, so the inspector stays put.
    expect(screen.getByLabelText("Selected program step").textContent).toContain("Write declared update");
    rerender(<ProgramWorkbench spec={processRecords} selectedId="for_each_record" onSelect={onSelect} />);
    expect(screen.getByLabelText("Selected program step").textContent).toContain("For each record");
  });

  it("a controlled cursor drives the trace, and the player reports moves", () => {
    function Host() {
      const [cursor, setCursor] = useState(0);
      return (
        <>
          <output data-testid="cursor">{cursor}</output>
          <ProgramWorkbench spec={referral.spec} folds={referral.folds} trace={referral.trace} cursor={cursor} onCursorChange={setCursor} iterationNoun="referral" />
        </>
      );
    }
    render(<Host />);
    fireEvent.click(screen.getByRole("button", { name: "Step forward" }));
    expect(screen.getByTestId("cursor").textContent).toBe("1");
    expect(screen.getByText("Referral 1 of 4")).toBeTruthy();
  });

  it("a playhead without a trace lights the step, the loop's return and the stop", () => {
    const at = (playhead: Playhead) =>
      render(<ProgramWorkbench spec={referral.spec} folds={referral.folds} stopExits={referral.folds.stops} playhead={playhead} />);
    const { container, unmount } = at({ node: "for_each", iteration: 2, status: "running" });
    expect(screen.getByRole("button", { name: /^step 01/ }).getAttribute("data-run")).toBe("active");
    expect(container.querySelector('.opg-edges g[data-back-edge="true"][data-run="latest"]')).toBeTruthy();
    unmount();
    const stopped = at({ node: "for_each::s::step_039", iteration: 4, status: "stopped", reason: "A matching chart exists." });
    expect(screen.getByRole("button", { name: /Steps 40–42/ }).getAttribute("data-run")).toBe("halted");
    expect(stopped.container.querySelector('.opg-edges g[data-kind="exception"][data-run="latest"]')).toBeTruthy();
    expect(screen.getByLabelText("Selected program step").textContent).toContain("A matching chart exists.");
  });

  it("traceStateFromPlayhead marks earlier steps reached", () => {
    const display = deriveDisplayGraph(referral.spec, { folds: referral.folds });
    const state = traceStateFromPlayhead(display.spec, { node: "for_each::s::step_020", iteration: 1, status: "running" }, display.ownerOf);
    expect(state.nodes.get("name")?.status).toBe("done");
    expect(state.nodes.get("address")?.status).toBe("active");
    expect(state.nodes.has("contact")).toBe(false);
  });

  it("can drop the chrome: no title bar, tabs, player or inspector", () => {
    const { container } = render(<ProgramMap spec={referral.spec} folds={referral.folds} trace={referral.trace} />);
    expect(container.querySelector(".opg-topbar")).toBeNull();
    expect(container.querySelector(".opg-player")).toBeNull();
    expect(screen.queryByRole("tablist")).toBeNull();
    expect(screen.queryByRole("tabpanel")).toBeNull();
    expect(screen.queryByLabelText("Selected program step")).toBeNull();
    // Seven cards, and the stop card the trace ends on.
    expect(container.querySelectorAll(".opg-node").length).toBe(8);
  });

  it("renders the inspector, the evidence lanes and the stop rules on their own", () => {
    render(<StepInspector spec={referral.spec} folds={referral.folds} nodeId="for_each::s::step_040" plainStopRules />);
    expect(screen.getByLabelText("Selected program step").textContent).toContain("Create the chart");
    expect(screen.getByText("Stops if reading the saved record back doesn’t confirm it.")).toBeTruthy();
    cleanup();
    render(<EvidenceLanes spec={processRecords} />);
    expect(screen.getByRole("table")).toBeTruthy();
    cleanup();
    render(<StopRules spec={referral.spec} trace={referral.trace} plainStopRules />);
    expect(screen.getByText(/this run stopped at step 41/)).toBeTruthy();
  });
});
