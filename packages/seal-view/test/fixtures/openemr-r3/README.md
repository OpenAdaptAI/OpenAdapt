# One published OpenEMR run's Seal witnesses

`seal.json` of run r3 as openadapt.ai publishes it (public/receipts/r3 in
OpenAdaptAI/openadapt-web), with its Sigstore Rekor entry (`seal.anchor.json`),
its RFC 3161 timestamp (`seal.tsa.json` naming `seal.tsr`), and the site's
pinned Seal keys (`site-keys.json`). The run's report is not here: the
witnesses cover the Seal file, so test/witnesses.test.mjs checks them alone.
