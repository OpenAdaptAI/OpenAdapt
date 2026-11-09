import { ensureControlOverlayCaptureExcluded } from "../lib/engine";

export const LEGACY_OVERLAY_PRESENTATION_KEY =
  "openadapt.control-overlay.include-in-recordings.v1";
export const OVERLAY_PRESENTATION_KEY =
  "openadapt.control-overlay.include-in-exports.v2";

export function overlayPresentationEnabled(): boolean {
  try {
    const current = window.localStorage.getItem(OVERLAY_PRESENTATION_KEY);
    if (current !== null) return current === "true";

    // Preserve the operator's existing choice while changing its boundary:
    // the overlay is now added only to a new presentation derivative, never
    // to raw recording/evidence capture.
    const legacy = window.localStorage.getItem(LEGACY_OVERLAY_PRESENTATION_KEY);
    if (legacy !== null) {
      const enabled = legacy === "true";
      window.localStorage.setItem(OVERLAY_PRESENTATION_KEY, String(enabled));
      window.localStorage.removeItem(LEGACY_OVERLAY_PRESENTATION_KEY);
      return enabled;
    }
    return false;
  } catch {
    return false;
  }
}

export async function saveOverlayPresentation(
  includeInExports: boolean,
): Promise<void> {
  // Native capture exclusion is enforced before publishing the preference to
  // the other Desktop window. The setting controls a later derivative export.
  await ensureControlOverlayCaptureExcluded();
  window.localStorage.setItem(
    OVERLAY_PRESENTATION_KEY,
    String(includeInExports),
  );
}
