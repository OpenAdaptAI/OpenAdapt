// Settings — deployment lane, PHI mode, hosted host, auth, and updater.
// Lane and PHI mode are the PHI-boundary routing controls (spec §1.3/§3e).
import { useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry, openExternal } from "../lib/engine";
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
  const [policy, setPolicy] = useState<EffectivePolicy | null>(null);

  useEffect(() => {
    engineTry<Cfg>(CMD.GET_CONFIG, {}, cfg).then(setCfg);
    // Tier-3 governed config (incl. the grounding model) is resolved by the
    // cloud control plane and fetched fail-closed by the engine. Read-only here;
    // degrade to null (managed-in-cloud note) if the engine cannot resolve it.
    engineTry<EffectivePolicy | null>(CMD.GET_EFFECTIVE_POLICY, {}, null).then(setPolicy);
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

      <GroundingModelCard policy={policy} host={cfg.host} />

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
}: {
  policy: EffectivePolicy | null;
  host: string;
}) {
  const gm = policy?.grounding_model ?? null;
  const isAdmin = Boolean(policy?.is_admin);
  const enabled = Boolean(gm?.enabled);

  return (
    <Card>
      <CardHead
        eyebrow="Grounding"
        title="Grounding model"
        sub="How OpenAdapt locates on-screen targets. Fully local by default; the model rung is a last-resort fallback for surfaces with no usable text."
      />

      <Callout tone="info" title="Grounding is local by default">
        Targets are located with a local ladder (structural / OCR) that makes zero
        outbound calls. The grounding-<em>model</em> rung is consulted only when the
        local rungs cannot locate a target on a text-less surface. Enabling the rung
        does not by itself send pixels off this machine — model-grounding egress is a
        separate opt-in. In PHI mode, egress is permitted only to attested
        allowlisted endpoints; public aggregators (e.g. OpenRouter) are blocked
        unless a signed BAA is attested.
      </Callout>

      {gm === null ? (
        <p className="page-sub" style={{ marginTop: "var(--space-3)" }}>
          Managed by your organization. This governed setting is resolved by the
          cloud control plane; sign in and open the dashboard to view or change it.
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
    </Card>
  );
}
