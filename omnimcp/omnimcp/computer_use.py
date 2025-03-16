"""Anthropic Computer Use integration for OmniMCP.

This module provides helpers for running Anthropic's Computer Use Docker container
with proper configuration for use with Claude.

Usage:
------
    # Run Computer Use with default settings
    python -m omnimcp.computer_use

    # Run with specific API key
    python -m omnimcp.computer_use --api-key=your_api_key

    # Run with custom screen size
    python -m omnimcp.computer_use --width=1280 --height=800
"""

import os
import platform
import subprocess
import sys

import fire
from loguru import logger

# Import pathing first to ensure OpenAdapt is in the path
from . import pathing
from openadapt.config import config


def ensure_docker_installed():
    """Verify that Docker is installed and available."""
    try:
        result = subprocess.run(
            ["docker", "--version"], 
            capture_output=True, 
            text=True, 
            check=True
        )
        logger.info(f"Docker is installed: {result.stdout.strip()}")
        return True
    except (subprocess.SubprocessError, FileNotFoundError):
        logger.error("Docker is not installed or not in the PATH. Please install Docker to use Computer Use.")
        return False


def get_home_dir():
    """Get the user's home directory in a cross-platform way."""
    return os.path.expanduser("~")


def run_computer_use(
    api_key: str = None,
    width: int = 1024,
    height: int = 768,
    api_provider: str = "anthropic",
    model: str = "claude-3-sonnet-20240229"
):
    """Run Anthropic's Computer Use Docker container.
    
    Args:
        api_key: Anthropic API key (uses config.ANTHROPIC_API_KEY if not provided)
        width: Screen width for the virtual desktop
        height: Screen height for the virtual desktop
        api_provider: API provider (anthropic, bedrock, or vertex)
        model: Claude model to use
    """
    if not ensure_docker_installed():
        return
    
    # Get API key from config if not provided
    actual_api_key = api_key or config.ANTHROPIC_API_KEY
    if not actual_api_key or actual_api_key == "<ANTHROPIC_API_KEY>":
        logger.error("Anthropic API key not set in config or as parameter")
        return
    
    # Define the Docker image
    docker_image = "ghcr.io/anthropics/anthropic-quickstarts:computer-use-demo-latest"
    
    # Set up environment variables
    env_vars = [
        f"-e ANTHROPIC_API_KEY={actual_api_key}",
        f"-e API_PROVIDER={api_provider}",
        f"-e WIDTH={width}",
        f"-e HEIGHT={height}",
        f"-e CLAUDE_MODEL={model}"
    ]
    
    # Set up volume mounts
    home_dir = get_home_dir()
    volumes = [
        f"-v {home_dir}/.anthropic:/home/computeruse/.anthropic"
    ]
    
    # Set up port mappings
    ports = [
        "-p 5900:5900",  # VNC
        "-p 8501:8501",  # Streamlit
        "-p 6080:6080",  # noVNC
        "-p 8080:8080"   # Combined interface
    ]
    
    # Build the full Docker command
    docker_cmd = (
        f"docker run -it {' '.join(env_vars)} {' '.join(volumes)} {' '.join(ports)} {docker_image}"
    )
    
    # Log the command (without API key for security)
    safe_cmd = docker_cmd.replace(actual_api_key, "***")
    logger.info(f"Running Docker command: {safe_cmd}")
    
    # Print instructions for the user
    print("\n" + "="*80)
    print("Starting Anthropic Computer Use Docker container")
    print("="*80)
    print("\nOnce the container is running, open your browser to:")
    print("  Main interface:   http://localhost:8080")
    print("  Streamlit only:   http://localhost:8501")
    print("  Desktop view:     http://localhost:6080/vnc.html")
    print("\nPress Ctrl+C to stop the container\n")
    
    try:
        # Run the Docker container interactively
        process = subprocess.run(docker_cmd, shell=True)
        return process.returncode
    except KeyboardInterrupt:
        logger.info("Docker container interrupted by user")
        return 0
    except Exception as e:
        logger.error(f"Error running Docker container: {e}")
        return 1


def main():
    """Main entry point for running Computer Use."""
    fire.Fire(run_computer_use)


if __name__ == "__main__":
    main()