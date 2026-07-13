// Token is minted server-side and handed to the browser via the launch URL
// (/?token=...). main.jsx stores it in sessionStorage; every call reads it
// fresh from there.
export const getToken = () => sessionStorage.getItem("panel_token") || "";

export async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    ...opts,
    headers: {
      "X-Panel-Token": getToken(),
      "Content-Type": "application/json",
      ...(opts.headers || {}),
    },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

// EventSource can't set headers, so the token rides as a query param (the
// server's auth dependency accepts either).
export function streamJob(jobId, { onLine, onDone }) {
  const url = `/api/jobs/${jobId}/stream?token=${encodeURIComponent(getToken())}`;
  const es = new EventSource(url);
  es.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.line != null && onLine) onLine(msg.line);
    if (msg.done) {
      es.close();
      if (onDone) onDone(msg);
    }
  };
  es.onerror = () => es.close();
  return es;
}

export async function stopJob(jobId) {
  try {
    await api(`/jobs/${jobId}/stop`, { method: "POST" });
  } catch {
    /* best-effort */
  }
}
