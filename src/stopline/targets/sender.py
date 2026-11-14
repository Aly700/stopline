"""Claims authorize one delivery attempt; uncertain attempts are reconciled."""

from collections.abc import Mapping
import threading

from stopline.contract import (Action, Effect, EffectState, GateAPI, NotClaimed, TargetAdapter,
                               TargetTimeout, TargetUnreachable, system_of)


class TargetSender:
    def __init__(self, gate: GateAPI, targets: Mapping[str, TargetAdapter], sender_id='sender'):
        self.gate = gate
        self.targets = targets
        self.sender_id = sender_id
        self._claims = set()
        self._lock = threading.Lock()

    def claim(self, action_id):
        claim = self.gate.claim(action_id, by=self.sender_id)
        if claim.claimed:
            with self._lock:
                self._claims.add(action_id)
        return claim

    def _effect(self, action_id) -> Effect:
        effect = self.gate.chain(action_id).effect
        if effect is None:
            raise NotClaimed(action_id)
        return effect

    def send(self, action_id) -> Effect:
        with self._lock:
            if action_id not in self._claims:
                raise NotClaimed(action_id)
            self._claims.remove(action_id)
        effect = self._effect(action_id)
        if effect['state'] != EffectState.SENT or effect['claimed_by'] != self.sender_id:
            raise NotClaimed(action_id)
        action = Action(action_id, effect['run_id'], effect['mandate_id'], effect['principal'],
                        effect['tool'], effect['target'], effect['args'], effect.get('reverses'))
        target = self.targets[system_of(action.target)]
        try:
            answer = target.send(action)
        except (TargetTimeout, TargetUnreachable):
            return effect
        state = EffectState.APPLIED if answer.applied else EffectState.REJECTED
        return self.gate.record_outcome(action_id, state, answer.evidence)

    def reconcile(self, action_id) -> Effect:
        effect = self._effect(action_id)
        if effect['state'] != EffectState.SENT:
            return effect
        target = self.targets[system_of(effect['target'])]
        try:
            lookup = target.lookup(action_id)
            if lookup.found:
                record, applied = lookup.evidence, lookup.applied
            else:
                record = target.fence(action_id)
                # A concurrent send can commit between lookup and fence.
                applied = record.get('applied', False)
        except (TargetTimeout, TargetUnreachable):
            return effect
        state = EffectState.APPLIED if applied else EffectState.REJECTED
        return self.gate.record_outcome(action_id, state, record)
