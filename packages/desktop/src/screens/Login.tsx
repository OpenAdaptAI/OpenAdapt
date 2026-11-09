// Cloud sign-in, reached from the welcome screen or Settings. OpenAdapt works
// on this computer without an account; Cloud connection stays optional through
// browser sign-in or a one-time token stored in the OS keychain.
import { useState } from "react";
import { CMD, engineInvoke, openExternal } from "../lib/engine";
import type { AuthStatus } from "../lib/types";
import { Button, Card, CardHead, Field, Callout } from "../ui/primitives";

const DEFAULT_HOST = "https://app.openadapt.ai";
const INGEST_SETTINGS_URL = `${DEFAULT_HOST}/dashboard/settings/ingest`;

export function Login({
  onAuthed,
  onLocal,
}: {
  onAuthed: (s: AuthStatus) => void;
  onLocal: () => void;
}) {
  const [busy, setBusy] = useState<"browser" | "paste" | null>(null);
  const [token, setToken] = useState("");
  const [host, setHost] = useState(DEFAULT_HOST);
  const [error, setError] = useState<string | null>(null);

  async function loginBrowser() {
    setBusy("browser");
    setError(null);
    try {
      const s = await engineInvoke<AuthStatus>(CMD.LOGIN_BROWSER, { host });
      onAuthed(s);
    } catch (e) {
      setError(
        `${String(e)}. The engine opens your browser to sign in. If it isn't running, paste a Cloud access token instead.`,
      );
    } finally {
      setBusy(null);
    }
  }

  async function loginPaste() {
    if (!token.trim()) {
      setError("Paste a Cloud access token first.");
      return;
    }
    setBusy("paste");
    setError(null);
    try {
      const s = await engineInvoke<AuthStatus>(CMD.LOGIN_PASTE, {
        token: token.trim(),
        host,
      });
      onAuthed(s);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="center-stage">
      <Card className="auth-card">
        <CardHead
          title="Connect to OpenAdapt Cloud"
          sub="Sign in to share workflows and run history with your team. You can keep using OpenAdapt on this computer without an account."
        />

        <Button
          variant="primary"
          block
          disabled={busy !== null}
          onClick={loginBrowser}
        >
          {busy === "browser" ? "Waiting for browser…" : "Sign in with browser"}
        </Button>
        <p className="hint" style={{ marginTop: "var(--space-2)" }}>
          Opens your browser to sign in with Google or an email link.
        </p>

        <div className="divider">or paste a Cloud access token</div>

        <Field
          label="Cloud access token"
          hint="Create one in Cloud settings. You'll only see it once."
        >
          <input
            className="input mono"
            placeholder="oai_ingest_…"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            spellCheck={false}
          />
        </Field>

        <div className="row">
          <Button
            variant="ghost"
            disabled={busy !== null}
            onClick={loginPaste}
          >
            {busy === "paste" ? "Validating…" : "Use token"}
          </Button>
          <Button variant="ghost" onClick={() => openExternal(INGEST_SETTINGS_URL)}>
            Open Cloud settings
          </Button>
        </div>

        <details className="technical">
          <summary>Cloud address (for IT)</summary>
          <div className="technical-body">
            <Field label="Host">
              <input
                className="input"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                spellCheck={false}
              />
            </Field>
          </div>
        </details>

        <div className="divider">or</div>

        <Button variant="ghost" block disabled={busy !== null} onClick={onLocal}>
          Continue without an account
        </Button>

        {error && (
          <div style={{ marginTop: "var(--space-4)" }}>
            <Callout tone="warn" title="Couldn't sign in">
              {error}
            </Callout>
          </div>
        )}
      </Card>
    </div>
  );
}
