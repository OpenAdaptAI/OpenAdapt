"""Configuration for OmniMCP.

This module provides a simple configuration system for OmniMCP.
Configuration values can be set via environment variables.
"""

import os
from typing import Any, Dict


class Config:
    """Configuration for OmniMCP."""
    
    def __init__(self):
        """Initialize configuration from environment variables."""
        # Anthropic API
        self.ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "<ANTHROPIC_API_KEY>")
        self.CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-latest")
        self.CLAUDE_MODEL_ALTERNATIVES = [
            "claude-3-7-sonnet-20250229",
            "claude-3-5-sonnet-latest"
        ]
        
        # OmniParser
        self.OMNIPARSER_URL = os.getenv("OMNIPARSER_URL", "http://localhost:8000")
        
        # AWS (for OmniParser deployment)
        self.AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID", "")
        self.AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "")
        self.AWS_REGION = os.getenv("AWS_REGION", "us-west-2")
        
        # Deployment
        self.PROJECT_NAME = os.getenv("PROJECT_NAME", "omnimcp")
        
        # MCP Server
        self.MCP_PORT = int(os.getenv("MCP_PORT", "8765"))


# Create a singleton instance
config = Config()