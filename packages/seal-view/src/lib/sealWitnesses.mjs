// The two outside services that witness a Seal, each checked on its own over
// the Seal file shown (so a changed Seal breaks them too):
//   - Sigstore Rekor, the public transparency log: seal.anchor.json, checked
//     offline (signed entry timestamp, inclusion proof, checkpoint) against the
//     pinned Rekor key (lib/sealAnchor.mjs);
//   - Sigstore's Timestamp Authority: seal.tsa.json naming seal.tsr, an RFC 3161 token checked
//     against the pinned Sigstore TSA root, stamping the Seal's own SHA-256
//     (lib/tsaToken.mjs).
// A witness the files don't have is null: the page shows nothing for it.
import { base64Bytes, duplicateKey, registryKeys } from './localSeal.mjs'
import { REKOR_PUBLIC_KEY_PEM, logKeyFromPem, sealSha256, verifyAnchor, verifyReceiptAnchor } from './sealAnchor.mjs'
import { verifySealTimestamp } from './tsaToken.mjs'

function parse(text) {
    if (text == null || duplicateKey(text) !== null) return null
    try {
        return JSON.parse(text)
    } catch {
        return null
    }
}

/**
 * Check `receipt.anchor.json` against `envelope` using only keys that both
 * signed the receipt (`signers`) and are active in `receiptKeys`.
 */
export async function checkReceiptAnchor(envelope, anchorText, receiptKeys, signers = null, { logKeys = null } = {}) {
    const anchor = parse(anchorText)
    if (!envelope || !anchor) return { ok: false, reason: 'anchor_malformed', entry: null }
    const keyList = Array.isArray(receiptKeys?.keys) ? receiptKeys.keys : registryKeys(receiptKeys)
    const signerIds = signers
        ? new Set(signers.map((s) => s.keyId))
        : new Set((Array.isArray(envelope.signatures) ? envelope.signatures : []).map((s) => s?.keyid).filter(Boolean))
    const candidates = keyList.filter((k) => signerIds.has(k.key_id) && (k.status ?? 'active') === 'active')
    if (!candidates.length) return { ok: false, reason: 'anchor_key_mismatch', entry: null }
    const logs = logKeys || [await logKeyFromPem(REKOR_PUBLIC_KEY_PEM)]
    let last = { ok: false, reason: 'anchor_mismatch', entry: null }
    for (const k of candidates) {
        const r = await verifyReceiptAnchor(envelope, anchor, base64Bytes(k.public_key), logs)
        if (r.ok && r.integratedTime != null) return { ...r, entry: anchor.entry }
        last = { ...r, entry: null }
    }
    return last
}

/**
 * files: { sealText?, anchorText?, receiptText?, receiptAnchorText?, receiptKeys?, tsaText?, tsrBytes? };
 * keys: the pinned Seal keys.
 * Returns { rekor, tsa }, each null or { ok, reason, ... }.
 */
export async function checkWitnesses(files, keys, { logKeys = null } = {}) {
    const seal = parse(files.sealText)
    let rekor = null
    let tsa = null
    if (files.receiptText != null && files.receiptAnchorText != null) {
        rekor = await checkReceiptAnchor(parse(files.receiptText), files.receiptAnchorText, files.receiptKeys, null, { logKeys })
    } else if (files.anchorText != null) {
        const anchor = parse(files.anchorText)
        const pinned = seal ? registryKeys(keys).filter((k) => k.key_id === seal.key_id && k.status === 'active') : []
        if (!seal || !anchor) rekor = { ok: false, reason: 'anchor_malformed' }
        else if (pinned.length !== 1) rekor = { ok: false, reason: 'anchor_key_mismatch' }
        else {
            const logs = logKeys || [await logKeyFromPem(REKOR_PUBLIC_KEY_PEM)]
            const r = await verifyAnchor(seal, anchor, base64Bytes(pinned[0].public_key), logs)
            rekor = { ...r, entry: r.ok ? anchor.entry : null }
        }
    }
    if (files.tsaText != null) {
        tsa = seal ? await verifySealTimestamp(files.tsaText, await sealSha256(seal), files.tsrBytes ?? null) : { ok: false, reason: 'tsa_malformed' }
    }
    return { rekor, tsa }
}
