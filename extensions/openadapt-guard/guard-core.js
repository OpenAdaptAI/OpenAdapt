/**
 * Pure, deterministic form-capture redaction and read-back verification engine.
 * Zero network calls, zero DOM globals required.
 */

const SENSITIVE_TYPES = new Set(["password", "hidden", "file"]);

const SENSITIVE_AUTOCOMPLETE = new Set([
  "current-password",
  "new-password",
  "one-time-code",
  "cc-number",
  "cc-csc",
  "cc-exp",
  "cc-exp-month",
  "cc-exp-year",
]);

const SENSITIVE_NAME_PATTERN =
  /(passw(or)?d|secret|token|csrf|xsrf|nonce|ssn|cvv|cvc|card[_-]?num|auth|api[_-]?key)/i;

/**
 * Fail-closed check: returns true if a field must never be captured or stored.
 * @param {{ type?: string, name?: string, id?: string, autocomplete?: string }} field
 */
export function isSensitiveField(field) {
  if (!field || typeof field !== "object") {
    return true;
  }
  const type = String(field.type || "text").toLowerCase().trim();
  if (SENSITIVE_TYPES.has(type)) {
    return true;
  }
  const autocomplete = String(field.autocomplete || "").toLowerCase().trim();
  if (autocomplete) {
    for (const token of autocomplete.split(/\s+/)) {
      if (SENSITIVE_AUTOCOMPLETE.has(token) || token.startsWith("cc-")) {
        return true;
      }
    }
  }
  const identifier = `${field.name || ""} ${field.id || ""}`.trim();
  if (identifier && SENSITIVE_NAME_PATTERN.test(identifier)) {
    return true;
  }
  return false;
}

/**
 * Normalize a field value for deterministic read-back comparison.
 * @param {unknown} value
 */
export function normalizeValue(value) {
  if (value === null || value === undefined) {
    return "";
  }
  return String(value).replace(/\s+/g, " ").trim();
}

/**
 * Extract a sanitized snapshot of non-sensitive fields from a list of field descriptors.
 * @param {Array<{ key?: string, name?: string, id?: string, label?: string, type?: string, autocomplete?: string, value?: unknown }>} fields
 */
export function extractFormSnapshot(fields) {
  const captured = {};
  let excludedCount = 0;

  for (const field of fields || []) {
    if (isSensitiveField(field)) {
      excludedCount += 1;
      continue;
    }
    const key = String(field.key || field.name || field.id || field.label || "").trim();
    if (!key) {
      excludedCount += 1;
      continue;
    }
    captured[key] = normalizeValue(field.value);
  }

  return { captured, excludedCount };
}

/**
 * Compare intended submitted values against observed post-submit read-back state.
 * @param {{ intended: Record<string, string>, observed: Record<string, string>, errorBanners?: string[] }} input
 */
export function evaluateReadBack({ intended = {}, observed = {}, errorBanners = [] }) {
  const banners = (errorBanners || [])
    .map((text) => normalizeValue(text))
    .filter(Boolean);

  const keys = Object.keys(intended).sort();
  const diffs = [];
  let mismatchCount = 0;

  for (const field of keys) {
    const expected = normalizeValue(intended[field]);
    const hasObserved = Object.prototype.hasOwnProperty.call(observed, field);
    const actual = hasObserved ? normalizeValue(observed[field]) : null;
    const status = actual === expected ? "MATCH" : hasObserved ? "MISMATCH" : "MISSING";
    if (status !== "MATCH") {
      mismatchCount += 1;
    }
    diffs.push({
      field,
      intended: expected,
      observed: actual,
      status,
    });
  }

  let verdict = "PASS";
  if (banners.length > 0) {
    verdict = "ERROR_BANNER";
  } else if (mismatchCount > 0) {
    verdict = "MISMATCH";
  }

  return {
    verdict,
    checkedFields: keys.length,
    mismatchCount,
    errorBanners: banners,
    diffs,
  };
}
