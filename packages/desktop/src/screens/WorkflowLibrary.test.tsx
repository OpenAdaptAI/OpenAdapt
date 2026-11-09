import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { engineTry } from "../lib/engine";
import { WorkflowLibrary } from "./WorkflowLibrary";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn() };
});

afterEach(cleanup);

it("routes deployment preparation through one Qualify action", async () => {
  vi.mocked(engineTry).mockResolvedValue([
    { id: "workflow-1", name: "Example", steps: 2, synced: false },
  ]);
  const onQualify = vi.fn();
  render(
    <WorkflowLibrary
      onQualify={onQualify}
      onWatch={() => {}}
      onTeach={() => {}}
      onRecord={() => {}}
    />,
  );

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Qualify" })).toBeTruthy(),
  );
  // A second label for the same handler would offer two different-looking
  // choices that do the same thing.
  expect(screen.queryByRole("button", { name: "Prepare to deploy" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "Qualify" }));

  expect(onQualify).toHaveBeenCalledTimes(1);
  expect(onQualify).toHaveBeenCalledWith("workflow-1");
});

it("offers Teach fix instead for a workflow with an open halt", async () => {
  vi.mocked(engineTry).mockResolvedValue([
    { id: "workflow-2", name: "Halted", steps: 2, synced: false, open_halts: 1 },
  ]);
  const onTeach = vi.fn();
  render(
    <WorkflowLibrary
      onQualify={() => {}}
      onWatch={() => {}}
      onTeach={onTeach}
      onRecord={() => {}}
    />,
  );

  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Teach fix" })).toBeTruthy(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Teach fix" }));
  expect(onTeach).toHaveBeenCalledWith("workflow-2");
});

it.each(["halted", "failed"] as const)(
  "doesn't lead toward the readiness test after a %s run",
  async (lastRun) => {
    vi.mocked(engineTry).mockResolvedValue([
      {
        id: "workflow-3",
        name: "Stopped",
        steps: 2,
        synced: false,
        open_halts: 0,
        last_run_state: lastRun,
      },
    ]);
    const onTeach = vi.fn();
    render(
      <WorkflowLibrary
        onQualify={() => {}}
        onWatch={() => {}}
        onTeach={onTeach}
        onRecord={() => {}}
      />,
    );

    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Watch run" })).toBeTruthy(),
    );
    expect(screen.queryByRole("button", { name: "Qualify" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Prepare to deploy" })).toBeNull();
    if (lastRun === "halted") {
      fireEvent.click(screen.getByRole("button", { name: "Teach fix" }));
      expect(onTeach).toHaveBeenCalledWith("workflow-3");
    }
  },
);
