"""
Shared utility functions used across multiple modules.
"""

import re


def safe_id(username: str) -> str:
    """Generate a filesystem-safe unique identifier from a username/email.
    
    Replaces any character that is not alphanumeric with an underscore.
    Used for state file names, Chrome profile directories, etc.
    """
    return re.sub(r'[^a-zA-Z0-9]', '_', str(username))
