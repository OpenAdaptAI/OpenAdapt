"""Tests for the [hosted] config.toml layer and env precedence."""

from __future__ import annotations

from pathlib import Path

from engine.config import EngineConfig


def _write_toml(path: Path, body: str) -> None:
    path.write_text(body)


class TestHostedConfig:
    def test_defaults(self, monkeypatch) -> None:
        monkeypatch.delenv("OPENADAPT_CONFIG_TOML", raising=False)
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", "/nonexistent/config.toml")
        cfg = EngineConfig()
        assert cfg.hosted_host == "https://app.openadapt.ai"
        assert cfg.deployment_lane == "cloud"
        assert cfg.phi_mode == "off"
        assert cfg.poll_interval_s == 60

    def test_toml_hosted_table(self, tmp_path: Path, monkeypatch) -> None:
        toml = tmp_path / "config.toml"
        _write_toml(
            toml,
            '[hosted]\n'
            'host = "https://byoc.example"\n'
            'deployment_lane = "byoc"\n'
            'phi_mode = "on"\n'
            'poll_interval_s = 120\n',
        )
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(toml))
        cfg = EngineConfig()
        assert cfg.hosted_host == "https://byoc.example"
        assert cfg.deployment_lane == "byoc"
        assert cfg.phi_mode == "on"
        assert cfg.poll_interval_s == 120

    def test_env_overrides_toml(self, tmp_path: Path, monkeypatch) -> None:
        toml = tmp_path / "config.toml"
        _write_toml(toml, '[hosted]\ndeployment_lane = "byoc"\n')
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(toml))
        monkeypatch.setenv("OPENADAPT_DEPLOYMENT_LANE", "cloud")
        cfg = EngineConfig()
        # Env wins over the file.
        assert cfg.deployment_lane == "cloud"

    def test_secrets_not_read_from_toml(self, tmp_path: Path, monkeypatch) -> None:
        # A token in the toml must NOT populate any credential field; there is
        # no token field on EngineConfig -- secrets live in the keychain only.
        toml = tmp_path / "config.toml"
        _write_toml(toml, '[hosted]\ntoken = "oai_ingest_secret"\n')
        monkeypatch.setenv("OPENADAPT_CONFIG_TOML", str(toml))
        cfg = EngineConfig()
        assert not hasattr(cfg, "token")
        assert "oai_ingest_secret" not in cfg.model_dump_json()
