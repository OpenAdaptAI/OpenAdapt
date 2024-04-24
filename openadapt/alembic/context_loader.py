"""Context loader for alembic migrations."""


import pathlib

from alembic.config import Config

from alembic import command


def load_alembic_context():
    ALEMBIC_INI = pathlib.Path(__file__).parent.parent / "alembic.ini"

    config = Config(ALEMBIC_INI)
    config.set_main_option("script_location", str(pathlib.Path(__file__).parent))
    command.upgrade(config, "head")


if __name__ == "__main__":
    load_alembic_context()
