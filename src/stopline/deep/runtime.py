"""Saved authority and bounded repair actions, independent of model packages."""

from datetime import datetime, timezone
import json
import threading
import time

from stopline.contract import (
    Action, DEMO_PLAN, EffectState, OPERATOR, REPAIR_AGENT, Reason, TaskStatus,
    WORKER_ONE, WORKER_TWO, action_id_for,
)

MAX_TOOL_CALLS = 10
RUN_SECONDS = 120
PRINCIPALS = (REPAIR_AGENT, WORKER_ONE, WORKER_TWO)


def now():
    return datetime.now(timezone.utc)


class RunEnded(Exception):
    """A fixed, public reason; never carries provider or driver error text."""


def create_records(db, gate, run_id, model, cluster, tracing):
    at = now()
    db.runs.insert_one({
        '_id': run_id, 'mode': 'stopline', 'cluster': cluster, 'status': 'running',
        'root_mandate_id': None, 'agent_mandate_id': None, 'stop_flag': False,
        'stop_at': None, 'stop_at_local': None, 'holds': [], 'receipt_id': None,
        'planner': f'deep agent · {model}', 'created_at': at,
        **({'trace_project': 'stopline'} if tracing else {}),
    })
    for principal, parent in ((OPERATOR, None), (REPAIR_AGENT, OPERATOR),
                              (WORKER_ONE, REPAIR_AGENT), (WORKER_TWO, REPAIR_AGENT)):
        db.principals.update_one({'_id': principal}, {'$setOnInsert': {
            'started_by': parent, 'kind': 'person' if principal == OPERATOR else 'agent',
        }}, upsert=True)
    tools = sorted({str(s['tool']) for steps in DEMO_PLAN.values() for s in steps})
    targets = sorted({s['target'] for steps in DEMO_PLAN.values() for s in steps})
    root = gate.grant_root(run_id=run_id, grantor=OPERATOR, holder=OPERATOR,
                          tools=tools, targets=targets,
                          limits={'max_actions': 12, 'max_shift_percent': 100}, ttl_seconds=3600)
    db.runs.update_one({'_id': run_id}, {'$set': {'root_mandate_id': root['_id']}})
    repair = gate.hand_down(parent_id=root['_id'], holder=REPAIR_AGENT,
                            tools=tools, targets=targets,
                            limits={'max_actions': 4, 'max_shift_percent': 50}, ttl_seconds=1800)
    if not repair.allowed:
        raise RunEnded('The repair permission was refused.')
    db.runs.update_one({'_id': run_id}, {'$set': {'agent_mandate_id': repair.mandate_id}})
    tasks = [{
        '_id': f'{run_id}:{principal}', 'run_id': run_id, 'principal': principal,
        'mandate_id': repair.mandate_id if principal == REPAIR_AGENT else None,
        'plan': [], 'position': 0, 'planner': 'model', 'status': TaskStatus.RUNNING,
        'held_at': None, 'pid': None, 'updated_at': at,
        'note': 'Considering the checkout repair.' if principal == REPAIR_AGENT else 'Waiting for delegated repair work.',
    } for principal in PRINCIPALS]
    db.tasks.insert_many(tasks)
    for principal in (WORKER_ONE, WORKER_TWO):
        scope = {
            'tools': sorted({str(s['tool']) for s in DEMO_PLAN[principal]}),
            'targets': sorted({s['target'] for s in DEMO_PLAN[principal]}),
            'limits': {'max_actions': 2, 'max_shift_percent': 50 if principal == WORKER_TWO else 0},
        }
        child = gate.hand_down(parent_id=repair.mandate_id, holder=principal,
                               ttl_seconds=900, **scope)
        if not child.allowed:
            raise RunEnded('A worker permission was refused.')
        db.tasks.update_one({'_id': f'{run_id}:{principal}'}, {'$set': {
            'mandate_id': child.mandate_id, 'fixed_rules': scope,
        }})


