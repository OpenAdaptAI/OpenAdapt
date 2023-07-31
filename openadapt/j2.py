import os

from griptape.utils.j2 import J2
from loguru import logger

from openadapt import config


def load_template(
    template_fname: str,
    templates_dirpath=config.TEMPLATES_DIRPATH,
    **kwargs,
) -> str:
    logger.info(f"{template_fname=}")
    logger.info(f"{templates_dirpath=}")
    return J2(
        template_name=template_fname,
        templates_dir=templates_dirpath,
    ).render(**kwargs)
