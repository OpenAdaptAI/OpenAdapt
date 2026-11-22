/**
 * OpenAdapt Guard content script (injected on-demand via activeTab + scripting).
 * Watches form submissions, excludes sensitive fields fail-closed, waits for DOM
 * settle, reads back visible persisted values and error banners, and persists a
 * sanitized capture record to chrome.storage.local (zero network egress).
 */

(() => {
  if (globalThis.__OPENADAPT_GUARD_ARMED__) {
    return;
  }
  globalThis.__OPENADAPT_GUARD_ARMED__ = true;

  const CAPTURE_STORAGE_KEY = "openadaptGuardLatestCapture";
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

  function normalizeValue(value) {
    if (value === null || value === undefined) return "";
    return String(value).replace(/\s+/g, " ").trim();
  }

  function isSensitiveElement(el) {
    const type = String(el.type || "text").toLowerCase().trim();
    if (SENSITIVE_TYPES.has(type)) return true;
    const autocomplete = String(el.getAttribute?.("autocomplete") || "").toLowerCase().trim();
    if (autocomplete) {
      for (const token of autocomplete.split(/\s+/)) {
        if (SENSITIVE_AUTOCOMPLETE.has(token) || token.startsWith("cc-")) {
          return true;
        }
      }
    }
    const identifier = `${el.name || ""} ${el.id || ""}`.trim();
    return Boolean(identifier && SENSITIVE_NAME_PATTERN.test(identifier));
  }

  function readElementValue(el) {
    const type = String(el.type || "").toLowerCase();
    if (type === "checkbox" || type === "radio") {
      return el.checked ? normalizeValue(el.value || "on") : "";
    }
    return normalizeValue(el.value);
  }

  function snapshotForm(form) {
    const captured = {};
    let excludedCount = 0;
    const controls = form.querySelectorAll("input, select, textarea");
    for (const el of controls) {
      if (el.disabled) continue;
      const type = String(el.type || "").toLowerCase();
      if (type === "submit" || type === "button" || type === "reset" || type === "image") {
        continue;
      }
      if (isSensitiveElement(el)) {
        excludedCount += 1;
        continue;
      }
      const key = String(el.name || el.id || el.getAttribute("aria-label") || "").trim();
      if (!key) {
        excludedCount += 1;
        continue;
      }
      if (type === "radio" && !el.checked) {
        continue;
      }
      captured[key] = readElementValue(el);
    }
    return { captured, excludedCount };
  }

  function collectErrorBanners() {
    const selectors = [
      '[role="alert"]',
      '[aria-live="assertive"]',
      ".error",
      ".alert-danger",
      ".alert-error",
      "[data-error]",
    ];
    const seen = new Set();
    const banners = [];
    for (const el of document.querySelectorAll(selectors.join(","))) {
      const style = globalThis.getComputedStyle?.(el);
      if (style && (style.display === "none" || style.visibility === "hidden")) {
        continue;
      }
      const text = normalizeValue(el.textContent);
      if (text && !seen.has(text)) {
        seen.add(text);
        banners.push(text.slice(0, 240));
      }
    }
    return banners;
  }

  function readBackFields(intendedKeys) {
    const observed = {};
    for (const key of intendedKeys) {
      const escaped = globalThis.CSS?.escape ? CSS.escape(key) : key;
      const control =
        document.querySelector(`[name="${escaped}"], #${escaped}`) ||
        document.querySelector(`[data-field="${escaped}"]`);
      if (!control) continue;
      if (control.matches?.("input, select, textarea")) {
        if (!isSensitiveElement(control)) {
          observed[key] = readElementValue(control);
        }
      } else {
        observed[key] = normalizeValue(control.textContent);
      }
    }
    return observed;
  }

  function waitForDomSettle(quietMs = 350, maxWaitMs = 2000) {
    return new Promise((resolve) => {
      let quietTimer = null;
      const maxTimer = setTimeout(done, maxWaitMs);
      const observer = new MutationObserver(() => {
        clearTimeout(quietTimer);
        quietTimer = setTimeout(done, quietMs);
      });
      function done() {
        clearTimeout(quietTimer);
        clearTimeout(maxTimer);
        observer.disconnect();
        resolve();
      }
      if (document.body) {
        observer.observe(document.body, {
          childList: true,
          subtree: true,
          characterData: true,
          attributes: true,
        });
      }
      quietTimer = setTimeout(done, quietMs);
    });
  }

  document.addEventListener(
    "submit",
    async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement)) return;
      const { captured, excludedCount } = snapshotForm(form);
      const intendedKeys = Object.keys(captured);
      if (intendedKeys.length === 0) return;

      const pagePath = `${window.location.origin}${window.location.pathname}`;
      const submittedAt = new Date().toISOString();

      await waitForDomSettle();

      const observed = readBackFields(intendedKeys);
      const errorBanners = collectErrorBanners();

      await chrome.storage.local.set({
        [CAPTURE_STORAGE_KEY]: {
          pagePath,
          submittedAt,
          verifiedAt: new Date().toISOString(),
          excludedSensitiveCount: excludedCount,
          intended: captured,
          observed,
          errorBanners,
        },
      });
    },
    true
  );
})();
