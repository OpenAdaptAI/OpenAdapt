"""Declarative effect-verifier kit config (deployment.yaml `effects:`).

Covers the kit's construction path: new kinds (sql / file), secret-isolated
auth references (env-var names, fail-loud when absent), explicit run-parameter
binding (path_params / search_param_exprs / sql_query_params resolved at
build), and byte-for-byte back-compat for pre-kit configs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from openadapt_flow.deployment import (
    DeploymentConfig,
    EffectsConfig,
    build_effect_verifier,
    load_deployment,
)
from openadapt_flow.runtime.effects import (
    AuthRef,
    DocumentHashVerifier,
    FhirEffectVerifier,
    FileArrivalVerifier,
    RestRecordVerifier,
    SqlRecordVerifier,
)


class TestAuthRef:
    def test_bearer_env_resolves(self, monkeypatch):
        monkeypatch.setenv("KIT_TOKEN", "s3cret")
        ref = AuthRef(bearer_env="KIT_TOKEN")
        assert ref.resolve_headers() == {"Authorization": "Bearer s3cret"}

    def test_header_value_env_resolves(self, monkeypatch):
        monkeypatch.setenv("KIT_KEY", "token k:s")
        ref = AuthRef(header="Authorization", value_env="KIT_KEY")
        assert ref.resolve_headers() == {"Authorization": "token k:s"}

    def test_basic_env_resolves(self, monkeypatch):
        monkeypatch.setenv("KIT_BASIC", "user:pass")
        headers = AuthRef(basic_env="KIT_BASIC").resolve_headers()
        assert headers["Authorization"].startswith("Basic ")

    def test_missing_env_fails_loud(self, monkeypatch):
        monkeypatch.delenv("KIT_ABSENT", raising=False)
        with pytest.raises(ValueError, match="KIT_ABSENT"):
            AuthRef(bearer_env="KIT_ABSENT").resolve_headers()

    def test_exactly_one_style_enforced(self):
        with pytest.raises(ValueError):
            AuthRef(bearer_env="A", basic_env="B")
        with pytest.raises(ValueError):
            AuthRef()
        with pytest.raises(ValueError):
            AuthRef(header="X-Key")  # header without value_env


class TestRestKitConfig:
    def test_path_params_bind_run_params_url_quoted(self, monkeypatch):
        monkeypatch.setenv("KIT_TOKEN", "tok")
        cfg = EffectsConfig(
            kind="rest",
            base_url="http://sor.local",
            records_path="/api/resource/Loan%20Application?applicant={applicant}",
            records_key="data",
            path_params={"applicant": {"param": "applicant"}},
            auth={"bearer_env": "KIT_TOKEN"},
        )
        v = build_effect_verifier(cfg, params={"applicant": "OpenAdapt Synthetic"})
        assert isinstance(v, RestRecordVerifier)
        # value URL-quoted into the template
        assert "applicant=OpenAdapt%20Synthetic" in v.records_path
        assert v.headers == {"Authorization": "Bearer tok"}

    def test_auth_headers_sent_on_reads_only_when_configured(self):
        class _Session:
            def __init__(self):
                self.calls = []

            def get(self, url, *, timeout, headers=None):
                self.calls.append(headers)

                class _R:
                    status_code = 200

                    @staticmethod
                    def json():
                        return {"records": []}

                return _R()

        with_auth = _Session()
        RestRecordVerifier(
            "http://x", session=with_auth, headers={"Authorization": "Bearer t"}
        ).capture_pre_state()
        assert with_auth.calls == [{"Authorization": "Bearer t"}]

        # No headers configured -> the narrow pre-kit session signature works.
        class _NarrowSession(_Session):
            def get(self, url, *, timeout):  # no headers kwarg at all
                self.calls.append("narrow")
                return _Session.get(self, url, timeout=timeout)

        narrow = _NarrowSession()
        state = RestRecordVerifier("http://x", session=narrow).capture_pre_state()
        assert state.reachable
        assert "narrow" in narrow.calls

    def test_unresolved_param_ref_fails_loud(self):
        cfg = EffectsConfig(
            kind="rest",
            base_url="http://sor.local",
            records_path="/x?a={applicant}",
            path_params={"applicant": {"param": "applicant"}},
        )
        with pytest.raises(ValueError, match="applicant"):
            build_effect_verifier(cfg, params={})
        with pytest.raises(ValueError, match="applicant"):
            build_effect_verifier(cfg)

    def test_bare_string_path_param_is_literal(self):
        cfg = EffectsConfig(
            kind="rest",
            base_url="http://sor.local",
            records_path="/x?a={fixed}",
            path_params={"fixed": "42"},
        )
        v = build_effect_verifier(cfg)  # no run params needed for a literal
        assert v.records_path == "/x?a=42"

    def test_no_path_params_no_formatting(self):
        # A literal '{' in a pre-kit path must survive verbatim.
        cfg = EffectsConfig(
            kind="rest", base_url="http://x", records_path="/db?f={raw}"
        )
        v = build_effect_verifier(cfg)
        assert v.records_path == "/db?f={raw}"
        assert v.headers is None

    def test_template_mismatch_fails_loud(self):
        cfg = EffectsConfig(
            kind="rest",
            base_url="http://x",
            records_path="/db?f={other}",
            path_params={"fixed": "1"},
        )
        with pytest.raises(ValueError, match="records_path"):
            build_effect_verifier(cfg)


class TestFhirKitConfig:
    def test_search_param_exprs_and_token_env(self, monkeypatch):
        monkeypatch.setenv("KIT_FHIR_TOKEN", "oauth-token")
        cfg = EffectsConfig(
            kind="fhir",
            base_url="https://emr.local/fhir",
            search_params={"category": "vital-signs"},
            search_param_exprs={"patient": {"param": "patient_ref"}},
            access_token_env="KIT_FHIR_TOKEN",
        )
        v = build_effect_verifier(cfg, params={"patient_ref": "Patient/9"})
        assert isinstance(v, FhirEffectVerifier)
        assert v.search_params == {
            "category": "vital-signs",
            "patient": "Patient/9",
        }
        assert v.access_token == "oauth-token"

    def test_token_env_missing_fails_loud(self, monkeypatch):
        monkeypatch.delenv("KIT_FHIR_ABSENT", raising=False)
        cfg = EffectsConfig(
            kind="fhir",
            base_url="https://emr.local/fhir",
            access_token_env="KIT_FHIR_ABSENT",
        )
        with pytest.raises(ValueError, match="KIT_FHIR_ABSENT"):
            build_effect_verifier(cfg)

    def test_token_env_wins_over_literal(self, monkeypatch):
        monkeypatch.setenv("KIT_FHIR_TOKEN", "from-env")
        cfg = EffectsConfig(
            kind="fhir",
            base_url="https://emr.local/fhir",
            access_token="literal-should-lose",
            access_token_env="KIT_FHIR_TOKEN",
        )
        v = build_effect_verifier(cfg)
        assert v.access_token == "from-env"


class TestSqlKitConfig:
    def test_sqlite_build_with_bound_params(self, tmp_path: Path):
        db = tmp_path / "sor.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, who TEXT)")
        conn.execute("INSERT INTO t (who) VALUES ('alice')")
        conn.commit()
        conn.close()
        cfg = EffectsConfig(
            kind="sql",
            sql_query="SELECT id, who FROM t WHERE who = :who",
            sql_query_params={"who": {"param": "who"}},
            sqlite_database=str(db),
        )
        v = build_effect_verifier(cfg, params={"who": "alice"})
        assert isinstance(v, SqlRecordVerifier)
        state = v.capture_pre_state()
        assert state.reachable
        assert state.records == [{"id": 1, "who": "alice"}]

    def test_mutating_query_refuses_at_build(self, tmp_path: Path):
        cfg = EffectsConfig(
            kind="sql",
            sql_query="DELETE FROM t",
            sqlite_database=str(tmp_path / "x.db"),
        )
        with pytest.raises(ValueError, match="read-only"):
            build_effect_verifier(cfg)

    def test_sql_requires_a_driver_choice(self):
        cfg = EffectsConfig(kind="sql", sql_query="SELECT 1")
        with pytest.raises(ValueError, match="sqlite_database"):
            build_effect_verifier(cfg)

    def test_sql_password_env_missing_fails_loud(self, monkeypatch):
        monkeypatch.delenv("KIT_DB_PW", raising=False)
        cfg = EffectsConfig(
            kind="sql",
            sql_query="SELECT 1",
            sql_driver="sqlite3",  # any importable DB-API module
            sql_password_env="KIT_DB_PW",
        )
        with pytest.raises(ValueError, match="KIT_DB_PW"):
            build_effect_verifier(cfg)


class TestFileKitConfig:
    def test_file_kind_builds(self, tmp_path: Path):
        cfg = EffectsConfig(
            kind="file",
            root=str(tmp_path),
            file_pattern="*.csv",
            file_min_size=1,
            file_mtime_window_s=60,
            file_content_probe="HEADER",
        )
        v = build_effect_verifier(cfg)
        assert isinstance(v, FileArrivalVerifier)
        assert v.pattern == "*.csv"
        assert v.mtime_window_s == 60
        assert v.transport is None

    def test_file_requires_root(self):
        with pytest.raises(ValueError, match="root"):
            build_effect_verifier(EffectsConfig(kind="file"))


class TestBackCompat:
    def test_pre_kit_yaml_loads_and_builds_identically(self, tmp_path: Path):
        (tmp_path / "d.yaml").write_text(
            "effects:\n"
            "  kind: rest\n"
            "  base_url: http://localhost:8080\n"
            "  records_path: /api/db\n"
            "  records_key: records\n"
        )
        cfg = load_deployment(tmp_path / "d.yaml")
        v = build_effect_verifier(cfg.effects)
        assert isinstance(v, RestRecordVerifier)
        assert v.records_path == "/api/db"
        assert v.headers is None

    def test_document_hash_unchanged(self, tmp_path: Path):
        cfg = EffectsConfig(kind="document-hash", root=str(tmp_path))
        assert isinstance(build_effect_verifier(cfg), DocumentHashVerifier)

    def test_unknown_kind_lists_all_kinds(self):
        with pytest.raises(ValueError, match="sql | file"):
            build_effect_verifier(EffectsConfig(kind="bogus"))

    def test_defaults_are_none_kind(self):
        assert build_effect_verifier(DeploymentConfig().effects) is None


class TestOnScreenKitConfig:
    def test_onscreen_builds_unbound_readback_verifier(self):
        from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier

        cfg = EffectsConfig(kind="onscreen", readback_min_ratio=0.95)
        v = build_effect_verifier(cfg)
        assert isinstance(v, OnScreenReadbackVerifier)
        # Backend is bound later (build_replayer); it exposes bind_backend.
        assert hasattr(v, "bind_backend")

    def test_onscreen_explicit_region_honored(self, tmp_path: Path):
        from openadapt_flow.runtime.effects.onscreen import OnScreenReadbackVerifier

        (tmp_path / "d.yaml").write_text(
            "effects:\n  kind: onscreen\n  readback_region: [10, 20, 100, 40]\n"
        )
        cfg = load_deployment(tmp_path / "d.yaml")
        v = build_effect_verifier(cfg.effects)
        assert isinstance(v, OnScreenReadbackVerifier)

    def test_onscreen_listed_in_unknown_kind_message(self):
        with pytest.raises(ValueError, match="onscreen"):
            build_effect_verifier(EffectsConfig(kind="bogus"))
