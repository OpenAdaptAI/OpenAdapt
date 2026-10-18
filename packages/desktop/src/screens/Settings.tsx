// Settings — deployment lane, PHI mode, hosted host, auth, and updater.
// Lane and PHI mode are the PHI-boundary routing controls (spec §1.3/§3e).
import { useEffect, useState } from "react";
import {
  CMD,
  engineInvoke,
  engineTry,
  openExternal,
  updaterAvailable,
} from "../lib/engine";
import type {
  AuthStatus,
  DeploymentLane,
  EffectivePolicy,
  PhiMode,
} from "../lib/types";
import {
  Button,
  Card,
  CardHead,
  Callout,
  Field,
  Pill,
  SegControl,
} from "../ui/primitives";
import { OsWarning } from "../ui/OsWarning";
import { ShadowModeCard } from "../ui/ShadowMode";
import {
  overlayPresentationEnabled,
  saveOverlayPresentation,
} from "../overlay/preferences";

const RELEASES_URL = "https://github.com/OpenAdaptAI/openadapt-desktop/releases";

interface Cfg {
  host: string;
  deployment_lane: DeploymentLane;
  phi_mode: PhiMode;
}

export function Settings({
  auth,
  onConnectCloud,
  onSignedOut,
}: {
  auth: AuthStatus;
  onConnectCloud: () => void;
  onSignedOut: () => void;
}) {
  const [cfg, setCfg] = useState<Cfg>({
    host: "https://app.openadapt.ai",
    deployment_lane: "cloud",
    phi_mode: "off",
  });
  const [updateMsg, setUpdateMsg] = useState<string | null>(null);
  // Null until the shell says whether this build can update itself.
  const [canUpdate, setCanUpdate] = useState<boolean | null>(null);
  const [policy, setPolicy] = useState<EffectivePolicy | null>(null);
  const [includeOverlay, setIncludeOverlay] = useState(
    overlayPresentationEnabled,
  );
  const [overlayError, setOverlayError] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);

  useEffect(() => {
    engineTry<Cfg>(CMD.GET_CONFIG, {}, cfg).then(setCfg);
    // Tier-3 governed config (incl. the grounding model) is resolved by the
    // cloud control plane and fetched fail-closed by the engine. Read-only here;
    // degrade to null (managed-in-cloud note) if the engine cannot resolve it.
    engineTry<EffectivePolicy | null>(CMD.GET_EFFECTIVE_POLICY, {}, null).then(setPolicy);
    void updaterAvailable().then(setCanUpdate);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save<K extends keyof Cfg>(key: K, value: Cfg[K]) {
    setConfigError(null);
    try {
      const result = await engineInvoke<{ ok: boolean; error?: string; host?: string }>(
        CMD.SET_CONFIG,
        { key, value },
      );
      if (!result.ok) {
        setConfigError(result.error || "The setting was refused.");
        setCfg(await engineTry<Cfg>(CMD.GET_CONFIG, {}, cfg));
        return;
      }
      setCfg((current) => ({
        ...current,
        [key]: key === "host" && result.host ? result.host : value,
      }));
    } catch (error) {
      setConfigError(`The setting was not saved: ${String(error)}`);
      setCfg(await engineTry<Cfg>(CMD.GET_CONFIG, {}, cfg));
    }
  }

  async function checkUpdate() {
    setUpdateMsg("Checking for updates…");
    try {
      const { check } = await import("@tauri-apps/plugin-updater");
      const update = await check();
      setUpdateMsg(
        update
          ? `Update ${update.version} available. Downloading…`
          : "You're on the latest version.",
      );
      if (update) {
        await update.downloadAndInstall();
        setUpdateMsg(`Installed ${update.version}. Restart to apply.`);
      }
    } catch (e) {
      console.warn("update check failed", e);
      setUpdateMsg(
        "OpenAdapt couldn't check for updates. Try again later, or get the latest version from the releases page.",
      );
    }
  }

  async function setOverlayExport(next: boolean) {
    setOverlayError(null);
    try {
      await saveOverlayPresentation(next);
      setIncludeOverlay(next);
    } catch {
      setOverlayError(
        "OpenAdapt could not save the presentation-export preference. Raw capture remains overlay-free, and the previous export setting was kept.",
      );
    }
  }

  return (
    <div className="content simple">
      <div className="simple-head">
        <div>
          <h1>Settings</h1>
          <p className="simple-sub">
            Your account, where your data stays, and how OpenAdapt works on
            this computer.
          </p>
        </div>
      </div>

      <Card>
        <CardHead
          title="Account"
          sub={
            auth.authenticated
              ? "Connected to OpenAdapt Cloud."
              : "Local only. OpenAdapt works on this computer without an account."
          }
        />
        <div className="row">
          {auth.authenticated && auth.org_id && (
            <span className="page-sub mono">{auth.org_id}</span>
          )}
          {auth.authenticated && (
            <Button
              variant="ghost"
              onClick={() => openExternal(`${cfg.host}/dashboard`)}
            >
              Open Cloud dashboard
            </Button>
          )}
          {auth.authenticated ? (
            <Button
              variant="danger"
              onClick={async () => {
                await engineInvoke(CMD.LOGOUT, {}).catch(() => {});
                onSignedOut();
              }}
            >
              Sign out
            </Button>
          ) : (
            <Button variant="primary" onClick={onConnectCloud}>
              Connect to OpenAdapt Cloud
            </Button>
          )}
        </div>
        <details className="technical">
          <summary>Cloud address (for IT)</summary>
          <div className="technical-body">
            <Field label="Host">
              <input
                className="input"
                value={cfg.host}
                onChange={(e) => setCfg((current) => ({ ...current, host: e.target.value }))}
                onBlur={(e) => void save("host", e.target.value)}
                spellCheck={false}
              />
            </Field>
            {auth.kind && <span className="page-sub mono">{auth.kind}</span>}
          </div>
        </details>
        {configError && (
          <Callout tone="warn" title="That setting didn't change">
            {configError}
          </Callout>
        )}
      </Card>

      <Card>
        <CardHead
          title="Where your data stays"
          sub="Choose where recordings and tasks live, and whether they can leave this computer."
        />
        <Field label="Data location">
          <SegControl<DeploymentLane>
            value={cfg.deployment_lane}
            onChange={(v) => save("deployment_lane", v)}
            options={[
              { value: "cloud", label: "OpenAdapt Cloud (no PHI)" },
              { value: "byoc", label: "Your own environment (PHI allowed)" },
            ]}
          />
        </Field>
        {cfg.deployment_lane === "byoc" && (
          <Callout tone="info" title="Patient data stays in your environment">
            OpenAdapt doesn't upload recordings, tasks, or fixes. When a stopped
            run is reported, only a short note about it, with no PHI, goes to
            your dashboard. Backup, sync, and security software on this
            computer can still copy its files.
          </Callout>
        )}
      </Card>

      <Card>
        <CardHead title="Sensitive data" />
        <Field
          label="PHI mode"
          hint="When on, OpenAdapt removes sensitive data before any upload and blocks the upload if it can't. Raw recordings never go to Cloud."
        >
          <SegControl<PhiMode>
            value={cfg.phi_mode}
            onChange={(v) => save("phi_mode", v)}
            options={[
              { value: "off", label: "Off" },
              { value: "on", label: "On" },
            ]}
          />
        </Field>
      </Card>

      <GroundingModelCard
        policy={policy}
        host={cfg.host}
        signedIn={auth.authenticated}
        onConnectCloud={onConnectCloud}
      />

      <ShadowModeCard />

      <Card>
        <CardHead
          title="On-screen controls"
          sub="A small status panel stays on top while OpenAdapt records or runs. It never shows screenshots, typed values, or patient details."
        />
        <label className="check-row">
          <input
            type="checkbox"
            checked={includeOverlay}
            onChange={(event) =>
              void setOverlayExport(event.target.checked)
            }
          />
          <span>
            Include the OpenAdapt overlay in presentation exports
            <span className="hint" style={{ display: "block" }}>
              Raw recordings and evidence always exclude it. Turn this on to
              add the status and exact retained target timeline to a separate
              presentation export.
            </span>
          </span>
        </label>
        {overlayError && (
          <Callout tone="warn" title="Overlay export preference unchanged">
            {overlayError}
          </Callout>
        )}
      </Card>

      <Card>
        <CardHead
          title="Version and updates"
          sub={
            canUpdate
              ? "Check for a new version and install it from here."
              : "Automatic updates stay off until signed releases ship. Each new release has to be installed by hand."
          }
        />
        <div className="row">
          {canUpdate ? (
            <Button variant="ghost" onClick={checkUpdate}>
              Check for updates
            </Button>
          ) : null}
          {canUpdate === false ? (
            <Button variant="ghost" onClick={() => void openExternal(RELEASES_URL)}>
              Open the releases page
            </Button>
          ) : null}
          {updateMsg && <span className="page-sub">{updateMsg}</span>}
        </div>
        <div style={{ marginTop: "var(--space-4)" }}>
          <OsWarning />
        </div>
      </Card>
    </div>
  );
}

// A single read-only label/value line for the governed grounding config.
function ReadRow({ label, value }: { label: string; value: string }) {
  return (
    <Field label={label}>
      <span className="page-sub mono">{value || "—"}</span>
    </Field>
  );
}

// Grounding model — an admin-scoped Tier-3 egress capability resolved from the
// cloud effective policy. READ-ONLY on the desktop: the canonical write path is
// the cloud dashboard (the desktop never writes Tier-2/3 locally). Off by
// default, fail-closed. The raw API key is never shown — only the env-var NAME.
function GroundingModelCard({
  policy,
  host,
  signedIn,
  onConnectCloud,
}: {
  policy: EffectivePolicy | null;
  host: string;
  signedIn: boolean;
  onConnectCloud: () => void;
}) {
  const gm = policy?.grounding_model ?? null;
  const isAdmin = Boolean(policy?.is_admin);
  const enabled = Boolean(gm?.enabled);
  // The engine's fail-closed default is not an organization's policy: neither
  // Cloud nor a saved copy answered. With nobody signed in, there is no
  // organization at all, so the card must not point to an admin.
  const noOrgPolicy = policy?.source === "fail-closed-default";
  const localOnly = noOrgPolicy && !signedIn;

  return (
    <Card>
      <CardHead
        title="How OpenAdapt finds buttons and fields"
        sub="It works on this computer by default. An AI model is used only as a last resort, and only if your admin turns it on."
      />

      <details className="technical">
        <summary>Technical details</summary>
        <div className="technical-body">
          <p className="technical-text">
            Targets are located with a local ladder (structural and OCR) that
            makes no outbound calls. The grounding-model rung is consulted only
            when the local rungs can't locate a target on a surface with no
            usable text. Turning the rung on doesn't by itself send pixels off
            this computer; model-grounding egress is a separate opt-in. In PHI
            mode, egress is allowed only to attested allowlisted endpoints, and
            public aggregators are blocked unless a signed BAA is attested.
          </p>
        </div>
      </details>

      {policy === null ? (
        <p className="page-sub" style={{ marginTop: "var(--space-3)" }}>
          Desktop can't read this setting from the local engine right now.
        </p>
      ) : noOrgPolicy ? (
        <>
          <Field label="Grounding-model rung">
            <Pill tone="ok">Off (local only)</Pill>
          </Field>
          <p className="page-sub">
            {localOnly
              ? "No OpenAdapt Cloud workspace is connected, so the model stays off and OpenAdapt finds buttons and fields on this computer only. Connect a workspace to use your organization's setting."
              : "OpenAdapt Cloud can't be reached and there's no saved copy of your organization's setting, so the model stays off for now."}
          </p>
        </>
      ) : gm === null ? (
        <p className="page-sub" style={{ marginTop: "var(--space-3)" }}>
          Your organization manages this setting in OpenAdapt Cloud. Sign in
          and open the dashboard to see or change it.
        </p>
      ) : (
        <>
          <Field label="Grounding-model rung">
            <Pill tone={enabled ? "warn" : "ok"}>{enabled ? "On" : "Off (local only)"}</Pill>
          </Field>
          {enabled && (
            <>
              <ReadRow label="Provider" value={gm!.provider} />
              {gm!.provider === "openai_compatible" && (
                <ReadRow label="Base URL" value={gm!.base_url} />
              )}
              <ReadRow label="Model" value={gm!.model} />
              <ReadRow label="API key env var" value={gm!.api_key_env} />
              <ReadRow
                label="PHI allowlist"
                value={(gm!.phi_grounding_allowlist ?? []).join(", ")}
              />
              <Field label="PHI egress to public aggregator">
                <Pill tone={gm!.phi_egress_attested ? "warn" : "ok"}>
                  {gm!.phi_egress_attested ? "Attested (BAA)" : "Not attested"}
                </Pill>
              </Field>
            </>
          )}
        </>
      )}

      {localOnly ? (
        <div className="row" style={{ marginTop: "var(--space-4)" }}>
          <span className="spacer" />
          <Button variant="ghost" onClick={onConnectCloud}>
            Connect to OpenAdapt Cloud
          </Button>
        </div>
      ) : policy !== null ? (
        <div className="row" style={{ marginTop: "var(--space-4)" }}>
          <Pill tone="neutral">read-only</Pill>
          <span className="page-sub">
            {isAdmin
              ? "Admins change this in the cloud dashboard (the single canonical write path)."
              : "Only an organization admin can change this."}
          </span>
          <span className="spacer" />
          <Button
            variant="ghost"
            onClick={() => openExternal(`${host}/dashboard/settings`)}
          >
            {isAdmin ? "Manage in cloud dashboard" : "Open cloud dashboard"}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
