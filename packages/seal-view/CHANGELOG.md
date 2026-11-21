# Changelog

## v0.1.0 (unreleased)

- Every string a reader sees says "receipt", not "Seal". The verdict card
  always shows a signer line ("Signed by this computer", "Demo key: checks
  only in this demo" with `host.signer: 'demo'`, or "Not verified: unknown
  signer"), and marks a `local-seal/v1` file "older format". The package name
  is unchanged.
- "Tamper with it" is "Try changing it": one click flips the outcome in a
  copy of the report and checks the copy. The verdict card shows the from→to
  line, "The published files are unchanged; this was a copy in your browser",
  "Restore" and the other two changes inline. The separate tamper panel is
  gone; Verify always checks the originals.
- The map inspector's "Back to the Seal ↑" button and its scroll-and-pulse
  are gone: the split view keeps the receipt on screen. The landing card keeps
  one link, "Open the receipt →". Card and page buttons wrap inside their card
  from 320 to 1920 px.
- A step's screenshots, when a host shows them, carry `host.mediaNote`
  (default "From this computer's run folder: not part of the receipt, never
  uploaded"). A receipt never holds screenshots.

- The Seal page is one screen, like an app: the program map on the left
  (resizable, or the whole width), the Seal and verdict top right, and
  Summary, Steps, Evidence and Raw tabs bottom right; one column with the map
  as a tab below 900 px. The verdict stays in the top bar. The URL's fragment
  keeps the tab and the step. Light and dark themes from the shared `--oa-*`
  roles; the offline viewer inlines them. A failed read-back now says "not
  exactly 1" or "not equal" instead of the passing wording, and the report's
  digest says "not computed" when nothing was computed.

- `SealPage`: one run's Seal, drawn from its signed report (step ring, screen
  checks, read-backs, right-record checks and an iris drawn from the
  digests), checked in the browser (Ed25519 over the exact `report.json`),
  replayed over the step table and the program map, with a tamper demo. The
  host's own parts (links, footer, live checks, how files load) come in
  `host`.
- `SealArt`, `MiniIris`, `SealEmblem`, `SealMap` (the full program
  workbench, with a Seal card in its inspector: `InspectorSealCard`,
  `useInspectorHost`), `HashLine`, `OpenAdaptMark`; `sealOutcomeLine`.
- The checks: offline Seals (`openadapt.local-seal/v1`), run authorizations,
  Sigstore Rekor anchors and RFC 3161 timestamps, the same rules and refusal
  codes as `openadapt-flow local-seal verify`. A pure-JavaScript Ed25519
  check runs when WebCrypto has no Ed25519.
- `sealPageProps`: the page's props from a run's files as data, in the
  browser or in Node.
- `dist/seal-viewer.html`: the offline viewer, one HTML file with no network
  requests. The launcher ships it as `openadapt/seal_view/viewer.html`.
- Extracted from openadapt.ai's Seal page (OpenAdaptAI/openadapt-web#587,
  through 4da61c59).
