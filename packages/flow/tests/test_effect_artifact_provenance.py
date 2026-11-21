"""File, PDF and email checks are graded by who produced the artifact.

A hash or arrival check of a file proves only as much as its producer allows.
A PDF the acting browser downloaded is the acting session's own copy, so its
check stays at ``same_screen`` and the run is not ``VERIFIED``, even though
the hash matches. The same bytes fetched by a separate session reach
``separate_session``; read from the system's own store they reach
``system_of_record``.
"""

from __future__ import annotations

import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from openadapt_flow.check_level import CheckLevel
from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.readback_ladder import summarize_report, tier_floor
from openadapt_flow.runtime.effects import (
    DocumentArrivalVerifier,
    DocumentHashVerifier,
    Effect,
    EffectKind,
    FileArrivalVerifier,
    MaildirDeliveryVerifier,
    Verdict,
)
from openadapt_flow.runtime.effects.artifact_provenance import (
    ArtifactProducer,
    grade_artifact,
    parse_producer,
)
from openadapt_flow.verification import VerificationTier, verifier_effect_tier

#: A hand-written one-page PDF; no generator library involved.
PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()


def _hash_effect(name: str = "claim.pdf", digest: str = PDF_SHA256) -> Effect:
    return Effect(
        kind=EffectKind.FIELD_EQUALS,
        match={"name": name},
        field="sha256",
        value=digest,
    )


def _run_readback(verifier, verdict) -> dict:
    """The run's ``readback`` block for one save checked by ``verifier``."""

    tier = verifier_effect_tier(verifier, None)
    item = SimpleNamespace(
        final_verdict="confirmed" if verdict.confirmed else "refuted",
        observed_effect=verdict.observed_effect,
        verification_tier=int(tier) if tier is not None else None,
    )
    result = SimpleNamespace(
        step_id="save", effect_evidence=[item], effect_contract_hashes=["h"]
    )
    block = summarize_report(
        SimpleNamespace(results=[result]), tier_floor("separate_session")
    )
    assert block is not None
    return block


# -- grading rules ------------------------------------------------------------


def test_undeclared_producer_is_graded_like_the_acting_session(tmp_path: Path):
    for verifier in (
        DocumentHashVerifier(tmp_path),
        FileArrivalVerifier(tmp_path),
        MaildirDeliveryVerifier(str(tmp_path)),
        DocumentArrivalVerifier(str(tmp_path)),
    ):
        assert verifier.verification_tier is VerificationTier.IMMEDIATE_SCREEN
        assert verifier_effect_tier(verifier, None) is VerificationTier.IMMEDIATE_SCREEN
        assert verifier.independent_system_of_record is False
        assert verifier.provenance.producer is ArtifactProducer.UNKNOWN


@pytest.mark.parametrize(
    ("producer", "tier", "level"),
    [
        ("acting_session", VerificationTier.IMMEDIATE_SCREEN, CheckLevel.SAME_SCREEN),
        (
            "separate_session",
            VerificationTier.INDEPENDENT_SESSION,
            CheckLevel.SEPARATE_SESSION,
        ),
        (
            "system_of_record",
            VerificationTier.INDEPENDENT_SYSTEM,
            CheckLevel.SYSTEM_OF_RECORD,
        ),
    ],
)
def test_declared_producer_sets_the_strongest_tier(tmp_path, producer, tier, level):
    for verifier in (
        DocumentHashVerifier(tmp_path, producer=producer),
        FileArrivalVerifier(tmp_path, producer=producer),
        MaildirDeliveryVerifier(str(tmp_path), producer=producer),
        DocumentArrivalVerifier(str(tmp_path), producer=producer),
    ):
        assert verifier_effect_tier(verifier, None) is tier
        assert verifier.provenance.check_level is level
        assert verifier.independent_system_of_record is (
            level is CheckLevel.SYSTEM_OF_RECORD
        )


