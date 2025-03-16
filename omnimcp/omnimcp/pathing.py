"""Setup Python path to include OpenAdapt modules."""

import os
import sys

def ensure_openadapt_in_path():
    """
    Add the OpenAdapt parent directory to sys.path so we can import modules.
    
    This function ensures that the OpenAdapt modules can be imported without 
    requiring a full OpenAdapt installation.
    """
    # Add the OpenAdapt parent directory to sys.path
    parent_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
        print(f"Added {parent_dir} to Python path")
    
    # Test if openadapt is importable now
    try:
        import openadapt
        return True
    except ImportError as e:
        print(f"Error importing OpenAdapt modules: {e}")
        print(f"Current sys.path: {sys.path}")
        print(f"Looking for OpenAdapt in: {parent_dir}")
        print("Make sure you are running this from within the OpenAdapt repository")
        raise

# Automatically configure path when this module is imported
ensure_openadapt_in_path()