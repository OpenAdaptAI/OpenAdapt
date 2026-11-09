"""Shadow-mode pattern mining: detection, privacy, and the hard invariants."""

from __future__ import annotations

import ast
import json
import os
import random
import socket
import sys
from pathlib import Path

import pytest

from openadapt_capture.shadow import (
    EPISODES_SCHEMA,
    CollectingNotifier,
    MotifPrompt,
    PromptAction,
    ShadowConfig,
    ShadowEvent,
    ShadowModeDisabled,
    ShadowObserver,
    SuppressionList,
    SymbolRing,
    Tokenizer,
    from_capture_event,
)
from openadapt_capture.shadow.consent import NOTICE_VERSION, Acknowledgement, notice_sha256
from openadapt_capture.shadow.motifs import lcp_array, poisson_tail, suffix_array
from openadapt_capture.shadow.notify import PromptLeak, assert_generic_prompt
from openadapt_capture.shadow.ring import RowCipher
from openadapt_capture.shadow.symbols import normalize_role, url_pattern
from tests.shadow_support.streams import (
    SECRET_VALUES,
    invoice_episode,
    random_events,
    repeated_task_stream,
    report_task_stream,
)

KEY = b"k" * 32
ACK = Acknowledgement(NOTICE_VERSION, notice_sha256(), 0.0)
WORK = frozenset({"erp.example.com"})


def _cipher() -> RowCipher:
    return RowCipher(b"s" * 32, key_id="test", check="check")


SHADOW_DIR = Path(__file__).resolve().parents[1] / "openadapt_capture" / "shadow"


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _observer(events, end_t, **cfg):
    clock = FakeClock(end_t)
    notifier = CollectingNotifier()
    cfg.setdefault("work_domains", WORK)
    config = ShadowConfig(enabled=True, **cfg)
    obs = ShadowObserver(
        config, notifier=notifier, acknowledgement=ACK, digest_key=KEY, clock=clock
    )
    obs.observe_all(events)
    return obs, notifier, clock


# ---------------------------------------------------------------------------
# Off by default, explicit opt-in
# ---------------------------------------------------------------------------


def test_off_by_default_drops_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENADAPT_SHADOW_ENABLED", "1")
    monkeypatch.setenv("OPENADAPT_SHADOW_MINING", "true")
    assert ShadowConfig().enabled is False
    store = tmp_path / "shadow" / "ring.db"
    obs = ShadowObserver(ShadowConfig(store_path=store))
    events, _ = repeated_task_stream(random.Random(1), 5)
    obs.observe_all(events)
    assert obs.enabled is False
    assert obs.symbols() == []
    assert obs.scan() == []
    assert not store.exists() and not store.parent.exists()
    with pytest.raises(ShadowModeDisabled):
        obs.decide("0" * 16, "review_draft")


