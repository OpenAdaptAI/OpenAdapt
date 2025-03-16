"""Entry point for OmniMCP CLI."""

# Setup path to include OpenAdapt modules
from . import pathing

# Import from OpenAdapt module
from openadapt.run_omnimcp import main

if __name__ == "__main__":
    main()