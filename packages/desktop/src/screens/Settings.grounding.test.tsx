import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

// "Continue locally" users have no Cloud workspace, so the engine resolves
// its fail-closed default policy. That is not an organization's policy, and
// the grounding card must not send them to an admin or a dashboard.

vi.mock("../lib/engine", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...actual,
    engineTry: vi.fn(),
    openExternal: vi.fn(async () => {}),
    updaterAvailable: vi.fn(async () => false),
  };
});

import { CMD, engineTry } from "../lib/engine";
import { Settings } from "./Settings";

const FAIL_CLOSED = {
  safety: {},
  user: {},
  org: {},
  is_admin: false,
  role: "member",
  policy_version: null,
  source: "fail-closed-default",
};

function withPolicy(policy: unknown) {
  vi.mocked(engineTry).mockImplementation(async (command, _params, fallback) =>
    command === CMD.GET_EFFECTIVE_POLICY ? policy : fallback,
  );
}

async function groundingCard() {
  const heading = await screen.findByText("How OpenAdapt finds buttons and fields");
  return heading.closest(".card") as HTMLElement;
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

it("says local only, not managed by an organization, with no workspace", async () => {
  withPolicy(FAIL_CLOSED);
  const onConnectCloud = vi.fn();
  render(
    <Settings
      auth={{ authenticated: false }}
      onConnectCloud={onConnectCloud}
      onSignedOut={() => {}}
    />,
  );

  const card = await groundingCard();
  expect(await within(card).findByText("Off (local only)")).toBeTruthy();
  expect(within(card).queryByText(/Managed by your organization|organization manages/i)).toBeNull();
  expect(within(card).queryByText(/Only an organization admin/)).toBeNull();
  expect(within(card).queryByRole("button", { name: /cloud dashboard/i })).toBeNull();

  fireEvent.click(within(card).getByRole("button", { name: "Connect to OpenAdapt Cloud" }));
  expect(onConnectCloud).toHaveBeenCalledTimes(1);
});

it("still says the organization manages it when Cloud sent a policy without the setting", async () => {
  withPolicy({ ...FAIL_CLOSED, source: "network" });
  render(
    <Settings
      auth={{ authenticated: true, org_id: "org-1" }}
      onConnectCloud={() => {}}
      onSignedOut={() => {}}
    />,
  );

  const card = await groundingCard();
  expect(await within(card).findByText(/organization manages this setting/)).toBeTruthy();
  expect(within(card).getByText(/Only an organization admin/)).toBeTruthy();
});
