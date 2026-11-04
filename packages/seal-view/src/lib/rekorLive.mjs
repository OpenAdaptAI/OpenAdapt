// In the browser: look a Seal's anchor up in the public Rekor log, live, as a
// second look beside the offline proof (lib/sealAnchor.mjs). Only the pinned
// host below is ever asked; no URL is taken from a file. Any network, CORS or
// format problem returns 'unavailable', and the page keeps the offline result.

export const REKOR_API = 'https://rekor.sigstore.dev/api/v1/log/entries'
export const rekorSearchUrl = (logIndex) => `https://search.sigstore.dev/?logIndex=${Number(logIndex)}`

/**
 * entry: the anchor's stored entry (seal.anchor.json `entry`, already checked
 * offline). Returns 'match' when the log serves the same entry (uuid, body,
 * integrated time, log ID and index), 'differs' when it serves another one,
 * or 'unavailable'.
 */
export async function rekorLiveMatch(entry, { fetchImpl = globalThis.fetch, timeoutMs = 6000 } = {}) {
    if (!entry || !Number.isInteger(entry.logIndex) || entry.logIndex < 0) return 'unavailable'
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null
    const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : null
    try {
        const r = await fetchImpl(`${REKOR_API}?logIndex=${entry.logIndex}`, {
            signal: ctrl?.signal,
            headers: { accept: 'application/json' },
            credentials: 'omit',
            referrerPolicy: 'no-referrer',
        })
        if (!r.ok) return 'unavailable'
        const data = await r.json()
        const keys = data && typeof data === 'object' ? Object.keys(data) : []
        if (keys.length !== 1) return 'unavailable'
        const live = data[keys[0]]
        // The log may name the entry by its full UUID (tree ID + leaf hash) or the leaf hash alone.
        const same =
            String(keys[0]).slice(-64) === String(entry.uuid).slice(-64) &&
            live?.body === entry.body &&
            live?.integratedTime === entry.integratedTime &&
            live?.logID === entry.logID &&
            live?.logIndex === entry.logIndex
        return same ? 'match' : 'differs'
    } catch {
        return 'unavailable'
    } finally {
        if (timer) clearTimeout(timer)
    }
}
