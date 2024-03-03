"""Gets recently accessed files."""

from datetime import datetime
from typing import List, Tuple
import os
import pathlib


def get_recent_files(
    folders: List[str], ignore_patterns: List[str], limit: int
) -> Tuple[List[Tuple[str, datetime]], int]:
    """Gets recently accessed files in folders."""
    files = []
    for folder in folders:
        for root, dirs, filenames in os.walk(folder):
            # Exclude hidden directories
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for filename in filenames:
                path = os.path.join(root, filename)
                if any(pattern in path for pattern in ignore_patterns):
                    continue
                # Ignore symbolic links
                if os.path.islink(path):
                    continue
                try:
                    accessed_time = datetime.fromtimestamp(os.path.getatime(path))
                    files.append((path, accessed_time))
                except FileNotFoundError:
                    pass
    files.sort(key=lambda x: x[1], reverse=True)
    return files[:limit], len(files)


def test_common_user_directories() -> None:
    """Gets recently accessed files in common user directories."""
    folders = [
        os.path.expanduser("~" + os.path.sep + "Documents"),
        os.path.expanduser("~" + os.path.sep + "Downloads"),
        os.path.expanduser("~" + os.path.sep + "Desktop"),
    ]
    ignore_patterns = [
        ".DS_Store",
        ".git",
        ".vscode",
        "__pycache__",
        "node_modules",
    ]
    limit = 10
    files, num_files = get_recent_files(folders, ignore_patterns, limit)
    print("Recent files in common user directories:")
    print("---------------------------------------")
    for path, accessed_time in files:
        print(f"{accessed_time.strftime('%Y-%m-%d %H:%M:%S')}: {path}")
    print(f"\nsearched {num_files:,} files.\n")


def test_system_directories() -> None:
    """Gets recently accessed files in system directories."""
    if os.name == "nt":
        folders = [os.environ["SystemRoot"] + os.path.sep + "System32"]
    else:
        folders = ["/usr/bin", "/usr/local/bin"]
    ignore_patterns = [
        ".DS_Store",
        ".git",
        ".vscode",
        "__pycache__",
        "node_modules",
    ]
    limit = 10
    files, num_files = get_recent_files(folders, ignore_patterns, limit)
    print("Recent files in system directories:")
    print("-----------------------------------")
    for path, accessed_time in files:
        print(f"{accessed_time.strftime('%Y-%m-%d %H:%M:%S')}: {path}")
    print(f"\nsearched {num_files:,} files.\n")


def test_home_directory() -> None:
    """Gets recently accessed files in home directory."""
    folders = [
        pathlib.Path.home(),
        pathlib.Path("/usr/bin"),
        pathlib.Path("/usr/local/bin"),
    ]
    ignore_patterns = [
        ".DS_Store",
        ".git",
        ".vscode",
        "__pycache__",
        "node_modules",
    ]
    limit = 10
    files, num_files = get_recent_files(folders, ignore_patterns, limit)
    print("Recent files in home directory:")
    print("--------------------------------")
    for path, accessed_time in files:
        print(f"{accessed_time.strftime('%Y-%m-%d %H:%M:%S')}: {path}")
    print(f"\nsearched {num_files:,} files.\n")


if __name__ == "__main__":
    test_common_user_directories()
    test_system_directories()
    test_home_directory()
