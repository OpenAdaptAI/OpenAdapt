"""Utility functions for the download app updates."""

import os
import shutil

from loguru import logger


def set_permissions(path: str) -> None:
    """Set the permissions of all files to make the executable."""
    for root, dirs, files in os.walk(path):
        for dir in dirs:
            dir_path = os.path.join(root, dir)
            try:
                os.chmod(os.path.join(root, dir), 0o755)
            except PermissionError:
                logger.info(f"Skipping directory due to PermissionError: {dir_path}")
            except Exception as e:
                logger.info(f"An error occurred for directory {dir_path}: {e}")
        for file in files:
            file_path = os.path.join(root, file)
            try:
                os.chmod(os.path.join(root, file), 0o755)
            except PermissionError:
                logger.info(f"Skipping file due to PermissionError: {file_path}")
            except Exception as e:
                logger.info(f"An error occurred for file {file_path}: {e}")


def unzip_file(file_path: str, base_file_name: str) -> None:
    """Unzip a file to the given directory."""
    if os.path.exists(file_path):
        unzipping_directory_path = f"{os.path.dirname(file_path)}/{base_file_name}"
        if not os.path.exists(unzipping_directory_path):
            os.makedirs(unzipping_directory_path)
        shutil.unpack_archive(file_path, unzipping_directory_path)
        set_permissions(unzipping_directory_path)
        logger.info("Unzipped")
