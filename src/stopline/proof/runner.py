"""Execute named interleavings; compute results only from captured records."""

import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from stopline.contract import (
    Action, DEMO_PLAN, HoldPoint, OPERATOR, REPAIR_AGENT, Tool, WORKER_ONE, WORKER_TWO,
    TargetTimeout, TargetUnreachable, action_id_for, system_of,
)
from stopline.proof.backend import SOURCES, UnsupportedSchedule, build_runtime
from stopline.proof.fakes import effect_for
from stopline.proof.metrics import DEFINITIONS, INVARIANTS, aggregate, measure
from stopline.proof.schedules import BOUNDS, schedules


def utcnow():
    return datetime.now(timezone.utc)


class Driver:
    def __init__(self, runtime, case, mode, run_id):
        self.runtime, self.db, self.case, self.mode, self.run_id = runtime, runtime.db, case, mode, run_id
        self.gate, self.sender, self.targets = runtime.gate, runtime.sender, runtime.targets
        self.root_id, self.repair_id = f'{run_id}-root', f'{run_id}-repair'
        self.worker_ids = {worker: f'{run_id}-{worker.split(":")[1]}' for worker in DEMO_PLAN}
        self.run = dict(_id=run_id, mode=mode, cluster='mock', status='running',
                        root_mandate_id=self.root_id if mode == 'stopline' else None,
                        agent_mandate_id=self.repair_id if mode == 'stopline' else None,
                        stop_flag=False, stop_at=None, stop_at_local=None, holds=[], receipt_id=None,
                        planner='scripted', created_at=utcnow())
        if case.kind == 'hold':
            self.run['holds'] = [dict(principal=case.worker, step=case.step, point=str(case.point), released=False)]
        self.db['runs'].insert_one(self.run)
        if mode == 'stopline':
            self.gate.setup()
            self.gate.grant_root(run_id=run_id, grantor=OPERATOR, holder=OPERATOR,
                                 **self.scope(16), ttl_seconds=3600, mandate_id=self.root_id)
            self.gate.hand_down(parent_id=self.root_id, holder=REPAIR_AGENT,
                                **self.scope(12), ttl_seconds=3600, mandate_id=self.repair_id)
            for worker in DEMO_PLAN:
                if case.kind != 'hand_down' or worker != WORKER_TWO:
                    self.grant_worker(worker)

    @staticmethod
    def scope(limit):
        return dict(tools=[str(t) for t in Tool], targets=[s['target'] for plan in DEMO_PLAN.values() for s in plan],
                    limits={'max_actions': limit, 'max_shift_percent': 100})

    def grant_worker(self, worker):
        return self.gate.hand_down(parent_id=self.repair_id, holder=worker,
                                   **self.scope(6), ttl_seconds=3600, mandate_id=self.worker_ids[worker])

    def action(self, worker, step):
        return Action(action_id_for(self.run_id, worker, step), self.run_id, self.worker_ids[worker], worker,
                      **DEMO_PLAN[worker][step])

    def _save_run(self):
        self.db['runs'].replace_one({'_id': self.run_id}, self.run)

    def stop(self):
        if self.run['stop_at_local'] is not None:
            return
        if self.mode == 'stopline':
            receipt = self.gate.stop(mandate_id=self.repair_id, by=OPERATOR)
            self.run.update(receipt_id=receipt['_id'], stop_at=receipt['stopped_at'], stop_seq=receipt['stop_seq'])
        else:
            self.run['stop_at'] = utcnow()
        self.run.update(stop_flag=True, stop_at_local=time.time(), status='stopped')
        for hold in self.run['holds']:
            hold['released'] = True
        self._save_run()
        self.observe('after_stop')

    def capture(self):
        data = {'run': self.db['runs'].find_one({'_id': self.run_id})}
        for collection in ('effects', 'receipts', 'audit', 'mandates'):
            data[collection] = list(self.db[collection].find({'run_id': self.run_id}))
        for short in ('sends', 'timings', 'observations'):
            data[short] = list(self.db[f'proof_{short}'].find({'run_id': self.run_id}))
        logs = []
        for target in self.targets.values():
            state = target.state()
            if 'log' not in state:
                raise UnsupportedSchedule(f'{target.name} does not expose a complete target-owned journal')
            logs.extend(row for row in state['log'] if row['action_id'].startswith(self.run_id + '-'))
        data['target_logs'] = logs
        return data

    def observe(self, event):
        data = self.capture()
        self.db['proof_observations'].insert_one(dict(
            run_id=self.run_id, event=event, receipts=data['receipts'], effects=data['effects'],
            target_logs=data['target_logs'], sends=data['sends'], at_epoch=time.time(),
        ))

    def _timing(self, worker, step, duration_ms):
        key = f'{worker}:{step}'
        collection = self.db['proof_timings']
        previous = collection.find_one({'run_id': self.run_id, 'key': key})
        if previous:
            previous['duration_ms'] += duration_ms
            collection.replace_one({'_id': previous['_id']}, previous)
        else:
            collection.insert_one(dict(run_id=self.run_id, key=key, duration_ms=duration_ms))

    def step(self, worker, step, *, hold=None, dispatch=None, returned=None, claim_only=False):
        action = self.action(worker, step)
        if hold == HoldPoint.BEFORE_CHECK:
            self.stop()
        started = time.perf_counter_ns()
        if self.mode == 'stopline':
            allowed = self.gate.admit(action).allowed
        elif self.mode == 'flag':
            allowed = not self.db['runs'].find_one({'_id': self.run_id})['stop_flag']
        else:
            allowed = True
        self._timing(worker, step, (time.perf_counter_ns() - started) / 1e6)
        if not allowed:
            return
        if self.mode != 'stopline':
            self.db['effects'].insert_one(effect_for(action, self.mode))
        if hold == HoldPoint.AFTER_CHECK:
            self.stop()
        if self.mode == 'stopline':
            started = time.perf_counter_ns()
            claimed = self.sender.claim(action.action_id).claimed
            self._timing(worker, step, (time.perf_counter_ns() - started) / 1e6)
            if not claimed:
                return
        if hold == HoldPoint.AFTER_CLAIM:
            self.stop()
        if claim_only:
            return
        target = self.targets[system_of(action.target)]
        if dispatch:
            target.on_dispatch[action.action_id] = dispatch
        if hold == HoldPoint.AFTER_SEND or returned:
            target.on_return[action.action_id] = returned or self.stop
        if self.mode == 'stopline':
            self.sender.send(action.action_id)
        else:
            effect = self.db['effects'].find_one({'_id': action.action_id})
            effect.update(state='sent', sent_at=utcnow())
            self.db['effects'].replace_one({'_id': action.action_id}, effect)
            try:
                result = target.send(action)
            except (TargetTimeout, TargetUnreachable):
                return
            effect.update(state='applied' if result.applied else 'rejected', evidence=result.evidence, finished_at=utcnow())
            self.db['effects'].replace_one({'_id': action.action_id}, effect)

    def reconcile(self, action_id):
        if self.mode == 'stopline':
            return self.sender.reconcile(action_id)
        effect = self.db['effects'].find_one({'_id': action_id})
        try:
            found = self.targets[system_of(effect['target'])].lookup(action_id)
        except TargetUnreachable:
            return effect
        if found.found:
            effect.update(state='applied' if found.applied else 'rejected', evidence=found.evidence, finished_at=utcnow())
            self.db['effects'].replace_one({'_id': action_id}, effect)
        return effect

    def execute(self):
        case = self.case
        if case.kind == 'hold':
            for worker in case.order:
                for step in range(len(DEMO_PLAN[worker])):
                    point = case.point if (worker, step) == (case.worker, case.step) else None
                    self.step(worker, step, hold=point)
        elif case.kind == 'parent_stop':
            if case.first == 'stop':
                self.stop()
            self.step(WORKER_ONE, 0, hold=HoldPoint.AFTER_CHECK if case.first == 'admit' else None)
        elif case.kind == 'hand_down':
            if case.first == 'stop':
                self.stop()
            self.grant_worker(WORKER_TWO)
            self.stop()
            if self.db['mandates'].find_one({'_id': self.worker_ids[WORKER_TWO]}):
                self.step(WORKER_TWO, 0)
        elif case.kind == 'expiry':
            self.step(WORKER_ONE, 0)
            self.runtime.expire()
            self.step(WORKER_ONE, 1)
        elif case.kind == 'retry':
            self.runtime.force_retry(self.stop)
            self.step(WORKER_ONE, 0)
        elif case.kind == 'paused_send':
            self.step(WORKER_TWO, 0, dispatch=self.stop)
        elif case.kind in ('lost_reply', 'router_restart'):
            self.targets['router'].cut()
            self.step(WORKER_TWO, 0)
            self.stop()
            action_id = self.action(WORKER_TWO, 0).action_id
            self.reconcile(action_id)
            self.observe('while_unreachable')
            if case.kind == 'router_restart':
                self.runtime.restart_router()
            else:
                self.targets['router'].restore()
            self.reconcile(action_id)
            if case.kind == 'router_restart':
                self.observe('after_restart')
        elif case.kind == 'late_duplicate':
            self.step(WORKER_TWO, 0)
            self.stop()
            self.targets['router'].redeliver(self.action(WORKER_TWO, 0).action_id)
        elif case.kind == 'receipt_closure':
            self.step(WORKER_TWO, 0, claim_only=True)
            self.stop()
            self.observe('before_reconcile')
            action_id = self.action(WORKER_TWO, 0).action_id
            self.reconcile(action_id)
            self.observe('after_fence')
            # Release the previously claimed request after reconciliation fenced it.
            self.sender.send(action_id)
        self.stop()
        self.run['status'] = 'done'
        self._save_run()
        self.observe('finished')
        return self.capture()


