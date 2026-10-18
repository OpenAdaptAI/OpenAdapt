"""Shadow-mode pattern mining. Off by default.

Watches value-free symbols of what a person does across applications, notices
when the same sequence repeats, and offers a draft. It needs an acknowledged
notice to turn on, keeps a 24-hour encrypted buffer on this computer, and
never opens a network connection. See
``docs/SHADOW_MINING.md`` for the privacy posture and the admission path.
"""

from openadapt_capture.shadow.config import ShadowConfig
from openadapt_capture.shadow.consent import (
    Acknowledgement,
    ConsentLedger,
    ShadowConsentRequired,
    notice,
)
from openadapt_capture.shadow.events import (
    ShadowEvent,
    from_browser_event,
    from_capture_event,
    from_window_event,
)
from openadapt_capture.shadow.live import RecorderShadowBridge
from openadapt_capture.shadow.motifs import Motif, Occurrence, mine_motifs
from openadapt_capture.shadow.notify import (
    CollectingNotifier,
    MotifNotifier,
    MotifPrompt,
    PromptAction,
    SuppressionList,
)
from openadapt_capture.shadow.observer import (
    EPISODES_SCHEMA,
    ShadowModeDisabled,
    ShadowObserver,
    export_episodes,
)
from openadapt_capture.shadow.ring import SymbolRing
from openadapt_capture.shadow.storage import (
    ShadowStorageRefused,
    app_data_dir,
    require_unsynced,
)
from openadapt_capture.shadow.symbols import ShadowSymbol, Tokenizer

__all__ = [
    "EPISODES_SCHEMA",
    "Acknowledgement",
    "CollectingNotifier",
    "ConsentLedger",
    "Motif",
    "MotifNotifier",
    "MotifPrompt",
    "Occurrence",
    "PromptAction",
    "RecorderShadowBridge",
    "ShadowConfig",
    "ShadowConsentRequired",
    "ShadowEvent",
    "ShadowModeDisabled",
    "ShadowStorageRefused",
    "ShadowObserver",
    "ShadowSymbol",
    "SuppressionList",
    "SymbolRing",
    "Tokenizer",
    "app_data_dir",
    "export_episodes",
    "from_browser_event",
    "from_capture_event",
    "from_window_event",
    "mine_motifs",
    "notice",
    "require_unsynced",
]
