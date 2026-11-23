import assert from "node:assert/strict";
import { readFileSync, existsSync } from "node:fs";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { evaluateReadBack, extractFormSnapshot, isSensitiveField } from "../guard-core.js";
import {
  RECEIPT_SCHEMA,
  canonicalizePayload,
  generateSigningKeyPair,
  getOrCreateKeyPair,
  signReceipt,
  verifyReceipt,
} from "../receipt.js";

const EXT_DIR = dirname(dirname(fileURLToPath(import.meta.url)));

test("manifest.json is valid Manifest V3 with minimal permissions and zero broad host access", () => {
  const manifest = JSON.parse(readFileSync(join(EXT_DIR, "manifest.json"), "utf8"));
  assert.equal(manifest.manifest_version, 3);
  assert.equal(manifest.name, "OpenAdapt Guard");
  assert.ok(Array.isArray(manifest.permissions));
  assert.deepEqual([...manifest.permissions].sort(), ["activeTab", "scripting", "storage"]);
  assert.equal(manifest.host_permissions, undefined);
  assert.equal(manifest.content_scripts, undefined);
  assert.ok(manifest.action?.default_popup);
  assert.ok(existsSync(join(EXT_DIR, manifest.action.default_popup)));
  assert.equal(manifest.icons, undefined);
});

test("extension source files have zero network egress, zero eval, and zero inline scripts", () => {
  const jsFiles = ["guard-core.js", "receipt.js", "content.js", "popup.js"];
  for (const file of jsFiles) {
    const src = readFileSync(join(EXT_DIR, file), "utf8");
    assert.doesNotMatch(src, /\bfetch\s*\(/, `${file} must not call fetch()`);
    assert.doesNotMatch(src, /\bXMLHttpRequest\b/, `${file} must not use XMLHttpRequest`);
    assert.doesNotMatch(src, /\bWebSocket\b/, `${file} must not open WebSockets`);
    assert.doesNotMatch(src, /\beval\s*\(/, `${file} must not call eval()`);
    assert.doesNotMatch(src, /\bnew\s+Function\b/, `${file} must not call new Function()`);
    assert.doesNotMatch(src, /innerHTML\s*=/, `${file} must not assign innerHTML`);
  }

  const popupHtml = readFileSync(join(EXT_DIR, "popup.html"), "utf8");
  assert.doesNotMatch(
    popupHtml,
    /\son[a-z]+\s*=/i,
    "popup.html must not contain inline event handlers"
  );
  assert.doesNotMatch(
    popupHtml,
    /<script(?![^>]*\bsrc=)/i,
    "popup.html must not contain inline <script> blocks"
  );
});

test("fail-closed privacy excludes passwords, hidden tokens, credit cards, and unnamed fields", () => {
  assert.equal(isSensitiveField({ type: "password", name: "pw" }), true);
  assert.equal(isSensitiveField({ type: "hidden", name: "form_build_id" }), true);
  assert.equal(isSensitiveField({ type: "text", name: "csrf_token" }), true);
  assert.equal(isSensitiveField({ type: "text", name: "card", autocomplete: "cc-number" }), true);
  assert.equal(isSensitiveField({ type: "text", name: "patient_name" }), false);

  const snapshot = extractFormSnapshot([
    { name: "patient_name", type: "text", value: "  Jane   Doe  " },
    { name: "visit_reason", type: "text", value: "Annual follow-up" },
    { name: "password", type: "password", value: "super-secret-123" },
    { name: "csrf_token", type: "hidden", value: "tok_abc123" },
    { name: "api_key", type: "text", value: "sk-live-secret" },
    { name: "card_number", type: "text", autocomplete: "cc-number", value: "4111-1111" },
  ]);

  assert.deepEqual(snapshot.captured, {
    patient_name: "Jane Doe",
    visit_reason: "Annual follow-up",
  });
  assert.equal(snapshot.excludedCount, 4);
  assert.equal("password" in snapshot.captured, false);
  assert.equal("csrf_token" in snapshot.captured, false);
  assert.equal("api_key" in snapshot.captured, false);
  assert.equal("card_number" in snapshot.captured, false);
});

test("evaluateReadBack returns deterministic PASS, MISMATCH, and ERROR_BANNER verdicts", () => {
  const pass = evaluateReadBack({
    intended: { patient_name: "Jane Doe", dosage: "10 mg" },
    observed: { patient_name: "Jane   Doe", dosage: "10 mg" },
    errorBanners: [],
  });
  assert.equal(pass.verdict, "PASS");
  assert.equal(pass.mismatchCount, 0);
  assert.equal(pass.checkedFields, 2);

  const mismatch = evaluateReadBack({
    intended: { patient_name: "Jane Doe", dosage: "10 mg" },
    observed: { patient_name: "Jane Doe", dosage: "100 mg" },
    errorBanners: [],
  });
  assert.equal(mismatch.verdict, "MISMATCH");
  assert.equal(mismatch.mismatchCount, 1);
  assert.deepEqual(
    mismatch.diffs.find((d) => d.field === "dosage"),
    { field: "dosage", intended: "10 mg", observed: "100 mg", status: "MISMATCH" }
  );

  const errorBanner = evaluateReadBack({
    intended: { patient_name: "Jane Doe" },
    observed: { patient_name: "Jane Doe" },
    errorBanners: ["Save failed: record locked by another user"],
  });
  assert.equal(errorBanner.verdict, "ERROR_BANNER");
  assert.deepEqual(errorBanner.errorBanners, ["Save failed: record locked by another user"]);
});

test("Ed25519 receipt signing persists keypair, verifies valid receipts, and rejects tampering", async () => {
  const backing = new Map();
  const mockStorage = {
    async get(key) {
      return { [key]: backing.get(key) };
    },
    async set(items) {
      for (const [k, v] of Object.entries(items)) {
        backing.set(k, v);
      }
    },
  };

  const kp1 = await getOrCreateKeyPair(mockStorage);
  const kp2 = await getOrCreateKeyPair(mockStorage);
  assert.deepEqual(kp1.publicKeyJwk, kp2.publicKeyJwk);

  const payload = {
    pagePath: "https://ehr.example.local/intake",
    submittedAt: "2026-11-22T18:00:00.000Z",
    verifiedAt: "2026-11-22T18:00:01.000Z",
    excludedSensitiveCount: 2,
    ...evaluateReadBack({
      intended: { patient_name: "Jane Doe", dosage: "10 mg" },
      observed: { patient_name: "Jane Doe", dosage: "10 mg" },
      errorBanners: [],
    }),
  };

  assert.equal(
    canonicalizePayload({ b: 1, a: { d: 2, c: 3 } }),
    '{"a":{"c":3,"d":2},"b":1}'
  );

  const receipt = await signReceipt(payload, kp1);
  assert.equal(receipt.schema, RECEIPT_SCHEMA);
  assert.equal(receipt.algorithm, "Ed25519");

  const ok = await verifyReceipt(receipt);
  assert.equal(ok.valid, true);

  // Tamper with verdict -> verification MUST fail
  const tampered = structuredClone(receipt);
  tampered.payload.verdict = "MISMATCH";
  const bad = await verifyReceipt(tampered);
  assert.equal(bad.valid, false);

  // Wrong public key -> verification MUST fail
  const otherKeys = await generateSigningKeyPair();
  const otherReceipt = await signReceipt(payload, otherKeys);
  const mismatchedKey = { ...receipt, publicKeyJwk: otherReceipt.publicKeyJwk };
  const wrongKeyResult = await verifyReceipt(mismatchedKey);
  assert.equal(wrongKeyResult.valid, false);
});
