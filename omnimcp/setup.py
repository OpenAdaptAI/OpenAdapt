import os
import sys
from setuptools import setup, find_packages

# Add the parent directory to sys.path to allow imports from OpenAdapt
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

setup(
    name="omnimcp",
    version="0.1.0",
    packages=find_packages(),
    install_requires=[
        "pynput>=1.7.6",
        "pillow>=10.0.0",
        "fire>=0.4.0",
        "anthropic>=0.42.0", 
        "loguru>=0.6.0",
        "mcp>=0.9.0",
        "requests>=2.31.0",
        "mss>=6.1.0",
    ],
    entry_points={
        'console_scripts': [
            'omnimcp=omnimcp.run_omnimcp:main',
        ],
    },
)