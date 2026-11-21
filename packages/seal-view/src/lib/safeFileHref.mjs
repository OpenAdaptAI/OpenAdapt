// A host-given file link on a receipt page: a relative path or an https URL.
// Never protocol-relative (//host, ///host), a backslash form (/\host),
// whitespace, or any other scheme (javascript:, data:, http:).
export function safeHref(href) {
    if (typeof href !== 'string' || !href) return null
    if (/[\\\s]/.test(href) || href.startsWith('//')) return null
    if (href.startsWith('https://')) return /^https:\/\/[A-Za-z0-9.-]+(?:[:/?#]|$)/.test(href) ? href : null
    if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(href)) return null
    return /^(?:\/|\.{1,2}\/|[\w.-]+(?:\/|$))/.test(href) ? href : null
}
