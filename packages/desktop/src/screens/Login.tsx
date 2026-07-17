// Login screen — offers BOTH sign-in paths behind one screen (spec §3a):
//   • "Click Login"  -> engine BrowserPkceProvider (system browser + loopback PKCE)
//   • "Paste a token" -> engine PasteTokenProvider (mint in cloud Settings, paste)
// Both run in the engine (engine.auth) and store one Credential in the keychain.
import { useState } from "react";
import { CMD, engineInvoke, openExternal } from "../lib/engine";
import type { AuthStatus } from "../lib/types";
import { Button, Card, CardHead, Field, Callout } from "../ui/primitives";

const DEFAULT_HOST = "https://app.openadapt.ai";
const INGEST_SETTINGS_URL = `${DEFAULT_HOST}/dashboard/settings/ingest`;

export function Login({ onAuthed }: { onAuthed: (s: AuthStatus) => void }) {
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
        `${String(e)} — the engine opens your browser to sign in. If it isn't running, use "Paste a token".`,
      );
    } finally {
      setBusy(null);
    }
  }

  async function loginPaste() {
    if (!token.trim()) {
      setError("Paste an ingest token first.");
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
          eyebrow="Connect to OpenAdapt"
          title="Sign in"
          sub="Link this machine to your OpenAdapt organization."
        />

        <Field label="Host">
          <input
            className="input"
            value={host}
            onChange={(e) => setHost(e.target.value)}
            spellCheck={false}
          />
        </Field>

        <Button
          variant="primary"
          block
          disabled={busy !== null}
          onClick={loginBrowser}
        >
          {busy === "browser" ? "Waiting for browser…" : "Click Login"}
        </Button>
        <p className="hint" style={{ marginTop: "var(--space-2)" }}>
          Opens your system browser — Google and magic-link sign-in just work.
        </p>

        <div className="divider">or paste a token</div>

        <Field
          label="Ingest token"
          hint="Mint one in cloud Settings → Ingest tokens. It is shown once."
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
            Open Settings → Ingest tokens
          </Button>
        </div>

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
