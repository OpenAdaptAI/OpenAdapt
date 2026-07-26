import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { engineTry } from "../lib/engine";
import type { CapabilityReport, ExecutionTarget } from "../lib/types";
import { ExecutionTargetForm, capabilityPill } from "./ExecutionTargetForm";

vi.mock("../lib/engine", async (importOriginal) => {
  const original = await importOriginal<typeof import("../lib/engine")>();
  return {
    ...original,
    engineTry: vi.fn(),
  };
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function report(): CapabilityReport {
  return {
    schema: "openadapt-desktop.capability-report/v1",
    generated_at: "2026-07-26T00:00:00+00:00",
    host: { os: "Darwin", os_version: "15.5", arch: "arm64", app_version: "0.14.0" },
    surfaces: {
      web: {
        state: "available",
        detail: "Playwright with a local Chromium build is ready.",
        remediation: null,
        driver: { name: "playwright", version: "1.50.0" },
      },
      macos: {
        state: "permission_required",
        detail: "macOS has not granted this app the Accessibility permission.",
        remediation:
          "Open System Settings > Privacy & Security > Accessibility, enable OpenAdapt Desktop.",
        driver: { name: "pyobjc-framework-applicationservices", version: "10.3" },
      },
      citrix: {
        state: "driver_required",
        detail: "No Citrix Workspace client app was found on this host.",
        remediation: "Install the Citrix Workspace app from citrix.com.",
        driver: { name: "Citrix Workspace", version: null },
      },
      linux: {
        state: "unsupported_host",
        detail: "The Linux AT-SPI surface cannot exist on Darwin.",
        remediation: "Run OpenAdapt Desktop on the Linux host.",
        driver: null,
      },
    },
  };
}

function renderForm(
  target: ExecutionTarget,
  capabilities: CapabilityReport | null | undefined,
  onChange: (t: ExecutionTarget) => void = () => {},
) {
  return render(
    <ExecutionTargetForm
      target={target}
      onChange={onChange}
      idPrefix="t"
      capabilities={capabilities}
    />,
  );
}

it("shows Available only for a detected-available surface", () => {
  renderForm({ backend: "web" }, report());
  expect(screen.getByText("Available")).toBeTruthy();
});

it("shows Permission required with the remediation text", () => {
  renderForm({ backend: "macos" }, report());
  expect(screen.getByText("Permission required")).toBeTruthy();
  expect(
    screen.getByText(/Privacy & Security > Accessibility/),
  ).toBeTruthy();
});

it("shows Driver required with the exact install remediation", () => {
  renderForm({ backend: "citrix" }, report());
  expect(screen.getByText("Driver required")).toBeTruthy();
  expect(
    screen.getByText(/Install the Citrix Workspace app from citrix\.com\./),
  ).toBeTruthy();
});

it("shows Not on this host for an unsupported surface", () => {
  renderForm({ backend: "linux" }, report());
  expect(screen.getByText("Not on this host")).toBeTruthy();
  expect(screen.getByText(/cannot exist on Darwin/)).toBeTruthy();
});

it("never claims Available before detection arrives", () => {
  renderForm({ backend: "web" }, null);
  expect(screen.queryByText("Available")).toBeNull();
  expect(screen.getByText("Checking availability")).toBeTruthy();
});

it("keeps every surface selectable even when not available", () => {
  const changes: ExecutionTarget[] = [];
  renderForm({ backend: "web" }, report(), (t) => changes.push(t));
  const select = screen.getByLabelText("Application surface") as HTMLSelectElement;
  expect(select.options.length).toBe(6);
  fireEvent.change(select, { target: { value: "citrix" } });
  expect(changes).toEqual([{ backend: "citrix" }]);
});

it("fetches the capability report from the engine when no prop is given", async () => {
  vi.mocked(engineTry).mockResolvedValue(report());
  render(
    <ExecutionTargetForm
      target={{ backend: "macos" }}
      onChange={() => {}}
      idPrefix="t"
    />,
  );
  await waitFor(() =>
    expect(screen.getByText("Permission required")).toBeTruthy(),
  );
  expect(vi.mocked(engineTry)).toHaveBeenCalledWith(
    "get_capabilities",
    {},
    null,
  );
});

it("maps every capability state to a pill and unknown input to neutral", () => {
  expect(capabilityPill(null).label).toBe("Checking availability");
  expect(capabilityPill(report().surfaces.web).label).toBe("Available");
  expect(capabilityPill(report().surfaces.macos).label).toBe("Permission required");
  expect(capabilityPill(report().surfaces.citrix).label).toBe("Driver required");
  expect(capabilityPill(report().surfaces.linux).label).toBe("Not on this host");
});
