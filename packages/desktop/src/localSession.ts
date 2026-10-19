const LOCAL_SESSION_KEY = "openadapt.desktop.local-session.v1";

export function localSessionEnabled(): boolean {
  try {
    return localStorage.getItem(LOCAL_SESSION_KEY) === "enabled";
  } catch {
    return false;
  }
}

export function rememberLocalSession(): void {
  try {
    localStorage.setItem(LOCAL_SESSION_KEY, "enabled");
  } catch {
    // A storage-restricted WebView may still use the local cockpit for this run.
  }
}

export function clearLocalSession(): void {
  try {
    localStorage.removeItem(LOCAL_SESSION_KEY);
  } catch {
    // The caller still leaves the in-memory local session and shows sign-in.
  }
}
