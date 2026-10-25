// A run's date as the Seal page shows it (openadapt.ai's lib/sealCard.mjs runDate).

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']

/** "20 Oct 2026, 03:03 UTC" from an ISO time, or null. */
export function runDate(iso) {
    const d = new Date(iso)
    if (!iso || Number.isNaN(d.getTime())) return null
    const pad = (n) => String(n).padStart(2, '0')
    return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}, ${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())} UTC`
}
