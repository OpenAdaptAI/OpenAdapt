import os
import sys
from setuptools import setup, find_packages

# Add the parent directory to sys.path to allow imports from OpenAdapt
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# The actual dependencies are defined in pyproject.toml
# This setup.py file exists mainly to add OpenAdapt to the Python path
setup(
    packages=find_packages(),
    # Entry points for CLI commands
    entry_points={
        'console_scripts': [
            'omnimcp=omnimcp.run_omnimcp:main',
            'computer-use=omnimcp.computer_use:main',
        ],
    },
)