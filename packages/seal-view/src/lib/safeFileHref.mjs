// A host-given file link on a receipt page: a relative path or an https URL.
// Never protocol-relative (//host, ///host), a backslash form (/\host),
// whitespace, userinfo (https://host:@evil), or any other scheme
// (javascript:, data:, http:).
export function safeHref(href) {
    if (typeof href !== 'string' || !href) return null
    if (/[\\\s]/.test(href) || href.startsWith('//')) return null
    // Host, then an optional numeric port, then the end or a path, query or
    // fragment. Nothing else may follow the host, so userinfo forms such as
    // https://openadapt.ai:443@evil.example never pass.
    if (href.startsWith('https://')) return /^https:\/\/[A-Za-z0-9.-]+(?::\d{1,5})?(?:[/?#]|$)/.test(href) ? href : null
    if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(href)) return null
    return /^(?:\/|\.{1,2}\/|[\w.-]+(?:\/|$))/.test(href) ? href : null
}
