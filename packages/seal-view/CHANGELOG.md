# Changelog

## v0.1.0 (unreleased)

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
