export const CONTROL_OVERLAY_COACH_VERSION =
  "openadapt.control-overlay-coach/v1" as const;
export const COACH_HINT_MAX_CHARS = 80;

export const COACH_TURNS = ["your_turn", "wait", "auth", "feedback"] as const;
export const COACH_PAUSE_REASONS = [
  "auth",
  "secret_field",
  "wrong_step",
  "skip",
  "done",
  "next",
] as const;
export const COACH_OPERATOR_RESPONSES = [
  "continue",
  "wrong",
  "skip",
  "done",
  "secret_field",
] as const;

export type CoachTurn = (typeof COACH_TURNS)[number];
export type CoachPauseReason = (typeof COACH_PAUSE_REASONS)[number];
export type CoachOperatorResponse = (typeof COACH_OPERATOR_RESPONSES)[number];

export interface CoachTargetBinding {
  kind: "observation_hmac_sha256";
  observation_hmac_sha256: string;
}

export interface CoachMediaFrameBinding {
  kind: "media_frame";
  media_sha256: string;
  frame_index: number;
}

export interface CoachTarget {
  coordinate_space: "top_level_viewport_normalized";
  rect: { x: number; y: number; width: number; height: number };
  binding: CoachTargetBinding | CoachMediaFrameBinding;
}

export interface ControlOverlayCoachV1 {
  schema_version: typeof CONTROL_OVERLAY_COACH_VERSION;
  hint: string | null;
  turn: CoachTurn | null;
  pause_reason: CoachPauseReason | null;
  target: CoachTarget | null;
  operator_response: CoachOperatorResponse | null;
}

export const EMPTY_COACH: ControlOverlayCoachV1 = {
  schema_version: CONTROL_OVERLAY_COACH_VERSION,
  hint: null,
  turn: null,
  pause_reason: null,
  target: null,
  operator_response: null,
};

const URL_RE = /https?:\/\/|www\./i;
const LONG_ID_RE = /\d{6,}/;
const HMAC_RE = /^[a-f0-9]{64}$/;

function isTurn(value: unknown): value is CoachTurn {
  return (
    typeof value === "string" &&
    (COACH_TURNS as readonly string[]).includes(value)
  );
}

function isPauseReason(value: unknown): value is CoachPauseReason {
  return (
    typeof value === "string" &&
    (COACH_PAUSE_REASONS as readonly string[]).includes(value)
  );
}

function isOperatorResponse(value: unknown): value is CoachOperatorResponse {
  return (
    typeof value === "string" &&
    (COACH_OPERATOR_RESPONSES as readonly string[]).includes(value)
  );
}

export function sanitizeCoachHint(raw: unknown): string | null {
  if (raw == null) return null;
  const text = String(raw).trim().replace(/\s+/g, " ");
  if (!text) return null;
  const clipped =
    text.length > COACH_HINT_MAX_CHARS
      ? text.slice(0, COACH_HINT_MAX_CHARS).trimEnd()
      : text;
  if (URL_RE.test(clipped) || clipped.includes("@") || LONG_ID_RE.test(clipped)) {
    return null;
  }
  return clipped;
}

