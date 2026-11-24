# Font sources

The site uses two faces, both licensed under the SIL Open Font License 1.1.
Each licence sits beside its file (`OFL-PublicSans.txt`, `OFL-InterTight.txt`).
Public Sans sets every page; Inter Tight sets the home page's display
headings only (components/home/Home.module.css), loaded there and not preloaded
site-wide. The site serves it itself;
no page calls a font CDN.

| File | Face | Upstream version | Taken from | Subset and axes | Size | SHA-256 |
|---|---|---|---|---|---|---|
| `PublicSans-Variable.woff2` | Public Sans, by the USWDS project | 2.001 | npm `@fontsource-variable/public-sans@5.2.7`, file `public-sans-latin-wght-normal.woff2` | Latin, weight 100 to 900 | 26,832 bytes | `5ed4d31c988e73b258894244f209069ebe77dc7e564861954b21198b6de90d68` |
| `InterTight-Variable.woff2` | Inter Tight, by The Inter Project Authors (Google Fonts family) | Google Fonts v9 | npm `@fontsource-variable/inter-tight@5.3.0`, file `inter-tight-latin-wght-normal.woff2` | Latin, weight 100 to 900 | 44,872 bytes | `77fefe8ca19b9f69b5284832c519e0493127c1f091f0a8936884be7721c4e618` |

Notes:

- This is the same file the simple home page (site-v2/simple-real, 2026-10-12)
  uses. Source Serif 4 was removed on 2026-10-14 when every page moved to the
  simple page's tokens.
- The Latin subset covers English text, the curly quotes and apostrophes the
  copy uses, and the currency signs. Characters outside it fall back to the
  system sans-serif.
- To update the face, take the same file from a newer Fontsource release,
  replace it here, update this table, and keep the licence file.
