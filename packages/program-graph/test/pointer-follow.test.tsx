import { act, cleanup, fireEvent, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ProgramMap, ProgramWorkbench, specNodes } from "../src/index.js";
import { processRecords } from "./fixtures.js";

// A graph outside the motivating referral playback, with a controlled external
// playhead: both workbench entry points must preserve the same native gesture.
let frames: Map<number, FrameRequestCallback>;
let nextFrame: number;
beforeEach(() => {
  frames = new Map(); nextFrame = 0;
  vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => { frames.set(++nextFrame, callback); return nextFrame; });
  vi.stubGlobal("cancelAnimationFrame", (id: number) => frames.delete(id));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const frame = () => act(() => { const pending = [...frames.values()]; frames.clear(); pending.forEach((callback) => callback(0)); });
const pointer = (target: Element | Window, type: string, id = 1, button = 0, primary = true) => {
  const event = new MouseEvent(type, { bubbles: true, button });
  Object.defineProperties(event, { pointerId: { value: id }, isPrimary: { value: primary } });
  fireEvent(target, event);
};

for (const [name, Map] of [["workbench", ProgramWorkbench], ["map", ProgramMap]] as const) {
  describe(`${name} camera during native pointer gestures`, () => {
    const mount = () => {
      const onSelect = vi.fn();
      const props = { spec: processRecords, compact: true, folds: null, onSelect };
      const view = render(<Map {...props} playhead={{ node: "open_worklist", status: "running" }} />);
      const viewport = view.container.querySelector(".opg-canvas-viewport") as HTMLElement;
      const target = [...view.container.querySelectorAll<HTMLButtonElement>(".opg-node")].at(-1)!;
      const scrollTo = vi.fn(); viewport.scrollTo = scrollTo;
      onSelect.mockClear();
      const advance = () => view.rerender(<Map {...props} playhead={{ node: "write_update", status: "running" }} />);
      return { ...view, viewport, target, scrollTo, onSelect, advance };
    };

    it("keeps the hit target still through release and click, then follows the latest playhead", () => {
      const m = mount(); pointer(m.target, "pointerdown"); m.advance();
      expect(m.scrollTo).not.toHaveBeenCalled();
      expect(m.onSelect.mock.calls.some(([, origin]) => origin === "user")).toBe(false);
      pointer(m.target, "pointerup");
      expect(m.scrollTo).not.toHaveBeenCalled();
      fireEvent.click(m.target);
      expect(m.onSelect).toHaveBeenLastCalledWith(specNodes(processRecords).at(-1)!.id, "user");
      frame(); expect(m.scrollTo).toHaveBeenCalledTimes(1);
    });

    for (const end of ["pointercancel", "pointerup", "blur"]) {
      it(`resumes after ${end} outside the node without synthesizing activation`, () => {
        const m = mount(); pointer(m.target, "pointerdown"); m.advance();
        expect(m.scrollTo).not.toHaveBeenCalled();
        if (end === "blur") fireEvent.blur(window); else pointer(window, end);
        frame(); expect(m.scrollTo).toHaveBeenCalledTimes(1);
        expect(m.onSelect.mock.calls.some(([, origin]) => origin === "user")).toBe(false);
      });
    }

    it("does not let an old release clear a new gesture that reuses its pointer id", () => {
      const m = mount(); pointer(m.target, "pointerdown"); pointer(window, "pointerup");
      pointer(m.target, "pointerdown"); m.advance(); frame();
      expect(m.scrollTo).not.toHaveBeenCalled();
      pointer(window, "pointercancel"); frame(); expect(m.scrollTo).toHaveBeenCalledTimes(1);
    });

    it("keeps background panning under the visitor's control as the run advances", () => {
      const m = mount(); pointer(m.viewport, "pointerdown"); m.advance();
      expect(m.scrollTo).not.toHaveBeenCalled();
      pointer(window, "pointerup"); frame(); expect(m.scrollTo).toHaveBeenCalledTimes(1);
    });

    it("does not suspend follow for secondary buttons or nonprimary pointers", () => {
      for (const [button, primary] of [[2, true], [0, false]] as const) {
        const m = mount(); pointer(m.target, "pointerdown", 1, button, primary); m.advance();
        expect(m.scrollTo).toHaveBeenCalledTimes(1); m.unmount();
      }
    });
  });
}