function bindCoachTarget(raw: unknown): CoachTarget | null {
  if (raw == null || typeof raw !== "object") return null;
  const value = raw as Record<string, unknown>;
  if (value.coordinate_space !== "top_level_viewport_normalized") return null;
  const rectRaw = value.rect;
  if (rectRaw == null || typeof rectRaw !== "object") return null;
  const rect = rectRaw as Record<string, unknown>;
  const x = Number(rect.x);
  const y = Number(rect.y);
  const width = Number(rect.width);
  const height = Number(rect.height);
  if (
    !Number.isFinite(x) ||
    !Number.isFinite(y) ||
    !Number.isFinite(width) ||
    !Number.isFinite(height) ||
    width <= 0 ||
    height <= 0 ||
    x < 0 ||
    y < 0 ||
    x + width > 1.0001 ||
    y + height > 1.0001
  ) {
    return null;
  }
  const bindingRaw = value.binding;
  if (bindingRaw == null || typeof bindingRaw !== "object") return null;
  const binding = bindingRaw as Record<string, unknown>;
  if (binding.kind === "observation_hmac_sha256") {
    const digest = binding.observation_hmac_sha256;
    if (typeof digest !== "string" || !HMAC_RE.test(digest)) return null;
    return {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x, y, width, height },
      binding: {
        kind: "observation_hmac_sha256",
        observation_hmac_sha256: digest,
      },
    };
  }
  if (binding.kind === "media_frame") {
    const digest = binding.media_sha256;
    const index = binding.frame_index;
    if (typeof digest !== "string" || !HMAC_RE.test(digest)) return null;
    if (!Number.isInteger(index) || Number(index) < 0) return null;
    return {
      coordinate_space: "top_level_viewport_normalized",
      rect: { x, y, width, height },
      binding: {
        kind: "media_frame",
        media_sha256: digest,
        frame_index: Number(index),
      },
    };
  }
  return null;
}

export function coachHoldsPause(coach: ControlOverlayCoachV1 | null): boolean {
  if (!coach) return false;
  return (
    coach.turn === "auth" ||
    coach.turn === "feedback" ||
    coach.pause_reason === "auth" ||
    coach.pause_reason === "secret_field" ||
    coach.pause_reason === "wrong_step" ||
    coach.pause_reason === "skip" ||
    coach.pause_reason === "done"
  );
}

export function overlayUsesStage(
  visible: boolean,
  interactive: boolean,
  coach: ControlOverlayCoachV1 | null,
): boolean {
  return Boolean(visible && !interactive && coach?.target);
}

export type OverlayLayout = "compact" | "paused" | "stage";

export function overlayLayoutFor(
  visible: boolean,
  interactive: boolean,
  expanded: boolean,
  coach: ControlOverlayCoachV1 | null,
): OverlayLayout {
  if (overlayUsesStage(visible, interactive, coach)) return "stage";
  if (expanded) return "paused";
  return "compact";
}

export function normalizeCoachPayload(
  raw: unknown,
  current: ControlOverlayCoachV1 | null = EMPTY_COACH,
): ControlOverlayCoachV1 {
  if (raw == null || typeof raw !== "object") {
    return current ?? EMPTY_COACH;
  }
  const incoming = raw as Record<string, unknown>;
  if (incoming.clear === true) return { ...EMPTY_COACH };
  const next: ControlOverlayCoachV1 = {
    ...(current ?? EMPTY_COACH),
    schema_version: CONTROL_OVERLAY_COACH_VERSION,
  };
  if ("hint" in incoming) next.hint = sanitizeCoachHint(incoming.hint);
  if ("turn" in incoming) {
    next.turn = isTurn(incoming.turn) ? incoming.turn : null;
  }
  if ("pause_reason" in incoming) {
    next.pause_reason = isPauseReason(incoming.pause_reason)
      ? incoming.pause_reason
      : null;
  }
  if ("target" in incoming) next.target = bindCoachTarget(incoming.target);
  if ("operator_response" in incoming) {
    if (isOperatorResponse(incoming.operator_response)) {
      next.operator_response = incoming.operator_response;
      if (incoming.operator_response === "continue") {
        next.pause_reason = null;
        if (next.turn === "auth" || next.turn === "feedback") {
          next.turn = "your_turn";
        }
      }
    } else if (incoming.operator_response == null) {
      next.operator_response = null;
    }
  }
  return {
    schema_version: CONTROL_OVERLAY_COACH_VERSION,
    hint: next.hint,
    turn: next.turn,
    pause_reason: next.pause_reason,
    target: next.target,
    operator_response: next.operator_response,
  };
}

export const AUTH_PAUSE_COPY =
  "Type in the application. Continue here when done.";
