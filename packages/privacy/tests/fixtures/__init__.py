"""Synthetic test fixtures for openadapt-privacy tests.

This module provides realistic synthetic PII/PHI data for testing
without relying on external assets or real personal information.
"""

from __future__ import annotations

# Synthetic PII examples by entity type
SYNTHETIC_TEXT = {
    "email": {
        "input": "Contact me at john.smith@example.com for details.",
        "entities": ["EMAIL_ADDRESS"],
    },
    "phone": {
        "input": "My phone number is 555-123-4567.",
        "entities": ["PHONE_NUMBER"],
    },
    "ssn": {
        "input": "SSN: 923-45-6789",
        "entities": ["US_SSN"],
    },
    "credit_card": {
        "input": "Card number: 4532-1234-5678-9012",
        "entities": ["CREDIT_CARD"],
    },
    "person": {
        "input": "Please contact John Smith regarding the account.",
        "entities": ["PERSON"],
    },
    "date": {
        "input": "Date of birth: 01/15/1985",
        "entities": ["DATE_TIME"],
    },
    "mixed": {
        "input": (
            "John Smith (john.smith@example.com, 555-123-4567) "
            "has SSN 923-45-6789 and card 4532-1234-5678-9012."
        ),
        "entities": ["PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "US_SSN", "CREDIT_CARD"],
    },
    "no_pii": {
        "input": "The quick brown fox jumps over the lazy dog.",
        "entities": [],
    },
    "empty": {
        "input": "",
        "entities": [],
    },
}

# Synthetic dict structures mimicking GUI automation data
SYNTHETIC_DICTS = {
    "simple_action": {
        "input": {
            "text": "Email: john@example.com",
            "timestamp": 1234567890,
            "action_type": "click",
        },
        "scrubbed_keys": ["text"],
    },
    "nested_action": {
        "input": {
            "text": "Contact John Smith",
            "metadata": {
                "title": "User Profile - John Smith",
                "tooltip": "Click to view john@example.com",
            },
            "coordinates": {"x": 100, "y": 200},
        },
        "scrubbed_keys": ["text", "title", "tooltip"],
    },
    "window_state": {
        "input": {
            "state": {
                "window_title": "Email to john@example.com",
                "active_element": "Send button",
            },
            "children": [
                {"text": "To: John Smith <john@example.com>"},
                {"text": "Subject: Meeting"},
            ],
        },
        "scrubbed_keys": ["window_title", "text"],
    },
    "key_sequence": {
        "input": {
            "text": "j-o-h-n-@-e-x-a-m-p-l-e-.-c-o-m",
            "canonical_text": "john@example.com",
            "key_char": "m",
        },
        "scrubbed_keys": ["text", "canonical_text"],
    },
}

# Synthetic recording structure (for data loader examples)
SYNTHETIC_RECORDING = {
    "task_description": "Send email to John Smith at john@example.com",
    "timestamp": 1234567890,
    "actions": [
        {
            "id": 1,
            "action_type": "click",
            "text": "Compose",
            "timestamp": 1234567891,
        },
        {
            "id": 2,
            "action_type": "type",
            "text": "j-o-h-n-@-e-x-a-m-p-l-e-.-c-o-m",
            "canonical_text": "john@example.com",
            "timestamp": 1234567892,
        },
        {
            "id": 3,
            "action_type": "click",
            "text": "Send",
            "window_title": "Email to john@example.com",
            "timestamp": 1234567893,
        },
    ],
    "screenshots": [
        {"id": 1, "action_id": 1, "path": "screenshot_001.png"},
        {"id": 2, "action_id": 2, "path": "screenshot_002.png"},
        {"id": 3, "action_id": 3, "path": "screenshot_003.png"},
    ],
}


def get_text_examples() -> dict:
    """Get synthetic text examples for testing."""
    return SYNTHETIC_TEXT.copy()


def get_dict_examples() -> dict:
    """Get synthetic dict examples for testing."""
    return SYNTHETIC_DICTS.copy()


def get_recording_example() -> dict:
    """Get synthetic recording example for testing."""
    import copy

    return copy.deepcopy(SYNTHETIC_RECORDING)
