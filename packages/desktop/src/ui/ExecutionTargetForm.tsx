import { useEffect, useState } from "react";
import { CMD, engineTry } from "../lib/engine";
import type {
  CapabilityReport,
  CapabilityState,
  ExecutionTarget,
  SurfaceCapability,
  TargetBackend,
} from "../lib/types";
import { Callout, Field, Pill } from "./primitives";

export const TARGETS: Record<
  TargetBackend,
  { label: string; description: string }
> = {
  web: {
    label: "Browser",
    description: "A web page opened and driven by OpenAdapt.",
  },
  windows: {
    label: "Windows",
    description: "A native Windows desktop connected through the local WAA agent.",
  },
  macos: {
    label: "macOS",
    description: "One native Mac application window.",
  },
  linux: {
    label: "Linux",
    description: "One exact Linux application window through AT-SPI.",
  },
  rdp: {
    label: "RDP",
    description: "A network RDP session or a local remote-desktop client window.",
  },
  citrix: {
    label: "Citrix",
    description: "The local Citrix Workspace or Viewer session window.",
  },
};

type PillTone = "neutral" | "ok" | "run" | "warn" | "crit";

/**
 * Pill copy per detected capability state. "Available" is shown ONLY when the
 * engine's detection (engine/capabilities.py) reports it; before the report
 * arrives, or without an engine, the pill says the state is being checked.
 */
export const CAPABILITY_PILLS: Record<
  CapabilityState,
  { label: string; tone: PillTone }
> = {
  available: { label: "Available", tone: "ok" },
  driver_required: { label: "Driver required", tone: "warn" },
  permission_required: { label: "Permission required", tone: "warn" },
  unsupported_host: { label: "Not on this host", tone: "crit" },
};

export function capabilityPill(
  capability: SurfaceCapability | null | undefined,
): { label: string; tone: PillTone } {
  if (!capability) {
    return { label: "Checking availability", tone: "neutral" };
  }
  return (
    CAPABILITY_PILLS[capability.state] ?? {
      label: "Availability unknown",
      tone: "neutral",
    }
  );
}

export type RdpMode = "network" | "window";

export function rdpModeForTarget(target: ExecutionTarget): RdpMode {
  return target.backend === "rdp" && "rdp_window" in target
    ? "window"
    : "network";
}

export function switchRdpTransport(
  current: ExecutionTarget,
  mode: RdpMode,
): ExecutionTarget {
  const shared = {
    rdp_readiness_text: current.rdp_readiness_text,
  };
  return mode === "network"
    ? { backend: "rdp", rdp_host: "", ...shared }
    : { backend: "rdp", rdp_window: "", ...shared };
}

