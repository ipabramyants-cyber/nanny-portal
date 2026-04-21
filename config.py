"""
config.py — Application configuration helpers.
"""
import os


def admin_ids() -> list[int]:
    """
    Return list of Telegram user IDs that have admin access.
    Set ADMIN_IDS env var as comma-separated numeric IDs, e.g.:
        ADMIN_IDS=123456789,987654321
    """
    raw = os.environ.get('ADMIN_IDS', '')
    result = []
    for part in raw.split(','):
        part = part.strip()
        if part.lstrip('-').isdigit():
            result.append(int(part))
    return result
