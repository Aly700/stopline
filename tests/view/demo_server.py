"""Local browser fixture. Uses only its own optional saved JSON records."""
import os
from pathlib import Path
import time
import threading
import asyncio

from stopline.contract import StageResult

from bson.json_util import JSONOptions, dumps, loads
from fastapi.responses import JSONResponse

from stopline.view import create_app
from .fakes import FakeDB, ScriptedStage, Target


db = FakeDB()
targets = {name: Target(name) for name in ('cluster', 'router')}
store = Path(os.environ['STOPLINE_VIEW_RECORDS']) if os.environ.get('STOPLINE_VIEW_RECORDS') else None


class SavedStage(ScriptedStage):
    def result(self, name, message, detail=None):
        result = super().result(name, message, detail)
        if store:
            store.write_text(dumps({'records': db.records, 'targets': {name: target.data for name, target in targets.items()}, 'serial': self.serial}))
        return result

    def press_stop(self):
        if getattr(self, 'defer_stop', False):
            self.defer_stop = False
            threading.Timer(.6, lambda: super(SavedStage, self).press_stop()).start()
            return StageResult(True, 'Stop requested.', pending=True, run_id=self.run['_id'])
        time.sleep(.35)  # Give the browser an observable pending interval.
        return super().press_stop()


stage = SavedStage(db, targets)
if store and store.exists():
    saved = loads(store.read_text(), json_options=JSONOptions(tz_aware=True))
    db.records = saved['records']
    stage.serial = saved['serial']
    stage.run = db.records['runs'][-1] if db.records['runs'] else None
    for name, data in saved['targets'].items():
        targets[name].data = data

app = create_app(db, stage, targets, numbers=lambda: [
    '40 schedules measured', 'Stop flag: 12 actions after stop', 'StopLine: 0 admitted after stop'])


@app.post('/fixture/defer-stop')
def defer_stop():
    stage.defer_stop = True
    return {'ok': True}


@app.get('/api/ask')
async def ask(q: str):
    """Scripted search responses for browser checks; no search service is called."""
    await asyncio.sleep(1 if q == 'slow' else .25)
    if q == 'missing':
        return JSONResponse({'detail': 'Not Found'}, status_code=404)
    engine = 'local ranking' if q == 'local' else 'atlas vector search'
    if q == 'error':
        return {'ok': False, 'engine': engine, 'rows': [], 'message': 'The audit index is not ready.'}
    if q == 'empty':
        return {'ok': True, 'engine': engine, 'rows': [], 'message': 'No matching audit entries.'}
    action = db.records['effects'][0] if db.records['effects'] else None
    sentences = [
        'Worker two asked to shift 25% of traffic to region B.',
        'The router took the request, but its answer was lost.',
        'The operator stopped the repair and its workers.',
        'Worker two resumed and its next request was refused.',
        'The router confirmed the earlier shift by action id.',
        'This sixth match must not appear.',
    ]
    if q == 'literal':
        sentences[0] = '<img src=x onerror=alert(1)> is audit text, not markup.'
    return {'ok': True, 'engine': engine, 'message': 'Found audit entries.', 'rows': [
        {'audit_id': f'fixture-{i}', 'action_id': action['_id'] if action and i == 0 else None,
         'kind': 'allow', 'at': '2026-11-14T20:00:00+00:00', 'text': sentence, 'score': round(.98 - i * .08, 2)}
        for i, sentence in enumerate(sentences)]}