def test_enabled_requires_a_real_bool():
    with pytest.raises(TypeError):
        ShadowConfig(enabled="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_detects_repeated_cross_app_task_and_offers_prompt():
    events, end = repeated_task_stream(random.Random(7), 4)
    obs, notifier, _ = _observer(events, end)
    prompts = obs.scan()
    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.count == 4
    assert prompt.body == "You did this 4 times today."
    assert [a.value for a in prompt.actions] == ["review_draft", "ignore"]
    assert notifier.prompts == prompts
    export = obs.decide(prompt.motif_id, PromptAction.REVIEW_DRAFT)
    assert export["schema"] == EPISODES_SCHEMA
    assert export["status"] == "candidate"
    assert set(export["apps"]) == {"Google Chrome", "Microsoft Excel"}
    assert len(export["episodes"]) == 4
    kinds = [s["kind"] for s in export["episodes"][0]["steps"]]
    assert kinds[:3] == ["focus", "click", "copy"]
    assert "paste" in kinds and kinds[-1] == "submit"
    # A second scan with nothing new offers nothing.
    assert obs.scan() == []


def test_detects_save_and_mail_task():
    events, end = report_task_stream(random.Random(4), 3)
    obs, _, _ = _observer(events, end)
    (prompt,) = obs.scan()
    assert prompt.count == 3
    export = obs.export(prompt.motif_id)
    labels = [s["display_label"] for s in export["episodes"][0]["steps"]]
    assert "Send" in labels and "New Message" in labels
    assert labels.count(None) >= 6  # field labels and values stay digests


def test_below_threshold_does_not_fire():
    events, end = repeated_task_stream(random.Random(7), 2)
    obs, _, _ = _observer(events, end)
    assert obs.scan() == []


def test_near_repeat_joins_by_structural_similarity():
    events, end = repeated_task_stream(random.Random(3), 4, variant_at=2)
    obs, _, _ = _observer(events, end)
    (prompt,) = obs.scan()
    export = obs.export(prompt.motif_id)
    assert prompt.count == 4
    flags = [e["exact"] for e in export["episodes"]]
    assert flags.count(False) == 1
    approx = next(e for e in export["episodes"] if not e["exact"])
    assert approx["similarity"] >= 0.8


def test_single_app_repeat_is_not_offered_by_default():
    rng = random.Random(5)
    events, t = [], 2_000_000.0
    for _ in range(5):
        noise, t = random_events(rng, t, 20)
        events += noise
        t += 30
        for i, label in enumerate(["Reply", "Archive", "Next", "Flag", "Move"]):
            events.append(ShadowEvent(t + i, "click", app="Mail", role="button", label=label))
        t += 10
    obs, _, _ = _observer(events, t)
    assert obs.scan() == []
    obs_one_app, _, _ = _observer(events, t, min_apps=1)
    assert len(obs_one_app.scan()) == 1


def test_window_limits_old_occurrences():
    events, end = repeated_task_stream(random.Random(9), 4, noise_between=5)
    # Push "now" so that only the last episode is inside a short window.
    obs, _, _ = _observer(events, end, window_s=120.0, max_age_s=86400)
    assert obs.scan() == []


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("alphabet", [8, 40, 200])
def test_no_false_positives_on_random_streams(seed, alphabet):
    rng = random.Random(seed * 1000 + alphabet)
    events, end = random_events(rng, 3_000_000.0, 4000, alphabet=alphabet)
    obs, _, _ = _observer(
        events, end, min_apps=1, idle_gap_s=10**6, max_age_s=86400, window_s=86400
    )
    assert obs.scan() == []


@pytest.mark.parametrize("seed", range(3))
def test_no_false_positives_on_a_full_buffer_of_cross_app_noise(seed):
    rng = random.Random(seed)
    events, end = random_events(rng, 4_000_000.0, 20_000, alphabet=40)
    obs, _, _ = _observer(events, end, idle_gap_s=10**6, max_age_s=86400, window_s=86400)
    assert obs.scan() == []


def test_detects_task_inside_a_full_buffer_of_noise():
    # About 22 hours of noise: the most a 24-hour buffer can hold around it.
    events, end = repeated_task_stream(random.Random(2), 4, noise_between=1900)
    obs, _, _ = _observer(events, end, idle_gap_s=10**6, max_age_s=86400, window_s=86400)
    prompts = obs.scan()
    assert [p.count for p in prompts] == [4]


def test_deterministic_across_runs():
    events, end = repeated_task_stream(random.Random(11), 5)
    a, _, _ = _observer(events, end)
    b, _, _ = _observer(events, end)
    pa, pb = a.scan(), b.scan()
    assert [p.motif_id for p in pa] == [p.motif_id for p in pb]
    assert a.export(pa[0].motif_id) == b.export(pb[0].motif_id)


def test_suffix_array_and_lcp_match_naive():
    rng = random.Random(0)
    seq = [rng.randint(1, 4) for _ in range(300)]
    sa = suffix_array(seq)
    assert sa == sorted(range(len(seq)), key=lambda i: seq[i:])
    lcp = lcp_array(seq, sa)
    for r in range(1, len(sa)):
        a, b = seq[sa[r - 1] :], seq[sa[r] :]
        k = 0
        while k < min(len(a), len(b)) and a[k] == b[k]:
            k += 1
        assert lcp[r] == k


def test_poisson_tail():
    assert poisson_tail(0, 3.0) == 1.0
    assert poisson_tail(1, 2.0) == pytest.approx(1 - 2.718281828**-2.0, rel=1e-6)
    assert poisson_tail(5, 1e-6) < 1e-29


# ---------------------------------------------------------------------------
# Ignore and suppression
# ---------------------------------------------------------------------------


def test_ignore_suppresses_motif_and_close_variants(tmp_path):
    events, end = repeated_task_stream(random.Random(7), 4)
    suppress_file = tmp_path / "ignored.json"
    clock = FakeClock(end)
    obs = ShadowObserver(
        ShadowConfig(enabled=True, work_domains=WORK),
        suppression=SuppressionList(suppress_file),
        acknowledgement=ACK,
        digest_key=KEY,
        clock=clock,
    )
    obs.observe_all(events)
    (prompt,) = obs.scan()
    assert obs.decide(prompt.motif_id, "ignore") is None
    more, end2 = repeated_task_stream(random.Random(8), 3, start=end + 60, variant_at=1)
    obs.observe_all(more)
    clock.now = end2
    assert obs.scan() == []
    # Persisted: a fresh observer with the same key and list stays quiet too.
    again = ShadowObserver(
        ShadowConfig(enabled=True, work_domains=WORK),
        suppression=SuppressionList(suppress_file),
        acknowledgement=ACK,
        digest_key=KEY,
        clock=clock,
    )
    again.observe_all(events + more)
    assert again.scan() == []
    stored = suppress_file.read_text()
    for value in SECRET_VALUES + ("Excel", "Chrome", "Submit"):
        assert value not in stored
    if sys.platform != "win32":  # POSIX mode bits; Windows uses ACLs
        assert oct(suppress_file.stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# Typed values never stored
# ---------------------------------------------------------------------------


def test_typed_values_never_reach_symbols_store_or_export(tmp_path):
    store = tmp_path / "ring.db"
    events, end = repeated_task_stream(random.Random(7), 4)
    events.append(
        ShadowEvent(
            end - 1,
            "type",
            app="Mail",
            role="AXSecureTextField",
            label="Password",
            text=SECRET_VALUES[5],
            secure=True,
        )
    )
    events.append(ShadowEvent(end - 0.5, "click", app="Mail", role="row", label=SECRET_VALUES[6]))
    events.append(
        ShadowEvent(
            end - 0.4, "type", app="Mail", role="textfield", label="To", text=SECRET_VALUES[2]
        )
    )
    obs, _, _ = _observer(events, end, store_path=store)
    (prompt,) = obs.scan()
    export = obs.export(prompt.motif_id)
    blobs = [
        json.dumps([s.to_dict() for s in obs.symbols()]),
        json.dumps(export),
        repr(obs.symbols()),
    ]
    obs.close()
    # The store file is ciphertext; searching its bytes for short strings tests
    # nothing (and random base64 can contain "ssn"). What was sealed into it is
    # the symbol list above; test_the_disk_buffer_holds_only_ciphertext covers
    # the file itself.
    assert store.exists()
    for blob in blobs:
        for value in SECRET_VALUES:
            assert value not in blob, value
            assert value.lower() not in blob.lower(), value
        assert "invoices/INV" not in blob
        assert "ssn" not in blob and "#Jane" not in blob
    tok = Tokenizer(digest_key=KEY)
    secure = tok.feed(events[-3]) + tok.flush()
    assert secure[0].value_digest is None and secure[0].token.endswith("|secure")


def test_short_chrome_labels_are_the_only_plain_text():
    tok = Tokenizer(ShadowConfig(enabled=True), digest_key=KEY)
    (save,) = tok.feed(ShadowEvent(1, "click", app="X", role="AXButton", label="Save"))
    (row,) = tok.feed(ShadowEvent(2, "click", app="X", role="row", label="Jane Doe"))
    (btn,) = tok.feed(ShadowEvent(3, "click", app="X", role="button", label="Pay 1200"))
    assert save.display_label == "Save"
    assert row.display_label is None and row.value_digest is not None
    assert "Jane" not in row.token
    assert btn.display_label == "<dynamic_label>"  # digits could be data
    off = Tokenizer(ShadowConfig(enabled=True, keep_chrome_labels=False), digest_key=KEY)
    (save2,) = off.feed(ShadowEvent(1, "click", app="X", role="button", label="Save"))
    assert save2.display_label is None


def test_type_bursts_merge_into_one_digest():
    tok = Tokenizer(digest_key=KEY)
    out = []
    out += tok.feed(ShadowEvent(1, "type", app="A", role="textfield", label="F", text="44"))
    out += tok.feed(ShadowEvent(1.5, "type", app="A", role="textfield", label="F", text="71"))
    assert out == []  # held until the next event or a flush
    out += tok.flush()
    (merged,) = out
    assert merged.value_digest == Tokenizer(digest_key=KEY).digest("value", "4471")
    assert merged.value_shape == "digits"
    # A long pause starts a new value.
    tok.feed(ShadowEvent(10, "type", app="A", role="textfield", label="F", text="1"))
    split = tok.feed(ShadowEvent(20, "type", app="A", role="textfield", label="F", text="2"))
    split += tok.flush()
    assert len(split) == 2


def test_digest_depends_on_key():
    a = Tokenizer(digest_key=b"a" * 32).digest("value", "4471")
    b = Tokenizer(digest_key=b"b" * 32).digest("value", "4471")
    assert a != b and len(a) == 16


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://erp.example.com/invoices/INV-1?x=1#y", "https://erp.example.com/invoices/*"),
        ("https://u:p@host.example.com:8443/Patients/123", "https://<id>.example.com/<id>/*"),
        ("https://mail.example.com/", "https://mail.example.com"),
        ("https://other.org/invoices", "non-work-browser"),
        ("file:///Users/jane/notes.txt", "file:*"),
        (None, None),
    ],
)
def test_url_pattern(url, expected):
    work = frozenset({"erp.example.com", "mail.example.com", "example.com"})
    assert url_pattern(url, work) == expected


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        ("AXButton", "button"),
        ("Edit", "textfield"),
        ("push button", "button"),
        ("AXTextField", "textfield"),
        (None, "unknown"),
        ("DataItem", "cell"),
    ],
)
def test_normalize_role(role, expected):
    assert normalize_role(role) == expected


