"""Small HTTPS embedding client, with one attempt and secret-safe errors."""
import asyncio
import math
import os
from pathlib import Path

import httpx

MODEL = 'voyage-4-lite'
DIMENSIONS = 1024
ENDPOINT = 'https://api.voyageai.com/v1/embeddings'
KEY_FILE = Path.home() / '.voyage_key'


class SearchError(Exception):
    """A fixed, public message safe to display verbatim."""


def load_key():
    try:
        key = os.environ.get('VOYAGE_API_KEY')
        if key is None:
            key = KEY_FILE.read_text()
        key = key.strip()
    except (OSError, UnicodeError):
        key = ''
    if not key:
        raise SearchError('Voyage key is missing; set VOYAGE_API_KEY or ~/.voyage_key.') from None
    return key


class VoyageClient:
    model = MODEL
    dimensions = DIMENSIONS

    def __init__(self, *, endpoint=ENDPOINT, timeout=10):
        self.endpoint = endpoint
        self.timeout = min(10, timeout)

    def embed(self, texts, input_type, *, timeout=None):
        if input_type not in ('document', 'query'):
            raise SearchError('Choose document or query embeddings.')
        if not texts:
            return []
        if len(texts) > 1000:
            raise SearchError('This run has too many audit entries for one embedding request.')
        key = load_key()
        seconds = min(self.timeout, timeout) if timeout is not None else self.timeout
        if seconds <= 0:
            raise SearchError('Search took too long; try again.')
        payload = dict(input=list(texts), model=self.model, input_type=input_type,
                       output_dimension=self.dimensions, output_dtype='float', truncation=False)

        async def request():
            async with asyncio.timeout(seconds):
                async with httpx.AsyncClient(timeout=seconds, follow_redirects=False) as client:
                    return await client.post(self.endpoint, json=payload,
                                             headers={'Authorization': 'Bearer ' + key})

        try:
            response = asyncio.run(request())
        except (TimeoutError, httpx.TimeoutException):
            raise SearchError('Voyage took too long; try again.') from None
        except Exception:
            raise SearchError('Voyage is unavailable; try again.') from None
        if response.status_code == 429:
            raise SearchError('Voyage is rate limited; try again in 20 seconds')
        if response.status_code in (401, 403):
            raise SearchError('Voyage could not accept the key; check its access.')
        if response.status_code != 200:
            raise SearchError('Voyage is unavailable; try again.')
        try:
            data = response.json()['data']
            if len(data) != len(texts) or sorted(item['index'] for item in data) != list(range(len(texts))):
                raise ValueError
            vectors = [item['embedding'] for item in sorted(data, key=lambda item: item['index'])]
            for vector in vectors:
                if (len(vector) != self.dimensions or
                        any(type(n) not in (float, int) or not math.isfinite(n) for n in vector) or
                        not any(vector)):
                    raise ValueError
            return vectors
        except Exception:
            raise SearchError('Voyage returned invalid embeddings; try again.') from None
