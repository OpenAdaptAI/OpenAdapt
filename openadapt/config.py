from typing import ClassVar
import json
import multiprocessing
import os
import pathlib
import shutil

from loguru import logger
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
import git
import sentry_sdk

ENV_FILE_PATH = pathlib.Path(__file__).parent.parent / ".env"
ENV_EXAMPLE_FILE_PATH = pathlib.Path(__file__).parent.parent / ".env.example"
ROOT_DIRPATH = pathlib.Path(__file__).parent.parent.resolve()
DATA_DIRECTORY_PATH = ROOT_DIRPATH / "data"
RECORDING_DIRECTORY_PATH = DATA_DIRECTORY_PATH / "recordings"

STOP_STRS = [
    "oa.stop",
    # TODO:
    # "<ctrl>+c,<ctrl>+c,<ctrl>+c"
]
# each list in SPECIAL_CHAR_STOP_SEQUENCES should contain sequences
# containing special chars, separated by keys
SPECIAL_CHAR_STOP_SEQUENCES = [["ctrl", "ctrl", "ctrl"]]

if not os.path.isfile(ENV_FILE_PATH):
    shutil.copy(ENV_EXAMPLE_FILE_PATH, ENV_FILE_PATH)


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE_PATH, extra="ignore")

    # Privacy
    AWS_API_KEY: str = ""
    PRIVATE_AI_API_KEY: str = ""

    # Segmentation
    AWS_SEGMENT_API_KEY: str = ""
    REPLICATE_API_KEY: str = ""

    # Completions
    OPENAI_API_KEY: str = "<set your api key in .env>"
    ANTHROPIC_API_KEY: str = ""
    GOOGLE_API_KEY: str = ""

    # OpenAdapt
    OPENADAPT_API_KEY: str = ""

    # Cache
    CACHE_DIR_PATH: str = ".cache"
    CACHE_ENABLED: bool = True
    CACHE_VERBOSITY: int = 0

    # Database
    DB_ECHO: bool = False
    DB_FNAME: str = "openadapt.db"

    DB_FPATH: ClassVar[str]
    if DB_FNAME == "openadapt.db":  # noqa
        DB_FPATH = ROOT_DIRPATH / DB_FNAME  # noqa
    else:
        DB_FPATH = RECORDING_DIRECTORY_PATH / DB_FNAME  # noqa
    DB_URL: str = f"sqlite:///{DB_FPATH}"

    # Error reporting
    ERROR_REPORTING_ENABLED: bool = True
    ERROR_REPORTING_DSN: str = (
        "https://dcf5d7889a3b4b47ae12a3af9ffcbeb7@app.glitchtip.com/3798"
    )
    ERROR_REPORTING_BRANCH: str = "main"

    # OpenAI
    OPENAI_MODEL_NAME: str = "gpt-3.5-turbo"

    # Record and replay
    RECORD_WINDOW_DATA: bool = False
    RECORD_READ_ACTIVE_ELEMENT_STATE: bool = False
    RECORD_VIDEO: bool = False
    RECORD_IMAGES: bool = True
    VIDEO_PIXEL_FORMAT: str = "rgb24"
    # sequences that when typed, will stop the recording of ActionEvents in record.py
    STOP_SEQUENCES: list[list[str]] = [
        list(stop_str) for stop_str in STOP_STRS
    ] + SPECIAL_CHAR_STOP_SEQUENCES

    # Warning suppression
    IGNORE_WARNINGS: bool = False
    MAX_NUM_WARNINGS_PER_SECOND: int = 5
    WARNING_SUPPRESSION_PERIOD: int = 1
    MESSAGES_TO_FILTER: list[str] = ["Cannot pickle Objective-C objects"]

    # Action event configurations
    ACTION_TEXT_SEP: str = "-"
    ACTION_TEXT_NAME_PREFIX: str = "<"
    ACTION_TEXT_NAME_SUFFIX: str = ">"

    # Performance plotting
    PLOT_PERFORMANCE: bool = True
    DIRNAME_PERFORMANCE_PLOTS: str = "performance"

    # Capture configurations
    CAPTURE_DIR_PATH: str = "captures"

    # App configurations
    APP_DARK_MODE: bool = False

    # Scrubbing configurations
    SCRUB_ENABLED: bool = False
    SCRUB_CHAR: str = "*"
    SCRUB_LANGUAGE: str = "en"
    SCRUB_FILL_COLOR: str = "rgba(0, 0, 0, 0.1)"
    SCRUB_CONFIG_TRF: dict = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_trf"}],
    }
    SCRUB_PRESIDIO_IGNORE_ENTITIES: list[str] = []
    SCRUB_KEYS_HTML: list[str] = [
        "text",
        "canonical_text",
        "title",
        "state",
        "task_description",
        "key_char",
        "canonical_key_char",
        "key_vk",
        "children",
    ]

    # Visualization configurations
    VISUALIZE_DARK_MODE: bool = False
    VISUALIZE_RUN_NATIVELY: bool = True
    VISUALIZE_DENSE_TREES: bool = True
    VISUALIZE_ANIMATIONS: bool = True
    VISUALIZE_EXPAND_ALL: bool = False
    VISUALIZE_MAX_TABLE_CHILDREN: int = 10

    # Screenshot configurations
    SAVE_SCREENSHOT_DIFF: bool = False

    # Spacy configurations
    SPACY_MODEL_NAME: str = "en_core_web_trf"

    # Dashboard configurations
    DASHBOARD_CLIENT_PORT: int = 3000
    DASHBOARD_SERVER_PORT: int = 8000

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

    def __setattr__(self, key, value):
        super().__setattr__(key, value)
        persist_config(self)

    classifications: ClassVar[dict[str, list[str]]] = {
        "api_keys": [
            "AWS_API_KEY",
            "PRIVATE_AI_API_KEY",
            "AWS_SEGMENT_API_KEY",
            "REPLICATE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "OPENADAPT_API_KEY",
        ],
        "scrubbing": [
            "SCRUB_ENABLED",
            "SCRUB_CHAR",
            "SCRUB_LANGUAGE",
            "SCRUB_FILL_COLOR",
        ],
        "record_and_replay": [
            "RECORD_WINDOW_DATA",
            "RECORD_READ_ACTIVE_ELEMENT_STATE",
            "RECORD_VIDEO",
            "RECORD_IMAGES",
            "VIDEO_PIXEL_FORMAT",
        ],
    }