# ---------------------------------------------------------------------------
# Ring buffer
# ---------------------------------------------------------------------------


def _sym(tok: Tokenizer, t: float, i: int):
    return tok.feed(ShadowEvent(t, "click", app="A", role="button", label=f"B{i}"))[0]


@pytest.mark.parametrize("on_disk", [False, True])
def test_ring_buffer_expires_by_age(tmp_path, on_disk):
    clock = FakeClock(100.0)
    ring = SymbolRing(
        max_symbols=100,
        max_age_s=10.0,
        clock=clock,
        path=tmp_path / "r.db" if on_disk else None,
        cipher=_cipher() if on_disk else None,
    )
    tok = Tokenizer(digest_key=KEY)
    for i in range(5):
        ring.append(_sym(tok, 95.0 + i, i))
    assert len(ring) == 5
    clock.now = 106.5  # symbols at 95 and 96 are now older than 10 s
    assert [s.t for s in ring.snapshot()] == [97.0, 98.0, 99.0]
    clock.now = 1000.0
    assert ring.snapshot() == []
    ring.close()


@pytest.mark.parametrize("on_disk", [False, True])
def test_ring_buffer_overwrites_oldest_at_size_cap(tmp_path, on_disk):
    clock = FakeClock(0.0)
    ring = SymbolRing(
        max_symbols=3,
        max_age_s=1e9,
        clock=clock,
        path=tmp_path / "r.db" if on_disk else None,
        cipher=_cipher() if on_disk else None,
    )
    tok = Tokenizer(digest_key=KEY)
    for i in range(7):
        ring.append(_sym(tok, float(i), i))
    assert [s.t for s in ring.snapshot()] == [4.0, 5.0, 6.0]
    ring.close()


