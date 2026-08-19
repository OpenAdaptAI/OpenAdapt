import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { engineTry } from "../lib/engine";
import { WorkflowLibrary } from "./WorkflowLibrary";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return { ...original, engineTry: vi.fn() };
});

afterEach(cleanup);

it("routes deployment preparation through qualification", async () => {
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
    expect(
      screen.getByRole("button", { name: "Prepare to deploy" }),
    ).toBeTruthy(),
  );
  fireEvent.click(screen.getByRole("button", { name: "Prepare to deploy" }));

  expect(onQualify).toHaveBeenCalledWith("workflow-1");
});