def test_a_misspelled_producer_is_refused(tmp_path: Path):
    with pytest.raises(ValueError, match="artifact producer"):
        DocumentHashVerifier(tmp_path, producer="system-of-recrod")
    assert parse_producer("system-of-record") is ArtifactProducer.SYSTEM_OF_RECORD
    assert parse_producer("") is ArtifactProducer.UNKNOWN


def test_a_store_inside_an_acting_folder_is_capped(tmp_path: Path):
    downloads = tmp_path / "acting-downloads"
    store = downloads / "exports"
    store.mkdir(parents=True)
    verifier = DocumentHashVerifier(
        store, producer="system_of_record", acting_roots=[downloads]
    )
    assert verifier.verification_tier is VerificationTier.IMMEDIATE_SCREEN
    assert verifier.provenance.declared is ArtifactProducer.SYSTEM_OF_RECORD
    assert verifier.provenance.producer is ArtifactProducer.ACTING_SESSION
    # A store that contains the acting folder is capped too.
    outer = DocumentHashVerifier(
        tmp_path, producer="system_of_record", acting_roots=[downloads]
    )
    assert outer.verification_tier is VerificationTier.IMMEDIATE_SCREEN


def test_the_users_downloads_folder_is_always_an_acting_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("HOME", str(tmp_path))
    store = tmp_path / "Downloads"
    store.mkdir()
    grade = grade_artifact("system_of_record", root=store)
    assert grade.producer is ArtifactProducer.ACTING_SESSION


def test_a_remote_root_is_not_compared_with_local_folders(tmp_path: Path):
    class Transport:
        def listdir_attr(self, path):
            return []

        def open(self, path, mode="rb"):
            raise OSError

    verifier = FileArrivalVerifier(
        str(tmp_path),
        producer="system_of_record",
        acting_roots=[tmp_path],
        transport=Transport(),
    )
    assert verifier.verification_tier is VerificationTier.INDEPENDENT_SYSTEM


def test_evidence_names_the_producer_and_no_path(tmp_path: Path):
    store = tmp_path / "store"
    store.mkdir()
    verifier = DocumentHashVerifier(store, producer="separate_session")
    before = verifier.capture_pre_state()
    (store / "claim.pdf").write_bytes(PDF_BYTES)
    verdict = verifier.verify(_hash_effect(), before)
    assert verdict.verdict is Verdict.CONFIRMED
    block = verdict.read_evidence["provenance"]
    assert block == {
        "producer": "separate_session",
        "declared_producer": "separate_session",
        "check_level": "separate_session",
        "producer_source": "declared",
    }
    assert str(tmp_path) not in repr(block)
    assert before.detail["provenance"]["producer"] == "separate_session"
    # The evidence survives serialization into the run record.
    assert verdict.model_dump(mode="json")["read_evidence"]["provenance"] == block


def test_email_and_file_verdicts_carry_provenance(tmp_path: Path):
    mail = tmp_path / "mail"
    mail.mkdir()
    email = MaildirDeliveryVerifier(str(mail), producer="system_of_record")
    before = email.capture_pre_state()
    (mail / "1.eml").write_bytes(
        b"To: a@example.test\r\nFrom: b@example.test\r\nSubject: s\r\n\r\nbody\r\n"
    )
    verdict = email.verify(
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"to": "a@example.test"},
            expected_count=1,
            count_new_only=True,
            timeout_s=0.05,
        ),
        before,
    )
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.read_evidence["provenance"]["check_level"] == "system_of_record"

    drop = tmp_path / "drop"
    drop.mkdir()
    arrival = FileArrivalVerifier(drop, producer="acting_session")
    before = arrival.capture_pre_state()
    (drop / "batch.csv").write_text("row\n")
    verdict = arrival.verify(
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"arrived": "True"},
            expected_count=1,
            count_new_only=True,
            forbid_collateral_loss=False,
            timeout_s=0.05,
        ),
        before,
    )
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.read_evidence["provenance"]["producer"] == "acting_session"


