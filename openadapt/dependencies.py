"""This file contains the code necessary to ensure the necessary system dependencies are installed."""
import os
import platform
import subprocess

from loguru import logger

DEP_NAME_TO_SYS_TO_INSTALL_CMD = {
    "cargo": {
        "Darwin": {
            "Apple": ["brew", "install", "rust"],
            "Intel": ["brew", "install", "rust"],
        },
        "Windows": [
            "curl",
            "--proto",
            "'=https'",
            "--tlsv1.2",
            "-sSf",
            "https://sh.rustup.rs",
            "|",
            "sh",
        ],
        "Location": ".cargo/bin",
    },
    "cairo": {
        "Darwin": {
            "Apple": ["arch", "-arm64", "brew", "install cairo"],
            "Intel": ["brew", "install", "cairo"],
        },
        "Windows": "",
        "Location": "",
    },
}


def ensure_dependency(name: str, is_executable: bool = True) -> str:
    """Returns the location of the dependency."""
    system = platform.system()
    root_directory = os.path.expanduser("~")
    if not is_dependency_installed(name, system, is_executable):
        install_dependency(name, system, root_directory)
    
    path_to_dep = os.path.join(
        root_directory, DEP_NAME_TO_SYS_TO_INSTALL_CMD[name]["Location"]
    )
    return path_to_dep


def is_dependency_installed(
    dependency_name: str, system: str = None, is_executable: bool = True, 
) -> bool:
    """
    Check if the specified dependency is installed on the computer.

    Args:
        - dependency_name (str): The name of the dependency to check
        - system (str, optional): The target system for which to check the dependency
            - If not provided, the system will be automatically determined.
        - is_executable (bool, optional): Whether the dependency is an executable or a package.
            - Defaults to True.

    Returns:
        bool: True if the dependency is installed, False otherwise.

    Note:
        On Windows, the function currently does not check for non-executable dependencies.
        Add implementation to check for non-executable dependencies on Windows in the future.
    """
    if not system:
        system = platform.system()

    if is_executable:
        try:
            # Run the command to check if the dependency is installed
            subprocess.check_output([dependency_name, "--version"])
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False
    else:
        if system == "Apple":
            try:
                subprocess.check_output(["brew", "list", dependency_name])
                return True
            except (subprocess.CalledProcessError, FileNotFoundError):
                return False
        else:
            # how to check on windows
            logger.info("Add how to check if a dependency is downloaded on Windows.")


def install_dependency(name: str, system: str, root_directory: str) -> None:
    "Installs the specified dependency on the given system and returns the location where the installation is completed."
    logger.info(f"installing dependency {name=}")

    if system == "Windows":
        cwd = os.getcwd()
        os.chdir(root_directory)
        subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system])
        os.chdir(cwd)
    else:
        cpu = subprocess.check_output(
            ["sysctl", "-n", "machdep.cpu.brand_string"]
        ).decode("utf-8")
        if "Apple" in cpu:
            subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system]["Apple"])
        else:
            subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system]["Intel"])

