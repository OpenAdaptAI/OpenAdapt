/**
 * Local WebCrypto Ed25519 receipt signing & offline verification.
 * Keys are generated locally and stored only in chrome.storage.local (zero egress).
 */

export const RECEIPT_SCHEMA = "openadapt.guard.receipt.v1";
const STORAGE_KEYPAIR_KEY = "openadaptGuardEd25519Keypair";

function getSubtle() {
  const subtle = globalThis.crypto?.subtle;
  if (!subtle) {
    throw new Error("WebCrypto subtle API is unavailable in this environment.");
  }
  return subtle;
}

function bytesToBase64(bytes) {
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) {
    binary += String.fromCharCode(bytes[i]);
  }
  return btoa(binary);
}

function base64ToBytes(b64) {
  const binary = atob(b64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) {
    bytes[i] = binary.charCodeAt(i);
  }
  return bytes;
}

/**
 * Deterministic recursive key-sorted JSON serialization for signing & verification.
 * @param {unknown} value
 * @returns {string}
 */
export function canonicalizePayload(value) {
  if (value === null || typeof value !== "object") {
    return JSON.stringify(value);
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => canonicalizePayload(item)).join(",")}]`;
  }
  const sortedKeys = Object.keys(value).sort();
  const entries = sortedKeys
    .filter((key) => value[key] !== undefined)
    .map((key) => `${JSON.stringify(key)}:${canonicalizePayload(value[key])}`);
  return `{${entries.join(",")}}`;
}

/**
 * Generate a fresh local Ed25519 keypair.
 */
export async function generateSigningKeyPair() {
  const subtle = getSubtle();
  return subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
}

/**
 * Load an existing Ed25519 keypair from storage, or generate and persist one locally.
 * @param {{ get: (key: string) => Promise<Record<string, unknown>>, set: (items: Record<string, unknown>) => Promise<void> }} storage
 */
export async function getOrCreateKeyPair(storage) {
  const subtle = getSubtle();
  const stored = await storage.get(STORAGE_KEYPAIR_KEY);
  const record = stored?.[STORAGE_KEYPAIR_KEY];

  if (record?.privateJwk && record?.publicJwk) {
    const privateKey = await subtle.importKey(
      "jwk",
      record.privateJwk,
      { name: "Ed25519" },
      true,
      ["sign"]
    );
    const publicKey = await subtle.importKey(
      "jwk",
      record.publicJwk,
      { name: "Ed25519" },
      true,
      ["verify"]
    );
    return { privateKey, publicKey, publicKeyJwk: record.publicJwk };
  }

  const keyPair = await generateSigningKeyPair();
  const privateJwk = await subtle.exportKey("jwk", keyPair.privateKey);
  const publicJwk = await subtle.exportKey("jwk", keyPair.publicKey);
  await storage.set({
    [STORAGE_KEYPAIR_KEY]: {
      privateJwk,
      publicJwk,
      createdAt: new Date().toISOString(),
    },
  });
  return { privateKey: keyPair.privateKey, publicKey: keyPair.publicKey, publicKeyJwk: publicJwk };
}

/**
 * Sign a receipt payload locally with Ed25519.
 * @param {Record<string, unknown>} payload
 * @param {{ privateKey: CryptoKey, publicKey?: CryptoKey, publicKeyJwk?: JsonWebKey }} keyMaterial
 */
export async function signReceipt(payload, keyMaterial) {
  const subtle = getSubtle();
  const publicKeyJwk =
    keyMaterial.publicKeyJwk || (await subtle.exportKey("jwk", keyMaterial.publicKey));
  const canonical = canonicalizePayload(payload);
  const data = new TextEncoder().encode(canonical);
  const sigBuffer = await subtle.sign({ name: "Ed25519" }, keyMaterial.privateKey, data);

  return {
    schema: RECEIPT_SCHEMA,
    algorithm: "Ed25519",
    signedAt: new Date().toISOString(),
    publicKeyJwk,
    payload,
    signatureB64: bytesToBase64(new Uint8Array(sigBuffer)),
  };
}

/**
 * Verify an offline-verifiable Ed25519 receipt JSON object.
 * @param {Record<string, unknown>} receipt
 * @returns {Promise<{ valid: boolean, reason?: string }>}
 */
export async function verifyReceipt(receipt) {
  try {
    if (!receipt || typeof receipt !== "object") {
      return { valid: false, reason: "Receipt must be an object." };
    }
    if (receipt.schema !== RECEIPT_SCHEMA) {
      return { valid: false, reason: `Unsupported receipt schema: ${receipt.schema}` };
    }
    if (receipt.algorithm !== "Ed25519") {
      return { valid: false, reason: `Unsupported algorithm: ${receipt.algorithm}` };
    }
    if (!receipt.publicKeyJwk || typeof receipt.signatureB64 !== "string" || !receipt.payload) {
      return { valid: false, reason: "Receipt is missing publicKeyJwk, payload, or signatureB64." };
    }
    const subtle = getSubtle();
    const publicKey = await subtle.importKey(
      "jwk",
      receipt.publicKeyJwk,
      { name: "Ed25519" },
      false,
      ["verify"]
    );
    const canonical = canonicalizePayload(receipt.payload);
    const data = new TextEncoder().encode(canonical);
    const sigBytes = base64ToBytes(receipt.signatureB64);
    const valid = await subtle.verify({ name: "Ed25519" }, publicKey, sigBytes, data);
    return valid ? { valid: true } : { valid: false, reason: "Ed25519 signature mismatch." };
  } catch (err) {
    return { valid: false, reason: err instanceof Error ? err.message : String(err) };
  }
}
