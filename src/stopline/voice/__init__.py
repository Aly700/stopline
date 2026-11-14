"""Opt-in spoken receipts. Importing this package starts no work."""

import threading

from .client import SpeechClient, enabled, note, silent_result
from .sentences import sentences_for_receipt

__all__ = ['say', 'sentences_for_receipt']
_client = None
_lock = threading.Lock()


def _speaker():
    global _client
    with _lock:
        if _client is None:
            _client = SpeechClient()
        return _client


def say(text):
    """Queue speech and return a future; failures resolve to None."""
    try:
        if enabled():
            return _speaker().say(text)
    except Exception:
        note('could not start')
    return silent_result()