def test_ring_file_is_private_and_expired_rows_are_gone(tmp_path):
    path = tmp_path / "sub" / "r.db"
    clock = FakeClock(0.0)
    ring = SymbolRing(max_symbols=1000, max_age_s=5.0, clock=clock, path=path, cipher=_cipher())
    tok = Tokenizer(digest_key=KEY)
    old = _sym(tok, 0.0, 0)
    ring.append(old)
    clock.now = 100.0
    ring.append(_sym(tok, 100.0, 1))
    ring.close()
    if sys.platform != "win32":  # POSIX mode bits; Windows uses ACLs
        assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert old.label_digest not in path.read_bytes().decode("latin-1")
    assert not Path(str(path) + "-wal").exists()


def test_purge_empties_store(tmp_path):
    events, end = repeated_task_stream(random.Random(7), 4)
    obs, _, _ = _observer(events, end, store_path=tmp_path / "r.db")
    assert obs.symbols()
    obs.purge()
    assert obs.symbols() == []


# ---------------------------------------------------------------------------
# Zero network egress
# ---------------------------------------------------------------------------

_NETWORK_MODULES = {
    "socket",
    "ssl",
    "http",
    "urllib.request",
    "requests",
    "httpx",
    "aiohttp",
    "smtplib",
    "ftplib",
    "websocket",
    "websockets",
    "boto3",
    "botocore",
    "grpc",
}


