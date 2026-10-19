"""Scrubbing pipelines package.

This module provides convenience functions and classes for scrubbing
nested data structures like dicts and lists.
"""

from openadapt_privacy.pipelines.dicts import DictScrubber, scrub_dict, scrub_list_dicts

__all__ = [
    "DictScrubber",
    "scrub_dict",
    "scrub_list_dicts",
]