class LazyConfig:
    def __init__(self):
        self._config = Config()

    def __getattr__(self, key):
        if key == "_config":
            return self._config
        return self._config.__getattribute__(key)

    def __setattr__(self, key, value):
        if key == "_config":
            self.__dict__[key] = value
        else:
            self._config.__setattr__(key, value)

    def model_dump(self):
        return self._config.model_dump()


config = LazyConfig()


def persist_config(new_config: Config) -> None:
    """Persist the configuration."""

    config_variables = new_config.model_dump()

    logger.info(f"Persisting config to {new_config.model_config['env_file']}")

    # clear the file
    env_lines = []
    with open(ENV_FILE_PATH, "r") as f:
        env_lines = f.readlines()

    for key, val in config_variables.items():
        found = False
        for i, line in enumerate(env_lines):
            if line.startswith(f"{key}="):
                val = val if type(val) is str else json.dumps(val)
                env_lines[i] = f"{key}='{val}'\n"
                found = True
                break
        if not found:
            val = val if type(val) is str else json.dumps(val)
            env_lines.append(f"{key}='{val}'\n")

    with open(ENV_FILE_PATH, "w") as f:
        f.writelines(env_lines)

    global config
    config._config = new_config


def obfuscate(val: str, pct_reveal: float = 0.1, char: str = "*") -> str:
    """Obfuscates a value by replacing a portion of characters.

    Args:
        val (str): The value to obfuscate.
        pct_reveal (float, optional): Percentage of characters to reveal (default: 0.1).
        char (str, optional): Obfuscation character (default: "*").

    Returns:
        str: Obfuscated value with a portion of characters replaced.

    Raises:
        AssertionError: If length of obfuscated value does not match original value.
    """
    num_reveal = int(len(val) * pct_reveal)
    num_obfuscate = len(val) - num_reveal
    obfuscated = char * num_obfuscate
    revealed = val[num_obfuscate:]
    rval = f"{obfuscated}{revealed}"
    assert len(rval) == len(val), (val, rval)
    return rval


_OBFUSCATE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN")
if multiprocessing.current_process().name == "MainProcess":
    for key, val in config.model_dump().items():
        if not key.startswith("_") and key.isupper():
            parts = key.split("_")
            if any([part in parts for part in _OBFUSCATE_KEY_PARTS]):
                val = obfuscate(val)
            logger.info(f"{key}={val}")

    if config.ERROR_REPORTING_ENABLED:  # type: ignore # noqa
        active_branch_name = git.Repo(ROOT_DIRPATH).active_branch.name
        logger.info(f"{active_branch_name=}")
        is_reporting_branch = active_branch_name == config.ERROR_REPORTING_BRANCH  # type: ignore # noqa
        logger.info(f"{is_reporting_branch=}")
        if is_reporting_branch:
            sentry_sdk.init(
                dsn=config.ERROR_REPORTING_DSN,  # type: ignore # noqa
                traces_sample_rate=1.0,
            )
