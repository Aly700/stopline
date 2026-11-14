"""Opt-in spoken receipts. Importing this package starts no work."""

import threading

from .client import SpeechClient, enabled, note, silent_result
from .sentences import sentences_for_receipt

__all__ = ['say', 'warm', 'watch', 'sentences_for_receipt']
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


def warm(sentences):
    """Queue cache generation without playback; return one future per clip."""
    try:
        if enabled():
            return _speaker().warm(sentences)
    except Exception:
        note('could not warm')
    return []


def watch(db, run_filter=None):
    """Follow new events; optionally restrict to one run ID or a predicate of it.

    Returns a watcher with close() and ready (a threading.Event). A disabled
    or unavailable watcher returns None. Never waits for the database.
    """
    try:
        if enabled():
            from .announcer import Watcher
            return Watcher(db, run_filter=run_filter, speaker=say).start()
    except Exception:
        note('could not watch')
    return None