def checks_for(case, mode, data):
    checks = {'stop_recorded': data['run']['stop_at_local'] is not None}
    effects = data['effects']
    if mode == 'stopline':
        checks['receipt_exists'] = len(data['receipts']) == 1
        checks['receipt_closed'] = all(r['status'] == 'closed' for r in data['receipts'])
        if case.kind == 'retry':
            checks['retry_rechecked_authority'] = any(
                a['kind'] == 'deny' and a['reason'] == 'revoked' and a['detail'].get('attempts') == 2 for a in data['audit'])
            checks['no_effect_from_aborted_attempt'] = not effects
        if case.kind == 'expiry':
            checks['expiry_refused'] = any(a['kind'] == 'deny' and a['reason'] == 'expired' for a in data['audit'])
            checks['earlier_action_applied'] = any(e['state'] == 'applied' for e in effects)
        if case.kind == 'hand_down':
            checks['no_active_descendant_after_stop'] = all(m['status'] == 'revoked' for m in data['mandates'] if m['depth'] > 0)
        if case.kind in ('lost_reply', 'router_restart', 'receipt_closure'):
            snapshots = [o for o in data['observations'] if o['event'] in ('while_unreachable', 'before_reconcile')]
            checks['open_while_unknown'] = bool(snapshots) and all(
                any(r['status'] == 'open' for r in o['receipts']) and any(e['state'] == 'sent' for e in o['effects']) for o in snapshots)
            checks['closed_after_reconcile'] = all(r['status'] == 'closed' for r in data['receipts'])
        if case.kind == 'receipt_closure':
            checks['fenced_late_arrival'] = any(row['event'] == 'fenced' for row in data['target_logs']) and not any(
                row['event'] == 'applied' for row in data['target_logs'])
    if case.kind == 'router_restart':
        checks['durable_after_restart'] = bool(effects) and all(e['state'] == 'applied' and e['evidence'] for e in effects)
    return checks


