// In the browser: fetch one run's published files for its Seal page, as the
// exact bytes and text the check runs on (lib/receiptCheck.mjs). Only this
// site's own files, listed in the page's props; nothing is sent anywhere.

async function get(url, bytes = false) {
    const r = await fetch(url, { cache: 'no-store' })
    if (!r.ok) throw new Error(`${url}: ${r.status}`)
    return bytes ? new Uint8Array(await r.arrayBuffer()) : r.text()
}

/** files: { report, seal, anchor?, tsa? }; authorization: authorizationFor() or null. */
export async function loadSealFiles(files, authorization = null) {
    const [report, sealText, anchorText, tsaText, auth] = await Promise.all([
        get(files.report, true),
        get(files.seal),
        files.anchor ? get(files.anchor) : null,
        files.tsa ? get(files.tsa) : null,
        authorization
            ? Promise.all([
                  get(authorization.statement),
                  get(authorization.sig),
                  get(authorization.signers),
                  authorization.anchor ? get(authorization.anchor) : null,
              ])
            : null,
    ])
    return {
        report,
        sealText,
        anchorText,
        tsaText,
        auth: auth
            ? { statement: auth[0], sig: auth[1], signers: auth[2], anchorText: auth[3] }
            : null,
    }
}
