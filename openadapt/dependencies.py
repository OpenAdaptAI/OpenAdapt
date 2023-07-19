"""This file contains the code necessary to ensure the necessary system dependencies are installed."""
import os
import platform
import subprocess

from loguru import logger

DEP_NAME_TO_SYS_TO_INSTALL_CMD = {
    "cargo": {
        "Darwin": {
            "Apple": {"brew install rust"},
            "Intel" : {"brew install rust"},
        },
        "Windows": {
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        },
        "Location": {
            ".cargo/bin"
        }
     },
    "cairo": {
        "Darwin": {
            "Apple": {"arch -arm64 brew install cairo"},
            "Intel" : {"brew install cairo"},
        },
        "Windows": {
            "",
        },
        "Location": {
            ""
        }
    },
}

def ensure_executable(name: str) -> str:
    if not is_executable_installed(name):
        return install_executable(name)
    else:
        return name

def is_executable_installed(dependency_name: str) -> bool:
    try:
        # Run the command to check if the dependency is installed
        subprocess.check_output([dependency_name, "--version"], check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False

def install_executable(name: str) -> str:
    logger.info(f"installing executable {name=}")
    system = platform.system()
    root_directory = os.path.expanduser('~')

    if system == "Windows":
        cwd = os.getcwd()
        os.chdir(root_directory)
        subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system])
        os.chdir(cwd)
    else:
        cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"])
        subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system][cpu])
    
    path_to_dep = os.path.join(root_directory, DEP_NAME_TO_SYS_TO_INSTALL_CMD[name]["Location"])
    return path_to_dep

# TODO:
    # - lint
    # - docstrings
    # - test