class Run:
    def __init__(self, db, gate, sender, run_id):
        self.db, self.gate, self.sender, self.run_id = db, gate, sender, run_id
        self.started = time.monotonic()
        self.deadline = self.started + RUN_SECONDS
        self.lock = threading.RLock()
        self.halted = threading.Event()
        self.finished = threading.Event()
        self.finalized = False
        self.tool_calls = self.model_calls = 0
        self.reason = ''
        self.last_notes = {}
        self.counters = {WORKER_ONE: 0, WORKER_TWO: 0}
        self.trace = None
        self.actions_allowed = self.actions_refused = 0

    def check(self):
        if self.halted.is_set() or self.finished.is_set():
            raise RunEnded(self.reason or 'The repair run has ended.')
        if time.monotonic() >= self.deadline:
            self.halt('Stopped after the two-minute time limit.')
            raise RunEnded(self.reason)

    def halt(self, sentence):
        self.reason = self.reason or sentence
        self.halted.set()

    def count_tool(self):
        with self.lock:
            self.check()
            if self.tool_calls >= MAX_TOOL_CALLS:
                self.halt('Stopped after ten tool calls.')
                raise RunEnded(self.reason)
            self.tool_calls += 1

    def note(self, principal, sentence, **fields):
        self.last_notes[principal] = sentence
        self.db.tasks.update_one({'_id': f'{self.run_id}:{principal}'}, {'$set': {
            'note': sentence, 'updated_at': now(), **fields,
        }})

    def refuse(self, principal, result):
        if result.reason in (Reason.REVOKED, Reason.CANCELLED_BY_STOP):
            reason = 'Operator stopped the permission'
            at = result.detail.get('revoked_at')
            if isinstance(at, datetime):
                reason += f' at {at.isoformat()}'
        else:
            try:
                reason = Reason(result.reason).value.replace('_', ' ').replace('mandate', 'permission')
            except ValueError:
                reason = 'permission was refused'
        self.halt(f'Refused: {reason}.')
        self.note(principal, self.reason, status=TaskStatus.STOPPED)
        return f'REFUSED: {reason}. Do not retry. Report back.'

    def act(self, principal, tool, target, args, *, counted=False):
        # One action at a time across both subagents, including admission and send.
        with self.lock:
            self.check()
            if not counted:
                self.count_tool()
            position = self.counters[principal]
            self.counters[principal] += 1
            task = self.db.tasks.find_one({'_id': f'{self.run_id}:{principal}'})
            action = Action(action_id_for(self.run_id, principal, position), self.run_id,
                            task['mandate_id'], principal, tool, target, args)
            step = {'action_id': action.action_id, 'tool': tool, 'target': target, 'args': args}
            # Persist intent before admission, just as the saved workers do.
            steps = task['plan'] + [step]
            if tool == 'shift_traffic':
                asked = f'Model asked to shift {args["percent"]}% to region B.'
            else:
                name = target.split('/', 1)[-1]
                # Only known resource labels reach the page; arbitrary model text does not.
                name = name if name in ('checkout-api', 'checkout-worker') else 'the requested deployment'
                asked = f'Model asked to {"roll back" if tool == "rollback" else "restart"} {name}.'
            self.note(principal, asked, plan=steps, position=position)
            admission = self.gate.admit(action)
            if admission.allowed:
                self.actions_allowed += 1
            else:
                self.actions_refused += 1
            if self.trace:
                self.trace.decision(action, admission)
            if not admission.allowed:
                return self.refuse(principal, admission)
            self.check()
            claimed = self.sender.claim(action.action_id)
            if not claimed.claimed:
                self.actions_refused += 1
                if self.trace:
                    self.trace.decision(action, claimed, counter=admission.seq)
                return self.refuse(principal, claimed)
            self.check()
            try:
                effect = self.sender.send(action.action_id)
            except Exception:
                self.halt('The target did not confirm the action; its outcome needs checking.')
                self.note(principal, self.reason, status=TaskStatus.STOPPED)
                return 'UNKNOWN: The target did not confirm the action. Stop and report back.'
            applied = effect['state'] == EffectState.APPLIED
            outcome = 'Allowed and applied.' if applied else 'Allowed; the outcome needs checking.'
            self.note(principal, f'{asked} {outcome}', position=position + 1)
            if not applied:
                self.halt('Stopped because an action was not confirmed as applied.')
            # Return only evidence fields defined by the target contract, never full effects.
            evidence = effect.get('evidence') or {}
            safe = {key: evidence[key] for key in ('action_id', 'system', 'at_epoch', 'applied', 'log_id')
                    if key in evidence and isinstance(evidence[key], (str, int, float, bool))}
            return f'{outcome} Evidence: {json.dumps(safe, sort_keys=True)}'

    def finish(self):
        if self.finalized:
            return
        self.finished.set()
        status = TaskStatus.STOPPED if self.halted.is_set() else TaskStatus.DONE
        # Keep a page stop from being overwritten by a successful model return.
        saved = self.db.runs.find_one({'_id': self.run_id}) or {}
        if saved.get('stop_flag') or saved.get('status') == 'stopped':
            status = TaskStatus.STOPPED
        note = self.reason or ('The repair run stopped.' if status == TaskStatus.STOPPED else 'Finished the delegated repair.')
        for principal in PRINCIPALS:
            previous = self.last_notes.get(principal, '')
            sentence = previous if previous.startswith('Refused:') or previous.endswith(note) else f'{previous} {note}'.strip()
            self.note(principal, sentence, status=status, held_at=None)
        self.db.runs.update_one({'_id': self.run_id}, {'$set': {
            'status': str(status), 'model_calls': self.model_calls, 'tool_calls': self.tool_calls,
            'seconds': round(time.monotonic() - self.started, 3),
        }})
        self.finalized = True
