"""Utility functions for the download app updates."""

import os
import shutil


def set_permissions(path: str) -> None:
    """Set the permissions of all files to make the executable."""
    for root, dirs, files in os.walk(path):
        for dir in dirs:
            dir_path = os.path.join(root, dir)
            try:
                os.chmod(os.path.join(root, dir), 0o755)
            except PermissionError:
                print(f"Skipping directory due to PermissionError: {dir_path}")
            except Exception as e:
                print(f"An error occurred for directory {dir_path}: {e}")
        for file in files:
            file_path = os.path.join(root, file)
            try:
                os.chmod(os.path.join(root, file), 0o755)
            except PermissionError:
                print(f"Skipping file due to PermissionError: {file_path}")
            except Exception as e:
                print(f"An error occurred for file {file_path}: {e}")


def unzip_file(file_path: str) -> None:
    """Unzip a file to the given directory."""
    if os.path.exists(file_path):
        shutil.unpack_archive(file_path, os.path.dirname(file_path))
        set_permissions(os.path.dirname(file_path))
        print("Unzipped")
