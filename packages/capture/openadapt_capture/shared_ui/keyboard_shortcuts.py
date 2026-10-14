"""Shared keyboard shortcuts for all OpenAdapt viewers.

This module provides a unified keyboard shortcut system that ensures
consistent UX across benchmark, training, and capture viewers.
"""

from __future__ import annotations


def get_keyboard_shortcuts_js() -> str:
    """Get JavaScript code for unified keyboard shortcuts.

    Returns:
        JavaScript code that implements standardized keyboard shortcuts.
    """
    return """
// Unified OpenAdapt Keyboard Shortcuts
// Standard shortcuts for consistent UX across all viewers

const KeyboardShortcuts = {
    // Shortcut definitions
    shortcuts: {
        'Space': { action: 'togglePlay', description: 'Play/Pause' },
        'ArrowLeft': { action: 'prevStep', description: 'Previous step' },
        'ArrowRight': { action: 'nextStep', description: 'Next step' },
        'Home': { action: 'firstStep', description: 'First step' },
        'End': { action: 'lastStep', description: 'Last step' },
        'Digit1': { action: 'setSpeed', param: 2000, description: 'Speed 0.5x' },
        'Digit2': { action: 'setSpeed', param: 1000, description: 'Speed 1x' },
        'Digit3': { action: 'setSpeed', param: 500, description: 'Speed 2x' },
        'Digit4': { action: 'setSpeed', param: 250, description: 'Speed 4x' },
        'Digit5': { action: 'setSpeed', param: 125, description: 'Speed 8x' },
        'Escape': { action: 'closeModals', description: 'Close modals/search' },
        'Slash': { action: 'showShortcutsOverlay', description: 'Show shortcuts', modifier: 'shift' },  // Shift+/ = ?
        'KeyF': { action: 'focusSearch', description: 'Search', modifier: 'ctrl' },
    },

    // Viewer-specific actions (override these)
    actions: {
        togglePlay: null,
        prevStep: null,
        nextStep: null,
        firstStep: null,
        lastStep: null,
        setSpeed: null,
        closeModals: null,
        showShortcutsOverlay: null,
        focusSearch: null,
    },

    // Initialize keyboard shortcuts
    init: function(actions) {
        // Register custom actions
        Object.assign(this.actions, actions);

        // Set up event listener
        document.addEventListener('keydown', (e) => this.handleKeydown(e));

        // Create shortcuts overlay
        this.createShortcutsOverlay();
    },

    // Handle keydown events
    handleKeydown: function(e) {
        // Ignore if typing in input/textarea
        if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') {
            return;
        }

        const shortcut = this.shortcuts[e.code];
        if (!shortcut) return;

        // Check modifier requirements
        if (shortcut.modifier === 'ctrl' && !e.ctrlKey && !e.metaKey) return;
        if (shortcut.modifier === 'shift' && !e.shiftKey) return;
        if (shortcut.modifier === 'alt' && !e.altKey) return;

        e.preventDefault();

        const action = this.actions[shortcut.action];
        if (action) {
            if (shortcut.param !== undefined) {
                action(shortcut.param);
            } else {
                action();
            }
        }
    },

    // Create shortcuts overlay
    createShortcutsOverlay: function() {
        const overlay = document.createElement('div');
        overlay.id = 'shortcuts-overlay';
        overlay.className = 'shortcuts-overlay';
        overlay.innerHTML = `
            <div class="shortcuts-panel">
                <div class="shortcuts-header">
                    <h2>Keyboard Shortcuts</h2>
                    <button class="shortcuts-close" onclick="KeyboardShortcuts.hideShortcutsOverlay()">×</button>
                </div>
                <div class="shortcuts-content">
                    ${this.renderShortcutsList()}
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        // Click outside to close
        overlay.addEventListener('click', (e) => {
            if (e.target === overlay) {
                this.hideShortcutsOverlay();
            }
        });
    },

    // Render shortcuts list
    renderShortcutsList: function() {
        const groups = {
            'Playback': ['Space', 'ArrowLeft', 'ArrowRight', 'Home', 'End'],
            'Speed': ['Digit1', 'Digit2', 'Digit3', 'Digit4', 'Digit5'],
            'Navigation': ['Escape', 'Slash', 'KeyF']
        };

        let html = '';
        for (const [group, keys] of Object.entries(groups)) {
            html += `<div class="shortcuts-group">
                <h3>${group}</h3>
                <table class="shortcuts-table">`;

            for (const key of keys) {
                const shortcut = this.shortcuts[key];
                if (!shortcut) continue;

                const keyDisplay = this.formatKey(key, shortcut.modifier);
                html += `
                    <tr>
                        <td class="shortcut-key">${keyDisplay}</td>
                        <td class="shortcut-description">${shortcut.description}</td>
                    </tr>`;
            }

            html += `</table></div>`;
        }

        return html;
    },

    // Format key for display
    formatKey: function(code, modifier) {
        const keyMap = {
            'Space': '␣ Space',
            'ArrowLeft': '← Left',
            'ArrowRight': '→ Right',
            'Home': '⇱ Home',
            'End': '⇲ End',
            'Escape': 'Esc',
            'Slash': '?',
            'KeyF': 'F',
            'Digit1': '1',
            'Digit2': '2',
            'Digit3': '3',
            'Digit4': '4',
            'Digit5': '5',
        };

        let key = keyMap[code] || code;

        if (modifier === 'ctrl') {
            key = (navigator.platform.includes('Mac') ? '⌘' : 'Ctrl+') + key;
        } else if (modifier === 'shift') {
            key = '⇧ ' + key;
        } else if (modifier === 'alt') {
            key = 'Alt+' + key;
        }

        return key;
    },

    // Show shortcuts overlay
    showShortcutsOverlay: function() {
        const overlay = document.getElementById('shortcuts-overlay');
        if (overlay) {
            overlay.classList.add('active');
        }
    },

    // Hide shortcuts overlay
    hideShortcutsOverlay: function() {
        const overlay = document.getElementById('shortcuts-overlay');
        if (overlay) {
            overlay.classList.remove('active');
        }
    }
};
""".strip()


