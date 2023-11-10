import os
import pytest
from openadapt import config

def test_getenv_fallback():
    os.environ["EXISTING_VAR"] = "existing_value"
    assert config.getenv_fallback("EXISTING_VAR") == "existing_value"
    assert config.getenv_fallback("NON_EXISTING_VAR", "default_value") == "default_value"
    with pytest.raises(ValueError):
        config.getenv_fallback("NON_EXISTING_VAR")

def test_persist_env(tmp_path):
    env_file = tmp_path / ".env"
    config.persist_env("TEST_VAR", "test_value", env_file)
    with open(env_file, "r") as f:
        lines = f.readlines()
    assert "TEST_VAR=test_value\n" in lines

def test_obfuscate():
    assert config.obfuscate("test_value", 0.5, "*") == "*****alue"
    assert config.obfuscate("another_test_value", 0.3, "#") == "#############t_value"
    assert config.obfuscate("yet_another_test_value", 0.1, "@") == "@@@@@@@@@@@@@@@@@@@@e"