def test_shadow_package_imports_no_network_module():
    for path in sorted(SHADOW_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                for banned in _NETWORK_MODULES:
                    assert not (name == banned or name.startswith(banned + ".")), (
                        f"{path.name} imports {name}"
                    )


def test_full_pipeline_makes_zero_network_calls(tmp_path, monkeypatch):
    attempts: list[str] = []

    def refuse(name):
        def _raise(*args, **kwargs):
            attempts.append(name)
            raise AssertionError(f"shadow mining attempted network access via {name}")

        return _raise

    for name in ("connect", "connect_ex", "sendto", "bind"):
        monkeypatch.setattr(socket.socket, name, refuse(f"socket.{name}"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    monkeypatch.setattr(socket, "gethostbyname", refuse("gethostbyname"))

    events, end = repeated_task_stream(random.Random(7), 4)
    obs, _, clock = _observer(events, end, store_path=tmp_path / "r.db")
    (prompt,) = obs.scan()
    export = obs.decide(prompt.motif_id, "review_draft")
    assert export is not None
    obs.decide(prompt.motif_id, "ignore")
    obs.purge()
    obs.close()
    assert attempts == []


# ---------------------------------------------------------------------------
# Prompt payload
# ---------------------------------------------------------------------------


def test_prompt_payload_is_generic():
    payload = MotifPrompt(motif_id="0123456789abcdef", count=5).payload()
    assert payload["body"] == "You did this 5 times today."
    assert [a["label"] for a in payload["actions"]] == ["Review draft", "Ignore"]
    with pytest.raises(PromptLeak):
        assert_generic_prompt({**payload, "app": "Microsoft Excel"})
    with pytest.raises(PromptLeak):
        assert_generic_prompt({**payload, "body": "You copied INV-1 5 times today."})


# ---------------------------------------------------------------------------
# Capture adapters
# ---------------------------------------------------------------------------


def test_from_capture_event_maps_clicks_types_and_clipboard_chords():
    from openadapt_capture.events import KeyShortcutEvent, KeyTypeEvent, MouseClickEvent
    from openadapt_capture.structural import (
        StructuralElement,
        StructuralObservation,
        StructuralProcessIdentity,
    )

    obs = StructuralObservation(
        provider="macos_ax",
        event_timestamp=1.0,
        observed_at=1.0,
        query_kind="point",
        element=StructuralElement(role="AXSecureTextField", name="Password"),
        process=StructuralProcessIdentity(process_name="Safari"),
    )
    click = from_capture_event(
        MouseClickEvent(timestamp=1.0, x=1, y=2, button="left", structural_observation=obs)
    )
    assert click.kind == "click" and click.app == "Safari" and click.secure is True
    typed = from_capture_event(KeyTypeEvent(timestamp=2.0, text="abc"))
    assert typed.kind == "type" and typed.text == "abc"
    copy = from_capture_event(KeyShortcutEvent(timestamp=3.0, modifiers=["meta"], key="c"))
    paste = from_capture_event(KeyShortcutEvent(timestamp=4.0, modifiers=["ctrl"], key="v"))
    save = from_capture_event(KeyShortcutEvent(timestamp=5.0, modifiers=["meta"], key="s"))
    assert (copy.kind, paste.kind, save.kind) == ("copy", "paste", "shortcut")
    assert "abc" not in repr(typed)


def test_invoice_episode_fixture_shape():
    assert len(invoice_episode(0.0, "INV-1", "12.50")) == 12
    assert os.path.basename(__file__) == "test_shadow_mining.py"