def test_deployment_config_passes_the_producer(tmp_path: Path):
    for kind in ("file", "document-hash", "email", "document"):
        undeclared = build_effect_verifier(EffectsConfig(kind=kind, root=str(tmp_path)))
        assert undeclared.verification_tier is VerificationTier.IMMEDIATE_SCREEN
        declared = build_effect_verifier(
            EffectsConfig(
                kind=kind, root=str(tmp_path), artifact_producer="system_of_record"
            )
        )
        assert declared.verification_tier is VerificationTier.INDEPENDENT_SYSTEM
        spoofed = build_effect_verifier(
            EffectsConfig(
                kind=kind,
                root=str(tmp_path),
                artifact_producer="system_of_record",
                acting_roots=[str(tmp_path)],
            )
        )
        assert spoofed.verification_tier is VerificationTier.IMMEDIATE_SCREEN


def test_the_redacting_wrapper_keeps_the_provenance_tier(tmp_path: Path):
    for producer, tier in (
        (None, VerificationTier.IMMEDIATE_SCREEN),
        ("system_of_record", VerificationTier.INDEPENDENT_SYSTEM),
    ):
        verifier = build_effect_verifier(
            EffectsConfig(
                kind="document-hash",
                root=str(tmp_path),
                artifact_producer=producer,
                evidence_redact_fields=["name"],
            )
        )
        assert verifier_effect_tier(verifier, None) is tier
        assert getattr(verifier, "independent_system_of_record", None) is (
            tier is VerificationTier.INDEPENDENT_SYSTEM
        )


# -- a real PDF downloaded by a headless browser --------------------------------


class _ClaimServer:
    """A loopback app that keeps the claim PDF in its own store and serves it."""

    def __init__(self, store: Path) -> None:
        self.store = store
        store.mkdir(parents=True)
        (store / "claim.pdf").write_bytes(PDF_BYTES)
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == "/claim.pdf":
                    raw = (server.store / "claim.pdf").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/pdf")
                    self.send_header(
                        "Content-Disposition", 'attachment; filename="claim.pdf"'
                    )
                else:
                    raw = b'<a id="get" href="/claim.pdf">Download claim</a>'
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_ClaimServer":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_a_pdf_the_acting_browser_downloaded_is_not_verified(tmp_path: Path):
    sync_api = pytest.importorskip("playwright.sync_api")
    acting_dir = tmp_path / "acting"
    separate_dir = tmp_path / "separate"
    acting_dir.mkdir()
    separate_dir.mkdir()
    with _ClaimServer(tmp_path / "app-store") as app:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 - no browser installed
                pytest.skip(f"headless Chromium unavailable: {exc}")
            try:
                acting = browser.new_context(accept_downloads=True)
                page = acting.new_page()
                page.goto(app.url + "/")
                acting_verifier = DocumentHashVerifier(
                    acting_dir,
                    glob="*.pdf",
                    producer="system_of_record",  # a wrong declaration
                    acting_roots=[acting_dir],
                )
                acting_before = acting_verifier.capture_pre_state()
                with page.expect_download() as info:
                    page.click("#get")
                download = info.value
                temp_path = Path(download.path())
                download.save_as(acting_dir / "claim.pdf")
                acting.close()

                # A separate, newly opened context is a separate session.
                separate = browser.new_context()
                response = separate.request.get(app.url + "/claim.pdf")
                (separate_dir / "claim.pdf").write_bytes(response.body())
                separate.close()
            finally:
                browser.close()

        # The acting browser's copy: the hash matches, the level does not.
        verdict = acting_verifier.verify(_hash_effect(), acting_before)
        assert verdict.verdict is Verdict.CONFIRMED
        assert acting_verifier.provenance.check_level <= CheckLevel.REOPENED
        assert verdict.read_evidence["provenance"]["producer"] == "acting_session"
        assert verdict.read_evidence["provenance"]["declared_producer"] == (
            "system_of_record"
        )
        assert verdict.read_evidence["provenance"]["producer_source"] == "observed"
        readback = _run_readback(acting_verifier, verdict)
        assert readback["checked_how"] == "same_screen"
        assert readback["below_minimum"] is True  # not VERIFIED

        # Playwright's own download folder is recognized without being told.
        observed = grade_artifact("system_of_record", root=temp_path.parent)
        assert observed.producer is ArtifactProducer.ACTING_SESSION

        # The same bytes from a separate session reach separate_session.
        separate_verifier = DocumentHashVerifier(
            separate_dir, glob="*.pdf", producer="separate_session"
        )
        verdict = separate_verifier.verify(
            _hash_effect(), separate_verifier.capture_pre_state()
        )
        assert verdict.verdict is Verdict.CONFIRMED
        readback = _run_readback(separate_verifier, verdict)
        assert readback["checked_how"] == "separate_session"
        assert readback["below_minimum"] is False

        # Read from the app's own store, they reach system_of_record.
        store_verifier = DocumentHashVerifier(
            app.store, glob="*.pdf", producer="system_of_record"
        )
        verdict = store_verifier.verify(
            _hash_effect(), store_verifier.capture_pre_state()
        )
        assert verdict.verdict is Verdict.CONFIRMED
        readback = _run_readback(store_verifier, verdict)
        assert readback["checked_how"] == "system_of_record"


