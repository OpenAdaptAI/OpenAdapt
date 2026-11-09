# Vendored design tokens — canonical source is `OpenAdaptAI/openadapt-web`

`openadapt-web` owns the OpenAdapt design tokens. `styles/tokens.json` there is
the single source of truth for palette, type scale, spacing, radii, elevation,
and motion, and it names this repository as a `vendor + hash-check` consumer.

Every file in this directory is a **byte-identical copy** of its counterpart in
`openadapt-web`. Do not hand-edit one. A hand edit is exactly the drift this
directory exists to make impossible, and it fails CI.

| vendored file | canonical path in `openadapt-web` |
| --- | --- |
| `tokens.json` | `styles/tokens.json` |
| `tokens.css` | `styles/tokens.css` |

`provenance.json` records the upstream commit and the SHA-256 of each copy.

## Changing a token

1. Change the value in `openadapt-web` (`styles/tokens.json` **and**
   `styles/tokens.css`; `tests/designTokens.test.js` there fails if they
   disagree). Merge it.
2. Here, run `node scripts/vendor-design-tokens.mjs --write`. It refetches the
   canonical files, rewrites this directory, and rewrites `provenance.json`.
3. Review the diff and commit it.

## What Desktop adds on top

`src/styles/tokens.css` imports `tokens.css` from this directory and then
defines the Desktop-only tokens the canonical set does not carry: the phosphor
(dark) execution surface, the status hues, the pill fills, and the 4px product
density scale. Every colour there derives from a canonical token.

Desktop deliberately keeps its own `--space-*` and `--fs-*` scales. The
canonical spacing scale is indexed for editorial layout; the Desktop scale is a
4px product-density scale where `--space-16` means 16 units of 4px. That is a
scale divergence, not a palette divergence, and
`src/styles/tokens.css` says so at the point where it happens.

## What enforces this

- `scripts/designTokens.test.ts` runs offline in `npm run test:ui`. It fails
  when a vendored file's SHA-256 stops matching `provenance.json`, when Desktop
  redefines a canonical **colour** token, and when a retired warm-palette value
  reappears in any stylesheet the app ships.
- `node scripts/vendor-design-tokens.mjs --check` runs in CI with network
  access. It fails when a vendored copy differs from the canonical file on
  `openadapt-web@main` — that is, when the surfaces have drifted apart.