def skip_reason(case, mode, backend):
    if mode != 'stopline' and case.kind == 'hold' and case.point == HoldPoint.AFTER_CLAIM:
        return 'baseline has no claim operation'
    if mode != 'stopline' and case.kind in ('hand_down', 'retry', 'receipt_closure'):
        return f'baseline has no {case.kind} operation'
    if backend == 'real' and case.kind in ('expiry', 'retry', 'router_restart'):
        return 'real fault hook not integrated; see sources and model bounds'
    return None


def finalize(result):
    for row in result['rows']:
        if row['status'] == 'skipped' or 'evidence' not in row:
            continue
        row['counts'] = measure(row['evidence'])
        row['checks'] = checks_for(next(s for s in schedules() if s.name == row['name']), row['mode'], row['evidence'])
        clean = row['mode'] != 'stopline' or all(row['counts'][key] == 0 for key in INVARIANTS)
        row['status'] = 'passed' if clean and all(row['checks'].values()) and not row.get('error') else 'failed'
    result['totals'] = aggregate(result['rows'])
    for status in ('passed', 'failed', 'skipped'):
        result[status] = [f"{r['mode']}:{r['name']}" for r in result['rows'] if r['status'] == status]
    result['run'] = [f"{r['mode']}:{r['name']}" for r in result['rows'] if r['status'] != 'skipped']
    result['flag_counterexamples'] = [r['name'] for r in result['rows'] if r['mode'] == 'flag'
                                      and r.get('counts', {}).get('started_after_stop_unsent', 0) > 0]
    result['complete'] = not result['failed'] and bool(result['flag_counterexamples'])
    return result


