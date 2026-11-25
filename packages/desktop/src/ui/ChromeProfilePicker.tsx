// Chrome profile picker for a Browser target. A person opts in to a separate,
// Desktop-managed Chrome profile after reading the engine's notice, signs in
// there once, and Desktop fills the target's signed-in browser address with
// the endpoint of the window it opened. The engine refuses (fail closed) when
// the notice changed, the profile is open elsewhere, or attach can't be
// verified; this component only shows what the engine says.
import { useCallback, useEffect, useState } from "react";
import { CMD, engineInvoke, engineTry } from "../lib/engine";
import { Button, Callout, Field } from "./primitives";

export interface ChromeProfile {
  profile_id: string;
  name: string;
  acknowledged_at: number | null;
  consent_current: boolean;
  open: boolean;
  browser_cdp_endpoint: string | null;
}

interface Notice {
  title: string;
  text: string;
  sha256: string;
}

interface Reply {
  ok?: boolean;
  error?: string;
  profiles?: ChromeProfile[];
  profile_id?: string;
  browser_cdp_endpoint?: string;
}

export function ChromeProfilePicker({
  url,
  endpoint,
  disabled,
  idPrefix,
  onEndpoint,
}: {
  url: string | undefined;
  endpoint: string | undefined;
  disabled: boolean;
  idPrefix: string;
  onEndpoint: (endpoint: string | undefined) => void;
}) {
  const [profiles, setProfiles] = useState<ChromeProfile[]>([]);
  const [selected, setSelected] = useState<string>("");
  const [notice, setNotice] = useState<Notice | null>(null);
  const [agreeing, setAgreeing] = useState<"create" | "reack" | null>(null);
  const [agreed, setAgreed] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const reply = await engineTry<Reply | null>(CMD.CHROME_PROFILE_LIST, {}, null);
    if (reply?.ok && reply.profiles) {
      setProfiles(reply.profiles);
      setSelected((current) =>
        current && reply.profiles?.some((p) => p.profile_id === current)
          ? current
          : (reply.profiles?.[0]?.profile_id ?? ""),
      );
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const call = useCallback(
    async (cmd: string, params: Record<string, unknown>): Promise<Reply | null> => {
      setBusy(true);
      setError(null);
      try {
        const reply = await engineInvoke<Reply>(cmd, params);
        if (!reply.ok) {
          setError(reply.error ?? "OpenAdapt couldn't do that. Nothing changed.");
          return null;
        }
        return reply;
      } catch (e) {
        setError(String(e));
        return null;
      } finally {
        setBusy(false);
      }
    },
    [],
  );

  const showNotice = useCallback(async (mode: "create" | "reack") => {
    const value = await engineTry<Notice | null>(CMD.CHROME_PROFILE_NOTICE, {}, null);
    if (value) {
      setNotice(value);
      setAgreed(false);
      setAgreeing(mode);
    } else {
      setError("OpenAdapt couldn't load the notice. Nothing changed.");
    }
  }, []);

  const current = profiles.find((p) => p.profile_id === selected) ?? null;

  const agree = async () => {
    if (!notice || !agreed) return;
    const reply = agreeing === "reack" && current
      ? await call(CMD.CHROME_PROFILE_ACKNOWLEDGE, {
          profile_id: current.profile_id,
          notice_sha256: notice.sha256,
          acknowledged: true,
        })
      : await call(CMD.CHROME_PROFILE_CREATE, {
          name,
          notice_sha256: notice.sha256,
          acknowledged: true,
        });
    if (reply) {
      setAgreeing(null);
      setName("");
      if (reply.profile_id) setSelected(reply.profile_id);
      await refresh();
    }
  };

  const open = async () => {
    if (!current) return;
    const reply = await call(CMD.CHROME_PROFILE_OPEN, {
      profile_id: current.profile_id,
      url: url || undefined,
    });
    if (reply?.browser_cdp_endpoint) onEndpoint(reply.browser_cdp_endpoint);
    await refresh();
  };

  const close = async () => {
    if (!current) return;
    const reply = await call(CMD.CHROME_PROFILE_CLOSE, { profile_id: current.profile_id });
    if (reply && endpoint && endpoint === current.browser_cdp_endpoint) onEndpoint(undefined);
    await refresh();
  };

  const remove = async () => {
    if (!current) return;
    const reply = await call(CMD.CHROME_PROFILE_REVOKE, {
      profile_id: current.profile_id,
      delete_data: true,
    });
    if (reply) await refresh();
  };

  const selectId = `${idPrefix}-chrome-profile`;
  const nameId = `${idPrefix}-chrome-profile-name`;
  const agreeId = `${idPrefix}-chrome-profile-agree`;

  return (
    <div className="chrome-profile" data-testid="chrome-profile-picker">
      {profiles.length > 0 && (
        <Field
          label="OpenAdapt Chrome profile (optional)"
          hint="Opens Google Chrome on a separate profile you signed in to before. Recordings and runs then start there, already signed in."
          htmlFor={selectId}
          hintId={`${selectId}-hint`}
        >
          <select
            id={selectId}
            className="input"
            value={selected}
            disabled={disabled || busy}
            aria-describedby={`${selectId}-hint`}
            onChange={(event) => setSelected(event.target.value)}
          >
            {profiles.map((p) => (
              <option key={p.profile_id} value={p.profile_id}>
                {p.name}
                {p.open ? " (open)" : ""}
              </option>
            ))}
          </select>
        </Field>
      )}
      <div className="row">
        {current && current.consent_current && !current.open && (
          <Button variant="primary" disabled={disabled || busy} onClick={() => void open()}>
            Open in Chrome
          </Button>
        )}
        {current && current.open && (
          <Button disabled={disabled || busy} onClick={() => void close()}>
            Close Chrome window
          </Button>
        )}
        {current && !current.consent_current && (
          <Button disabled={disabled || busy} onClick={() => void showNotice("reack")}>
            Review the notice again
          </Button>
        )}
        <Button disabled={disabled || busy} onClick={() => void showNotice("create")}>
          {profiles.length ? "New Chrome profile" : "Use a Chrome profile"}
        </Button>
        {current && !current.open && (
          <Button variant="danger" disabled={disabled || busy} onClick={() => void remove()}>
            Delete profile
          </Button>
        )}
      </div>
      {agreeing && notice && (
        <Callout title={notice.title}>
          <div className="notice-text" style={{ whiteSpace: "pre-line" }}>
            {notice.text}
          </div>
          {agreeing === "create" && (
            <Field label="Profile name" htmlFor={nameId}>
              <input
                id={nameId}
                className="input"
                value={name}
                disabled={busy}
                maxLength={60}
                onChange={(event) => setName(event.target.value)}
                placeholder="Work portals"
              />
            </Field>
          )}
          <label htmlFor={agreeId}>
            <input
              id={agreeId}
              type="checkbox"
              checked={agreed}
              disabled={busy}
              onChange={(event) => setAgreed(event.target.checked)}
            />{" "}
            I've read this and agree
          </label>
          <div className="row">
            <Button
              variant="primary"
              disabled={busy || !agreed || (agreeing === "create" && !name.trim())}
              onClick={() => void agree()}
            >
              Agree
            </Button>
            <Button disabled={busy} onClick={() => setAgreeing(null)}>
              Cancel
            </Button>
          </div>
        </Callout>
      )}
      {error && (
        <Callout tone="warn" title="Chrome profile">
          {error}
        </Callout>
      )}
    </div>
  );
}
