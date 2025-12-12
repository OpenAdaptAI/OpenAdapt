"""Tests for configuration module."""

from openadapt_privacy.config import PrivacyConfig, config


class TestPrivacyConfig:
    """Tests for PrivacyConfig dataclass."""

    def test_default_values(self) -> None:
        """Test that default configuration values are set correctly."""
        cfg = PrivacyConfig()

        assert cfg.SCRUB_CHAR == "*"
        assert cfg.SCRUB_LANGUAGE == "en"
        assert cfg.SCRUB_FILL_COLOR == 0x0000FF
        assert isinstance(cfg.SCRUB_KEYS_HTML, list)
        assert "text" in cfg.SCRUB_KEYS_HTML
        assert "canonical_text" in cfg.SCRUB_KEYS_HTML
        assert "title" in cfg.SCRUB_KEYS_HTML
        assert cfg.ACTION_TEXT_NAME_PREFIX == "<"
        assert cfg.ACTION_TEXT_NAME_SUFFIX == ">"
        assert cfg.ACTION_TEXT_SEP == "-"
        assert cfg.SPACY_MODEL_NAME == "en_core_web_trf"

    def test_custom_values(self) -> None:
        """Test that custom configuration values can be set."""
        cfg = PrivacyConfig(
            SCRUB_CHAR="X",
            SCRUB_LANGUAGE="de",
            SCRUB_FILL_COLOR=0xFF0000,
            SCRUB_KEYS_HTML=["custom_key"],
        )

        assert cfg.SCRUB_CHAR == "X"
        assert cfg.SCRUB_LANGUAGE == "de"
        assert cfg.SCRUB_FILL_COLOR == 0xFF0000
        assert cfg.SCRUB_KEYS_HTML == ["custom_key"]

    def test_global_config_instance(self) -> None:
        """Test that global config instance exists and has defaults."""
        assert config is not None
        assert isinstance(config, PrivacyConfig)
        assert config.SCRUB_CHAR == "*"

    def test_scrub_config_trf(self) -> None:
        """Test that SCRUB_CONFIG_TRF has correct structure."""
        cfg = PrivacyConfig()

        assert "nlp_engine_name" in cfg.SCRUB_CONFIG_TRF
        assert cfg.SCRUB_CONFIG_TRF["nlp_engine_name"] == "spacy"
        assert "models" in cfg.SCRUB_CONFIG_TRF
        assert len(cfg.SCRUB_CONFIG_TRF["models"]) == 1
        assert cfg.SCRUB_CONFIG_TRF["models"][0]["lang_code"] == "en"
        assert cfg.SCRUB_CONFIG_TRF["models"][0]["model_name"] == "en_core_web_trf"

    def test_ignore_entities_default_empty(self) -> None:
        """Test that SCRUB_PRESIDIO_IGNORE_ENTITIES defaults to empty."""
        cfg = PrivacyConfig()
        assert list(cfg.SCRUB_PRESIDIO_IGNORE_ENTITIES) == []