def run_suite(*, work_dir=None, backend='fake', scratch_db=None, router_url='http://127.0.0.1:8766'):
    if work_dir is None:
        with TemporaryDirectory(prefix='stopline-proof-') as directory:
            return run_suite(work_dir=Path(directory), backend=backend, scratch_db=scratch_db, router_url=router_url)
    work_dir = Path(work_dir)
    started = time.monotonic()
    result = dict(_id=uuid4().hex, created_at=utcnow(), backend=backend, schema_version=1,
                  bounds=BOUNDS, definitions=DEFINITIONS, sources=dict(SOURCES) if backend == 'fake' else {
                      'gate': 'build_gate(scratch_db)', 'sender': 'build_sender',
                      'cluster': 'build_targets(cluster=mock)', 'router': 'existing local router',
                      'workers': 'deterministic worker routines, not production worker processes',
                      'limitations': 'expiry/retry/restart hooks unavailable; complete target journals required',
                  }, schedule_count=len(schedules()), rows=[])
    for mode in ('stopline', 'flag', 'fixed'):
        for index, case in enumerate(schedules()):
            row = dict(name=case.name, mode=mode, schedule=asdict(case), status='skipped')
            reason = skip_reason(case, mode, backend)
            if reason:
                row['reason'] = reason
                result['rows'].append(row)
                continue
            runtime = None
            try:
                runtime = build_runtime(backend=backend, work_dir=work_dir / f'{mode}-{index}',
                                        db=scratch_db if backend == 'real' else None, router_url=router_url)
                driver = Driver(runtime, case, mode, f'{result["_id"][:8]}-{mode}-{index}')
                row['evidence'] = driver.execute()
                row['status'] = 'passed'
            except UnsupportedSchedule as error:
                row['reason'] = str(error)
            except Exception as error:
                # Database errors may include connection strings; persist only the class.
                row.update(status='failed', error=type(error).__name__)
            finally:
                if runtime:
                    runtime.close()
            result['rows'].append(row)
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    result['under_six_minutes'] = result['elapsed_seconds'] < 360
    return finalize(result)
