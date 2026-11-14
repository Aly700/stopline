"""Local browser fixture. Uses only its own optional saved JSON records."""
import os
from pathlib import Path
import time

from bson.json_util import dumps, loads

from stopline.view import create_app
from .fakes import FakeDB, ScriptedStage, Target


db = FakeDB()
targets = {name: Target(name) for name in ('cluster', 'router')}
store = Path(os.environ['STOPLINE_VIEW_RECORDS']) if os.environ.get('STOPLINE_VIEW_RECORDS') else None


class SavedStage(ScriptedStage):
    def result(self, name, message):
        result = super().result(name, message)
        if store:
            store.write_text(dumps({'records': db.records, 'targets': {name: target.data for name, target in targets.items()}, 'serial': self.serial}))
        return result

    def press_stop(self):
        time.sleep(.35)  # Give the browser an observable pending interval.
        return super().press_stop()


stage = SavedStage(db, targets)
if store and store.exists():
    saved = loads(store.read_text())
    db.records = saved['records']
    stage.serial = saved['serial']
    stage.run = db.records['runs'][-1] if db.records['runs'] else None
    for name, data in saved['targets'].items():
        targets[name].data = data

app = create_app(db, stage, targets, numbers=lambda: [
    '40 schedules measured', 'Stop flag: 12 actions after stop', 'StopLine: 0 admitted after stop'])
