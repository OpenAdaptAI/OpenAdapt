// Small building blocks for the operator screens (Tasks, Needs you, Results,
// Check it, Run it). Status is always icon + word + color, never color alone.
import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type ReactNode,
} from "react";
import type { Tone } from "../lib/outcomes";

export type StatusIcon =
  | "check"
  | "attention"
  | "search"
  | "dot"
  | "ring"
  | "half"
  | "play"
  | "cross"
  | "minus"
  | "shield";

export function Icon({ name }: { name: StatusIcon }) {
  const common = {
    width: 16,
    height: 16,
    viewBox: "0 0 16 16",
    "aria-hidden": true,
    focusable: false,
  } as const;
  switch (name) {
    case "check":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.14" />
          <path d="M4.6 8.3l2.2 2.2 4.6-4.8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      );
    case "attention":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.14" />
          <path d="M8 4.2v4.6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
          <circle cx="8" cy="11.4" r="1" fill="currentColor" />
        </svg>
      );
    case "search":
      return (
        <svg {...common}>
          <circle cx="7" cy="7" r="4.2" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M10.2 10.2l3.2 3.2" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      );
    case "ring":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" strokeDasharray="2.6 2.2" />
        </svg>
      );
    case "half":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" strokeWidth="1.6" />
          <path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor" />
        </svg>
      );
    case "play":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.14" />
          <path d="M6.5 5.2v5.6L11 8z" fill="currentColor" />
        </svg>
      );
    case "cross":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.14" />
          <path d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      );
    case "shield":
      // Held before saving: the protection worked. Ink, never a verdict colour.
      return (
        <svg {...common}>
          <path d="M8 1.8l5 1.9v3.9c0 3.1-2.1 5.6-5 6.6-2.9-1-5-3.5-5-6.6V3.7z" fill="currentColor" opacity="0.14" />
          <path d="M8 1.8l5 1.9v3.9c0 3.1-2.1 5.6-5 6.6-2.9-1-5-3.5-5-6.6V3.7z" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
        </svg>
      );
    case "minus":
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="7" fill="currentColor" opacity="0.14" />
          <path d="M5 8h6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
        </svg>
      );
    default:
      return (
        <svg {...common}>
          <circle cx="8" cy="8" r="3.2" fill="currentColor" />
        </svg>
      );
  }
}

/** One status: icon, word, and tone. */
export function Status({
  tone,
  icon,
  children,
}: {
  tone: Tone | "run";
  icon: StatusIcon;
  children: ReactNode;
}) {
  return (
    <span className={`status status-${tone}`}>
      <Icon name={icon} />
      <span>{children}</span>
    </span>
  );
}

export function iconForTone(tone: Tone): StatusIcon {
  return tone === "ok"
    ? "check"
    : tone === "warn"
      ? "attention"
      : tone === "crit"
        ? "cross"
        : "minus";
}

export function PageHead({
  title,
  sub,
  action,
}: {
  title: string;
  sub?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <header className="simple-head">
      <div>
        <h1>{title}</h1>
        {sub && <p className="simple-sub">{sub}</p>}
      </div>
      {action && <div className="simple-head-action">{action}</div>}
    </header>
  );
}

/** A large choice: a title the reader acts on and one line on what it does. */
export function ChoiceButton({
  title,
  sub,
  tone = "default",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  title: string;
  sub?: string;
  tone?: "default" | "primary";
}) {
  return (
    <button
      type="button"
      className={`choice ${tone === "primary" ? "choice-primary" : ""}`}
      {...rest}
    >
      <strong>{title}</strong>
      {sub && <span>{sub}</span>}
    </button>
  );
}

export function StatTile({
  label,
  value,
  note,
  tone = "neutral",
  icon,
}: {
  label: string;
  value: ReactNode;
  note?: ReactNode;
  tone?: Tone;
  icon?: StatusIcon;
}) {
  return (
    <div className={`stat stat-${tone}`}>
      <span className="stat-label">
        {icon && <Icon name={icon} />}
        {label}
      </span>
      <span className="stat-value tnum">{value}</span>
      {note && <span className="stat-note">{note}</span>}
    </div>
  );
}

/**
 * The retained screen around the point where a run stopped, with that point
 * ringed. Falls back to the whole screen when no point is known.
 */
