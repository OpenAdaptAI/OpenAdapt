import pathlib

from loguru import logger
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PATH = pathlib.Path(__file__).parent.parent.resolve() / ".env"


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_PATH, extra="ignore")

    # Privacy
    AWS_API_KEY: str = ""
    PRIVATE_AI_API_KEY: str = ""

    # Segmentation
    AWS_SEGMENT_API_KEY: str = ""
    REPLICATE_API_KEY: str = ""

    # Completions
    OPENAI_API_KEY: str = ""
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""

    # OpenAdapt
    OPENADAPT_API_KEY: str = ""

    @model_validator(mode="after")
    @classmethod
    def validate_requirements(cls, data):
        if data.OPENADAPT_API_KEY != "":
            return data
        if data.AWS_SEGMENT_API_KEY == "" and data.REPLICATE_API_KEY == "":
            raise ValueError(
                {
                    "AWS_SEGMENT_API_KEY": (
                        "At least one of AWS_SEGMENT_API_KEY or REPLICATE_API_KEY must"
                        " be defined"
                    ),
                    "REPLICATE_API_KEY": (
                        "At least one of AWS_SEGMENT_API_KEY or REPLICATE_API_KEY must"
                        " be defined"
                    ),
                }
            )
        if (
            data.OPENAI_API_KEY == ""
            and data.ANTHROPIC_API_KEY == ""
            and data.GOOGLE_API_KEY == ""
        ):
            raise ValueError(
                {
                    "OPENAI_API_KEY": (
                        "At least one of OPENAI_API_KEY, ANTHROPIC_API_KEY, or"
                        " GOOGLE_API_KEY must be defined"
                    ),
                    "ANTHROPIC_API_KEY": (
                        "At least one of OPENAI_API_KEY, ANTHROPIC_API_KEY, or"
                        " GOOGLE_API_KEY must be defined"
                    ),
                    "GOOGLE_API_KEY": (
                        "At least one of OPENAI_API_KEY, ANTHROPIC_API_KEY, or"
                        " GOOGLE_API_KEY must be defined"
                    ),
                }
            )
        return data


config = Config()


def get_config() -> Config:
    global config
    return config


def persist_config(new_config: Config) -> None:
    """Persist the configuration."""

    config_variables = new_config.model_dump()

    logger.info(f"Persisting config to {new_config.model_config['env_file']}")

    # clear the file
    env_lines = []
    with open(ENV_PATH, "r") as f:
        env_lines = f.readlines()

    for key, val in config_variables.items():
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}="):
                env_lines[i] = f'{key}="{val}"\n'
                found = True
                break
        if not found:
            env_lines.append(f'{key}="{val}"\n')

    with open(ENV_PATH, "w") as f:
        f.writelines(env_lines)

    global config
    config = new_config.model_copy(deep=True)
