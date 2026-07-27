import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { Login } from "./Login";

vi.mock("../lib/engine", () => ({
  CMD: { LOGIN_BROWSER: "login_browser", LOGIN_PASTE: "login_paste" },
  engineInvoke: vi.fn(),
  openExternal: vi.fn(),
}));

it("opens the local cockpit without requiring Cloud authentication", () => {
  const onLocal = vi.fn();
  render(<Login onAuthed={() => {}} onLocal={onLocal} />);

  fireEvent.click(screen.getByRole("button", { name: "Continue locally" }));

  expect(onLocal).toHaveBeenCalledOnce();
});
