"""Configuration module for OpenAdapt."""


from enum import Enum
from typing import Any, ClassVar, Union
import json
import multiprocessing
import pathlib
import shutil

from loguru import logger
from pydantic import field_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource
import git
import sentry_sdk

from openadapt.build_utils import (
    get_path_to_preferences_folder,
    is_running_from_executable,
)

CURRENT_DIRPATH = pathlib.Path(__file__).parent
PREFERENCES_DIRPATH = get_path_to_preferences_folder(CURRENT_DIRPATH)
CONFIG_FILE_PATH = CURRENT_DIRPATH / "config.json"
LOCAL_CONFIG_FILE_PATH = (PREFERENCES_DIRPATH / "config.local.json").absolute()
ROOT_DIRPATH = CURRENT_DIRPATH.parent
RECORDING_DIRECTORY_PATH = (PREFERENCES_DIRPATH / "recordings").absolute()
DIRNAME_PERFORMANCE_PLOTS_DIR = (PREFERENCES_DIRPATH / "performance").absolute()
CAPTURE_DIR_PATH = (PREFERENCES_DIRPATH / "captures").absolute()

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
    """Get the JSON config settings source."""

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
    """Configuration class for OpenAdapt."""

    # Privacy
    PRIVATE_AI_API_KEY: str = ""

    # Segmentation
    REPLICATE_API_TOKEN: str = ""

    class SegmentationAdapter(str, Enum):
        """Adapter for the segmentation API."""

        SOM: str = "som"
        REPLICATE: str = "replicate"
        ULTRALYTICS: str = "ultralytics"

    DEFAULT_SEGMENTATION_ADAPTER: SegmentationAdapter = SegmentationAdapter.ULTRALYTICS

    # Completions
    OPENAI_API_KEY: str = "<OPENAI_API_KEY>"
    ANTHROPIC_API_KEY: str = "<ANTHROPIC_API_KEY>"
    GOOGLE_API_KEY: str = "<GOOGLE_API_KEY>"

    # Cache
    CACHE_DIR_PATH: str = ".cache"
    CACHE_ENABLED: bool = True
    CACHE_VERBOSITY: int = 0

    # Database
    DB_ECHO: bool = False
    DB_URL: ClassVar[str] = (
        f"sqlite:///{(PREFERENCES_DIRPATH / 'openadapt.db').absolute()}"
    )

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
    REPLAY_STRIP_ELEMENT_STATE: bool = True
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

    # App configurations
    APP_DARK_MODE: bool = False

    # Scrubbing configurations
    SCRUB_ENABLED: bool = False
    SCRUB_CHAR: str = "*"
    SCRUB_LANGUAGE: str = "en"
    SCRUB_FILL_COLOR: Union[str, int] = 0x0000FF

    @field_validator("SCRUB_FILL_COLOR")
    @classmethod
    def validate_scrub_fill_color(cls, v: Union[str, int]) -> int:  # noqa: ANN102
        """Validate the scrub fill color.

        Convert the color to a hex value if it is a string.
        """
        if isinstance(v, str):
            return int(v, 16)
        return v

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

    SOM_SERVER_URL: str = "<SOM_SERVER_URL>"

    class Adapter(str, Enum):
        """Adapter for the completions API."""

        OPENAI: str = "openai"
        ANTHROPIC: str = "anthropic"
        GOOGLE: str = "google"

    DEFAULT_ADAPTER: Adapter = Adapter.OPENAI

    @classmethod
    def settings_customise_sources(
        cls,  # noqa: F821,ANN102
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Define the sources and their order for loading the settings values.

        The order of preference is as follows:
        1. settings with which the class is initialized
        2. settings from the local config json file
        3. settings from the config json file
        """
        return (
            init_settings,
            get_json_config_settings_source(LOCAL_CONFIG_FILE_PATH)(settings_cls),
            get_json_config_settings_source(CONFIG_FILE_PATH)(settings_cls),
        )

    def __setattr__(self, key: str, value: Any) -> None:
        """Set the attribute."""
        super().__setattr__(key, value)
        persist_config(self)

    classifications: ClassVar[dict[str, list[str]]] = {
        "api_keys": [
            "PRIVATE_AI_API_KEY",
            "REPLICATE_API_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
        ],
        "scrubbing": [
            "SCRUB_ENABLED",
            "SCRUB_CHAR",
            "SCRUB_LANGUAGE",
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
    """A lazy configuration class that reads the configuration from the json files.

    This class acts as an intermediary between the configuration and the user.
    So that the fetched configuration is always up-to-date.
    """

    def __init__(self) -> None:
        """Initialize the LazyConfig class."""
        self._config = Config()

    def __getattr__(self, key: str) -> Any:
        """Get the attribute from the inner Config object.

        This method reloads the configuration from the json files if the key
        is not _config. So that the configuration is always up-to-date.
        """
        if key == "_config":
            return self._config
        # Create a new Config object to read the latest values from the json files
        self._config = Config()
        return self._config.__getattribute__(key)

    def __setattr__(self, key: str, value: Any) -> None:
        """Set the attribute."""
        if key == "_config":
            self.__dict__[key] = value
        else:
            self._config.__setattr__(key, value)

    def model_dump(self) -> dict[str, Any]:
        """Dump the model as a dictionary."""
        model_dump_dict = {}
        for k in self._config.model_fields.keys():
            model_dump_dict[k] = getattr(self, k)
        return model_dump_dict


config = LazyConfig()


def persist_config(new_config: Config) -> None:
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


def print_config() -> None:
    """Print the configuration."""
    _OBFUSCATE_KEY_PARTS = ("KEY", "PASSWORD", "TOKEN")
    if multiprocessing.current_process().name == "MainProcess":
        logger.info(f"Reading from {LOCAL_CONFIG_FILE_PATH}")
        for key, val in config.model_dump().items():
            if not key.startswith("_") and key.isupper():
                parts = key.split("_")
                if any([part in parts for part in _OBFUSCATE_KEY_PARTS]):
                    val = obfuscate(val)
                logger.info(f"{key}={val}")

        if config.ERROR_REPORTING_ENABLED:  # type: ignore # noqa
            if is_running_from_executable():
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
