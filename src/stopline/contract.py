"""Shared names, record shapes and protocols.

Every part of StopLine builds against this file. Records are stored in MongoDB
as plain dicts whose keys are the field names below. Results that cross a
package boundary are small dataclasses.

Packages and what each must export:

    stopline.gate      build_gate(db) -> GateAPI
    stopline.targets   build_targets(cluster="kind" | "mock", router_url=ROUTER_URL) -> dict[str, TargetAdapter]
                       build_sender(gate, targets, sender_id="sender") -> Sender
                       python -m stopline.targets.router --port 8766 --store <sqlite file>
    stopline.agents    build_stage(db, gate, targets, sender, cluster="kind" | "mock") -> StageAPI
    stopline.view      create_app(db, stage, targets, numbers=None) -> FastAPI app
    stopline.proof     summary(db) -> list[str] (three lines), and python -m stopline.proof

The pieces are wired together in stopline.app. Worker processes reach the
sender through a small HTTP client that satisfies the Sender protocol, so a
worker in stopline mode never calls a target system itself.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, NotRequired, Protocol, TypedDict

# ---------------------------------------------------------------- names

DB_NAME = "stopline"
PAGE_PORT = int(os.environ.get("STOPLINE_PAGE_PORT", "8785"))      # the environment override exists for tests
ROUTER_PORT = int(os.environ.get("STOPLINE_ROUTER_PORT", "8766"))
ROUTER_URL = f"http://127.0.0.1:{ROUTER_PORT}"
KUBE_CONTEXT = "kind-stopline"
KUBE_NAMESPACE = "checkout"

MAX_TREE_SIZE = 16  # mandates under one root, enforced by hand-down and by stop
MAX_DEPTH = 3       # root is depth 0

PRINCIPALS = "principals"
MANDATES = "mandates"
POLICIES = "policies"
EFFECTS = "effects"
AUDIT = "audit"
RECEIPTS = "receipts"
TASKS = "tasks"
RUNS = "runs"

OPERATOR = "person:operator"
REPAIR_AGENT = "agent:repair"
WORKER_ONE = "agent:worker-1"
WORKER_TWO = "agent:worker-2"

CLUSTER = "cluster"
ROUTER = "router"
TARGET_API = "cluster/checkout-api"
TARGET_WORKER = "cluster/checkout-worker"
TARGET_REGION_B = "router/region-b"


def system_of(target: str) -> str:
    """'cluster/checkout-api' -> 'cluster'. The key into the targets mapping."""
    return target.split("/", 1)[0]


def resource_of(target: str) -> str:
    """'cluster/checkout-api' -> 'checkout-api'."""
    return target.split("/", 1)[1]


class Tool(StrEnum):
    ROLLBACK = "rollback"      # cluster: set a Deployment's image to the good version. args {"to_version": "v1"}
    RESTART = "restart"        # cluster: restart a Deployment. args {}
    SHIFT = "shift_traffic"    # router: set region B's share of traffic. args {"percent": 25}


class Mode(StrEnum):
    STOPLINE = "stopline"  # every action goes through the gate
    FLAG = "flag"          # baseline: workers read a stop flag before each step and call targets directly
    FIXED = "fixed"        # baseline: the rules are read once when the run starts


class EffectState(StrEnum):
    ADMITTED = "admitted"    # allowed, nothing sent
    SENT = "sent"            # claimed for sending; "unknown" on the page until there is evidence
    APPLIED = "applied"      # terminal: the target gave evidence that it took effect
    REJECTED = "rejected"    # terminal: the target gave evidence of no effect, and holds a fence for the id
    CANCELLED = "cancelled"  # terminal: never sent


TERMINAL_STATES = frozenset({EffectState.APPLIED, EffectState.REJECTED, EffectState.CANCELLED})


class MandateStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class AuditKind(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    HAND_DOWN = "hand_down"
    STOP = "stop"
    CLAIM = "claim"
    OUTCOME = "outcome"
    UNDO = "undo"


class Reason(StrEnum):
    OK = "ok"
    REVOKED = "revoked"                        # the mandate or one above it was stopped
    EXPIRED = "expired"                        # judged by server time at the decision
    UNKNOWN_MANDATE = "unknown_mandate"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    TARGET_NOT_ALLOWED = "target_not_allowed"
    LIMIT_EXCEEDED = "limit_exceeded"
    WIDER_THAN_PARENT = "wider_than_parent"
    TREE_TOO_LARGE = "tree_too_large"
    SAME_ID_DIFFERENT_ARGS = "same_id_different_args"
    CANCELLED_BY_STOP = "cancelled_by_stop"    # a claim on an effect the stop cancelled
    NOT_ADMITTED = "not_admitted"              # a claim or send on something that is not in the right state
    NOT_CLAIMED = "not_claimed"
    NOT_AUTHORISED = "not_authorised"          # for example a stop by someone who holds nothing above the mandate
    NO_UNDO = "no_undo"
    STATE_CHANGED = "state_changed_since"
    FENCED = "fenced"
    TARGET_UNREACHABLE = "target_unreachable"


class HoldPoint(StrEnum):
    """Where the test schedule can hold a worker inside one step."""
    BEFORE_CHECK = "before_check"
    AFTER_CHECK = "after_check"    # after admit in stopline mode; after reading the flag or the fixed rules in a baseline
    AFTER_CLAIM = "after_claim"    # stopline mode only
    AFTER_SEND = "after_send"      # the request has gone to the target; the outcome is not recorded yet


class TaskStatus(StrEnum):
    RUNNING = "running"
    HELD = "held"
    STOPPED = "stopped"   # refused, or heard the stop
    DONE = "done"
    KILLED = "killed"


class UndoMark(StrEnum):
    UNDONE = "undone"
    UNKNOWN = "undo_unknown"
    NO_UNDO = "no_undo"


def args_hash(args: Mapping[str, Any]) -> str:
    """One way to hash arguments, used by the gate and by every target."""
    return hashlib.sha256(json.dumps(args, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def action_id_for(run_id: str, principal: str, step: int) -> str:
    """The caller fixes the id before anything else happens: 'r7f3a-worker-1-0'."""
    return f"{run_id}-{principal.split(':', 1)[1]}-{step}"


# ---------------------------------------------------------------- records


class Limits(TypedDict):
    max_actions: int        # shared: the children's limits together cannot exceed the parent's
    max_shift_percent: int  # ceiling: a child's cannot exceed the parent's


class Mandate(TypedDict):
    _id: str
    run_id: str
    root_id: str                 # never changes; equals _id on the root
    parent_id: str | None        # never changes
    depth: int
    grantor: str                 # principal id
    holder: str                  # principal id
    tools: list[str]
    targets: list[str]
    limits: Limits
    expires_at: datetime
    status: str                  # MandateStatus
    revoked_by: str | None
    revoked_at_seq: int | None
    revoked_at: datetime | None  # server time
    created_at: datetime
    seq: NotRequired[int]        # root only: the shared counter for the whole tree
    clock: NotRequired[datetime] # root only: server time of the latest tick


class Policy(TypedDict):
    _id: int          # version
    text: str         # the full rule text, never edited after insert
    hash: str
    created_at: datetime


class Effect(TypedDict):
    _id: str                    # the action id, fixed by the caller before any transaction
    run_id: str
    mode: str                   # Mode
    root_id: str | None         # None in the baseline modes
    mandate_id: str | None
    chain: list[str]            # mandate ids from the root down to mandate_id, fixed at admission
    principal: str
    tool: str
    target: str
    args: dict[str, Any]
    args_hash: str
    admitted_seq: int | None    # the counter value this action was admitted at
    state: str                  # EffectState
    evidence: dict[str, Any] | None
    reverses: str | None        # for an undo: the action it reverses
    undo: str | None            # on a reversed or irreversible action: UndoMark
    policy_version: int | None
    policy_hash: str | None
    claimed_by: str | None
    admitted_at: datetime
    sent_at: datetime | None
    finished_at: datetime | None


class AuditEntry(TypedDict):
    _id: Any
    run_id: str
    kind: str                   # AuditKind
    principal: str              # who acted or asked
    mandate_id: str | None
    root_id: str | None
    action_id: str | None
    policy_version: int
    policy_hash: str
    reason: str                 # Reason
    seq: int | None             # the counter value this decision took or saw
    at: datetime                # server time
    detail: dict[str, Any]      # tool, target, args, revoked_by, revoked_at and the like


class Receipt(TypedDict):
    _id: str
    run_id: str
    root_id: str
    mandate_id: str             # the mandate that was stopped
    stopped_by: str
    stop_seq: int
    stopped_at: datetime        # server time
    revoked: list[str]          # every mandate this stop revoked
    covered: list[str]          # action ids, fixed when the stop commits
    status: str                 # "open" | "closed"
    closed_at: datetime | None


class Step(TypedDict):
    action_id: str
    tool: str
    target: str
    args: dict[str, Any]


class Task(TypedDict):
    _id: str                    # f"{run_id}:{principal}"
    run_id: str
    principal: str
    mandate_id: str | None
    plan: list[Step]
    position: int               # index of the next step to attempt
    planner: str                # "scripted" | "saved" | "model"
    status: str                 # TaskStatus
    held_at: dict[str, Any] | None   # {"step": 1, "point": "after_check"}
    pid: int | None
    note: str                   # the last thing that happened, in plain words
    updated_at: datetime


class Hold(TypedDict):
    principal: str
    step: int
    point: str                  # HoldPoint
    released: bool


class Run(TypedDict):
    _id: str
    mode: str                   # Mode
    cluster: str                # "kind" | "mock"
    status: str                 # "running" | "stopped" | "done"
    root_mandate_id: str | None     # the operator's own grant; undo runs under it
    agent_mandate_id: str | None    # the repair agent's mandate; the stop key revokes this one
    stop_flag: bool                 # what the flag baseline reads
    stop_at: datetime | None        # server time
    stop_at_local: float | None     # time.time() on this machine, comparable with the targets' own logs
    holds: list[Hold]
    receipt_id: str | None
    planner: str
    created_at: datetime


# The schedule the demo uses. A stop releases every hold; cut_and_kill releases worker two's.
DEMO_HOLDS: dict[str, list[Hold]] = {
    Mode.FLAG: [
        {"principal": WORKER_ONE, "step": 1, "point": HoldPoint.AFTER_CHECK, "released": False},
        {"principal": WORKER_TWO, "step": 1, "point": HoldPoint.BEFORE_CHECK, "released": False},
    ],
    Mode.FIXED: [
        {"principal": WORKER_ONE, "step": 1, "point": HoldPoint.AFTER_CHECK, "released": False},
        {"principal": WORKER_TWO, "step": 1, "point": HoldPoint.BEFORE_CHECK, "released": False},
    ],
    Mode.STOPLINE: [
        {"principal": WORKER_ONE, "step": 1, "point": HoldPoint.AFTER_CHECK, "released": False},
        {"principal": WORKER_TWO, "step": 0, "point": HoldPoint.AFTER_CLAIM, "released": False},
        {"principal": WORKER_TWO, "step": 1, "point": HoldPoint.BEFORE_CHECK, "released": False},
    ],
}

# The work the demo does. Worker one: roll back the bad release, then restart the worker
# Deployment. Worker two: move 25% of traffic to region B, then 50%.
DEMO_PLAN: dict[str, list[dict[str, Any]]] = {
    WORKER_ONE: [
        {"tool": Tool.ROLLBACK, "target": TARGET_API, "args": {"to_version": "v1"}},
        {"tool": Tool.RESTART, "target": TARGET_WORKER, "args": {}},
    ],
    WORKER_TWO: [
        {"tool": Tool.SHIFT, "target": TARGET_REGION_B, "args": {"percent": 25}},
        {"tool": Tool.SHIFT, "target": TARGET_REGION_B, "args": {"percent": 50}},
    ],
}


# ---------------------------------------------------------------- values that cross boundaries


@dataclass(frozen=True)
class Action:
    action_id: str
    run_id: str
    mandate_id: str
    principal: str
    tool: str
    target: str
    args: dict[str, Any]
    reverses: str | None = None


@dataclass(frozen=True)
class Admission:
    allowed: bool
    action_id: str
    reason: str                     # Reason
    seq: int | None = None          # the admission sequence, or the counter value a refusal saw
    detail: dict[str, Any] = field(default_factory=dict)   # on a refusal: revoked_by, revoked_at, revoked_at_seq
    attempts: int = 1               # how many times the transaction callback ran
    replayed: bool = False          # True when the same id and arguments were admitted before


@dataclass(frozen=True)
class HandDown:
    allowed: bool
    mandate_id: str | None
    reason: str
    seq: int | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    attempts: int = 1


@dataclass(frozen=True)
class Claim:
    claimed: bool
    action_id: str
    state: str                      # the effect's state after the attempt
    reason: str = Reason.OK
    detail: dict[str, Any] = field(default_factory=dict)   # on a refusal: who stopped it and when


@dataclass(frozen=True)
class SendResult:
    """What a target answered. evidence always carries action_id, system and at_epoch (the target's own clock)."""
    applied: bool
    evidence: dict[str, Any]
    reason: str = Reason.OK


@dataclass(frozen=True)
class Lookup:
    found: bool                     # the target has a record of this id
    applied: bool                   # and it took effect
    evidence: dict[str, Any]


@dataclass(frozen=True)
class Inverse:
    tool: str
    target: str
    args: dict[str, Any]
    expect: dict[str, Any]          # what the target's state must still show for the undo to go ahead


@dataclass(frozen=True)
class ReceiptRow:
    action_id: str
    principal: str
    tool: str
    target: str
    args: dict[str, Any]
    outcome: str                    # "completed" | "unknown" | "blocked" | "no effect"
    state: str | None               # the effect's state; None when the row is a refusal after the stop
    reason: str
    chain: list[dict[str, Any]]     # root first: {"mandate_id", "holder", "grantor"}
    evidence: dict[str, Any] | None
    undo: str | None                # UndoMark


@dataclass(frozen=True)
class ReceiptView:
    receipt: Receipt
    rows: list[ReceiptRow]          # covered actions in admission order, then refusals after the stop
    open: bool


@dataclass(frozen=True)
class ChainView:
    action_id: str
    effect: Effect | None
    mandates: list[Mandate]         # root first
    policy: Policy | None
    audit: list[AuditEntry]         # every entry for this action, oldest first


@dataclass(frozen=True)
class StageResult:
    ok: bool
    message: str                    # one plain sentence for the page
    pending: bool = False           # True when the call returned before the database confirmed
    run_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class TargetUnreachable(Exception):
    """The target did not accept the connection. Nothing was applied by this call."""


class TargetTimeout(Exception):
    """The request went out and no answer came back. It may or may not have been applied."""


class NotAuthorised(Exception):
    """The caller holds nothing that allows this."""


class NotClaimed(Exception):
    """The sender was asked to send an effect it has not claimed."""


# ---------------------------------------------------------------- protocols


class TargetAdapter(Protocol):
    """One system that agents act on. Only the sender calls send, lookup and fence in stopline mode."""

    name: str  # CLUSTER or ROUTER

    def send(self, action: Action) -> SendResult:
        """Apply the action once, keyed by action_id. A repeat with the same arguments returns the first
        result; a repeat with different arguments, or a fenced id, returns applied=False.
        Raises TargetUnreachable or TargetTimeout."""

    def lookup(self, action_id: str) -> Lookup:
        """What the target's own records say about this id. Raises TargetUnreachable."""

    def fence(self, action_id: str) -> dict[str, Any]:
        """Make the target refuse this id from now on, and return evidence of that. Raises TargetUnreachable."""

    def state(self) -> dict[str, Any]:
        """The target in its own words, for the page. Never raises; says reachable False instead.
        cluster: {"system", "reachable", "deployments": {name: {"image", "version", "revision",
                  "restarted_at", "action_id", "ready"}}}
        router:  {"system", "reachable", "weights": {"region-a": 75, "region-b": 25}, "last_action_id", "log": [...]}"""

    def inverse_of(self, action: Action) -> Inverse | None:
        """The action that reverses this one, or None when there is no undo."""

    def cut(self) -> None:
        """Test switch: apply the next send without answering, then be unreachable until restore()."""

    def restore(self) -> None:
        """Undo cut()."""


class GateAPI(Protocol):
    """The check. Hand-down, admit and stop each run in one transaction that increments the root
    mandate's counter, so no two of them can commit from the same snapshot."""

    def setup(self) -> None:
        """Create collections, indexes, the audit validator and the first policy version. Safe to repeat."""

    def grant_root(self, *, run_id: str, grantor: str, holder: str, tools: list[str], targets: list[str],
                   limits: Limits, ttl_seconds: int, mandate_id: str | None = None) -> Mandate: ...

    def hand_down(self, *, parent_id: str, holder: str, tools: list[str], targets: list[str],
                  limits: Limits, ttl_seconds: int, mandate_id: str | None = None) -> HandDown: ...

    def admit(self, action: Action) -> Admission:
        """Allowed: the effect exists as admitted and an allow entry is in the audit, committed together.
        Refused: nothing is created, and a deny entry is written as its own insert."""

    def claim(self, action_id: str, by: str) -> Claim:
        """admitted -> sent in one conditional update. by names the sender."""

    def stop(self, *, mandate_id: str, by: str) -> Receipt:
        """Revoke the mandate and everything below it, cancel what was admitted and not yet claimed, and
        write the receipt with its fixed covered list. by must hold the mandate or one above it,
        otherwise NotAuthorised."""

    def record_outcome(self, action_id: str, state: str, evidence: dict[str, Any]) -> Effect:
        """sent -> applied or rejected, with the target's evidence. Closes an open receipt when every
        covered effect is terminal, in the same transaction."""

    def reconcile(self, action_id: str, target: TargetAdapter) -> Effect:
        """Ask the target about one sent action. Applied: record it. No record: fence the id, then
        record rejected. Unreachable: leave it sent."""

    def undo(self, action_id: str, *, mandate_id: str, principal: str,
             targets: Mapping[str, TargetAdapter], undo_id: str | None = None) -> Admission:
        """Check the inverse and that the target's state has not moved, then admit the reversing action
        under mandate_id. The caller delivers it through the sender like any other action."""

    def receipt(self, receipt_id: str) -> ReceiptView: ...

    def chain(self, action_id: str) -> ChainView: ...


class Sender(Protocol):
    """The only code that calls a target in stopline mode."""

    def claim(self, action_id: str) -> Claim: ...

    def send(self, action_id: str) -> Effect:
        """Send an effect this sender has claimed, then record the outcome. On TargetTimeout or
        TargetUnreachable the effect is left as sent. Raises NotClaimed for anything it has not claimed."""

    def reconcile(self, action_id: str) -> Effect: ...

    def sweep(self, action_ids: list[str]) -> list[Effect]:
        """After a stop: reconcile every listed action still sent. One that never reached its target is
        fenced there, so a send that turns up late is refused. An unreachable target leaves it unknown."""


class StageAPI(Protocol):
    """What the page's keys call. Every call returns within two seconds or says pending."""

    def start_run(self, mode: str, holds: list[Hold] | None = None) -> StageResult: ...   # keys 1 and 2

    def press_stop(self) -> StageResult: ...             # S: set the flag or call the gate's stop, then release every hold

    def cut_and_kill(self) -> StageResult: ...           # K: cut the router, let worker two send, kill worker two

    def resume_worker(self) -> StageResult: ...          # W: start worker two again from its saved plan

    def restore_and_reconcile(self) -> StageResult: ...  # R: restore the router, reconcile what is still sent

    def undo(self) -> StageResult: ...                   # U: undo what has an inverse, mark the rest "no undo"

    def validation_rejection(self) -> StageResult: ...   # V: try to insert a malformed audit entry and show the refusal

    def status(self) -> dict[str, Any]: ...              # the current run, its mode, its tasks and its receipt id
