# Spoken receipts

This package is optional. Importing it starts no thread, network request,
playback or database connection. It never writes database records.

```python
from stopline.voice import say, warm, watch, sentences_for_receipt
from stopline.voice.sentences import DEMO_SENTENCES

warm(DEMO_SENTENCES)          # background generation; no playback
announcer = watch(db)        # use the database already opened by stopline.db
# Before beginning the demo, announcer.ready can be checked for stream readiness.
# At shutdown: announcer.close() if announcer else None
```

Start one watcher when the app starts, before starting a run. `watch(db,
run_filter="run-id")` restricts events to a run. A callable filter receives the
run ID and returns whether to speak it; this can follow the page's current run.
The returned watcher's `ready` event is set after the first stream opens.
`close()` signals shutdown without waiting. When disabled, `watch()` returns
`None`. Database access happens only in its daemon thread, with six-second
operation deadlines and automatic stream resumption.

Receipt insertion waits one second, then reads covered effects and refusals.
Blocked counts combine cancelled effects and distinct later refused action IDs;
a refused retry of a covered action is counted once. Applied is completed,
sent is unknown, and rejected is refused. Closing only says that the traffic
shift applied if the stored shift is applied. An early close stays after its
stop announcement, and the insertion announcement uses the latest stored state.

Baseline stop announcements also wait one second. Their count is the applied
effects observed then whose target `evidence.at_epoch` is strictly later than
the run's `stop_at_local`. A missing stop clock produces an unknown outcome.
This is a snapshot, not a prediction about work that might finish later.

Each insertion, close and baseline stop is queued once per watcher, including
stream reconnects. This deduplication is in memory; it is not shared between
multiple watchers or persisted across process restarts. A new watcher follows
new changes rather than replaying historical runs. It resumes a disconnected
stream while its MongoDB resume token remains valid.

`sentences_for_receipt(receipt, effects, refusals, closing=False)` is pure and
returns a list of short clips in speaking order. Set `closing=True` for the
later closure message. `sentence_for_baseline(count)` is in `sentences.py`.

`say(text)` immediately returns a future whose result is the cached audio path
after playback, or `None` on failure. `warm(sentences)` returns one future per
clip without playing. Application callers can ignore these futures; only the
sound-check CLI waits for them. One bounded daemon queue serializes speech.
An API error, timeout, missing key or failed player logs a fixed short message;
request bodies, exception messages and keys are never logged by this package.

The default voice is **River** (`SAz9YHcvj6GT2YYXdXww`), selected from the account's
voice list on 2026-11-14. `STOPLINE_VOICE_ID` overrides it. The model is
`eleven_flash_v2_5`; output is `mp3_44100_128`. These use the official
[create-speech reference](https://elevenlabs.io/docs/api-reference/text-to-speech/convert),
[voice-list reference](https://elevenlabs.io/docs/api-reference/voices/search), and
[model reference](https://elevenlabs.io/docs/overview/models).

Set `ELEVENLABS_API_KEY`, or leave it unset to read `~/.elevenlabs_key`.
An explicitly empty environment value does not fall back to that file.
Set `STOPLINE_VOICE=off` to disable all generation, playback and watching.
Audio is stored in `stopline-voice` under Python's system temp directory,
keyed by sentence, voice, model and output format. Writes are atomic and file
locks prevent concurrent generation of the same clip by separate processes.
Once warmed, cached speech works without a key or network access.

```sh
uv run python -m stopline.voice --say "The receipt is open."
uv run python -m stopline.voice --warm
uv run pytest tests/voice -o addopts=""
STOPLINE_VOICE_LIVE=1 uv run pytest tests/voice/test_live.py -o addopts="" -s
STOPLINE_VOICE_DB_TEST=1 uv run pytest tests/voice/test_database.py -o addopts=""
```

The live test skips without a key, generates one four-word clip, and plays it
once at 15% player volume. Normal tests use a local fake HTTP server and fake
player. The database test creates and drops a `stopline_test_voice_*` database.
The five warmed demo clips measured 1.149–3.370 seconds with the default voice.
Arbitrary sound-check text or an override voice may take longer. Playback uses
`afplay` and the Mac's current audio output; use the laptop speaker output for
the demo. There are no added dependencies.
