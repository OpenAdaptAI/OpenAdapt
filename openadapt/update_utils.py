"""Utility functions for the download app updates."""

import os
import shutil


def set_permissions(path: str) -> None:
    """Set the permissions of all files to make the executable."""
    for root, dirs, files in os.walk(path):
        for dir in dirs:
            os.chmod(os.path.join(root, dir), 0o755)
        for file in files:
            os.chmod(os.path.join(root, file), 0o755)


def unzip_file(file_path: str) -> None:
    """Unzip a file to the given directory."""
    if os.path.exists(file_path):
        shutil.unpack_archive(file_path, os.path.dirname(file_path))
        set_permissions(os.path.dirname(file_path))
        print("Unzipped")
