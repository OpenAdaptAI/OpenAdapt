import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { defaultTourStops, ProgramWorkbench, withMedia, type MediaContext } from "../src/index.js";
import { referral } from "./fixtures.js";

afterEach(cleanup);
const spec = withMedia(referral.spec, referral.media);
const end = referral.trace.events.length;

describe("compact stage", () => {
  it("folds straight runs when no folds are given, zooms, expands and closes the inspector", () => {
    const { container } = render(<ProgramWorkbench spec={referral.spec} compact />);
    expect(container.querySelectorAll(".opg-node").length).toBeLessThan(20);
    const viewport = container.querySelector(".opg-canvas-viewport")!;
    expect(viewport.getAttribute("data-stage")).toBe("true");
    fireEvent.click(screen.getByRole("button", { name: "Zoom out" }));
    expect(screen.getByText("90%")).toBeTruthy();
    expect((container.querySelector(".opg-canvas") as HTMLElement).style.transform).toBe("scale(0.9)");
    fireEvent.click(screen.getByRole("button", { name: "Expand" }));
    expect(viewport.hasAttribute("data-stage")).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Close the step details" }));
    expect(screen.queryByLabelText("Selected program step")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Show step details" }));
    expect(screen.getByLabelText("Selected program step")).toBeTruthy();
  });

  it("keeps explicit folds, and null means no folds", () => {
    const { container } = render(<ProgramWorkbench spec={referral.spec} compact folds={null} />);
    expect(container.querySelectorAll(".opg-node").length).toBe(31);
  });
});

describe("media", () => {
  it("shows nothing without a resolver: the host decides what is safe", () => {
    render(<ProgramWorkbench spec={spec} folds={referral.folds} trace={referral.trace} initialCursor={end} />);
    expect(screen.queryAllByRole("img", { name: /recorded|this run/i })).toHaveLength(0);
  });

  it("shows the recorded crop and this run's frame side by side, lazily, through the resolver", () => {
    const resolve = vi.fn((ref: string, context: MediaContext) => `/media/${context.kind}/${ref}`);
    render(<ProgramWorkbench spec={spec} folds={referral.folds} trace={referral.trace} initialCursor={end} resolveMedia={resolve} />);
    const inspector = screen.getByLabelText("Selected program step");
    const pictures = within(inspector).getAllByRole("img");
    expect(pictures.map((img) => img.getAttribute("src"))).toEqual([
      "/media/recorded-crop/recorded/step_039.webp",
      "/media/live/run/r4/step_039.webp",
    ]);
    for (const img of pictures) expect(img.getAttribute("loading")).toBe("lazy");
    expect(inspector.textContent).toContain("This run, iteration 4");
    expect(resolve).toHaveBeenCalledWith("run/r4/step_039.webp", {
      node: "for_each::s::step_039",
      kind: "live",
      iteration: 4,
    });
    // The player shows the live frame of the step the run is at.
    expect(screen.getByRole("img", { name: "The screen in this run now" }).getAttribute("src")).toBe(
      "/media/live/run/r4/step_039.webp",
    );
  });

  it("a resolver can refuse a reference", () => {
    render(
      <ProgramWorkbench spec={spec} folds={referral.folds} trace={referral.trace} initialCursor={end} resolveMedia={(ref) => (ref.startsWith("recorded/") ? `/m/${ref}` : null)} />,
    );
    expect(within(screen.getByLabelText("Selected program step")).getAllByRole("img")).toHaveLength(1);
  });
});

describe("receipt", () => {
  it("appears when playback reaches the end, with its link", () => {
    vi.useFakeTimers();
    render(<ProgramWorkbench spec={referral.spec} trace={referral.trace} initialCursor={end - 1} />);
    expect(screen.queryByRole("region", { name: "Seal" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Step forward" }));
    const card = screen.getByRole("region", { name: "Seal" });
    expect(card.textContent).toContain("HALTED_BEFORE_EFFECT");
    expect(within(card).getByRole("link", { name: "Open the Seal" }).getAttribute("href")).toMatch(/4-r6-rosa-delgado\/engine\/seal\.json$/);
    vi.useRealTimers();
  });
});

describe("tour", () => {
  it("builds six stops from the referral program and its run", () => {
    expect(defaultTourStops(referral.spec, { trace: referral.trace }).map((stop) => stop.id)).toEqual([
      "recording",
      "step",
      "loop",
      "right-record",
      "stop",
      "receipt",
    ]);
  });

  it("opens unless seen, drives selection and playback, and reports how it ended", () => {
    const onTourEnd = vi.fn();
    const { container } = render(
      <ProgramWorkbench spec={referral.spec} folds={referral.folds} trace={referral.trace} tour onTourEnd={onTourEnd} iterationNoun="referral" />,
    );
    const tour = () => screen.getByRole("region", { name: /Recorded once|One step|The loop|right-record|A stop|The Seal/ });
    expect(tour().textContent).toContain("1 of 6");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(tour().textContent).toContain("The loop");
    expect(screen.getByText("Referral 2 of 4")).toBeTruthy();
    expect(container.querySelector('.opg-edges g[data-back-edge="true"][data-run="latest"]')).toBeTruthy();
    expect(screen.getByLabelText("Selected program step").textContent).toContain("Repeat the bounded steps");
    fireEvent.keyDown(tour(), { key: "ArrowRight" });
    fireEvent.keyDown(tour(), { key: "ArrowRight" });
    expect(tour().textContent).toContain("A stop");
    expect(screen.getByLabelText("Selected program step").textContent).toContain("OpenEMR found a matching chart");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("region", { name: "Seal" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onTourEnd).toHaveBeenCalledWith("done");
    expect(screen.getByRole("button", { name: "Take the tour" })).toBeTruthy();
  });

  it("stays closed when seen; Escape skips it", () => {
    const onTourEnd = vi.fn();
    render(<ProgramWorkbench spec={referral.spec} trace={referral.trace} tour tourSeen onTourEnd={onTourEnd} />);
    expect(screen.queryByText(/1 of 6/)).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Take the tour" }));
    const heading = screen.getByRole("heading", { name: "Recorded once" });
    expect(document.activeElement).toBe(heading);
    act(() => {
      fireEvent.keyDown(heading, { key: "Escape" });
    });
    expect(onTourEnd).toHaveBeenCalledWith("skipped");
  });
});

describe("receipt status", () => {
  it("marks a stopped run's Seal as a stop", () => {
    render(<ProgramWorkbench spec={referral.spec} trace={referral.trace} initialCursor={end} />);
    expect(screen.getByRole("region", { name: "Seal" }).getAttribute("data-status")).toBe("halted_before_effect");
  });
});
