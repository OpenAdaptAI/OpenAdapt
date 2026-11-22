import { evaluateReadBack } from "./guard-core.js";
import { getOrCreateKeyPair, signReceipt, verifyReceipt } from "./receipt.js";

const CAPTURE_STORAGE_KEY = "openadaptGuardLatestCapture";
const RECEIPT_STORAGE_KEY = "openadaptGuardLatestReceipt";

const badgeEl = document.getElementById("verdict-badge");
const armBtn = document.getElementById("arm-btn");
const armStatusEl = document.getElementById("arm-status");
const summarySection = document.getElementById("summary-section");
const pagePathEl = document.getElementById("page-path");
const verifiedAtEl = document.getElementById("verified-at");
const excludedCountEl = document.getElementById("excluded-count");
const bannersBox = document.getElementById("banners-box");
const bannersList = document.getElementById("banners-list");
const diffBody = document.getElementById("diff-body");
const receiptActions = document.getElementById("receipt-actions");
const verifyBtn = document.getElementById("verify-btn");
const exportBtn = document.getElementById("export-btn");
const verifyStatusEl = document.getElementById("verify-status");

let currentReceipt = null;

function setBadge(verdict) {
  badgeEl.className = "badge";
  if (verdict === "PASS") {
    badgeEl.classList.add("badge-pass");
    badgeEl.textContent = "VERIFIED";
  } else if (verdict === "MISMATCH") {
    badgeEl.classList.add("badge-mismatch");
    badgeEl.textContent = "MISMATCH CAUGHT";
  } else if (verdict === "ERROR_BANNER") {
    badgeEl.classList.add("badge-error");
    badgeEl.textContent = "ERROR BANNER";
  } else {
    badgeEl.classList.add("badge-idle");
    badgeEl.textContent = "IDLE";
  }
}

function renderReceipt(receipt) {
  const payload = receipt.payload;
  setBadge(payload.verdict);
  summarySection.classList.remove("hidden");
  receiptActions.classList.remove("hidden");

  pagePathEl.textContent = payload.pagePath || "(unknown)";
  verifiedAtEl.textContent = payload.verifiedAt || receipt.signedAt || "";
  excludedCountEl.textContent = `${payload.excludedSensitiveCount || 0} sensitive field(s) skipped`;

  bannersList.textContent = "";
  if (payload.errorBanners && payload.errorBanners.length > 0) {
    bannersBox.classList.remove("hidden");
    for (const banner of payload.errorBanners) {
      const li = document.createElement("li");
      li.textContent = banner;
      bannersList.appendChild(li);
    }
  } else {
    bannersBox.classList.add("hidden");
  }

  diffBody.textContent = "";
  for (const row of payload.diffs || []) {
    const tr = document.createElement("tr");

    const fieldTd = document.createElement("td");
    fieldTd.textContent = row.field;
    fieldTd.title = row.field;

    const intendedTd = document.createElement("td");
    intendedTd.textContent = row.intended;
    intendedTd.title = row.intended;

    const observedTd = document.createElement("td");
    observedTd.textContent = row.observed === null ? "(missing)" : row.observed;
    observedTd.title = observedTd.textContent;

    const statusTd = document.createElement("td");
    statusTd.textContent = row.status;
    statusTd.className = row.status === "MATCH" ? "status-match" : "status-mismatch";

    tr.append(fieldTd, intendedTd, observedTd, statusTd);
    diffBody.appendChild(tr);
  }
}

async function ensureSignedReceipt() {
  const stored = await chrome.storage.local.get([CAPTURE_STORAGE_KEY, RECEIPT_STORAGE_KEY]);
  const capture = stored[CAPTURE_STORAGE_KEY];
  const cachedReceipt = stored[RECEIPT_STORAGE_KEY];

  if (!capture) {
    setBadge("IDLE");
    return null;
  }

  if (
    cachedReceipt &&
    cachedReceipt.payload?.submittedAt === capture.submittedAt &&
    cachedReceipt.payload?.verifiedAt === capture.verifiedAt
  ) {
    currentReceipt = cachedReceipt;
    renderReceipt(cachedReceipt);
    return cachedReceipt;
  }

  const evaluation = evaluateReadBack({
    intended: capture.intended,
    observed: capture.observed,
    errorBanners: capture.errorBanners,
  });

  const payload = {
    pagePath: capture.pagePath,
    submittedAt: capture.submittedAt,
    verifiedAt: capture.verifiedAt,
    excludedSensitiveCount: capture.excludedSensitiveCount || 0,
    ...evaluation,
  };

  const keyPair = await getOrCreateKeyPair(chrome.storage.local);
  const signed = await signReceipt(payload, keyPair);
  await chrome.storage.local.set({ [RECEIPT_STORAGE_KEY]: signed });
  currentReceipt = signed;
  renderReceipt(signed);
  return signed;
}

armBtn.addEventListener("click", async () => {
  try {
    const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!tab?.id) {
      armStatusEl.textContent = "No active tab available to arm.";
      return;
    }
    await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      files: ["content.js"],
    });
    armStatusEl.textContent = "Guard armed on this tab. Submit a form, then reopen this popup.";
  } catch (err) {
    armStatusEl.textContent = `Could not arm this tab: ${err instanceof Error ? err.message : String(err)}`;
  }
});

verifyBtn.addEventListener("click", async () => {
  if (!currentReceipt) {
    verifyStatusEl.textContent = "No signed receipt available yet.";
    return;
  }
  const result = await verifyReceipt(currentReceipt);
  verifyStatusEl.textContent = result.valid
    ? "Ed25519 signature verified locally (valid)."
    : `Signature verification FAILED: ${result.reason}`;
});

exportBtn.addEventListener("click", async () => {
  if (!currentReceipt) {
    verifyStatusEl.textContent = "No signed receipt available to export.";
    return;
  }
  const json = `${JSON.stringify(currentReceipt, null, 2)}\n`;
  const blob = new Blob([json], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  const stamp = (currentReceipt.signedAt || new Date().toISOString()).replace(/[:.]/g, "-");
  link.href = url;
  link.download = `openadapt-guard-receipt-${stamp}.json`;
  link.click();
  URL.revokeObjectURL(url);
  verifyStatusEl.textContent = "Exported signed receipt JSON locally.";
});

(async () => {
  try {
    await ensureSignedReceipt();
  } catch (err) {
    armStatusEl.textContent = `Error loading receipt: ${err instanceof Error ? err.message : String(err)}`;
  }
})();
