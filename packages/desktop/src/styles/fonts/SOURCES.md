# Font sources

Desktop ships two typefaces with the app. Both are licensed under the SIL Open
Font License 1.1, and each licence sits beside its font (`OFL-<family>.txt`).
The fonts are bundled into the app by Vite, so Desktop never calls a font
service on the internet.

| File | Face | Use | Upstream version | Taken from | Subset and axes | Size | SHA-256 |
|---|---|---|---|---|---|---|---|
| `SourceSerif4-Variable.woff2` | Source Serif 4, by Adobe | Page titles, the question on a stop, big numbers, compared values | 4.004 | npm `@fontsource-variable/source-serif-4@5.2.9`, file `files/source-serif-4-latin-wght-normal.woff2` | Latin, weight 200 to 900, no optical-size axis | 50,824 bytes | `c1df4596be5029233ed2afbb8b2f6ea20784b3fb1aa5d6b5c6519ccd85eb3dfb` |
| `PublicSans-Variable.woff2` | Public Sans, by the USWDS project | Everything a person operates: body, labels, buttons, tables | 2.001 | npm `@fontsource-variable/public-sans@5.2.7`, file `files/public-sans-latin-wght-normal.woff2` | Latin, weight 100 to 900 | 26,832 bytes | `5ed4d31c988e73b258894244f209069ebe77dc7e564861954b21198b6de90d68` |

These are the same files, with the same hashes, that the openadapt-web
redesign adds under `styles/fonts/`, so the app and the website set type the
same way.

The built app carries both licence files: `vite.config.ts` copies them to
`licenses/` in the frontend bundle next to the fonts it ships.

Notes:

- Each font also carries its copyright line and licence URL in its own name
  table, so the licence travels with the file inside the built app.
- Characters outside the Latin subset fall back to Georgia (serif) or Segoe UI
  and the system UI face (sans).
- Source Serif 4's licence reserves the font name "Source". The file here is
  the Latin subset that Fontsource republishes from Google Fonts, used
  unchanged. Don't subset or edit it under the same name; the OFL requires a
  modified version to use a different family name.
- To update a face, take the same file from a newer Fontsource release,
  replace it here, update this table, and keep the licence file.