# -- the grade is bound to each artifact a check reads ---------------------------

_EML = b"To: a@example.test\r\nFrom: b@example.test\r\nSubject: s\r\n\r\nbody\r\n"

#: Each local reader, built from configuration over a recursive store, with
#: the artifact it reads and an effect that the artifact satisfies.
_READERS = {
    "document-hash": (
        {"glob": "**/*.pdf"},
        "claim.pdf",
        PDF_BYTES,
        _hash_effect(),
    ),
    "file": (
        {"file_pattern": "**/*.csv"},
        "batch.csv",
        b"row\n",
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"arrived": "True"},
            expected_count=1,
            count_new_only=True,
            forbid_collateral_loss=False,
            timeout_s=0.05,
        ),
    ),
    "document": (
        {"file_pattern": "**/*.json", "document_format": "json"},
        "report.json",
        b'{"id": "r1"}',
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"parseable": "True"},
            expected_count=1,
            count_new_only=True,
            forbid_collateral_loss=False,
            timeout_s=0.05,
        ),
    ),
    "email": (
        {"mail_pattern": "**/*.eml"},
        "1.eml",
        _EML,
        Effect(
            kind=EffectKind.RECORD_WRITTEN,
            match={"to": "a@example.test"},
            expected_count=1,
            count_new_only=True,
            forbid_collateral_loss=False,
            timeout_s=0.05,
        ),
    ),
}


def _reader(kind: str, store: Path, producer: str = "system_of_record"):
    options, _name, _raw, _effect = _READERS[kind]
    return build_effect_verifier(
        EffectsConfig(kind=kind, root=str(store), artifact_producer=producer, **options)
    )


