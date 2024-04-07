from typing import Any, ClassVar
import json
import multiprocessing
import pathlib
import shutil

from loguru import logger
from pydantic import model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
import git
import sentry_sdk

CURRENT_DIRPATH = pathlib.Path(__file__).parent
CONFIG_FILE_PATH = CURRENT_DIRPATH / "config.json"
LOCAL_CONFIG_FILE_PATH = CURRENT_DIRPATH / "config.local.json"
ROOT_DIRPATH = CURRENT_DIRPATH.parent
DATA_DIRECTORY_PATH = CURRENT_DIRPATH / "data"
RECORDING_DIRECTORY_PATH = CURRENT_DIRPATH / "recordings"

STOP_STRS = [
    "oa.stop",
    # TODO:
    # "<ctrl>+c,<ctrl>+c,<ctrl>+c"
]
# each list in SPECIAL_CHAR_STOP_SEQUENCES should contain sequences
# containing special chars, separated by keys
SPECIAL_CHAR_STOP_SEQUENCES = [["ctrl", "ctrl", "ctrl"]]


if not LOCAL_CONFIG_FILE_PATH.exists():
    shutil.copy(CONFIG_FILE_PATH, LOCAL_CONFIG_FILE_PATH)


def get_json_config_settings_source(
    file_path: pathlib.Path,
) -> PydanticBaseSettingsSource:
    class JsonConfigSettingsSource(PydanticBaseSettingsSource):
        """A settings source that reads from a JSON file."""

        def get_field_value(
            self, field: FieldInfo, field_name: str
        ) -> tuple[Any, str, bool]:
            """Get the field value from the JSON file."""
            encoding = self.config.get("env_file_encoding")
            file_content_json = json.loads(
                pathlib.Path(file_path).read_text(encoding=encoding)
            )
            field_value = file_content_json.get(field_name)
            return field_value, field_name, False

        def prepare_field_value(
            self, field_name: str, field: FieldInfo, value: Any, value_is_complex: bool
        ) -> Any:
            """Prepare the field value."""
            return value

        def __call__(self) -> dict[str, Any]:
            """Return the settings as a dictionary."""
            d: dict[str, Any] = {}

            for field_name, field in self.settings_cls.model_fields.items():
                field_value, field_key, value_is_complex = self.get_field_value(
                    field, field_name
                )
                field_value = self.prepare_field_value(
                    field_name, field, field_value, value_is_complex
                )
                if field_value is not None:
                    d[field_key] = field_value

            return d

    return JsonConfigSettingsSource


class Config(BaseSettings):
    # Environment
    ENV: str = "build"

    # Privacy
    AWS_API_KEY: str = ""
    PRIVATE_AI_API_KEY: str = ""

    # Segmentation
    AWS_SEGMENT_API_KEY: str = ""
    REPLICATE_API_KEY: str = ""

    # Completions
    OPENAI_API_KEY: str = "<set your api key in config.json>"
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
        DB_FPATH = CURRENT_DIRPATH / DB_FNAME  # noqa
    else:
        DB_FPATH = RECORDING_DIRECTORY_PATH / DB_FNAME  # noqa

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

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            get_json_config_settings_source(LOCAL_CONFIG_FILE_PATH)(settings_cls),
            get_json_config_settings_source(CONFIG_FILE_PATH)(settings_cls),
        )

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
        if key == "DB_URL":  # special case for DB_URL
            return f"sqlite:///{self._config.DB_FPATH}"
        self._config = Config()
        return self._config.__getattribute__(key)

    def __setattr__(self, key, value):
        if key == "_config":
            self.__dict__[key] = value
        else:
            self._config.__setattr__(key, value)

    def model_dump(self):
        model_dump_dict = {}
        # access the attributes so they are re-read from the json files
        for k in self._config.model_fields.keys():
            model_dump_dict[k] = getattr(self, k)
        return model_dump_dict


config = LazyConfig()


def persist_config(new_config: Config):
    """Persist the configuration."""
    config_variables = new_config.model_dump()

    logger.info(f"Persisting config to {CONFIG_FILE_PATH}")

    with open(LOCAL_CONFIG_FILE_PATH, "w") as f:
        json.dump(config_variables, f, indent=4)

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


def print_config():
    _OBFUSCATE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN")
    if multiprocessing.current_process().name == "MainProcess":
        logger.info(f"Reading from {CONFIG_FILE_PATH}")
        for key, val in config.model_dump().items():
            if not key.startswith("_") and key.isupper():
                parts = key.split("_")
                if any([part in parts for part in _OBFUSCATE_KEY_PARTS]):
                    val = obfuscate(val)
                logger.info(f"{key}={val}")
        logger.info(f"DB_URL={config.DB_URL}")

        if config.ERROR_REPORTING_ENABLED:  # type: ignore # noqa
            if config.ENV == "build":
                is_reporting_branch = True
            else:
                active_branch_name = git.Repo(ROOT_DIRPATH).active_branch.name
                logger.info(f"{active_branch_name=}")
                is_reporting_branch = active_branch_name == config.ERROR_REPORTING_BRANCH  # type: ignore # noqa
                logger.info(f"{is_reporting_branch=}")
            if is_reporting_branch:
                sentry_sdk.init(
                    dsn=config.ERROR_REPORTING_DSN,  # type: ignore # noqa
                    traces_sample_rate=1.0,
                )
