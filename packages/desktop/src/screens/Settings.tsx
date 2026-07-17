// Settings — deployment lane, PHI mode, hosted host, auth, and updater.
// Lane and PHI mode are the PHI-boundary routing controls (spec §1.3/§3e).
import { useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry, openExternal } from "../lib/engine";
import type { AuthStatus, DeploymentLane, PhiMode } from "../lib/types";
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

interface Cfg {
  host: string;
  deployment_lane: DeploymentLane;
  phi_mode: PhiMode;
}

export function Settings({
  auth,
  onSignedOut,
}: {
  auth: AuthStatus;
  onSignedOut: () => void;
}) {
  const [cfg, setCfg] = useState<Cfg>({
    host: "https://app.openadapt.ai",
    deployment_lane: "cloud",
    phi_mode: "off",
  });
  const [updateMsg, setUpdateMsg] = useState<string | null>(null);

  useEffect(() => {
    engineTry<Cfg>(CMD.GET_CONFIG, {}, cfg).then(setCfg);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function save<K extends keyof Cfg>(key: K, value: Cfg[K]) {
    setCfg((c) => ({ ...c, [key]: value }));
    try {
      await engineInvoke(CMD.SET_CONFIG, { key, value });
    } catch {
      /* offline: kept in local UI state */
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
      setUpdateMsg(`Update check unavailable: ${String(e)}`);
    }
  }

  return (
    <div className="content">
      <div className="page-head">
        <div className="titles">
          <p className="eyebrow">Configure</p>
          <h1>Settings</h1>
        </div>
      </div>

      <Card>
        <CardHead
          eyebrow="Routing"
          title="Deployment lane"
          sub="Where compile, replay, and teach run — and whether recordings may leave this machine."
        />
        <Field label="Lane">
          <SegControl<DeploymentLane>
            value={cfg.deployment_lane}
            onChange={(v) => save("deployment_lane", v)}
            options={[
              { value: "cloud", label: "Cloud (non-PHI)" },
              { value: "byoc", label: "BYOC / self-hosted (PHI)" },
            ]}
          />
        </Field>
        {cfg.deployment_lane === "byoc" && (
          <Callout tone="warn" title="PHI boundary active">
            Recordings, bundles, and corrections stay on this machine. Only
            PHI-free break descriptors sync to the dashboard for triage.
          </Callout>
        )}
      </Card>

      <Card>
        <CardHead eyebrow="Privacy" title="PHI mode" />
        <Field
          label="PHI mode"
          hint="On enforces fail-closed scrubbing and disables cloud push of raw recordings."
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

      <Card>
        <CardHead eyebrow="Connection" title="Hosted organization" />
        <Field label="Host">
          <input
            className="input"
            value={cfg.host}
            onChange={(e) => save("host", e.target.value)}
            spellCheck={false}
          />
        </Field>
        <div className="row">
          <Pill tone={auth.authenticated ? "ok" : "neutral"}>
            {auth.authenticated ? "signed in" : "not signed in"}
          </Pill>
          {auth.kind && <span className="page-sub mono">{auth.kind}</span>}
          {auth.org_id && <span className="page-sub mono">{auth.org_id}</span>}
          <span className="spacer" />
          <Button
            variant="ghost"
            onClick={() => openExternal(`${cfg.host}/dashboard`)}
          >
            Open cloud dashboard
          </Button>
          <Button
            variant="danger"
            onClick={async () => {
              await engineInvoke(CMD.LOGOUT, {}).catch(() => {});
              onSignedOut();
            }}
          >
            Sign out
          </Button>
        </div>
      </Card>

      <Card>
        <CardHead eyebrow="Maintenance" title="Updates" />
        <div className="row">
          <Button variant="ghost" onClick={checkUpdate}>
            Check for updates
          </Button>
          {updateMsg && <span className="page-sub">{updateMsg}</span>}
        </div>
        <div style={{ marginTop: "var(--space-4)" }}>
          <OsWarning />
        </div>
      </Card>
    </div>
  );
}