def get_keyboard_shortcuts_css() -> str:
    """Get CSS for keyboard shortcuts overlay.

    Returns:
        CSS code for the shortcuts overlay panel.
    """
    return """
/* Keyboard Shortcuts Overlay */
.shortcuts-overlay {
    display: none;
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.75);
    backdrop-filter: blur(4px);
    z-index: 9999;
    align-items: center;
    justify-content: center;
}

.shortcuts-overlay.active {
    display: flex;
}

.shortcuts-panel {
    background: var(--bg-secondary, #12121a);
    border: 1px solid var(--border-color, rgba(255, 255, 255, 0.06));
    border-radius: 12px;
    max-width: 600px;
    width: 90%;
    max-height: 80vh;
    overflow-y: auto;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
}

.shortcuts-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 20px 24px;
    border-bottom: 1px solid var(--border-color, rgba(255, 255, 255, 0.06));
}

.shortcuts-header h2 {
    font-size: 1.2rem;
    font-weight: 600;
    color: var(--text-primary, #f0f0f0);
    margin: 0;
}

.shortcuts-close {
    background: var(--bg-tertiary, #1a1a24);
    border: 1px solid var(--border-color, rgba(255, 255, 255, 0.06));
    color: var(--text-secondary, #888);
    width: 32px;
    height: 32px;
    border-radius: 50%;
    cursor: pointer;
    font-size: 1.5rem;
    line-height: 1;
    transition: all 0.15s ease;
}

.shortcuts-close:hover {
    background: var(--bg-primary, #0a0a0f);
    color: var(--text-primary, #f0f0f0);
    border-color: rgba(255, 255, 255, 0.12);
}

.shortcuts-content {
    padding: 24px;
}

.shortcuts-group {
    margin-bottom: 24px;
}

.shortcuts-group:last-child {
    margin-bottom: 0;
}

.shortcuts-group h3 {
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--text-muted, #555);
    text-transform: uppercase;
    letter-spacing: 0.05em;
    margin-bottom: 12px;
}

.shortcuts-table {
    width: 100%;
    border-collapse: collapse;
}

.shortcuts-table tr {
    border-bottom: 1px solid var(--border-color, rgba(255, 255, 255, 0.04));
}

.shortcuts-table tr:last-child {
    border-bottom: none;
}

.shortcut-key {
    padding: 10px 12px;
    font-family: "SF Mono", Monaco, "Cascadia Code", monospace;
    font-size: 0.85rem;
    font-weight: 600;
    color: var(--accent, #00d4aa);
    white-space: nowrap;
    width: 140px;
}

.shortcut-description {
    padding: 10px 12px;
    font-size: 0.85rem;
    color: var(--text-secondary, #888);
}

/* Keyboard hint in footer */
.keyboard-hint {
    text-align: center;
    padding: 16px;
    color: var(--text-muted, #555);
    font-size: 0.75rem;
    letter-spacing: 0.02em;
}

.keyboard-hint a {
    color: var(--accent, #00d4aa);
    text-decoration: none;
    cursor: pointer;
}

.keyboard-hint a:hover {
    text-decoration: underline;
}
""".strip()
