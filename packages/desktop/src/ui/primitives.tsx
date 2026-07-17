// Small presentational primitives built on the design tokens.
import type { ButtonHTMLAttributes, ReactNode } from "react";
import type { StepState } from "../lib/types";

export function Eyebrow({ children }: { children: ReactNode }) {
  return <p className="eyebrow">{children}</p>;
}

type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "ghost" | "danger";
  size?: "md" | "sm";
  block?: boolean;
};
export function Button({
  variant = "ghost",
  size = "md",
  block = false,
  className = "",
  ...rest
}: ButtonProps) {
  const cls = [
    "btn",
    `btn-${variant}`,
    size === "sm" ? "btn-sm" : "",
    block ? "btn-block" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  return <button className={cls} {...rest} />;
}

export function Card({
  children,
  className = "",
}: {
  children: ReactNode;
  className?: string;
}) {
  return <div className={`card ${className}`}>{children}</div>;
}

export function CardHead({
  eyebrow,
  title,
  sub,
}: {
  eyebrow?: string;
  title: string;
  sub?: string;
}) {
  return (
    <div className="card-head">
      {eyebrow && <Eyebrow>{eyebrow}</Eyebrow>}
      <h3>{title}</h3>
      {sub && <span className="page-sub">{sub}</span>}
    </div>
  );
}

const PILL_MAP: Record<StepState, { cls: string; label: string }> = {
  pending: { cls: "pill-neutral", label: "queued" },
  running: { cls: "pill-run", label: "running" },
  verified: { cls: "pill-ok", label: "verified" },
  halted: { cls: "pill-warn", label: "halted" },
  failed: { cls: "pill-crit", label: "failed" },
};

export function StatePill({ state }: { state: StepState }) {
  const p = PILL_MAP[state];
  return (
    <span className={`pill ${p.cls}`}>
      <span className="dot" />
      {p.label}
    </span>
  );
}

export function Pill({
  tone = "neutral",
  children,
}: {
  tone?: "neutral" | "ok" | "run" | "warn" | "crit";
  children: ReactNode;
}) {
  return <span className={`pill pill-${tone}`}>{children}</span>;
}

export function StatusDot({
  tone,
}: {
  tone: "ok" | "run" | "warn" | "off";
}) {
  return <span className={`status-dot ${tone}`} />;
}

export function EmptyState({
  title,
  body,
  action,
  motif = "▚▖",
}: {
  title: string;
  body: string;
  action?: ReactNode;
  motif?: string;
}) {
  return (
    <div className="empty">
      <div className="motif">{motif}</div>
      <h3>{title}</h3>
      <p>{body}</p>
      {action}
    </div>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <div className="field">
      <label>{label}</label>
      {children}
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

export function SegControl<T extends string>({
  options,
  value,
  onChange,
}: {
  options: { value: T; label: string }[];
  value: T;
  onChange: (v: T) => void;
}) {
  return (
    <div className="seg" role="group">
      {options.map((o) => (
        <button
          key={o.value}
          className={o.value === value ? "active" : ""}
          onClick={() => onChange(o.value)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function Callout({
  tone = "info",
  title,
  children,
}: {
  tone?: "info" | "warn" | "crit";
  title?: string;
  children: ReactNode;
}) {
  const cls = tone === "info" ? "callout" : `callout ${tone}`;
  return (
    <div className={cls}>
      <div>
        {title && <span className="callout-title">{title}</span>}
        {children}
      </div>
    </div>
  );
}
