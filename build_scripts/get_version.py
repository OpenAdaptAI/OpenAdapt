"""Get the version of the package."""

import importlib.metadata


def get_version() -> str:
    """Get the version of the package."""
    return importlib.metadata.version("openadapt")


if __name__ == "__main__":
    print(get_version())
