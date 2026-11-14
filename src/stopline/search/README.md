# Ask the audit

Retrieve stored audit events by meaning. Each result contains the original audit id, action id, kind, timestamp, a deterministic sentence and a similarity score. No answer text is generated.

The optional integration is:

```python
from stopline.search import AuditSearch, search_routes, sentence_for, watch

app.include_router(search_routes(db))
receipt_watcher = watch(db)
# During shutdown:
if receipt_watcher is not None:
    receipt_watcher.stop()
```

Use a database obtained from `stopline.db.database(name)`. Indexing refuses the reserved database named `stopline`; choose a separate database containing the run. The package only writes `audit_search`. It never changes an audit entry, effect, permission or receipt.

`GET /api/ask?q=what%20was%20refused&run_id=example` returns HTTP 200 with `ok`, `engine`, `rows` and `message`. Omitting `run_id` selects the most recent stored run. An empty question, missing credentials, rate limit or unavailable database produces one plain message and `ok: false`.

```sh
python -m stopline.search "what was refused after the stop" --run example --database example_database
```

`STOPLINE_SEARCH_DATABASE` sets the CLI database when `--database` is omitted. The Voyage key comes from `VOYAGE_API_KEY`, or from `~/.voyage_key` when that variable is unset. Credentials and provider response bodies are never included in messages.

`AuditSearch(db).ensure_index()` provisions an Atlas Vector Search index and waits at most 90 seconds for it to become queryable. Receipt watching provisions it in a separate daemon thread. Questions never wait for provisioning. `index_run(run_id)` embeds all missing audit events in a single request and returns the number inserted; failures raise a `SearchError` with a fixed public message. `ask(run_id, question, k=5)` contains all failures and returns the same envelope as the HTTP endpoint.

The model is `voyage-4-lite`, with 1,024 float dimensions. Documents use `input_type="document"`; each question makes one query embedding request with `input_type="query"`. There is no reranking request and no retry after a rate limit. Receipt insertion and closure each trigger at most one document request per run. The HTTP endpoint and watcher share a service to avoid duplicate concurrent batches.

Atlas uses cosine similarity and a `run_id` pre-filter. After new entries are stored, retrieval checks index visibility for up to five seconds, bounded by the question's eleven-second overall budget. If the index is unavailable or incomplete, it ranks the run's stored embeddings locally. Both engines use scores between zero and one, with local cosine normalized as `(1 + cosine) / 2`. The response names the engine actually used. Without a query embedding or readable stored entries, it returns a plain failure instead.

Times in sentences retain the stored timestamp's timezone; `at` includes its offset. A claim is described as preparation for sending, and an undo event as a request, never as proof of completion. Missing permission names are not guessed.

Run the isolated checks:

```sh
python -m pytest tests/search/test_search.py
python -m pytest tests/search/test_live.py -s
```

The live check skips without a key, creates a fresh `stopline.db.test_database("search")`, seeds eleven events under the strict audit validator, makes at most four Voyage requests, prints timings and the actual engine, then drops the index and database. Requests are spaced outside the measured questions to respect a three-per-minute account limit. Do not run it repeatedly against a rate-limited account.

API details checked on 14 November 2026: [Voyage authentication](https://docs.voyageai.com/docs/api-key-and-installation), [embedding models](https://docs.voyageai.com/docs/embeddings), [embedding requests](https://docs.voyageai.com/reference/embeddings-api), [PyMongo index creation](https://www.mongodb.com/docs/languages/python/pymongo-driver/current/indexes/), and [filtered vector queries](https://www.mongodb.com/docs/vector-search/query/aggregation-stages/vector-search-stage/).
