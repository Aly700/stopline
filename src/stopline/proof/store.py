"""Small record store for protocol fakes, not a MongoDB emulator."""

from copy import deepcopy
from uuid import uuid4


class MemoryCollection:
    def __init__(self):
        self.documents = {}

    def insert_one(self, document):
        document = deepcopy(document)
        key = document.setdefault('_id', uuid4().hex)
        if key in self.documents:
            raise ValueError('duplicate record id')
        self.documents[key] = document

    def insert_many(self, documents):
        for document in documents:
            self.insert_one(document)

    def find(self, query=None):
        return [deepcopy(row) for row in self.documents.values()
                if all(row.get(key) == value for key, value in (query or {}).items())]

    def find_one(self, query=None, sort=None):
        rows = self.find(query)
        if sort:
            for field, direction in reversed(sort):
                rows.sort(key=lambda r: r.get(field, ''), reverse=direction < 0)
        return rows[0] if rows else None

    def replace_one(self, query, document, upsert=False):
        old = self.find_one(query)
        if old is not None:
            self.documents[old['_id']] = deepcopy(document)
        elif upsert:
            self.insert_one(document)

    def update_one(self, query, update):
        old = self.find_one(query)
        if old is not None:
            old.update(deepcopy(update['$set']))
            self.replace_one({'_id': old['_id']}, old)


class MemoryDatabase:
    name = 'in_memory_protocol_fakes'

    def __init__(self):
        self.collections = {}

    def __getitem__(self, name):
        return self.collections.setdefault(name, MemoryCollection())
