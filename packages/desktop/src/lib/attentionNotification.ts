// Generic operating-system notifications for attended decisions.
//
// A lock screen is a public surface. Nothing about the workflow, the person,
// the application, the question, or the observed values may appear in a
// notification -- only that OpenAdapt needs a decision.
//
// The guarantee is structural: this module never forwards a string from the
// engine. It reads one integer count and renders the body from a fixed
// template, mirroring `engine/portal/notifications.py`. There is no code path
// by which protected content can reach the notification plugin.

/** The complete set of fields a notification payload may carry. */
export const NOTIFICATION_FIELDS = ["title", "body", "openCount"] as const;

export const NOTIFICATION_TITLE = "OpenAdapt needs a decision";

const MAX_OPEN_COUNT = 9999;

export type AttentionNotification = {
  title: string;
  body: string;
  openCount: number;
};

function safeCount(value: unknown): number {
  if (typeof value !== "number" || !Number.isInteger(value) || Number.isNaN(value)) {
    return 0;
  }
  return Math.max(0, Math.min(MAX_OPEN_COUNT, value));
}

/**
 * Build the only payload the notification plugin may render.
 *
 * `input` is whatever the engine emitted. Only its `open_count` is read; every
 * string on it -- including any `title` or `body` -- is discarded.
 */
export function genericNotification(input: unknown): AttentionNotification {
  const count = safeCount(
    input && typeof input === "object"
      ? (input as Record<string, unknown>).open_count
      : 0,
  );
  const noun = count === 1 ? "decision" : "decisions";
  const body =
    count === 0
      ? "Open OpenAdapt to review."
      : `${count} ${noun} waiting on this computer. Open OpenAdapt to review.`;
  return { title: NOTIFICATION_TITLE, body, openCount: count };
}

/**
 * Deliver one generic notification, requesting permission on first use.
 *
 * Returns false when the operating system declined or the plugin is
 * unavailable; the in-app badge remains the reliable channel either way.
 */
export async function deliverAttentionNotification(
  input: unknown,
): Promise<boolean> {
  const payload = genericNotification(input);
  if (payload.openCount === 0) return false;
  try {
    const plugin = await import("@tauri-apps/plugin-notification");
    let granted = await plugin.isPermissionGranted();
    if (!granted) {
      granted = (await plugin.requestPermission()) === "granted";
    }
    if (!granted) return false;
    // Exactly two fields cross this boundary, both derived above.
    plugin.sendNotification({ title: payload.title, body: payload.body });
    return true;
  } catch {
    return false;
  }
}
