"""Optional speech: one daemon queue, an atomic disk cache, and no raised errors.

API checked 2026-11-14:
https://elevenlabs.io/docs/api-reference/text-to-speech/convert
https://elevenlabs.io/docs/overview/models
River was selected from this account's GET /v2/voices response on that date.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import subprocess
import tempfile
import threading
from concurrent.futures import Future
from collections.abc import Callable

import httpx

MODEL = 'eleven_flash_v2_5'
OUTPUT_FORMAT = 'mp3_44100_128'
VOICE_ID = 'SAz9YHcvj6GT2YYXdXww'
VOICE_NAME = 'River'
TIMEOUT = 6.0
_log = logging.getLogger('stopline.voice')


def enabled() -> bool:
    return os.environ.get('STOPLINE_VOICE', '').strip().lower() != 'off'


def read_key() -> str:
    """An explicitly empty environment value does not fall back to the file."""
    key = os.environ.get('ELEVENLABS_API_KEY')
    if key is None:
        try:
            key = (Path.home() / '.elevenlabs_key').read_text()
        except OSError:
            key = ''
    key = key.strip()
    # HTTP debug logging includes the offending header on protocol errors.
    # Reject control/non-ASCII characters before handing the key to httpx.
    if any(ord(character) < 33 or ord(character) > 126 for character in key):
        return ''
    return key


def note(reason: str) -> None:
    # Callers supply fixed labels only, never exceptions, request bodies or URLs.
    try:
        _log.warning('speech unavailable (%s)', reason)
    except Exception:
        pass


def silent_result() -> Future:
    result = Future()
    result.set_result(None)
    return result


def play(path: Path) -> None:
    subprocess.run(['afplay', str(path)], check=True, timeout=TIMEOUT,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class SpeechClient:
    def __init__(self, *, cache_dir: Path | None = None,
                 base_url: str = 'https://api.elevenlabs.io',
                 player: Callable[[Path], None] = play):
        self.cache_dir = cache_dir
        self.base_url = base_url
        self.player = player
        self.timeout = TIMEOUT
        self._queue: queue.Queue = queue.Queue(maxsize=64)
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._work, name='stopline-speech', daemon=True)
        self._thread.start()

    def say(self, text: str) -> Future[Path | None]:
        """Return immediately; the future resolves after playback, never raises."""
        return self._submit(text, True)

    def warm(self, sentences) -> list[Future]:
        """Queue generation only; repeated text uses the same cache entry."""
        if not enabled():
            return []
        return [self._submit(text, False) for text in sentences]

    def _submit(self, text: str, playback: bool) -> Future:
        if not enabled() or self._closed.is_set():
            return silent_result()
        result = Future()
        try:
            self._queue.put_nowait((text, playback, result))
        except Exception:
            note('queue full')
            result.set_result(None)
        return result

    def close(self) -> None:
        """Stop without waiting for a network request or the speakers."""
        self._closed.set()

    def _work(self) -> None:
        while True:
            try:
                text, playback, result = self._queue.get(timeout=.2)
            except queue.Empty:
                if self._closed.is_set():
                    return
                continue
            path = None
            try:
                if enabled() and not self._closed.is_set() and not result.cancelled():
                    path = self._render(text)
                    if path and playback and enabled():
                        self.player(path)
            except Exception:
                note('playback failed')
                path = None
            finally:
                if not result.done():
                    result.set_result(path)
                self._queue.task_done()

    def _render(self, text: str) -> Path | None:
        temporary = None
        try:
            if not text.strip():
                return None
            voice = os.environ.get('STOPLINE_VOICE_ID', '').strip() or VOICE_ID
            digest = hashlib.sha256(json.dumps([text, voice, MODEL, OUTPUT_FORMAT]).encode()).hexdigest()
            directory = self.cache_dir or Path(tempfile.gettempdir()) / 'stopline-voice'
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = directory / f'{digest}.mp3'
            # Also serialize separate --warm and app processes using this cache.
            with (directory / f'{digest}.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if path.exists() and path.stat().st_size:
                    return path
                key = read_key()
                if not key:
                    note('no key')
                    return None
                audio = asyncio.run(self._audio(text, voice, key))
                with tempfile.NamedTemporaryFile(dir=directory, suffix='.tmp', delete=False) as output:
                    temporary = Path(output.name)
                    output.write(audio)
                temporary.replace(path)
                return path
        except (TimeoutError, httpx.TimeoutException):
            note('timeout')
        except Exception:
            note('generation failed')
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return None

    async def _audio(self, text: str, voice: str, key: str) -> bytes:
        # httpx's socket timeouts alone do not bound a slowly trickling response.
        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f'{self.base_url}/v1/text-to-speech/{voice}',
                    params={'output_format': OUTPUT_FORMAT},
                    headers={'xi-api-key': key, 'Content-Type': 'application/json', 'Accept': 'audio/mpeg'},
                    json={'text': text, 'model_id': MODEL},
                )
                response.raise_for_status()
                if not response.content or not response.headers.get('content-type', '').startswith('audio/'):
                    raise ValueError('invalid audio')
                return response.content
