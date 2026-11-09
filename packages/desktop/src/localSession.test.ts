import { beforeEach, expect, it } from "vitest";
import {
  clearLocalSession,
  localSessionEnabled,
  rememberLocalSession,
} from "./localSession";

beforeEach(() => localStorage.clear());

it("remembers and clears the local-first entry choice", () => {
  expect(localSessionEnabled()).toBe(false);
  rememberLocalSession();
  expect(localSessionEnabled()).toBe(true);
  clearLocalSession();
  expect(localSessionEnabled()).toBe(false);
});
