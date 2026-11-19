// The first screen describes OpenAdapt for any app. Regulated settings may
// appear as examples, never as who the product is for.
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";

import { Welcome } from "./Welcome";

afterEach(cleanup);

function welcomeText(): string {
  const { container } = render(
    <Welcome onSample={() => {}} onShow={() => {}} onConnectCloud={() => {}} />,
  );
  return (container.textContent ?? "").replace(/\s+/g, " ");
}

test("says what OpenAdapt does for any app, for a person or an AI agent", () => {
  const text = welcomeText();
  expect(text).toContain("A person or an AI agent does a workflow once, in any app.");
  expect(text).toContain("checks each save by reading it back");
  expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
    "Do a workflow once. OpenAdapt repeats it.",
  );
});

test("names regulated settings only as examples", () => {
  const text = welcomeText();
  expect(text).toContain("such as medical records and insurance");
  expect(text).not.toMatch(/EMR|payer|Citrix|patient|member|clinic/i);
});
