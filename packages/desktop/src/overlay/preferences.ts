import { setControlOverlayPresentation } from "../lib/engine";

export const OVERLAY_PRESENTATION_KEY =
  "openadapt.control-overlay.include-in-recordings.v1";

export function overlayPresentationEnabled(): boolean {
  try {
    return window.localStorage.getItem(OVERLAY_PRESENTATION_KEY) === "true";
  } catch {
    return false;
  }
}

export async function saveOverlayPresentation(
  includeInRecordings: boolean,
): Promise<void> {
  // Apply the native capture policy first. A platform refusal must not persist
  // a setting that claims the overlay will appear (or be excluded) when it will
  // not.
  await setControlOverlayPresentation(includeInRecordings);
  window.localStorage.setItem(
    OVERLAY_PRESENTATION_KEY,
    String(includeInRecordings),
  );
}
