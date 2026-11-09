// Service worker for the OpenAdapt decision portal.
//
// SAFETY CONTRACT — this worker is structurally incapable of caching protected
// content, and `tests/test_portal_shell.py` asserts each clause below:
//
//   1. SHELL_ASSETS is a frozen literal list of shell paths. It is the ONLY
//      argument ever passed to `cache.addAll`, and nothing is appended to it at
//      runtime.
//   2. There is no after-the-fact cache write anywhere in this file, so a
//      response that was fetched can never later be stored.
//   3. The fetch handler returns WITHOUT calling `respondWith` for any request
//      that is not an exact SHELL_ASSETS path. Those requests -- every task
//      projection, decision, and evidence image under /api/portal/ -- go to the
//      network untouched by this worker and are never read from or written to
//      a cache.
//   4. Shell assets are served NETWORK-FIRST, with the precached copy only as
//      an offline fallback. Cache-first would pin whatever shell was installed
//      the first time a phone paired: a browser only reinstalls a worker when
//      sw.js itself changes bytes, so a fix shipped in app.js would never
//      reach an already-paired device. Network-first keeps that patch path
//      open without adding a post-fetch cache write.
//
// The protected prefix is named here only so the guard can be asserted; the
// guard itself is an allowlist, so a new protected route is excluded by
// default rather than by remembering to add it.

const SHELL_CACHE = "openadapt-portal-shell-v1";

const PROTECTED_PREFIX = "/api/portal/";

const SHELL_ASSETS = Object.freeze([
  "/",
  "/app.js",
  "/styles.css",
  "/manifest.webmanifest",
]);

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key)),
        ),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  // Allowlist guard. Anything outside the literal shell list -- including
  // everything under PROTECTED_PREFIX -- is left entirely to the network.
  if (!SHELL_ASSETS.includes(url.pathname)) return;
  event.respondWith(
    fetch(request).catch(() =>
      caches.match(request).then((hit) => hit || Response.error()),
    ),
  );
});
