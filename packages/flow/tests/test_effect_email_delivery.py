"""Maildir / SMTP-capture email-delivery adapter: qualification fixtures.

Adversarial coverage: message to the WRONG recipient never matches (wrong
entity); a duplicate send REFUTEs (two new messages); a missing message is an
affirmative absence; a vanished/unreadable store is UNAVAILABLE, never a pass;
stale (out-of-window) delivery does not satisfy a fresh contract; a collateral
hook catches mail leaking to unexpected recipients.
"""

from __future__ import annotations

import os
import time

from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
from openadapt_flow.runtime.effects import (
    AdapterResult,
    Effect,
    EffectKind,
    MaildirDeliveryVerifier,
    Verdict,
    classify_adapter_result,
)

MESSAGE = """\
Message-ID: <{mid}@example.test>
From: system@example.test
To: {to}
Subject: {subject}

Claim {claim} has been submitted.
"""


def write_eml(
    root,
    name,
    *,
    to="alice@example.test",
    subject="Claim receipt",
    claim="c-77",
    mid=None,
):
    path = root / name
    path.write_text(
        MESSAGE.format(mid=mid or name, to=to, subject=subject, claim=claim)
    )
    return path


def delivery_effect(**kwargs):
    kwargs.setdefault("match", {"to": "alice@example.test", "content_match": "True"})
    kwargs.setdefault("count_new_only", True)
    kwargs.setdefault("timeout_s", 0.0)
    return Effect(kind=EffectKind.RECORD_WRITTEN, **kwargs)


def make_verifier(root, **kwargs):
    kwargs.setdefault("content_probe", r"Claim c-77 has been submitted")
    kwargs.setdefault("poll_interval_s", 0.0)
    return MaildirDeliveryVerifier(str(root), **kwargs)


class TestDelivery:
    def test_exactly_one_delivery_confirms(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_eml(tmp_path, "m1.eml")
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.CONFIRMED
        assert verdict.matched_records[0]["to"] == "alice@example.test"

    def test_wrong_recipient_never_matches(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_eml(tmp_path, "m1.eml", to="mallory@example.test")
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 0

    def test_duplicate_send_refutes(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_eml(tmp_path, "m1.eml", mid="a")
        write_eml(tmp_path, "m2.eml", mid="b")
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_count == 2
        assert classify_adapter_result(verdict) is AdapterResult.CONFLICTING

    def test_no_delivery_is_affirmative_absence(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert verdict.observed_effect == "absent"

    def test_missing_store_is_unavailable_not_pass(self, tmp_path):
        verifier = make_verifier(tmp_path / "nope")
        before = verifier.capture_pre_state()
        assert before.reachable is False
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.INDETERMINATE
        assert classify_adapter_result(verdict) is AdapterResult.UNAVAILABLE
        assert verifier.test_connection().ok is False

    def test_wrong_body_content_does_not_match(self, tmp_path):
        verifier = make_verifier(tmp_path)
        before = verifier.capture_pre_state()
        write_eml(tmp_path, "m1.eml", claim="c-99")  # wrong claim in the body
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.REFUTED

    def test_stale_delivery_outside_window_does_not_satisfy_fresh(self, tmp_path):
        path = write_eml(tmp_path, "m1.eml")
        old = 10_000.0
        os.utime(path, (old, old))
        verifier = make_verifier(tmp_path, mtime_window_s=60.0)
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        effect = delivery_effect(
            match={
                "to": "alice@example.test",
                "content_match": "True",
                "fresh": "True",
            }
        )
        verdict = verifier.verify(effect, before)
        assert verdict.should_halt is True

    def test_far_future_delivery_does_not_satisfy_fresh(self, tmp_path):
        path = write_eml(tmp_path, "m1.eml")
        future = time.time() + 3600
        os.utime(path, (future, future))
        verifier = make_verifier(tmp_path, mtime_window_s=60.0)
        before = verifier.capture_pre_state().model_copy(update={"records": []})
        effect = delivery_effect(
            match={
                "to": "alice@example.test",
                "content_match": "True",
                "fresh": "True",
            }
        )
        assert verifier.verify(effect, before).should_halt is True

    def test_maildir_layout_cur_and_new_are_scanned(self, tmp_path):
        (tmp_path / "cur").mkdir()
        (tmp_path / "new").mkdir()
        write_eml(tmp_path / "cur", "1.msg")
        write_eml(tmp_path / "new", "2.msg", to="bob@example.test", claim="c-99")
        verifier = make_verifier(tmp_path)
        records = verifier._scan()
        assert {r["to"] for r in records} == {
            "alice@example.test",
            "bob@example.test",
        }

    def test_collateral_hook_catches_leak_to_unexpected_recipient(self, tmp_path):
        def no_other_recipients(before, after):
            before_ids = {r["id"] for r in before.records}
            leaked = [
                r["to"]
                for r in after.records
                if r["id"] not in before_ids and r["to"] != "alice@example.test"
            ]
            if leaked:
                return f"new message(s) to unexpected recipient(s): {leaked}"
            return None

        verifier = make_verifier(tmp_path, collateral_hooks=(no_other_recipients,))
        before = verifier.capture_pre_state()
        write_eml(tmp_path, "m1.eml")
        write_eml(tmp_path, "m2.eml", to="mallory@example.test", claim="c-99")
        verdict = verifier.verify(delivery_effect(), before)
        assert verdict.verdict is Verdict.REFUTED
        assert "unexpected recipient" in verdict.reason

    def test_multipart_body_is_probed(self, tmp_path):
        (tmp_path / "m1.eml").write_text(
            "Message-ID: <m1@example.test>\n"
            "From: system@example.test\n"
            "To: alice@example.test\n"
            "Subject: Claim receipt\n"
            'Content-Type: multipart/alternative; boundary="b"\n'
            "\n"
            "--b\n"
            "Content-Type: text/plain\n"
            "\n"
            "Claim c-77 has been submitted.\n"
            "--b\n"
            "Content-Type: text/html\n"
            "\n"
            "<p>Claim c-77 has been submitted.</p>\n"
            "--b--\n"
        )
        verifier = make_verifier(tmp_path)
        [record] = verifier._scan()
        assert record["content_match"] == "True"

    def test_config_builds_email_verifier(self, tmp_path):
        cfg = EffectsConfig(
            kind="email",
            root=str(tmp_path),
            file_content_probe="submitted",
            file_mtime_window_s=300.0,
        )
        verifier = build_effect_verifier(cfg)
        assert isinstance(verifier, MaildirDeliveryVerifier)
        assert verifier.mtime_window_s == 300.0
