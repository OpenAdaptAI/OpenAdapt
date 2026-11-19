"""JavaScript the Playwright backend runs to walk a frame chain.

Each frame boundary of a descent (``PlaywrightBackend._frame_point`` and
``_focused_element_call``) is judged by these functions. Kept apart from the
backend so its size budget holds; the backend re-exports every name.
"""

from __future__ import annotations

_FRAME_SELECTOR_JS = r"""(el) => {
    const doc = el.ownerDocument;
    const unique = (selector) => {
        try { return doc.querySelectorAll(selector).length === 1; }
        catch (_) { return false; }
    };
    if (el.id) {
        const byId = '#' + CSS.escape(el.id);
        if (unique(byId)) return byId;
    }
    const segments = [];
    let node = el;
    while (node && node !== doc.documentElement) {
        const tag = node.tagName.toLowerCase();
        let index = 1;
        for (let sibling = node.previousElementSibling; sibling;
                sibling = sibling.previousElementSibling) {
            if (sibling.tagName === node.tagName) index += 1;
        }
        segments.unshift(tag + ':nth-of-type(' + index + ')');
        const candidate = segments.join(' > ');
        if (unique(candidate)) return candidate;
        node = node.parentElement;
    }
    return null;
}"""


_PROJECT_FRAME_POINT_JS = r"""(el, point) => {
    const [px, py] = point;
    if (document.elementFromPoint(px, py) !== el) return null;

    // getBoundingClientRect can safely invert only positive axis-aligned
    // scale/translation. Reject rotation, skew, mirroring, perspective, and
    // 3-D transforms on the frame or any ancestor rather than guessing.
    const epsilon = 1e-7;
    for (let node = el; node; node = node.parentElement) {
        const style = window.getComputedStyle(node);
        if (style.perspective && style.perspective !== 'none') return null;
        const rotate = style.rotate;
        if (rotate && rotate !== 'none' && rotate !== '0deg') return null;
        const scale = style.scale;
        if (scale && scale !== 'none') {
            const components = scale.trim().split(/\s+/).map(Number);
            if (components.length < 1 || components.length > 2 ||
                    components.some(value => !Number.isFinite(value) || value <= 0)) {
                return null;
            }
        }
        const transform = style.transform;
        if (transform && transform !== 'none') {
            let matrix;
            try { matrix = new DOMMatrixReadOnly(transform); }
            catch (_) { return null; }
            if (!matrix.is2D || Math.abs(matrix.b) > epsilon ||
                    Math.abs(matrix.c) > epsilon || matrix.a <= 0 || matrix.d <= 0) {
                return null;
            }
        }
    }

    const rect = el.getBoundingClientRect();
    const offsetWidth = Number(el.offsetWidth);
    const offsetHeight = Number(el.offsetHeight);
    if (!(rect.width > 0 && rect.height > 0 &&
            offsetWidth > 0 && offsetHeight > 0)) return null;
    const scaleX = rect.width / offsetWidth;
    const scaleY = rect.height / offsetHeight;
    if (!(Number.isFinite(scaleX) && Number.isFinite(scaleY) &&
            scaleX > 0 && scaleY > 0)) return null;
    const contentX = rect.left + Number(el.clientLeft) * scaleX;
    const contentY = rect.top + Number(el.clientTop) * scaleY;
    const localX = (px - contentX) / scaleX;
    const localY = (py - contentY) / scaleY;
    const inside = localX >= 0 && localY >= 0 &&
        localX < Number(el.clientWidth) && localY < Number(el.clientHeight);
    return inside ? {inside: true, x: localX, y: localY} : {inside: false};
}"""

# The tag of the element under a point, without a handle: what a frame descent
# needs to learn that it has reached the target document. ``null`` when
# nothing is there.
_HIT_TAG_JS = r"""([px, py]) => {
    const el = document.elementFromPoint(px, py);
    return el ? el.localName : null;
}"""

# Whether a document (or anything in it) hosts an open shadow root. Without
# one, Playwright's CSS engine and the native ``querySelectorAll`` match the
# same elements for a plain CSS selector, so a native count proves what
# ``locator(selector).count()`` would.
_HAS_SHADOW_JS = r"""(doc) => {
    const walker = doc.createTreeWalker(doc, NodeFilter.SHOW_ELEMENT);
    for (let node = walker.currentNode; node; node = walker.nextNode()) {
        if (node.shadowRoot) return true;
    }
    return false;
}"""

# One frame boundary of a descent, read in a single call on the frame
# element: its tag, the selector that names it, whether that selector names
# exactly this element, and (``point`` given) the point projected into its
# content and the child viewport's size. ``unique`` is ``null`` when this call
# can't decide it the way Playwright would (a shadow root in the document);
# the caller then asks Playwright. ``child`` is ``null`` when the child window
# isn't readable from here (cross-origin); the caller then asks the child.
_FRAME_BOUNDARY_JS = (
    r"""(el, point) => {
    const tag = el.localName;
    if (tag !== 'iframe' && tag !== 'frame') return {tag};
    let projection = null;
    if (point) {
        projection = ("""
    + _PROJECT_FRAME_POINT_JS
    + r""")(el, point);
        if (!projection || projection.inside !== true) return {tag, projection};
    }
    const selector = ("""
    + _FRAME_SELECTOR_JS
    + r""")(el);
    let unique = null;
    if (typeof selector === 'string' && selector && selector.length <= 512 &&
            !("""
    + _HAS_SHADOW_JS
    + r""")(el.ownerDocument)) {
        try {
            const found = el.ownerDocument.querySelectorAll(selector);
            unique = found.length === 1 && found[0] === el;
        } catch (_) {
            unique = false;
        }
    }
    let child = null;
    try {
        const win = el.contentWindow;
        if (win) child = [win.innerWidth, win.innerHeight];
    } catch (_) {
        child = null;
    }
    return {tag, projection, selector, unique, child};
}"""
)