@pytest.mark.parametrize("kind", sorted(_READERS))
def test_a_system_owned_artifact_in_a_recursive_store_is_verified(
    tmp_path: Path, kind: str
):
    _options, name, raw, effect = _READERS[kind]
    store = tmp_path / "store"
    (store / "reports").mkdir(parents=True)
    verifier = _reader(kind, store)
    before = verifier.capture_pre_state()
    (store / "reports" / name).write_bytes(raw)
    verdict = verifier.verify(effect, before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.read_evidence["provenance"]["check_level"] == "system_of_record"
    assert verdict.read_evidence["provenance"]["producer_source"] == "declared"
    assert verifier_effect_tier(verifier, None) is VerificationTier.INDEPENDENT_SYSTEM


@pytest.mark.parametrize("kind", sorted(_READERS))
def test_a_browser_download_beneath_a_strong_store_is_refused(
    tmp_path: Path, kind: str
):
    _options, name, raw, effect = _READERS[kind]
    store = tmp_path / "store"
    (store / "reports").mkdir(parents=True)
    verifier = _reader(kind, store)
    before = verifier.capture_pre_state()
    downloads = store / "playwright-artifacts-abc123"
    downloads.mkdir()
    (downloads / name).write_bytes(raw)
    verdict = verifier.verify(effect, before)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert not verdict.confirmed
    assert verdict.matched_records == []
    assert "browser download folder" in verdict.reason
    block = verdict.read_evidence["provenance"]
    assert block["producer"] == "acting_session"
    assert block["declared_producer"] == "system_of_record"
    assert block["check_level"] == "same_screen"
    assert block["producer_source"] == "observed"
    assert str(tmp_path) not in repr(block)


@pytest.mark.parametrize("kind", sorted(_READERS))
def test_a_symlink_out_of_a_strong_store_is_refused(tmp_path: Path, kind: str):
    _options, name, raw, effect = _READERS[kind]
    store = tmp_path / "store"
    (store / "reports").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / name).write_bytes(raw)
    verifier = _reader(kind, store)
    before = verifier.capture_pre_state()
    (store / "reports" / name).symlink_to(elsewhere / name)
    verdict = verifier.verify(effect, before)
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "outside the store" in verdict.reason


def test_a_weak_producer_reads_the_same_store_unchanged(tmp_path: Path):
    _options, name, raw, effect = _READERS["document-hash"]
    store = tmp_path / "store"
    (store / "playwright-artifacts-abc123").mkdir(parents=True)
    verifier = _reader("document-hash", store, producer="acting_session")
    before = verifier.capture_pre_state()
    (store / "playwright-artifacts-abc123" / name).write_bytes(raw)
    verdict = verifier.verify(effect, before)
    assert verdict.verdict is Verdict.CONFIRMED
    assert verdict.read_evidence["provenance"]["check_level"] == "same_screen"


def test_a_real_download_beneath_a_mixed_store_is_not_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The acting browser downloads into a folder beneath a broad store."""

    sync_api = pytest.importorskip("playwright.sync_api")
    store = tmp_path / "store"
    (store / "reports").mkdir(parents=True)
    # Playwright makes its download folder under the temporary directory, so
    # pointing that at the store puts the acting browser's copy inside it.
    monkeypatch.setenv("TMPDIR", str(store))
    monkeypatch.setenv("TMP", str(store))
    monkeypatch.setenv("TEMP", str(store))
    verifier = DocumentHashVerifier(store, glob="**/*.pdf", producer="system_of_record")
    before = verifier.capture_pre_state()
    with _ClaimServer(tmp_path / "app-store") as app:
        with sync_api.sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:  # noqa: BLE001 - no browser installed
                pytest.skip(f"headless Chromium unavailable: {exc}")
            try:
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                page.goto(app.url + "/")
                with page.expect_download() as info:
                    page.click("#get")
                downloaded = Path(info.value.path()).resolve()
                if not downloaded.is_relative_to(store.resolve()):
                    pytest.skip("this Playwright build ignores TMPDIR for downloads")
                # Read while the acting browser's copy is still in the store.
                verdict = verifier.verify(_hash_effect(), before)
                context.close()
            finally:
                browser.close()
    assert verdict.verdict is Verdict.INDETERMINATE
    assert "browser download folder" in verdict.reason
    assert verdict.read_evidence["provenance"]["producer"] == "acting_session"

    # The positive case: the system writes the same bytes into the store.
    for leftover in store.glob("playwright-artifacts-*"):
        for child in leftover.rglob("*"):
            if child.is_file():
                child.unlink()
    (store / "reports" / "claim.pdf").write_bytes(PDF_BYTES)
    fresh = DocumentHashVerifier(store, glob="**/*.pdf", producer="system_of_record")
    verdict = fresh.verify(_hash_effect(), before)
    assert verdict.verdict is Verdict.CONFIRMED, verdict.reason
    assert verdict.read_evidence["provenance"]["check_level"] == "system_of_record"
