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

def ensure_dependency(name: str, is_executable: bool = True) -> str:
    system = platform.system()
    if not is_dependency_installed(name, system, is_executable):
        return install_executable(name, system)
    else:
        return name

def is_dependency_installed(dependency_name: str, system: str, is_executable: bool = True) -> bool:
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
            # add
            logger.info("tbd")

def install_executable(name: str, system: str) -> str:
    logger.info(f"installing executable {name=}")
    root_directory = os.path.expanduser('~')

    if system == "Windows":
        cwd = os.getcwd()
        os.chdir(root_directory)
        subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system])
        os.chdir(cwd)
    else:
        cpu = subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"]).decode('utf-8')
        if "Apple" in cpu:
            subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system]["Apple"])
        else:
            subprocess.run(DEP_NAME_TO_SYS_TO_INSTALL_CMD[name][system]["Intel"])
    
    path_to_dep = os.path.join(root_directory, DEP_NAME_TO_SYS_TO_INSTALL_CMD[name]["Location"])
    return path_to_dep

# TODO:
    # - lint
    # - docstrings
    # - test