export function ExecutionTargetForm({
  target,
  onChange,
  idPrefix,
  disabled = false,
  capabilities,
}: {
  target: ExecutionTarget;
  onChange: (target: ExecutionTarget) => void;
  idPrefix: string;
  disabled?: boolean;
  /**
   * Optional pre-fetched capability report. When omitted, the form queries
   * the engine itself (get_capabilities) and degrades to "Checking
   * availability" offline. Selecting a non-available surface stays allowed:
   * the user may be configuring ahead of installing a driver or grant.
   */
  capabilities?: CapabilityReport | null;
}) {
  const selected = TARGETS[target.backend];
  const rdpMode = rdpModeForTarget(target);
  const [fetched, setFetched] = useState<CapabilityReport | null>(null);
  const external = capabilities !== undefined;
  useEffect(() => {
    if (external) return;
    let cancelled = false;
    void engineTry<CapabilityReport | null>(CMD.GET_CAPABILITIES, {}, null).then(
      (report) => {
        if (!cancelled) setFetched(report);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [external]);
  const report = external ? capabilities : fetched;
  const capability = report?.surfaces?.[target.backend] ?? null;
  const pill = capabilityPill(capability);

  function selectBackend(backend: TargetBackend) {
    onChange(
      backend === "rdp"
        ? { backend: "rdp", rdp_host: "" }
        : { backend },
    );
  }

  function setField<K extends keyof ExecutionTarget>(
    key: K,
    value: ExecutionTarget[K],
  ) {
    onChange({ ...target, [key]: value });
  }

  return (
    <>
      <Field label="Application surface" htmlFor={`${idPrefix}-backend`}>
        <select
          id={`${idPrefix}-backend`}
          className="input"
          value={target.backend}
          disabled={disabled}
          aria-describedby={`${idPrefix}-backend-description`}
          onChange={(event) =>
            selectBackend(event.target.value as TargetBackend)
          }
        >
          {(Object.keys(TARGETS) as TargetBackend[]).map((backend) => (
            <option key={backend} value={backend}>
              {TARGETS[backend].label}
            </option>
          ))}
        </select>
      </Field>
      <div
        className="row target-summary"
        id={`${idPrefix}-backend-description`}
      >
        <Pill tone={pill.tone}>{pill.label}</Pill>
        <span className="page-sub">{selected.description}</span>
      </div>
      {capability && capability.state !== "available" && (
        <p className="page-sub" data-testid={`${idPrefix}-capability-detail`}>
          {capability.detail}
          {capability.remediation ? ` ${capability.remediation}` : ""}
        </p>
      )}

      <TargetFields
        target={target}
        rdpMode={rdpMode}
        setField={setField}
        switchRdpMode={(mode) => onChange(switchRdpTransport(target, mode))}
        idPrefix={idPrefix}
        disabled={disabled}
      />
    </>
  );
}

function TargetFields({
  target,
  rdpMode,
  setField,
  switchRdpMode,
  idPrefix,
  disabled,
}: {
  target: ExecutionTarget;
  rdpMode: RdpMode;
  setField: <K extends keyof ExecutionTarget>(
    key: K,
    value: ExecutionTarget[K],
  ) => void;
  switchRdpMode: (mode: RdpMode) => void;
  idPrefix: string;
  disabled: boolean;
}) {
  switch (target.backend) {
    case "web":
      return (
        <Field
          label="Page URL"
          hint="Leave blank to use Flow's bundled local demonstration."
          htmlFor={`${idPrefix}-web-url`}
          hintId={`${idPrefix}-web-url-hint`}
        >
          <input
            id={`${idPrefix}-web-url`}
            className="input"
            type="url"
            value={target.url ?? ""}
            disabled={disabled}
            aria-describedby={`${idPrefix}-web-url-hint`}
            onChange={(event) => setField("url", event.target.value)}
            placeholder="https://your-app.example"
            spellCheck={false}
          />
        </Field>
      );
    case "windows":
      return (
        <Field
          label="Windows connection"
          hint="The WAA agent URL on this machine or through your approved tunnel."
          htmlFor={`${idPrefix}-windows-agent`}
          hintId={`${idPrefix}-windows-agent-hint`}
        >
          <input
            id={`${idPrefix}-windows-agent`}
            className="input"
            type="url"
            value={target.agent_url ?? ""}
            disabled={disabled}
            aria-describedby={`${idPrefix}-windows-agent-hint`}
            onChange={(event) => setField("agent_url", event.target.value)}
            placeholder="http://localhost:5001"
            spellCheck={false}
          />
        </Field>
      );
    case "macos":
      return (
        <>
          <Field
            label="Mac application"
            hint="The owner application, for example TextEdit."
            htmlFor={`${idPrefix}-macos-app`}
            hintId={`${idPrefix}-macos-app-hint`}
          >
            <input
              id={`${idPrefix}-macos-app`}
              className="input"
              value={target.macos_app ?? ""}
              disabled={disabled}
              aria-describedby={`${idPrefix}-macos-app-hint`}
              onChange={(event) => setField("macos_app", event.target.value)}
              placeholder="TextEdit"
            />
          </Field>
          <Field
            label="Window title"
            hint="Optional title substring; ambiguous matches stop safely."
            htmlFor={`${idPrefix}-macos-window-title`}
            hintId={`${idPrefix}-macos-window-title-hint`}
          >
            <input
              id={`${idPrefix}-macos-window-title`}
              className="input"
              value={target.macos_window_title ?? ""}
              disabled={disabled}
              aria-describedby={`${idPrefix}-macos-window-title-hint`}
              onChange={(event) =>
                setField("macos_window_title", event.target.value)
              }
            />
          </Field>
        </>
      );
    case "linux":
      return (
        <>
          <Field
            label="Linux application"
            hint="Exact AT-SPI application name."
            htmlFor={`${idPrefix}-linux-app`}
            hintId={`${idPrefix}-linux-app-hint`}
          >
            <input
              id={`${idPrefix}-linux-app`}
              className="input"
              value={target.linux_app ?? ""}
              disabled={disabled}
              aria-describedby={`${idPrefix}-linux-app-hint`}
              onChange={(event) => setField("linux_app", event.target.value)}
              placeholder="gedit"
            />
          </Field>
          <Field
            label="Window title"
            hint="Exact top-level title; zero or multiple matches stop safely."
            htmlFor={`${idPrefix}-linux-window-title`}
            hintId={`${idPrefix}-linux-window-title-hint`}
          >
            <input
              id={`${idPrefix}-linux-window-title`}
              className="input"
              value={target.linux_window_title ?? ""}
              disabled={disabled}
              aria-describedby={`${idPrefix}-linux-window-title-hint`}
              onChange={(event) =>
                setField("linux_window_title", event.target.value)
              }
            />
          </Field>
          <label
            className="check-row"
            htmlFor={`${idPrefix}-linux-physical-input`}
          >
            <input
              id={`${idPrefix}-linux-physical-input`}
              type="checkbox"
              checked={Boolean(target.linux_allow_physical_input)}
              disabled={disabled}
              onChange={(event) =>
                setField("linux_allow_physical_input", event.target.checked)
              }
            />
            <span>
              Allow window-bound physical input if native AT-SPI action is
              unavailable
            </span>
          </label>
        </>
      );
    case "rdp":
      return (
        <>
          <fieldset
            className="field target-fieldset"
            disabled={disabled}
          >
            <legend>RDP connection</legend>
            <div className="seg radio-seg">
              {(
                [
                  ["network", "Network host"],
                  ["window", "Local client window"],
                ] as const
              ).map(([mode, label]) => (
                <label
                  key={mode}
                  className={rdpMode === mode ? "active" : ""}
                >
                  <input
                    type="radio"
                    name={`${idPrefix}-rdp-mode`}
                    value={mode}
                    checked={rdpMode === mode}
                    onChange={() => switchRdpMode(mode)}
                  />
                  <span>{label}</span>
                </label>
              ))}
            </div>
          </fieldset>
          {rdpMode === "network" ? (
            <Field
              label="RDP host"
              hint="Host name or IP only. Credentials stay in the deployment config."
              htmlFor={`${idPrefix}-rdp-host`}
              hintId={`${idPrefix}-rdp-host-hint`}
            >
              <input
                id={`${idPrefix}-rdp-host`}
                className="input"
                value={target.rdp_host ?? ""}
                disabled={disabled}
                aria-describedby={`${idPrefix}-rdp-host-hint`}
                onChange={(event) => setField("rdp_host", event.target.value)}
                placeholder="10.0.0.5"
                spellCheck={false}
              />
            </Field>
          ) : (
            <Field
              label="Client window owner"
              hint="Exact local app/process that paints the remote session."
              htmlFor={`${idPrefix}-rdp-window`}
              hintId={`${idPrefix}-rdp-window-hint`}
            >
              <input
                id={`${idPrefix}-rdp-window`}
                className="input"
                value={target.rdp_window ?? ""}
                disabled={disabled}
                aria-describedby={`${idPrefix}-rdp-window-hint`}
                onChange={(event) => setField("rdp_window", event.target.value)}
                placeholder="Microsoft Remote Desktop"
              />
            </Field>
          )}
          <RemoteWindowFields
            target={target}
            setField={setField}
            idPrefix={`${idPrefix}-rdp`}
            showWindowTitle={rdpMode === "window"}
            disabled={disabled}
          />
        </>
      );
    case "citrix":
      return (
        <>
          <Callout tone="info" title="Open the session first">
            OpenAdapt targets the Citrix Workspace or Viewer window already
            running on this computer. The default owner is selected for your
            operating system.
          </Callout>
          <Field
            label="Ready-screen text"
            hint="Stable text that confirms the intended app is open before input."
            htmlFor={`${idPrefix}-citrix-readiness`}
            hintId={`${idPrefix}-citrix-readiness-hint`}
          >
            <input
              id={`${idPrefix}-citrix-readiness`}
              className="input"
              value={target.rdp_readiness_text ?? ""}
              disabled={disabled}
              aria-describedby={`${idPrefix}-citrix-readiness-hint`}
              onChange={(event) =>
                setField("rdp_readiness_text", event.target.value)
              }
              placeholder="Patient Search"
            />
          </Field>
          <RemoteWindowFields
            target={target}
            setField={setField}
            idPrefix={`${idPrefix}-citrix`}
            citrix
            showWindowTitle
            disabled={disabled}
          />
        </>
      );
  }
}

function RemoteWindowFields({
  target,
  setField,
  idPrefix,
  citrix = false,
  showWindowTitle,
  disabled,
}: {
  target: ExecutionTarget;
  setField: <K extends keyof ExecutionTarget>(
    key: K,
    value: ExecutionTarget[K],
  ) => void;
  idPrefix: string;
  citrix?: boolean;
  showWindowTitle: boolean;
  disabled: boolean;
}) {
  return (
    <>
      {citrix && (
        <Field
          label="Workspace window owner"
          hint="Optional override for a nonstandard Citrix client name."
          htmlFor={`${idPrefix}-window`}
          hintId={`${idPrefix}-window-hint`}
        >
          <input
            id={`${idPrefix}-window`}
            className="input"
            value={target.rdp_window ?? ""}
            disabled={disabled}
            aria-describedby={`${idPrefix}-window-hint`}
            onChange={(event) => setField("rdp_window", event.target.value)}
            placeholder="Citrix Viewer"
          />
        </Field>
      )}
      {showWindowTitle && (
        <Field
          label="Session window title"
          hint="Optional exact title to disambiguate multiple remote sessions."
          htmlFor={`${idPrefix}-window-title`}
          hintId={`${idPrefix}-window-title-hint`}
        >
          <input
            id={`${idPrefix}-window-title`}
            className="input"
            value={target.rdp_window_title ?? ""}
            disabled={disabled}
            aria-describedby={`${idPrefix}-window-title-hint`}
            onChange={(event) =>
              setField("rdp_window_title", event.target.value)
            }
          />
        </Field>
      )}
      {!citrix && (
        <Field
          label="Ready-screen text"
          hint="Optional stable text checked on the current remote frame before input."
          htmlFor={`${idPrefix}-readiness`}
          hintId={`${idPrefix}-readiness-hint`}
        >
          <input
            id={`${idPrefix}-readiness`}
            className="input"
            value={target.rdp_readiness_text ?? ""}
            disabled={disabled}
            aria-describedby={`${idPrefix}-readiness-hint`}
            onChange={(event) =>
              setField("rdp_readiness_text", event.target.value)
            }
          />
        </Field>
      )}
    </>
  );
}
