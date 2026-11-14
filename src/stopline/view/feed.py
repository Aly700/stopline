"""One shared, restartable database change stream per application."""
import threading

from pymongo.errors import OperationFailure


class ChangeFeed:
    def __init__(self, db):
        self.db = db
        self.revision = 0
        self.connected = False
        self.stopping = threading.Event()
        self.stream = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, name='page-changes', daemon=True)
        self.thread.start()

    def _run(self):
        token = None
        pipeline = [{'$match': {'ns.coll': {'$in': ['effects', 'audit', 'receipts', 'mandates', 'runs', 'tasks']}}}]
        while not self.stopping.is_set():
            try:
                options = dict(max_await_time_ms=500)
                if token:
                    options['resume_after'] = token
                with self.db.watch(pipeline, **options) as stream:
                    self.stream = stream
                    self.connected = True
                    self.revision += 1  # Rebuild on opening, even when the resume token expired.
                    while not self.stopping.is_set():
                        change = stream.try_next()
                        if stream.resume_token:
                            token = stream.resume_token
                        if change:
                            self.revision += 1
            except Exception as error:
                # Never expose connection details. A lost history token needs a fresh stream + rebuild.
                if isinstance(error, OperationFailure) and error.code in (136, 237, 260, 280, 286, 40573):
                    token = None
                self.connected = False
                self.revision += 1
                self.stopping.wait(.5)
            finally:
                self.stream = None
        self.connected = False

    def close(self):
        self.stopping.set()
        if self.stream is not None:
            try:
                self.stream.close()
            except Exception:
                pass
        if self.thread is not None:
            self.thread.join(timeout=2)