export function ScreenCrop({
  dataUrl,
  width,
  height,
  focus,
  caption,
  provenance,
  region,
}: {
  dataUrl: string;
  width: number;
  height: number;
  focus?: { x: number; y: number } | null;
  caption: string;
  provenance?: string;
  /** "top": show the top-left of the screen, where most apps put the record. */
  region?: "top";
}) {
  const valid = width > 0 && height > 0;
  const hasFocus = Boolean(
    focus &&
      valid &&
      focus.x >= 0 &&
      focus.y >= 0 &&
      focus.x <= width &&
      focus.y <= height,
  );
  // With a stopped point, show a wide band of the screen around it, so the
  // question and its choices stay in view. Without one, show the whole screen.
  const top = !hasFocus && region === "top";
  const zoom = hasFocus ? 1.4 : top ? 1 / 0.6 : 1;
  const winW = valid ? width / zoom : 1;
  const winH = valid
    ? hasFocus
      ? Math.min(height, winW / 2.6)
      : top
        ? Math.min(height, winW / 2)
        : height
    : 1;
  const fx = hasFocus && focus ? focus.x : top ? winW / 2 : width / 2;
  const fy = hasFocus && focus ? focus.y : top ? winH / 2 : height / 2;
  const x0 = valid ? Math.min(width - winW, Math.max(0, fx - winW / 2)) : 0;
  const y0 = valid ? Math.min(height - winH, Math.max(0, fy - winH / 2)) : 0;
  return (
    <figure className="screen-crop">
      <div
        className="screen-crop-frame"
        style={{ aspectRatio: valid ? `${winW} / ${winH}` : "16 / 10" }}
      >
        <img
          src={dataUrl}
          alt={caption}
          style={{
            width: `${(width / winW) * 100}%`,
            left: `${(-x0 / winW) * 100}%`,
            top: `${(-y0 / winH) * 100}%`,
          }}
        />
        {hasFocus && (
          <span
            className="screen-crop-ring"
            aria-hidden="true"
            style={{
              left: `${((fx - x0) / winW) * 100}%`,
              top: `${((fy - y0) / winH) * 100}%`,
            }}
          />
        )}
      </div>
      <figcaption>
        <span>{caption}</span>
        {provenance && <span className="provenance">{provenance}</span>}
      </figcaption>
    </figure>
  );
}

/** A plain disclosure for exact engine terms one level deeper. */
export function TechnicalDetails({ children }: { children: ReactNode }) {
  return (
    <details className="technical">
      <summary>Technical details</summary>
      <div className="technical-body">{children}</div>
    </details>
  );
}

/** The gear menu: Settings and Advanced live one click deeper. */
export function GearMenu({
  items,
  disabled,
  title,
}: {
  items: { label: string; sub?: string; onSelect: () => void }[];
  disabled?: boolean;
  title?: string;
}) {
  const [open, setOpen] = useState(false);
  const wrap = useRef<HTMLDivElement>(null);
  const menuId = useId();
  useEffect(() => {
    if (!open) return;
    function onDocument(event: MouseEvent) {
      if (wrap.current && !wrap.current.contains(event.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocument);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDocument);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="gear" ref={wrap}>
      <button
        type="button"
        className="gear-button"
        aria-label="Settings and advanced tools"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        disabled={disabled}
        title={title}
        onClick={() => setOpen((value) => !value)}
      >
        <svg width="18" height="18" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
          <path
            d="M12 15.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7zm8.4-2.1l1.6 1.2-2 3.4-1.9-.7a7.9 7.9 0 0 1-1.9 1.1L16 20.5h-4l-.3-2.1a7.9 7.9 0 0 1-1.9-1.1l-1.9.7-2-3.4 1.6-1.2a8 8 0 0 1 0-2.2L5.9 10l2-3.4 1.9.7a7.9 7.9 0 0 1 1.9-1.1L12 4h4l.3 2.2c.7.3 1.3.6 1.9 1.1l1.9-.7 2 3.4-1.6 1.2c.1.7.1 1.5-.1 2.2z"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinejoin="round"
            transform="translate(-2 0)"
          />
        </svg>
      </button>
      {open && (
        <div className="gear-menu" role="menu" id={menuId}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              onClick={() => {
                setOpen(false);
                item.onSelect();
              }}
            >
              <strong>{item.label}</strong>
              {item.sub && <span>{item.sub}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** "3 minutes ago", "yesterday", or a date. */
export function relativeTime(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const seconds = Math.max(0, Math.round((now - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  if (days === 1) return "yesterday";
  if (days < 7) return `${days} days ago`;
  return new Date(then).toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
  });
}

/** "1 h 40 min", "12 min", "45 s". */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "";
  const total = Math.max(0, Math.round(seconds));
  if (total < 120) return `${total} s`;
  const minutes = Math.round(total / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}
