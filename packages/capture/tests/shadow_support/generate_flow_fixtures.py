"""Write the shadow-episode fixtures that openadapt-flow's tests read.

Usage, from the repository root::

    python -m tests.shadow_support.generate_flow_fixtures \
        ../openadapt-flow/tests/fixtures/shadow
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from openadapt_capture.shadow import ShadowConfig, ShadowObserver
from openadapt_capture.shadow.consent import NOTICE_VERSION, Acknowledgement, notice_sha256
from tests.shadow_support.streams import repeated_task_stream, report_task_stream

FIXTURE_KEY = b"fixture-key-0123456789abcdef0123"

CASES = (
    ("invoice_exact", lambda: repeated_task_stream(random.Random(7), 4)),
    ("invoice_variant", lambda: repeated_task_stream(random.Random(7), 4, variant_at=2)),
    ("report_mail", lambda: report_task_stream(random.Random(4), 3)),
)


def main(out_dir: str) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, make in CASES:
        events, end = make()
        observer = ShadowObserver(
            ShadowConfig(enabled=True, work_domains={"erp.example.com"}),
            acknowledgement=Acknowledgement(NOTICE_VERSION, notice_sha256(), end),
            digest_key=FIXTURE_KEY,
            clock=lambda end=end: end,
        )
        observer.observe_all(events)
        (prompt,) = observer.scan()
        export = observer.decide(prompt.motif_id, "review_draft")
        text = json.dumps(export, indent=1, sort_keys=True) + "\n"
        (out / f"{name}.episodes.json").write_text(text)
        print(name, prompt.count, [len(e["steps"]) for e in export["episodes"]])


if __name__ == "__main__":
    main(sys.argv[1])
